#!/usr/bin/env python3
"""Fail closed unless calibration proves model, usage, and skill controls."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from run_suite import CURATED_CANVAS_SKILLS
from usage_ledger import validate_record


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("ledger", type=Path)
    parser.add_argument("calibration_result", type=Path)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--harness", action="append", required=True)
    parser.add_argument("--expected-model", default="claude-sonnet-4-6")
    args = parser.parse_args()

    calibration = json.loads(args.calibration_result.read_text(encoding="utf-8"))
    rows = [
        json.loads(line)
        for line in args.ledger.read_text(encoding="utf-8").splitlines()
        if line
    ]
    selected = [
        row for row in rows
        if (row.get("context") or {}).get("run_id") == args.run_id
    ]
    errors: list[str] = []
    receipts: dict[str, dict] = {}
    parameter_shapes: dict[str, list[str]] = {}
    seen_response_ids: set[str] = set()
    expected_skills = list(CURATED_CANVAS_SKILLS)

    for harness in args.harness:
        harness_rows = [row for row in selected if row.get("harness") == harness]
        if not harness_rows:
            errors.append(f"{harness}: no provider-boundary records")
            continue
        models = sorted({row.get("request", {}).get("model") for row in harness_rows})
        parameters = sorted({
            json.dumps(row.get("request", {}).get("parameters", {}), sort_keys=True)
            for row in harness_rows
        })
        parameter_shapes[harness] = parameters
        if models != [args.expected_model]:
            errors.append(f"{harness}: observed request models {models!r}")
        for index, row in enumerate(harness_rows, 1):
            if not 200 <= int(row.get("response_status", 0)) < 300:
                errors.append(f"{harness} row {index}: HTTP {row.get('response_status')}")
            if row.get("error_type") or row.get("provider_error"):
                errors.append(f"{harness} row {index}: provider error recorded")
            for detail in validate_record(row):
                errors.append(f"{harness} row {index}: {detail}")
            response_id = row.get("provider_response_id")
            if response_id in seen_response_ids:
                errors.append(f"{harness} row {index}: duplicate provider response ID")
            elif response_id:
                seen_response_ids.add(response_id)
        recorded_skills = calibration.get("harnesses", {}).get(harness, {}).get("skill_names")
        if recorded_skills != expected_skills:
            errors.append(
                f"{harness}: curated skill allow-list differs "
                f"(expected {len(expected_skills)}, got {len(recorded_skills or [])})"
            )
        calibration_harness = calibration.get("harnesses", {}).get(harness, {})
        if (
            calibration_harness.get("agent_kind") == "openhands"
            and calibration_harness.get("enable_sub_agents") is not False
        ):
            errors.append(f"{harness}: sub-agents are not explicitly disabled")
        receipts[harness] = {
            "model_calls": len(harness_rows),
            "models": models,
            "usage_receipts": sum(isinstance(row.get("raw_usage"), dict) for row in harness_rows),
            "skill_count": len(recorded_skills or []),
            "agent_kind": calibration_harness.get("agent_kind"),
            "sub_agents_enabled": calibration_harness.get("enable_sub_agents"),
            "request_parameters": [json.loads(value) for value in parameters],
        }

    if len({tuple(values) for values in parameter_shapes.values()}) > 1:
        errors.append(f"provider request parameters differ across harnesses: {parameter_shapes!r}")

    report = {
        "passed": not errors,
        "run_id": args.run_id,
        "expected_model": args.expected_model,
        "harnesses": receipts,
        "errors": errors,
    }
    print(json.dumps(report, indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
