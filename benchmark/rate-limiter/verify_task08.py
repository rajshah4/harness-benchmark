#!/usr/bin/env python3
"""Instructor-owned black-box verifier for the Task 8 comparison."""

from __future__ import annotations

import argparse
import importlib
import inspect
import json
import sys
import time
import uuid
from pathlib import Path


def check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def load_submission(workspace: Path):
    target = workspace / "rate_limiter.py"
    check(target.is_file(), "rate_limiter.py is missing")
    sys.path.insert(0, str(workspace))
    try:
        return importlib.import_module("rate_limiter")
    finally:
        sys.path.pop(0)


def registry_for(module):
    registry_type = getattr(module, "RateLimiter", None)
    if inspect.isclass(registry_type):
        return registry_type()
    if registry_type is not None:
        return registry_type
    return module


def run(workspace: Path) -> dict[str, object]:
    module = load_submission(workspace)
    bucket_type = getattr(module, "TokenBucket", None)
    error_type = getattr(module, "RateLimitExceeded", None)
    decorator = getattr(module, "rate_limit", None)

    check(inspect.isclass(bucket_type), "TokenBucket must be a class")
    check(inspect.isclass(error_type), "RateLimitExceeded must be an exception class")
    check(callable(decorator), "rate_limit must be callable")

    results: list[str] = []

    bucket = bucket_type(capacity=5, refill_rate=0)
    check([bucket.allow() for _ in range(6)] == [True] * 5 + [False], "basic capacity behavior failed")
    results.append("basic_allow")

    bucket = bucket_type(capacity=1, refill_rate=20)
    check(bucket.allow() is True, "new bucket did not start full")
    check(bucket.allow() is False, "empty bucket allowed a request")
    time.sleep(0.08)
    check(bucket.allow() is True, "bucket did not refill after elapsed time")
    results.append("refill_over_time")

    bucket = bucket_type(capacity=2, refill_rate=100)
    check(bucket.allow() is True and bucket.allow() is True, "burst setup failed")
    time.sleep(0.08)
    outcomes = [bucket.allow(), bucket.allow(), bucket.allow()]
    check(outcomes == [True, True, False], "refill exceeded burst capacity")
    results.append("burst_capacity")

    bucket = bucket_type(capacity=0, refill_rate=100)
    time.sleep(0.02)
    check(bucket.allow() is False, "zero-capacity bucket allowed a request")
    results.append("zero_capacity")

    registry = registry_for(module)
    get_limiter = getattr(registry, "get_limiter", None)
    check_limiter = getattr(registry, "check", None)
    check(callable(get_limiter), "registry must expose get_limiter")
    check(callable(check_limiter), "registry must expose check")
    name = f"verify-{uuid.uuid4()}"
    first = get_limiter(name, 2, 0)
    second = get_limiter(name, 99, 99)
    check(first is second, "same registry name did not return the same bucket")
    results.append("registry_identity")

    check_name = f"check-{uuid.uuid4()}"
    get_limiter(check_name, 1, 0)
    check(check_limiter(check_name) is True, "registry check denied an available token")
    check(check_limiter(check_name) is False, "registry check allowed an empty bucket")
    results.append("registry_check")

    decorator_name = f"decorator-{uuid.uuid4()}"

    @decorator(name=decorator_name, capacity=1, refill_rate=0)
    def protected(value):
        return value * 2

    check(protected(3) == 6, "decorated function did not return its value")
    try:
        protected(3)
    except error_type:
        pass
    else:
        raise AssertionError("decorator did not raise RateLimitExceeded")
    results.append("decorator_blocks")

    return {"passed": True, "checks": results, "workspace": str(workspace)}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("workspace", type=Path)
    args = parser.parse_args()
    try:
        result = run(args.workspace.resolve())
    except Exception as exc:
        print(json.dumps({"passed": False, "error": f"{type(exc).__name__}: {exc}"}, indent=2))
        return 1
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
