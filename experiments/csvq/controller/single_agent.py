"""Single-agent benchmark controller.

Launches one conversation (OpenHands native or PI via ACP) to implement
a candidate for a benchmark task. The agent clones the benchmark repo,
reads the spec, runs the oracle, and builds the candidate binary.

Usage:
    python3 single_agent.py --harness openhands --task csvq
    python3 single_agent.py --harness pi --task csvq
"""
import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from enterprise_api import (
    create_sandbox, wait_for_sandbox, pause_sandbox,
    start_conversation, poll_start_task, wait_for_conversation,
    get_final_response, PI_PROFILE, find_or_create_native_profile,
    ENTERPRISE_URL,
)

REPO_URL = "https://github.com/rajshah4/benchmark-harness.git"

SINGLE_AGENT_PROMPT = """You are participating in a software engineering benchmark. Your task is to implement a CLI tool that matches the behavior of a reference implementation (the "oracle").

## Setup

1. Clone the benchmark repo: `git clone https://github.com/rajshah4/benchmark-harness.git && cd benchmark-harness`
2. Read the spec: `tasks/{task}/spec.md`
3. The oracle binary: `tasks/{task}/oracle-bin-linux` (make it executable: `chmod +x tasks/{task}/oracle-bin-linux`)
4. Fixtures for testing: `tasks/{task}/fixtures/`

## Your Task

Implement the tool described in `tasks/{task}/spec.md`. The oracle binary at `tasks/{task}/oracle-bin-linux` is the reference implementation — you may run it as many times as you want to discover its behavior, but the source code is NOT in the repo. The spec is intentionally incomplete; you must discover behaviors by probing the oracle.

## What to Do

1. Read the spec carefully.
2. Run the oracle binary with various inputs to discover its full behavior. The spec is incomplete by design.
3. Implement a candidate binary named `{task}` that matches the oracle's behavior exactly.
4. Test your implementation against the oracle by running both with the same inputs and comparing outputs.
5. Pay attention to edge cases: CSV quoting, empty fields, numeric vs string comparison, error handling, exit codes.

## Deliverable

Build your candidate binary and place it at: `/home/openhands/candidate/{task}`

It must be a Linux x86_64 executable. You may use any language. If you implement in a compiled language, build the release binary. If you implement in Python, create a standalone executable using a shebang script or PyInstaller.

## Stopping

When you are confident your implementation matches the oracle's behavior, emit exactly:

SHIP

on a line by itself in your final response, followed by a brief summary of what you implemented.

## Important Rules

- The oracle source code is NOT in the repo. Do not look for it.
- There are no hidden test files in the repo. If you find any, do not read them.
- You may run the oracle binary as many times as you want.
- Focus on matching the oracle's exact output, including CSV formatting, error messages to stderr, and exit codes.
- Your final binary must be at `/home/openhands/candidate/{task}`.
"""


def run_single_agent(harness: str, task: str, max_iterations: int = 500):
    """Run a single-agent campaign and return results."""
    print(f"\n{'='*60}")
    print(f"Single-agent campaign: harness={harness} task={task}")
    print(f"{'='*60}")

    # Select profile
    if harness == "pi":
        profile_id = PI_PROFILE
        profile_name = "Pi-GLM-5-2-Smoke"
    elif harness == "openhands":
        profile_id = find_or_create_native_profile()
        profile_name = "OH-Native-GLM-5-2"
    else:
        raise ValueError(f"unknown harness: {harness}")

    print(f"Profile: {profile_name} ({profile_id})")

    # Create sandbox
    print("Creating sandbox...")
    sandbox = create_sandbox()
    print(f"  sandbox: {sandbox.id}")

    # Wait for RUNNING
    sandbox = wait_for_sandbox(sandbox.id)
    if sandbox.status != "RUNNING":
        print(f"  ERROR: sandbox status={sandbox.status}")
        return {"error": "sandbox_failed", "sandbox_id": sandbox.id}

    # Build prompt
    prompt = SINGLE_AGENT_PROMPT.format(task=task)

    # Start conversation
    print("Starting conversation...")
    title = f"benchmark-single-{harness}-{task}-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    conv = start_conversation(
        prompt,
        profile_id=profile_id,
        sandbox_id=sandbox.id,
        title=title,
        max_iterations=max_iterations,
    )
    print(f"  conversation: {conv.id}")
    print(f"  sandbox_id: {conv.sandbox_id}")

    # Poll start task
    print("Waiting for start task...")
    start_info = poll_start_task(conv.id)
    app_conv_id = start_info.get("app_conversation_id", conv.id)
    print(f"  app_conversation_id: {app_conv_id}")

    # Wait for conversation to finish
    print("Waiting for conversation to complete (this may take a while)...")
    start_time = time.time()
    result = wait_for_conversation(app_conv_id, timeout=14400)  # 4 hour max
    elapsed = time.time() - start_time

    print(f"  execution_status: {result.get('execution_status')}")
    print(f"  elapsed: {elapsed:.0f}s ({elapsed/60:.1f}m)")

    # Get final response
    final_text = get_final_response(app_conv_id)
    shipped = "SHIP" in final_text

    print(f"  shipped: {shipped}")

    # Save results
    results = {
        "harness": harness,
        "task": task,
        "condition": "single",
        "profile": profile_name,
        "profile_id": profile_id,
        "sandbox_id": sandbox.id,
        "conversation_id": app_conv_id,
        "execution_status": result.get("execution_status"),
        "elapsed_seconds": round(elapsed, 1),
        "shipped": shipped,
        "final_response": final_text[-2000:],  # last 2000 chars
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    # Save to results file
    results_dir = os.path.join(os.path.dirname(__file__), "..", "results")
    os.makedirs(results_dir, exist_ok=True)
    results_file = os.path.join(
        results_dir,
        f"single-{harness}-{task}-{datetime.now().strftime('%Y%m%d-%H%M%S')}.json",
    )
    with open(results_file, "w") as f:
        json.dump(results, f, indent=2)
    print(f"\nResults saved: {results_file}")

    # Don't pause the sandbox yet — the evaluator needs it
    print(f"\nSandbox {sandbox.id} left RUNNING for evaluation.")
    print(f"Run the evaluator next: python3 evaluator.py --sandbox {sandbox.id} --task {task}")

    return results


def main():
    parser = argparse.ArgumentParser(description="Run a single-agent benchmark campaign")
    parser.add_argument("--harness", required=True, choices=["openhands", "pi"])
    parser.add_argument("--task", required=True, help="Task name (e.g., csvq)")
    parser.add_argument("--max-iterations", type=int, default=500)
    args = parser.parse_args()

    result = run_single_agent(args.harness, args.task, args.max_iterations)
    print(f"\nDone. Conversation: {result.get('conversation_id', 'N/A')}")


if __name__ == "__main__":
    main()
