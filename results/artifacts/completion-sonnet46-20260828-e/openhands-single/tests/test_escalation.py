"""Tests for EscalationWorker."""

from __future__ import annotations

import threading

import pytest

from incident_ops.escalation import EscalationWorker
from incident_ops.sqlite_store import SQLiteIncidentStore


@pytest.fixture
def db_path(tmp_path):
    return str(tmp_path / "esc.db")


def test_run_once_escalates_overdue(db_path):
    store = SQLiteIncidentStore(db_path, lease_seconds=30)
    inc, _ = store.ingest_alert("fp1", "A", "P1", now=1000.0)
    worker = EscalationWorker(store, "w1")
    result = worker.run_once(now=1061.0)
    assert result is not None
    assert result.escalation_level == 1


def test_run_once_returns_none_when_idle(db_path):
    store = SQLiteIncidentStore(db_path)
    store.ingest_alert("fp1", "A", "P1", now=1000.0)
    worker = EscalationWorker(store, "w1")
    result = worker.run_once(now=1059.0)  # not yet overdue
    assert result is None


def test_run_until_idle_processes_all(db_path):
    store = SQLiteIncidentStore(db_path, lease_seconds=30)
    for i in range(3):
        store.ingest_alert(f"fp{i}", f"Inc {i}", "P2", now=1000.0)
    # SLA deadline = 1000 + 300 = 1300
    worker = EscalationWorker(store, "w1")
    results = worker.run_until_idle(now=1301.0)
    assert len(results) == 3
    for inc in results:
        assert inc.escalation_level == 1


def test_run_until_idle_max_incidents(db_path):
    store = SQLiteIncidentStore(db_path, lease_seconds=30)
    for i in range(5):
        store.ingest_alert(f"fp{i}", f"Inc {i}", "P1", now=1000.0)
    worker = EscalationWorker(store, "w1")
    results = worker.run_until_idle(max_incidents=2, now=1061.0)
    assert len(results) == 2


def test_run_until_idle_returns_snapshots_not_live_objects(db_path):
    store = SQLiteIncidentStore(db_path, lease_seconds=30)
    inc, _ = store.ingest_alert("fp1", "A", "P1", now=1000.0)
    worker = EscalationWorker(store, "w1")
    results = worker.run_until_idle(now=1061.0)
    snapshot = results[0]
    # Resolve the incident after snapshot was taken.
    store.update(inc.id, expected_version=snapshot.version, status="resolved", now=1100.0)
    # Snapshot is unchanged.
    assert snapshot.status.value == "open"


def test_concurrent_workers_no_double_processing(db_path):
    """Two concurrent workers must each escalate different incidents."""
    store = SQLiteIncidentStore(db_path, lease_seconds=30)
    for i in range(4):
        store.ingest_alert(f"fp{i}", f"Inc {i}", "P1", now=1000.0)

    processed = []
    errors = []

    def run_worker(wid):
        try:
            w = EscalationWorker(store, wid)
            results = w.run_until_idle(now=1061.0)
            processed.extend(results)
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=run_worker, args=(f"w{i}",)) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)

    assert not errors
    # All 4 incidents should be escalated exactly once.
    assert len(processed) == 4
    ids = [i.id for i in processed]
    assert len(set(ids)) == 4  # no duplicates
