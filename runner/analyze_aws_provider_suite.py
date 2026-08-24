#!/usr/bin/env python3
"""Summarize an AWS harness suite from provider-ledger and run evidence."""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path
from typing import Any


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("ledger", type=Path)
    parser.add_argument("runs", type=Path)
    parser.add_argument("run_prefix")
    return parser.parse_args()


def blank_metrics() -> dict[str, Any]:
    return {
        "model_calls": 0,
        "input_tokens": 0,
        "cache_read_input_tokens": 0,
        "fresh_input_tokens": 0,
        "output_tokens": 0,
        "provider_total_tokens": 0,
        "cost": 0.0,
        "provider_errors": 0,
        "tool_counts": set(),
    }


def main() -> None:
    args = parse_args()
    cells: dict[tuple[str, str], dict[str, Any]] = defaultdict(blank_metrics)

    with args.ledger.open(encoding="utf-8") as handle:
        for line in handle:
            record = json.loads(line)
            context = record.get("context")
            if not context:
                continue
            if not context["run_id"].startswith(args.run_prefix):
                continue
            key = (context["task_id"], record["harness"])
            usage = record["raw_usage"]
            if record.get("error_type") or not usage:
                cells[key]["provider_errors"] += 1
                cells[key]["tool_counts"].add(record["request"]["tool_count"])
                continue
            cached = usage.get("prompt_tokens_details", {}).get("cached_tokens", 0)
            cell = cells[key]
            cell["model_calls"] += 1
            cell["input_tokens"] += usage["prompt_tokens"]
            cell["cache_read_input_tokens"] += cached
            cell["fresh_input_tokens"] += usage["prompt_tokens"] - cached
            cell["output_tokens"] += usage["completion_tokens"]
            cell["provider_total_tokens"] += usage["total_tokens"]
            cell["cost"] += usage.get("cost", 0.0)
            cell["tool_counts"].add(record["request"]["tool_count"])

    for path in sorted(args.runs.glob(f"{args.run_prefix}-*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        task = data["tasks"][0]
        for harness, result in task["harnesses"].items():
            cell = cells[(task["task_id"], harness)]
            cell["elapsed_seconds"] = result["trace"]["elapsed_seconds"]
            cell["agent_tool_actions"] = result["trace"]["tool_calls"]
            cell["first_pass"] = result["verification"]["first_pass"]
            cell["final_pass"] = result["verification"]["final_pass"]

    aggregates: dict[str, dict[str, Any]] = defaultdict(blank_metrics)
    for (task_id, harness), cell in cells.items():
        aggregate = aggregates[harness]
        for field in (
            "model_calls",
            "input_tokens",
            "cache_read_input_tokens",
            "fresh_input_tokens",
            "output_tokens",
            "provider_total_tokens",
            "cost",
            "provider_errors",
        ):
            aggregate[field] += cell[field]
        aggregate["elapsed_seconds"] = aggregate.get("elapsed_seconds", 0) + cell[
            "elapsed_seconds"
        ]
        aggregate["agent_tool_actions"] = aggregate.get("agent_tool_actions", 0) + cell[
            "agent_tool_actions"
        ]
        aggregate["tasks_passed"] = aggregate.get("tasks_passed", 0) + int(
            cell["final_pass"]
        )
        aggregate["tool_counts"].update(cell["tool_counts"])

    def serializable(metrics: dict[str, Any]) -> dict[str, Any]:
        result = dict(metrics)
        result["cost"] = round(result["cost"], 6)
        result["tool_counts"] = sorted(
            result["tool_counts"], key=lambda value: -1 if value is None else value
        )
        if result["input_tokens"]:
            result["cache_read_rate"] = round(
                result["cache_read_input_tokens"] / result["input_tokens"], 6
            )
            result["average_input_tokens_per_call"] = round(
                result["input_tokens"] / result["model_calls"], 2
            )
        return result

    output = {
        "run_prefix": args.run_prefix,
        "cell_count": len(cells),
        "aggregates": {
            harness: serializable(metrics)
            for harness, metrics in sorted(aggregates.items())
        },
        "cells": {
            f"{task_id}/{harness}": serializable(metrics)
            for (task_id, harness), metrics in sorted(cells.items())
        },
    }
    print(json.dumps(output, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
