#!/usr/bin/env python3
"""Run the current-main Incident Operations Center matrix sequentially.

Each cell gets a fresh workspace, the pinned 11-skill Canvas context, no repair
round, and the shared provider-boundary ledger. Existing result artifacts are
skipped so an interrupted matrix can resume without repeating completed cells.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RUNNER = Path(__file__).resolve().parent / "run_suite.py"
RESULTS = ROOT / "results" / "raw" / "generated"

MATRIX = (
    ("glm", "openhands"),
    ("glm", "pi"),
    ("glm", "opencode"),
    ("glm", "codex-glm"),
    ("deepseek", "openhands-deepseek"),
    ("deepseek", "pi-deepseek"),
    ("deepseek", "opencode-deepseek"),
    ("deepseek", "codex-deepseek"),
    ("sonnet", "openhands-sonnet"),
    ("sonnet", "pi-sonnet"),
    ("sonnet", "opencode-sonnet"),
    ("sonnet", "codex-sonnet"),
)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", default="20260824")
    parser.add_argument("--ledger-proxy-url", default="http://127.0.0.1:4010")
    parser.add_argument("--timeout-seconds", type=int, default=2400)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    for model, harness in MATRIX:
        run_id = f"current-main-long-{model}-{harness}-{args.date}"
        result = RESULTS / f"{run_id}.json"
        if result.exists() and not args.force:
            print(f"SKIP {run_id}: {result} exists", flush=True)
            continue
        command = [
            sys.executable,
            str(RUNNER),
            "--run-id",
            run_id,
            "--task",
            "incident-operations-center",
            "--harness",
            harness,
            "--skill-context",
            "canvas-curated",
            "--repair-rounds",
            "0",
            "--timeout-seconds",
            str(args.timeout_seconds),
            "--ledger-proxy-url",
            args.ledger_proxy_url,
            "--ledger-settle-seconds",
            "2",
        ]
        print(f"START {run_id}", flush=True)
        subprocess.run(command, cwd=ROOT, check=True)
        print(f"DONE {run_id}", flush=True)


if __name__ == "__main__":
    main()
