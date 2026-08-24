#!/usr/bin/env python3
"""Audit v2 worker behavior when the original task omitted its return type."""

from __future__ import annotations

import argparse
import importlib
import json
import sys
import tempfile
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("workspace", type=Path)
    args = parser.parse_args()
    workspace = args.workspace.resolve()
    sys.path.insert(0, str(workspace))
    importlib.invalidate_caches()

    store_module = importlib.import_module("incident_ops.sqlite_store")
    worker_module = importlib.import_module("incident_ops.escalation")
    store_type = store_module.SQLiteIncidentStore
    worker_type = worker_module.EscalationWorker

    with tempfile.TemporaryDirectory() as directory:
        store = store_type(Path(directory) / "worker.db", clock=lambda: 2000.0)
        incident, _ = store.ingest_alert(
            "worker-run",
            "Worker run once",
            "P1",
            now=2000.0,
        )
        result = worker_type(store, "worker-c").run_once(now=2061.0)
        persisted = store.get(incident.id)
        passed = bool(result) and persisted is not None and persisted.escalation_level == 1
        print(
            json.dumps(
                {
                    "passed": passed,
                    "return_type": type(result).__name__,
                    "return_value": result if isinstance(result, bool) else None,
                    "incident_id": incident.id,
                    "persisted_escalation_level": None
                    if persisted is None
                    else persisted.escalation_level,
                },
                sort_keys=True,
            )
        )
        return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
