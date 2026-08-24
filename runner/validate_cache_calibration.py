#!/usr/bin/env python3
"""Block benchmark runs unless every harness proves cache telemetry and reuse."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from usage_ledger import cache_observation


def validate(path: Path, run_id: str, harnesses: list[str]) -> dict:
    records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
    results = {}
    passed = True
    for harness in harnesses:
        selected = [
            row for row in records
            if row.get("harness") == harness
            and (row.get("context") or {}).get("run_id") == run_id
            and not row.get("error_type")
        ]
        observations = [cache_observation(row.get("raw_usage")) for row in selected]
        missing = sum(not item["reported"] for item in observations)
        positive = sum((item["tokens"] or 0) > 0 for item in observations)
        harness_passed = len(selected) >= 3 and missing == 0 and positive >= 1
        passed &= harness_passed
        results[harness] = {
            "passed": harness_passed,
            "successful_calls": len(selected),
            "cache_field_missing_calls": missing,
            "positive_cache_read_calls": positive,
            "cache_read_tokens": sum((item["tokens"] or 0) for item in observations),
        }
    return {"passed": passed, "run_id": run_id, "harnesses": results}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("ledger", type=Path)
    parser.add_argument("run_id")
    parser.add_argument(
        "--harness", action="append",
        choices=(
            "openhands", "openhands-sonnet", "pi", "pi-sonnet",
            "opencode", "opencode-sonnet",
        )
    )
    args = parser.parse_args()
    result = validate(args.ledger, args.run_id, args.harness or ["openhands", "pi", "opencode"])
    print(json.dumps(result, indent=2))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
