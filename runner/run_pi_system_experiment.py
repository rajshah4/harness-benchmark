#!/usr/bin/env python3
"""Run only the missing Pi completion-system cell of the 2x2 experiment."""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path

import run_completion_experiment as completion
from run_suite import CURATED_CANVAS_SKILLS, FREIGHT_TOWER_DIR, hash_file, resolve_curated_agent_settings, save_results


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument("--ledger-proxy-url", default="http://127.0.0.1:4020")
    parser.add_argument("--timeout-seconds", type=int, default=3600)
    parser.add_argument("--max-system-repairs", type=int, default=2)
    parser.add_argument(
        "--verifier-python",
        default=str(Path(__file__).resolve().parents[1] / ".venv-verifier" / "bin" / "python"),
    )
    args = parser.parse_args()
    completion.TASK_ID = "freight-control-tower"
    pi_settings = resolve_curated_agent_settings(("pi-sonnet46",))["pi-sonnet46"]
    results = {
        "run_id": args.run_id,
        "experiment": "completion-loop-app/v1-pi-system-cell",
        "model": completion.MODEL,
        "provider": "OpenHands hosted LLM provider",
        "task": completion.TASK_ID,
        "created_at": datetime.now().astimezone().isoformat(),
        "controls": {
            "aws_host": True,
            "skill_source": "OpenHands/OpenHands#16860; @openhands/extensions 0.18.0",
            "skill_names": list(CURATED_CANVAS_SKILLS),
            "task_sha256": hash_file(FREIGHT_TOWER_DIR / "task.md"),
            "verifier_sha256": hash_file(FREIGHT_TOWER_DIR / "verify_freight_control_tower.py"),
            "system_information_wall": "separate LocalWorkspace directories; operational, not cryptographic",
            "system_max_repairs": args.max_system_repairs,
            "system_harness": "pi",
        },
        "conditions": {},
    }
    save_results(args.run_id, results)
    results["conditions"]["pi-system"] = completion.run_system(
        run_id=args.run_id,
        native_base_settings=pi_settings,
        ledger_proxy_url=args.ledger_proxy_url,
        timeout_seconds=args.timeout_seconds,
        verifier_python=args.verifier_python,
        max_repairs=args.max_system_repairs,
        system_harness="pi",
    )
    results["provider_usage"] = completion.provider_usage(args.ledger, args.run_id)
    results["completed_at"] = datetime.now().astimezone().isoformat()
    output = save_results(args.run_id, results)
    print(output)
    return 0 if results["provider_usage"]["publishable"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
