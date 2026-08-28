#!/usr/bin/env python3
"""Refresh post-run metrics that depend on the final Git and ledger state."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from run_completion_experiment import provider_usage
from run_suite import diff_metrics


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("result", type=Path)
    parser.add_argument("ledger", type=Path)
    args = parser.parse_args()

    result = json.loads(args.result.read_text(encoding="utf-8"))
    for name in ("openhands-single", "pi-single"):
        condition = result.get("conditions", {}).get(name)
        if condition:
            condition["diff"] = diff_metrics(Path(condition["workspace"]))
    system = result.get("conditions", {}).get("openhands-system")
    if system:
        system["diff"] = diff_metrics(Path(system["workspace"]))
    result["provider_usage"] = provider_usage(args.ledger, result["run_id"])
    args.result.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({
        "result": str(args.result),
        "accounting_publishable": result["provider_usage"]["publishable"],
        "accounting_errors": result["provider_usage"]["accounting_errors"],
        "reliability_incidents": result["provider_usage"]["reliability_incidents"],
    }, indent=2))


if __name__ == "__main__":
    main()
