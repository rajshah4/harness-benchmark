#!/usr/bin/env python3
"""Normalize and validate provider usage records for harness benchmarks."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def normalize_usage(usage: dict[str, Any]) -> dict[str, int | None]:
    """Preserve provider totals while exposing non-overlapping token fields."""
    input_total = usage.get("prompt_tokens", usage.get("input_tokens"))
    output = usage.get("completion_tokens", usage.get("output_tokens"))
    total = usage.get("total_tokens")
    prompt_details = usage.get("prompt_tokens_details") or usage.get("input_tokens_details") or {}
    completion_details = usage.get("completion_tokens_details") or usage.get("output_tokens_details") or {}
    cached = prompt_details.get("cached_tokens")
    cache_write = prompt_details.get("cache_creation_tokens")
    reasoning = completion_details.get("reasoning_tokens")

    fresh = None
    if input_total is not None and cached is not None:
        fresh = input_total - cached
        if fresh < 0:
            raise ValueError("cached input exceeds provider input total")

    return {
        "input_tokens": input_total,
        "fresh_input_tokens": fresh,
        "cache_read_input_tokens": cached,
        "cache_write_input_tokens": cache_write,
        "output_tokens": output,
        "reasoning_tokens": reasoning,
        "provider_total_tokens": total,
    }


def validate_record(record: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    usage = record.get("raw_usage")
    if not isinstance(usage, dict) or not usage:
        return ["missing raw provider usage"]
    normalized = normalize_usage(usage)
    if normalized["input_tokens"] is None:
        errors.append("missing provider input tokens")
    if normalized["output_tokens"] is None:
        errors.append("missing provider output tokens")
    if normalized["provider_total_tokens"] is None:
        errors.append("missing provider total tokens")
    expected = None
    if normalized["input_tokens"] is not None and normalized["output_tokens"] is not None:
        expected = normalized["input_tokens"] + normalized["output_tokens"]
    if expected is not None and normalized["provider_total_tokens"] != expected:
        errors.append(
            "provider total does not equal input plus output; preserve raw fields and investigate"
        )
    return errors


def summarize(path: Path) -> dict[str, Any]:
    records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
    errors: list[dict[str, Any]] = []
    seen: set[str] = set()
    totals = {
        "input_tokens": 0,
        "fresh_input_tokens": 0,
        "cache_read_input_tokens": 0,
        "output_tokens": 0,
        "provider_total_tokens": 0,
    }
    grouped: dict[str, dict[str, int]] = {}
    for index, record in enumerate(records, 1):
        response_id = record.get("provider_response_id")
        if not response_id:
            errors.append({"row": index, "error": "missing provider response ID"})
        elif response_id in seen:
            errors.append({"row": index, "error": "duplicate provider response ID"})
        else:
            seen.add(response_id)
        record_errors = validate_record(record)
        for error in record_errors:
            errors.append({"row": index, "error": error})
        if not record_errors:
            normalized = normalize_usage(record["raw_usage"])
            for field in totals:
                totals[field] += int(normalized[field] or 0)
            context = record.get("context") or {}
            key = "/".join(
                [str(record.get("harness", "unlabeled"))]
                + [
                    str(context.get(field, "unlabeled"))
                    for field in ("run_id", "task_id", "phase")
                ]
            )
            if key not in grouped:
                grouped[key] = {field: 0 for field in totals}
            for field in totals:
                grouped[key][field] += int(normalized[field] or 0)
    return {
        "publishable": bool(records) and not errors,
        "model_call_count": len(records),
        "totals": totals,
        "by_context": grouped,
        "errors": errors,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("ledger", type=Path)
    args = parser.parse_args()
    result = summarize(args.ledger)
    print(json.dumps(result, indent=2))
    raise SystemExit(0 if result["publishable"] else 1)


if __name__ == "__main__":
    main()
