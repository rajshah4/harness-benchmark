"""Tests for EscalationWorker."""

from __future__ import annotations

import pytest

from incident_ops import EscalationWorker, SQLiteIncidentStore


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "esc.db"


def test_run_once_processes_overdue_incident(db_path):
    now = 1000.0
    store = SQLiteIncidentStore(db_path, clock=lambda: now, lease_seconds=300)
    inc, _ = store.ingest_alert("fp1", "Issue", "P1", now=now)
    worker = EscalationWorker(store, worker_id="w1")
    result = worker.run_once(now=9000.0)  # well past P1 SLA of 60s
    assert result is not None
    assert result.id == inc.id
    assert result.escalation_level == 1


def test_run_once_returns_none_when_idle(db_path):
    now = 1000.0
    store = SQLiteIncidentStore(db_path, clock=lambda: now)
    worker = EscalationWorker(store, worker_id="w1")
    result = worker.run_once(now=now)  # nothing to process
    assert result is None


def test_run_until_idle_returns_all_completed(db_path):
    now = 1000.0
    store = SQLiteIncidentStore(db_path, clock=lambda: now, lease_seconds=300)
    store.ingest_alert("fp1", "A", "P1", now=now)
    store.ingest_alert("fp2", "B", "P1", now=now)
    store.ingest_alert("fp3", "C", "P1", now=now)

    worker = EscalationWorker(store, worker_id="w1")
    # now just past P1 initial deadline (1060); after one round deadlines advance to 1120 > 1065
    completed = worker.run_until_idle(now=1065.0)
    assert len(completed) == 3
    for inc in completed:
        assert inc.escalation_level == 1


def test_run_until_idle_respects_max_incidents(db_path):
    now = 1000.0
    store = SQLiteIncidentStore(db_path, clock=lambda: now, lease_seconds=300)
    for i in range(5):
        store.ingest_alert(f"fp{i}", f"Issue {i}", "P1", now=now)

    worker = EscalationWorker(store, worker_id="w1")
    completed = worker.run_until_idle(max_incidents=2, now=9000.0)
    assert len(completed) == 2


def test_worker_id_generated_if_omitted(db_path):
    store = SQLiteIncidentStore(db_path)
    w1 = EscalationWorker(store)
    w2 = EscalationWorker(store)
    assert w1.worker_id != w2.worker_id


def test_escalation_advances_deadline(db_path):
    now = 1000.0
    store = SQLiteIncidentStore(db_path, clock=lambda: now, lease_seconds=300)
    inc, _ = store.ingest_alert("fp1", "P2 issue", "P2", now=now)
    # P2 initial deadline = 1000 + 300 = 1300
    worker = EscalationWorker(store, worker_id="w1")
    result = worker.run_once(now=9000.0)
    assert result is not None
    # Next deadline advances by another 300s from original deadline
    assert result.sla_deadline == 1300.0 + 300


def test_multiple_escalations_accumulate(db_path):
    now = 1000.0
    store = SQLiteIncidentStore(db_path, clock=lambda: now, lease_seconds=300)
    inc, _ = store.ingest_alert("fp1", "Chronic", "P1", now=now)
    worker = EscalationWorker(store, worker_id="w1")

    r1 = worker.run_once(now=9000.0)   # 1st escalation
    r2 = worker.run_once(now=10000.0)  # 2nd escalation
    assert r1 is not None
    assert r2 is not None
    assert r2.escalation_level == 2


def test_resolved_incident_skipped(db_path):
    now = 1000.0
    store = SQLiteIncidentStore(db_path, clock=lambda: now)
    inc, _ = store.ingest_alert("fp1", "Already done", "P1", now=now)
    store.update(inc.id, expected_version=1, status="resolved")

    worker = EscalationWorker(store, worker_id="w1")
    result = worker.run_once(now=9999.0)
    assert result is None


def test_concurrent_workers_no_double_claim(db_path):
    """Two workers racing to claim the same overdue incident each get it at most once."""
    import threading

    now = 1000.0
    # Short lease so claim does not affect the other worker adversely
    store = SQLiteIncidentStore(db_path, clock=lambda: now, lease_seconds=300)
    store.ingest_alert("fp1", "One incident", "P1", now=now)

    claims = []
    barrier = threading.Barrier(2)

    def try_claim(worker_id):
        barrier.wait()  # both threads race simultaneously
        r = store.claim_due_escalation(worker_id, now=9000.0)
        claims.append(r)

    threads = [threading.Thread(target=try_claim, args=(f"w{i}",)) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    # Exactly one worker should have claimed the incident; the other gets None
    successful = [r for r in claims if r is not None]
    assert len(successful) == 1
