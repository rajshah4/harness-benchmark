#!/usr/bin/env python3
"""Recover a result envelope after its controller disconnects post-run.

The conversation, workspace, provider ledger, and external verifier remain
valid when an SSH controller exits. This utility reconstructs the missing task
record without re-running the agent.
"""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path

from run_suite import (
    BASE_URL,
    EVIDENCE_ROOT,
    all_events,
    diff_metrics,
    event_metrics,
    request_json,
    task_prompt,
    verify,
)


def baseline_tree(workspace: Path) -> str:
    return subprocess.run(
        ["git", "-C", str(workspace), "rev-parse", "HEAD^{tree}"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--task", required=True)
    parser.add_argument("--harness", required=True)
    parser.add_argument("--conversation-id", required=True)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--verifier-python", required=True)
    parser.add_argument(
        "--execution-status",
        help="Record a deliberate terminal classification instead of Canvas status.",
    )
    args = parser.parse_args()

    path = EVIDENCE_ROOT / f"{args.run_id}.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    record = request_json("GET", f"/api/conversations/{args.conversation_id}")
    events = all_events(args.conversation_id)
    trace = event_metrics(events, args.harness, args.workspace)
    passed, output = verify(args.task, args.workspace, args.verifier_python)
    payload["tasks"] = [
        {
            "task_id": args.task,
            "prompt": task_prompt(args.task),
            "harnesses": {
                args.harness: {
                    "conversation_id": args.conversation_id,
                    "conversation_url": f"{BASE_URL}/conversations/{args.conversation_id}",
                    "execution_status": args.execution_status
                    or record.get("execution_status"),
                    "workspace": str(args.workspace),
                    "baseline_tree": baseline_tree(args.workspace),
                    "verification": {
                        "first_pass": passed,
                        "final_pass": passed,
                        "repair_rounds": 0,
                        "output": output,
                    },
                    "attempt_timing": [
                        {
                            "phase": "primary",
                            "submitted_at": trace.get("started_at"),
                            "terminal_at": trace.get("finished_at"),
                        }
                    ],
                    "trace": trace,
                    "diff": diff_metrics(args.workspace),
                }
            },
        }
    ]
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(path)


if __name__ == "__main__":
    main()
