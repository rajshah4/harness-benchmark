"""Tests for SQLiteIncidentStore."""
from __future__ import annotations

import threading
import time

import pytest

from incident_ops.models import (
    IncidentStatus,
    IncidentNotFound,
    InvalidTransition,
    Severity,
    VersionConflict,
)
from incident_ops.sqlite_store import SQLiteIncidentStore


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def store(tmp_path):
    tick = 1000.0
    clock = lambda: tick
    return SQLiteIncidentStore(str(tmp_path / "test.db"), clock=clock, dedupe_window=300, lease_seconds=60)


@pytest.fixture
def store_factory(tmp_path):
    """Return a factory that opens a store on the same file."""
    db = str(tmp_path / "shared.db")

    def make(tick=1000.0):
        clock = lambda t=tick: t
        return SQLiteIncidentStore(db, clock=clock, dedupe_window=300, lease_seconds=60)

    return make


# ---------------------------------------------------------------------------
# Basic ingestion
# ---------------------------------------------------------------------------

def test_ingest_creates_incident(store):
    inc, created = store.ingest_alert("fp1", "DB down", "P1", now=1000.0)
    assert created is True
    assert inc.fingerprint == "fp1"
    assert inc.title == "DB down"
    assert inc.severity == Severity.P1
    assert inc.status == IncidentStatus.OPEN
    assert inc.alert_count == 1
    assert inc.version == 1
    assert inc.escalation_level == 0
    assert inc.created_at == 1000.0
    assert inc.sla_deadline == 1060.0  # P1 = 60s


def test_ingest_deduplication_merges(store):
    inc1, created1 = store.ingest_alert("fp1", "DB down", "P1", now=1000.0)
    assert created1 is True

    inc2, created2 = store.ingest_alert("fp1", "DB down again", "P1", now=1100.0)
    assert created2 is False
    assert inc2.id == inc1.id
    assert inc2.alert_count == 2
    assert inc2.version == 2
    assert inc2.updated_at == 1100.0


def test_ingest_outside_window_creates_new(store):
    inc1, _ = store.ingest_alert("fp1", "DB down", "P1", now=1000.0)
    # Outside 300s window
    inc2, created2 = store.ingest_alert("fp1", "DB down", "P1", now=1400.0)
    assert created2 is True
    assert inc2.id != inc1.id


def test_resolved_incident_never_reused(store):
    inc1, _ = store.ingest_alert("fp1", "DB down", "P1", now=1000.0)
    store.update(inc1.id, expected_version=1, status="resolved", now=1001.0)

    inc2, created = store.ingest_alert("fp1", "DB down", "P1", now=1100.0)
    assert created is True
    assert inc2.id != inc1.id


def test_ingest_idempotency_key(store):
    inc1, created1 = store.ingest_alert("fp1", "DB down", "P1", idempotency_key="key-1", now=1000.0)
    assert created1 is True

    inc2, created2 = store.ingest_alert("fp1", "DB down", "P1", idempotency_key="key-1", now=1050.0)
    assert created2 is True
    assert inc2.id == inc1.id
    assert inc2.version == 1  # unchanged


def test_ingest_dup_idempotency_key(store):
    inc1, _ = store.ingest_alert("fp1", "DB down", "P1", now=1000.0)
    inc2, created2 = store.ingest_alert("fp1", "DB down", "P1", idempotency_key="dup-key", now=1050.0)
    assert created2 is False

    # Repeating same idempotency key returns same result without changing version
    inc3, created3 = store.ingest_alert("fp1", "DB down", "P1", idempotency_key="dup-key", now=1060.0)
    assert created3 is False
    assert inc3.version == inc2.version


# ---------------------------------------------------------------------------
# Get / list
# ---------------------------------------------------------------------------

def test_get_returns_none_for_missing(store):
    assert store.get("no-such-id") is None


def test_list_filters(store):
    store.ingest_alert("fp1", "A", "P1", now=1000.0)
    store.ingest_alert("fp2", "B", "P2", now=1001.0)

    all_incidents = store.list()
    assert len(all_incidents) == 2

    p1_only = store.list(severity="P1")
    assert len(p1_only) == 1
    assert p1_only[0].fingerprint == "fp1"


def test_list_by_status(store):
    inc, _ = store.ingest_alert("fp1", "A", "P1", now=1000.0)
    store.update(inc.id, expected_version=1, status="resolved", now=1001.0)

    open_list = store.list(status="open")
    assert open_list == []
    resolved_list = store.list(status="resolved")
    assert len(resolved_list) == 1


# ---------------------------------------------------------------------------
# Update
# ---------------------------------------------------------------------------

def test_update_status_transitions(store):
    inc, _ = store.ingest_alert("fp1", "A", "P2", now=1000.0)

    # open -> acknowledged
    inc2 = store.update(inc.id, expected_version=1, status="acknowledged", now=1001.0)
    assert inc2.status == IncidentStatus.ACKNOWLEDGED

    # acknowledged -> resolved
    inc3 = store.update(inc.id, expected_version=inc2.version, status="resolved", now=1002.0)
    assert inc3.status == IncidentStatus.RESOLVED


def test_update_invalid_transition(store):
    inc, _ = store.ingest_alert("fp1", "A", "P2", now=1000.0)
    store.update(inc.id, expected_version=1, status="resolved", now=1001.0)

    with pytest.raises(InvalidTransition):
        store.update(inc.id, expected_version=2, status="acknowledged", now=1002.0)


def test_update_version_conflict(store):
    inc, _ = store.ingest_alert("fp1", "A", "P2", now=1000.0)
    store.update(inc.id, expected_version=1, status="acknowledged", now=1001.0)

    with pytest.raises(VersionConflict):
        # Use stale version 1 when current is 2
        store.update(inc.id, expected_version=1, status="resolved", now=1002.0)


def test_update_not_found(store):
    with pytest.raises(IncidentNotFound):
        store.update("ghost-id", expected_version=1, status="acknowledged")


def test_update_owner(store):
    inc, _ = store.ingest_alert("fp1", "A", "P3", now=1000.0)
    inc2 = store.update(inc.id, expected_version=1, owner="alice", now=1001.0)
    assert inc2.owner == "alice"


def test_update_idempotency(store):
    inc, _ = store.ingest_alert("fp1", "A", "P3", now=1000.0)
    inc2 = store.update(inc.id, expected_version=1, status="acknowledged", idempotency_key="upd-1", now=1001.0)
    inc3 = store.update(inc.id, expected_version=inc2.version, status="acknowledged", idempotency_key="upd-1", now=1002.0)
    assert inc3.version == inc2.version


# ---------------------------------------------------------------------------
# Audit events
# ---------------------------------------------------------------------------

def test_events_appended(store):
    inc, _ = store.ingest_alert("fp1", "A", "P2", now=1000.0)
    store.update(inc.id, expected_version=1, owner="bob", status="acknowledged", now=1001.0)

    evs = store.events(inc.id)
    types = [e.type for e in evs]
    assert "created" in types
    assert "owner_changed" in types
    assert "status_changed" in types


def test_duplicate_alert_event(store):
    inc, _ = store.ingest_alert("fp1", "A", "P1", now=1000.0)
    store.ingest_alert("fp1", "A", "P1", now=1100.0)

    evs = store.events(inc.id)
    types = [e.type for e in evs]
    assert "duplicate_alert" in types


# ---------------------------------------------------------------------------
# Snapshots are immutable
# ---------------------------------------------------------------------------

def test_snapshots_are_immutable(store):
    inc1, _ = store.ingest_alert("fp1", "A", "P1", now=1000.0)
    old_version = inc1.version
    store.ingest_alert("fp1", "A", "P1", now=1100.0)  # merge
    assert inc1.version == old_version  # snapshot unchanged


# ---------------------------------------------------------------------------
# Separate store instances share state
# ---------------------------------------------------------------------------

def test_separate_stores_share_state(store_factory):
    s1 = store_factory(1000.0)
    s2 = store_factory(2000.0)

    inc, _ = s1.ingest_alert("fp1", "A", "P2", now=1000.0)
    assert s2.get(inc.id) is not None
    assert s2.get(inc.id).title == "A"


# ---------------------------------------------------------------------------
# Escalation
# ---------------------------------------------------------------------------

def test_claim_due_escalation(store):
    inc, _ = store.ingest_alert("fp1", "A", "P1", now=1000.0)
    # P1 SLA = 60s; claim at 1100 (overdue)
    claimed = store.claim_due_escalation("worker-1", now=1100.0)
    assert claimed is not None
    assert claimed.id == inc.id


def test_claim_returns_none_when_not_due(store):
    store.ingest_alert("fp1", "A", "P1", now=1000.0)
    # Not yet overdue (sla_deadline = 1060, now = 1050)
    claimed = store.claim_due_escalation("worker-1", now=1050.0)
    assert claimed is None


def test_claim_exclusive(store):
    store.ingest_alert("fp1", "A", "P1", now=1000.0)
    c1 = store.claim_due_escalation("worker-1", now=1100.0)
    assert c1 is not None
    c2 = store.claim_due_escalation("worker-2", now=1100.0)
    assert c2 is None  # already claimed


def test_complete_escalation(store):
    inc, _ = store.ingest_alert("fp1", "A", "P1", now=1000.0)
    store.claim_due_escalation("w1", now=1100.0)
    completed = store.complete_escalation(inc.id, "w1", now=1100.0)
    assert completed.escalation_level == 1
    assert completed.sla_deadline == 1100.0 + 60  # next deadline


def test_recover_expired_claims(store):
    inc, _ = store.ingest_alert("fp1", "A", "P1", now=1000.0)
    store.claim_due_escalation("w1", now=1100.0)  # lease_expires = 1160
    recovered = store.recover_expired_claims(now=1200.0)
    assert recovered == 1

    # Now another worker can claim
    c2 = store.claim_due_escalation("w2", now=1200.0)
    assert c2 is not None


def test_resolved_never_claimed(store):
    inc, _ = store.ingest_alert("fp1", "A", "P1", now=1000.0)
    store.update(inc.id, expected_version=1, status="resolved", now=1001.0)
    claimed = store.claim_due_escalation("w1", now=1100.0)
    assert claimed is None


# ---------------------------------------------------------------------------
# Concurrent ingestion
# ---------------------------------------------------------------------------

def test_concurrent_ingestion_creates_one_incident(tmp_path):
    """Multiple threads ingesting the same fingerprint create exactly one incident."""
    db = str(tmp_path / "concurrent.db")
    results = []
    errors = []

    def ingest(tick):
        try:
            s = SQLiteIncidentStore(db, clock=lambda: tick, dedupe_window=300, lease_seconds=60)
            inc, created = s.ingest_alert("fp-concurrent", "Concurrent", "P2", now=tick)
            results.append((inc.id, created))
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=ingest, args=(1000.0 + i,)) for i in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert not errors, errors
    # All results should refer to the same incident
    ids = {r[0] for r in results}
    assert len(ids) == 1, f"Expected 1 incident, got IDs: {ids}"
