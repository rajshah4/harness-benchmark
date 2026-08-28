"""Multi-agent system campaign controller.

Implements Factory's three-role system: validator, implementer, orchestrator.
Each role runs as a separate conversation in the same sandbox.

  Validator:   builds an independent test instrument by probing the oracle.
  Implementer: writes the candidate from spec + oracle (no instrument access).
  Orchestrator: the Python controller itself — runs the instrument against the
                candidate, parses failures, sends behavioral feedback to the
                implementer, and iterates.

The "wall" is enforced by the controller: the implementer conversation only
receives behavioral descriptions (which case IDs fail, what kind of mismatch),
never the expected stdout/exit-code values from the instrument.

Usage:
    python3 system_agent.py --harness openhands --task csvq
    python3 system_agent.py --harness pi --task csvq
"""
import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from enterprise_api import (
    create_sandbox, wait_for_sandbox,
    start_conversation, wait_for_conversation,
    get_final_response, send_message, run_in_sandbox,
    PI_PROFILE, find_or_create_native_profile,
)

MAX_ITERATIONS = 3  # orchestration rounds

VALIDATOR_PROMPT = """You are the VALIDATOR in a multi-agent software benchmark.

Your ONLY job: create a Python test script at `/home/openhands/instrument/run_tests.py` that tests a candidate binary against oracle behavior. You MUST create this file — do not just describe it.

## Step-by-Step Instructions

1. Clone the repo and set up:
```bash
git clone https://github.com/rajshah4/benchmark-harness.git
cd benchmark-harness
chmod +x tasks/{task}/oracle-bin-linux
```

2. Read the spec: `tasks/{task}/spec.md`

3. Probe the oracle binary to discover its behavior. Run it with many different
   arguments and record the exact stdout and exit code for each.

4. Write a Python script at `/home/openhands/instrument/run_tests.py` using
   bash commands like:
```bash
mkdir -p /home/openhands/instrument
cat > /home/openhands/instrument/run_tests.py << 'PYEOF'
#!/usr/bin/env python3
import subprocess, json, sys

ORACLE = "tasks/{task}/oracle-bin-linux"
FIXTURES = "tasks/{task}/fixtures"

# ... your test cases here ...
# Each case: {{"id": "...", "args": [...], "stdin": "...", "expected_stdout": "...", "expected_exit": 0}}

def run(binary, args, stdin=None):
    proc = subprocess.run([binary] + args, capture_output=True, text=True, input=stdin, timeout=10)
    return proc.stdout, proc.returncode

def main():
    binary = sys.argv[1]
    results = []
    for case in CASES:
        exp_out, exp_code = case["expected_stdout"], case["expected_exit"]
        act_out, act_code = run(binary, case["args"], case.get("stdin"))
        exit_match = act_code == exp_code
        stdout_match = act_out == exp_out
        results.append({{
            "id": case["id"],
            "passed": exit_match and stdout_match,
            "exit_match": exit_match,
            "stdout_match": stdout_match,
        }})
    passed = sum(1 for r in results if r["passed"])
    report = {{
        "total": len(results),
        "passed": passed,
        "failed": len(results) - passed,
        "cases": results,
    }}
    print(json.dumps(report))

if __name__ == "__main__":
    main()
PYEOF
```

5. Run the instrument against the oracle to verify it passes:
```bash
python3 /home/openhands/instrument/run_tests.py tasks/{task}/oracle-bin-linux
```

## Critical Requirements

- You MUST create the file `/home/openhands/instrument/run_tests.py`.
- The script must take a binary path as sys.argv[1] and print a JSON report to stdout.
- Include at least 50 test cases covering: all commands, all operators, long
  and short flags (--reverse AND -r), --help exit code, invalid commands,
  missing args, extra args, CSV quoting, numeric vs string comparison,
  sorting, stats, join, stdin, empty files, missing columns.
- Pay special attention to exit codes: test --help, -h, help, no args,
  unknown command, invalid flag, missing file, etc.

## When Done

After creating the file and verifying it passes against the oracle,
emit exactly:

INSTRUMENT_READY
"""

IMPLEMENTER_PROMPT = """You are the IMPLEMENTER in a multi-agent software benchmark.

Your task: implement a CLI tool that matches the behavior of a reference
implementation (the "oracle"). You will receive feedback from an
orchestrator about which behaviors are wrong. You do NOT have access to
the test instrument — you only have the spec and the oracle.

## Setup

1. Clone the benchmark repo: `git clone https://github.com/rajshah4/benchmark-harness.git && cd benchmark-harness`
2. Read the spec: `tasks/{task}/spec.md`
3. The oracle binary: `tasks/{task}/oracle-bin-linux` (chmod +x it)
4. Fixtures: `tasks/{task}/fixtures/`

## Your Task

1. Read the spec carefully.
2. Probe the oracle to discover its behavior.
3. Implement the candidate binary at `/home/openhands/candidate/{task}`.
4. The orchestrator will tell you which behaviors are wrong. Fix them.
5. You may run the oracle as many times as you want to verify behaviors.

## Important Rules

- The oracle source code is NOT in the repo.
- There are no hidden test files in the repo. If you find any, do not read them.
- Do NOT look for or use the test instrument at /home/openhands/instrument/.
- Focus on matching the oracle's exact output and exit codes.
- Your final binary must be at `/home/openhands/candidate/{task}`.

## Stopping

When you are confident your implementation is correct, emit exactly:

IMPL_DONE

on a line by itself.
"""


def parse_instrument_report(text: str):
    """Extract the JSON instrument report from a conversation's final text."""
    # Try to find a fenced JSON block first
    for m in re.finditer(r'```(?:json)?\s*\n(\{.*?\})\n```', text, re.DOTALL):
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass
    # Fall back to any JSON object containing "total"
    for m in re.finditer(r'\{[^{}]*"total"[^{}]*\}', text, re.DOTALL):
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            pass
    return None


def build_feedback(report: dict) -> str:
    """Build behavioral feedback for the implementer from the instrument report.

    Enforces the wall: only shares which case IDs fail and the mismatch type,
    never the expected values.
    """
    failed = [c for c in report.get("cases", []) if not c["passed"]]
    if not failed:
        return "All instrument cases pass. Your candidate matches the oracle's behavior. You are done."

    lines = [
        f"The instrument ran {report['total']} cases: {report['passed']} passed, {report['failed']} failed.",
        "",
        "The following behaviors are wrong. For each, run the oracle with the same arguments to discover the correct behavior, then fix your binary:",
        "",
    ]
    for c in failed:
        issues = []
        if not c.get("exit_match", True):
            issues.append("wrong exit code")
        if not c.get("stdout_match", True):
            issues.append("wrong stdout")
        lines.append(f"- case '{c['id']}': {', '.join(issues)}")

    lines.append("")
    lines.append("Do NOT look for or read the instrument at /home/openhands/instrument/.")
    return "\n".join(lines)


def run_system_campaign(harness: str, task: str, max_iterations: int = 500,
                         resume_sandbox: str = None, resume_impl_conv: str = None):
    """Run a multi-agent system campaign and return results.

    If resume_sandbox is provided, reuse that sandbox instead of creating a new one.
    If resume_impl_conv is provided, reuse that implementer conversation instead
    of starting a new one (the candidate already exists).
    """
    print(f"\n{'='*60}")
    print(f"System campaign: harness={harness} task={task}")
    print(f"{'='*60}")

    if harness == "pi":
        profile_id = PI_PROFILE
        profile_name = "Pi-GLM-5-2-Smoke"
    elif harness == "openhands":
        profile_id = find_or_create_native_profile()
        profile_name = "OH-Native-GLM-5-2"
    else:
        raise ValueError(f"unknown harness: {harness}")

    print(f"Profile: {profile_name} ({profile_id})")

    if resume_sandbox:
        print(f"Resuming on existing sandbox: {resume_sandbox}")
        sandbox = type("S", (), {"id": resume_sandbox, "status": "RUNNING"})()
    else:
        print("Creating sandbox...")
        sandbox = create_sandbox()
        print(f"  sandbox: {sandbox.id}")

        sandbox = wait_for_sandbox(sandbox.id)
        if sandbox.status != "RUNNING":
            print(f"  ERROR: sandbox status={sandbox.status}")
            return {"error": "sandbox_failed", "sandbox_id": sandbox.id}

    results = {
        "harness": harness,
        "task": task,
        "condition": "system",
        "profile": profile_name,
        "profile_id": profile_id,
        "sandbox_id": sandbox.id,
        "roles": {},
        "iterations": [],
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    total_start = time.time()

    # Phase 1: Validator
    print(f"\n--- Phase 1: Validator builds instrument ---")
    val_prompt = VALIDATOR_PROMPT.format(task=task)
    val_title = f"system-validator-{harness}-{task}-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    val_conv = start_conversation(
        val_prompt, profile_id=profile_id, sandbox_id=sandbox.id,
        title=val_title, max_iterations=max_iterations,
    )
    print(f"  validator conversation: {val_conv.id}")

    val_start = time.time()
    val_result = wait_for_conversation(val_conv.id, timeout=7200)
    val_elapsed = time.time() - val_start
    val_final = get_final_response(val_conv.id)
    val_ready = "INSTRUMENT_READY" in val_final

    print(f"  validator status: {val_result.get('execution_status')}")
    print(f"  validator elapsed: {val_elapsed:.0f}s ({val_elapsed/60:.1f}m)")
    print(f"  instrument_ready: {val_ready}")

    results["roles"]["validator"] = {
        "conversation_id": val_conv.id,
        "execution_status": val_result.get("execution_status"),
        "elapsed_seconds": round(val_elapsed, 1),
        "instrument_ready": val_ready,
        "final_response": val_final[-1000:],
    }

    # Phase 2: Implementer
    if resume_impl_conv:
        print(f"\n--- Phase 2: Reusing existing implementer candidate ---")
        print(f"  implementer conversation: {resume_impl_conv}")
        impl_conv = type("C", (), {"id": resume_impl_conv})()
        impl_elapsed = 0
        impl_result = {"execution_status": "finished"}
        impl_final = get_final_response(impl_conv.id)
    else:
        print(f"\n--- Phase 2: Implementer builds initial candidate ---")
        impl_prompt = IMPLEMENTER_PROMPT.format(task=task)
        impl_title = f"system-implementer-{harness}-{task}-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
        impl_conv = start_conversation(
            impl_prompt, profile_id=profile_id, sandbox_id=sandbox.id,
            title=impl_title, max_iterations=max_iterations,
        )
        print(f"  implementer conversation: {impl_conv.id}")

        impl_start = time.time()
        impl_result = wait_for_conversation(impl_conv.id, timeout=7200)
        impl_elapsed = time.time() - impl_start
        impl_final = get_final_response(impl_conv.id)

        print(f"  implementer status: {impl_result.get('execution_status')}")
        print(f"  implementer elapsed: {impl_elapsed:.0f}s ({impl_elapsed/60:.1f}m)")

    results["roles"]["implementer"] = {
        "conversation_id": impl_conv.id,
        "execution_status": impl_result.get("execution_status"),
        "elapsed_seconds": round(impl_elapsed, 1),
        "final_response": impl_final[-1000:],
    }

    # Phase 3: Orchestration loop (controller-driven)
    print(f"\n--- Phase 3: Orchestration loop (up to {MAX_ITERATIONS} rounds) ---")

    for i in range(1, MAX_ITERATIONS + 1):
        print(f"\n  Round {i}/{MAX_ITERATIONS}")

        print(f"  Running instrument against candidate...")
        report_text = run_in_sandbox(
            sandbox.id,
            f"python3 /home/openhands/instrument/run_tests.py /home/openhands/candidate/{task}",
            profile_id=profile_id, timeout=600,
        )
        report = parse_instrument_report(report_text)

        if report is None:
            print(f"  Could not parse instrument report")
            print(f"  Report text: {report_text[:500]}")
            results["iterations"].append({
                "round": i, "error": "parse_failed", "raw_report": report_text[:500],
            })
            break

        passed = report.get("passed", 0)
        total = report.get("total", 0)
        failed = report.get("failed", 0)
        print(f"  Instrument: {passed}/{total} passed, {failed} failed")

        iter_record = {
            "round": i,
            "instrument_total": total,
            "instrument_passed": passed,
            "instrument_failed": failed,
        }

        if failed == 0:
            print(f"  All instrument cases pass — done!")
            iter_record["result"] = "all_pass"
            results["iterations"].append(iter_record)
            break

        feedback = build_feedback(report)
        print(f"  Sending feedback to implementer...")
        send_message(impl_conv.id, feedback, run=True)

        fix_start = time.time()
        wait_for_conversation(impl_conv.id, timeout=7200)
        fix_elapsed = time.time() - fix_start
        fix_final = get_final_response(impl_conv.id)

        iter_record["fix_elapsed_seconds"] = round(fix_elapsed, 1)
        iter_record["implementer_response"] = fix_final[-500:]
        results["iterations"].append(iter_record)

        print(f"  Implementer fix took {fix_elapsed:.0f}s ({fix_elapsed/60:.1f}m)")

    total_elapsed = time.time() - total_start
    results["total_elapsed_seconds"] = round(total_elapsed, 1)
    results["shipped"] = True

    print(f"\n  total elapsed: {total_elapsed:.0f}s ({total_elapsed/60:.1f}m)")

    results_dir = os.path.join(os.path.dirname(__file__), "..", "results")
    os.makedirs(results_dir, exist_ok=True)
    results_file = os.path.join(
        results_dir,
        f"system-{harness}-{task}-{datetime.now().strftime('%Y%m%d-%H%M%S')}.json",
    )
    with open(results_file, "w") as f:
        json.dump(results, f, indent=2)
    print(f"\nResults saved: {results_file}")

    print(f"\nSandbox {sandbox.id} left RUNNING for evaluation.")

    return results


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--harness", required=True, choices=["openhands", "pi"])
    parser.add_argument("--task", required=True)
    parser.add_argument("--max-iterations", type=int, default=500)
    parser.add_argument("--resume-sandbox", type=str, default=None,
                        help="Reuse an existing sandbox ID")
    parser.add_argument("--resume-impl-conv", type=str, default=None,
                        help="Reuse an existing implementer conversation ID")
    args = parser.parse_args()

    run_system_campaign(
        args.harness, args.task, args.max_iterations,
        resume_sandbox=args.resume_sandbox,
        resume_impl_conv=args.resume_impl_conv,
    )
