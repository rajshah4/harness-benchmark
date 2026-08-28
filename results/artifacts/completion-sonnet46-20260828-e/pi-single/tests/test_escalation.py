"""Tests for EscalationWorker."""
from __future__ import annotations

import pytest

from incident_ops.escalation import EscalationWorker
from incident_ops.sqlite_store import SQLiteIncidentStore


@pytest.fixture
def store(tmp_path):
    return SQLiteIncidentStore(str(tmp_path / "esc.db"), dedupe_window=300, lease_seconds=60)


def test_run_once_returns_none_when_idle(store):
    worker = EscalationWorker(store, "w1")
    assert worker.run_once(now=1000.0) is None


def test_run_once_escalates_overdue(store):
    inc, _ = store.ingest_alert("fp1", "Alert", "P1", now=1000.0)
    worker = EscalationWorker(store, "w1")
    result = worker.run_once(now=1100.0)
    assert result is not None
    assert result.id == inc.id
    assert result.escalation_level == 1


def test_run_until_idle(store):
    store.ingest_alert("fp1", "A1", "P1", now=1000.0)
    store.ingest_alert("fp2", "A2", "P1", now=1000.0)
    worker = EscalationWorker(store, "w1")
    results = worker.run_until_idle(now=1200.0)
    assert len(results) == 2
    for inc in results:
        assert inc.escalation_level == 1


def test_run_until_idle_max_incidents(store):
    store.ingest_alert("fp1", "A1", "P1", now=1000.0)
    store.ingest_alert("fp2", "A2", "P1", now=1000.0)
    store.ingest_alert("fp3", "A3", "P1", now=1000.0)
    worker = EscalationWorker(store, "w1")
    results = worker.run_until_idle(max_incidents=2, now=1200.0)
    assert len(results) == 2
