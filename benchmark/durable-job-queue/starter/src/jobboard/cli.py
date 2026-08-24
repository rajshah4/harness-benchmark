"""Minimal command line demonstration for the in-memory runner."""

from __future__ import annotations

import argparse
import json

from .memory_store import MemoryJobStore
from .runner import JobRunner


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("kind", choices=("echo", "sum"))
    parser.add_argument("payload")
    args = parser.parse_args()
    payload = json.loads(args.payload)
    handlers = {
        "echo": lambda value: value,
        "sum": lambda value: sum(value["numbers"]),
    }
    store = MemoryJobStore()
    job = store.enqueue(args.kind, payload)
    JobRunner(store, handlers).run_next()
    print(json.dumps({"id": job.id, "state": job.state.value, "result": job.result}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
