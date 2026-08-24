#!/usr/bin/env python3
"""Normalize and validate provider usage records for harness benchmarks."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def normalize_usage(usage: dict[str, Any]) -> dict[str, int | None]:
    """Preserve provider totals while exposing non-overlapping token fields."""
    if "input_tokens" in usage and (
        "cache_read_input_tokens" in usage or "cache_creation_input_tokens" in usage
    ):
        fresh = usage.get("input_tokens")
        cached = usage.get("cache_read_input_tokens")
        cache_write = usage.get("cache_creation_input_tokens")
        input_total = None
        if fresh is not None:
            input_total = fresh + (cached or 0) + (cache_write or 0)
        output = usage.get("output_tokens")
        return {
            "input_tokens": input_total,
            "fresh_input_tokens": fresh,
            "cache_read_input_tokens": cached,
            "cache_write_input_tokens": cache_write,
            "output_tokens": output,
            "reasoning_tokens": None,
            "provider_total_tokens": usage.get("total_tokens"),
        }
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


def cache_observation(usage: dict[str, Any] | None) -> dict[str, Any]:
    """Describe whether cache telemetry was reported, not merely its value."""
    if not isinstance(usage, dict):
        return {"reported": False, "tokens": None, "field": None}
    for details_key in ("prompt_tokens_details", "input_tokens_details"):
        details = usage.get(details_key)
        if isinstance(details, dict) and "cached_tokens" in details:
            return {
                "reported": True,
                "tokens": details["cached_tokens"],
                "field": f"{details_key}.cached_tokens",
            }
    if "cache_read_input_tokens" in usage:
        return {
            "reported": True,
            "tokens": usage["cache_read_input_tokens"],
            "field": "cache_read_input_tokens",
        }
    return {"reported": False, "tokens": None, "field": None}


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
    anthropic_schema = "input_tokens" in usage and "output_tokens" in usage
    if normalized["provider_total_tokens"] is None and not anthropic_schema:
        errors.append("missing provider total tokens")
    if not cache_observation(usage)["reported"]:
        errors.append("missing provider cache-read field")
    expected = None
    if normalized["input_tokens"] is not None and normalized["output_tokens"] is not None:
        expected = normalized["input_tokens"] + normalized["output_tokens"]
    if (
        expected is not None
        and normalized["provider_total_tokens"] is not None
        and normalized["provider_total_tokens"] != expected
    ):
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
    cache_reported_calls = 0
    positive_cache_read_calls = 0
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
            observation = cache_observation(record["raw_usage"])
            cache_reported_calls += int(observation["reported"])
            positive_cache_read_calls += int((observation["tokens"] or 0) > 0)
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
        "cache_telemetry": {
            "reported_calls": cache_reported_calls,
            "missing_calls": len(records) - cache_reported_calls,
            "positive_cache_read_calls": positive_cache_read_calls,
            "cache_read_rate": (
                totals["cache_read_input_tokens"] / totals["input_tokens"]
                if totals["input_tokens"]
                else None
            ),
        },
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
