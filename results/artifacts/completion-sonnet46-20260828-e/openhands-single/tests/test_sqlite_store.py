"""Tests for SQLiteIncidentStore."""

from __future__ import annotations

import threading
import time
import tempfile
import os

import pytest

from incident_ops.exceptions import (
    ClaimOwnershipError,
    IncidentNotFound,
    InvalidTransition,
    VersionConflict,
)
from incident_ops.models import IncidentStatus, Severity
from incident_ops.sqlite_store import SQLiteIncidentStore


@pytest.fixture
def db_path(tmp_path):
    return str(tmp_path / "test.db")


@pytest.fixture
def store(db_path):
    return SQLiteIncidentStore(db_path, clock=lambda: 1000.0)


# ---------------------------------------------------------------------------
# ingest_alert
# ---------------------------------------------------------------------------

def test_ingest_creates_new_incident(store):
    inc, created = store.ingest_alert("fp1", "DB down", "P1")
    assert created is True
    assert inc.fingerprint == "fp1"
    assert inc.title == "DB down"
    assert inc.severity == Severity.P1
    assert inc.status == IncidentStatus.OPEN
    assert inc.alert_count == 1
    assert inc.version == 1
    assert inc.escalation_level == 0
    assert inc.sla_deadline == 1000.0 + 60  # P1 = 60 s


def test_ingest_merges_duplicate_within_window(db_path):
    tick = [1000.0]
    store = SQLiteIncidentStore(db_path, clock=lambda: tick[0], dedupe_window=300)
    inc1, c1 = store.ingest_alert("fp1", "DB down", "P1")
    assert c1 is True

    tick[0] = 1100.0  # within 300 s window
    inc2, c2 = store.ingest_alert("fp1", "DB down again", "P1", now=1100.0)
    assert c2 is False
    assert inc2.id == inc1.id
    assert inc2.alert_count == 2
    assert inc2.version == 2


def test_ingest_creates_new_outside_window(db_path):
    tick = [1000.0]
    store = SQLiteIncidentStore(db_path, clock=lambda: tick[0], dedupe_window=300)
    inc1, _ = store.ingest_alert("fp1", "DB down", "P1")

    tick[0] = 1400.0  # 400 s later, outside window
    inc2, c2 = store.ingest_alert("fp1", "DB down", "P1", now=1400.0)
    assert c2 is True
    assert inc2.id != inc1.id


def test_ingest_resolved_always_creates_new(db_path):
    store = SQLiteIncidentStore(db_path, dedupe_window=9999)
    inc1, _ = store.ingest_alert("fp1", "DB down", "P1", now=1000.0)
    store.update(inc1.id, expected_version=1, status="resolved", now=1001.0)
    inc2, created = store.ingest_alert("fp1", "DB down", "P1", now=1002.0)
    assert created is True
    assert inc2.id != inc1.id


def test_ingest_idempotency_key(db_path):
    store = SQLiteIncidentStore(db_path)
    inc1, c1 = store.ingest_alert("fp1", "DB down", "P1", idempotency_key="key-1", now=1000.0)
    inc2, c2 = store.ingest_alert("fp1", "DB down", "P1", idempotency_key="key-1", now=1001.0)
    assert inc1.id == inc2.id
    assert c1 == c2
    assert inc2.version == inc1.version  # no change


def test_ingest_bad_severity_raises(db_path):
    store = SQLiteIncidentStore(db_path)
    with pytest.raises(ValueError):
        store.ingest_alert("fp1", "title", "P9")


# ---------------------------------------------------------------------------
# get / list
# ---------------------------------------------------------------------------

def test_get_returns_none_for_missing(store):
    assert store.get("no-such-id") is None


def test_list_filter_by_status(db_path):
    store = SQLiteIncidentStore(db_path)
    inc, _ = store.ingest_alert("fp1", "A", "P1", now=1.0)
    store.ingest_alert("fp2", "B", "P2", now=2.0)
    store.update(inc.id, expected_version=1, status="resolved", now=3.0)

    resolved = store.list(status="resolved")
    assert len(resolved) == 1
    assert resolved[0].id == inc.id

    open_ = store.list(status="open")
    assert len(open_) == 1


def test_list_filter_by_severity(db_path):
    store = SQLiteIncidentStore(db_path)
    store.ingest_alert("fp1", "A", "P1", now=1.0)
    store.ingest_alert("fp2", "B", "P2", now=2.0)
    assert len(store.list(severity="P1")) == 1
    assert len(store.list(severity="P2")) == 1


def test_list_filter_by_owner(db_path):
    store = SQLiteIncidentStore(db_path)
    inc, _ = store.ingest_alert("fp1", "A", "P1", now=1.0)
    store.ingest_alert("fp2", "B", "P2", now=2.0)
    store.update(inc.id, expected_version=1, owner="alice", now=3.0)
    assert len(store.list(owner="alice")) == 1
    assert len(store.list(owner="bob")) == 0


def test_list_sorted_by_created_at(db_path):
    store = SQLiteIncidentStore(db_path)
    inc_b, _ = store.ingest_alert("fp2", "B", "P2", now=200.0)
    inc_a, _ = store.ingest_alert("fp1", "A", "P1", now=100.0)
    ids = [i.id for i in store.list()]
    assert ids == [inc_a.id, inc_b.id]


# ---------------------------------------------------------------------------
# update
# ---------------------------------------------------------------------------

def test_update_assigns_owner(store):
    inc, _ = store.ingest_alert("fp1", "A", "P1", now=1.0)
    updated = store.update(inc.id, expected_version=1, owner="alice", now=2.0)
    assert updated.owner == "alice"
    assert updated.version == 2


def test_update_status_transition_open_to_ack(store):
    inc, _ = store.ingest_alert("fp1", "A", "P1", now=1.0)
    updated = store.update(inc.id, expected_version=1, status="acknowledged", now=2.0)
    assert updated.status == IncidentStatus.ACKNOWLEDGED


def test_update_status_transition_ack_to_resolved(store):
    inc, _ = store.ingest_alert("fp1", "A", "P1", now=1.0)
    inc = store.update(inc.id, expected_version=1, status="acknowledged", now=2.0)
    inc = store.update(inc.id, expected_version=2, status="resolved", now=3.0)
    assert inc.status == IncidentStatus.RESOLVED


def test_update_invalid_transition_raises(store):
    inc, _ = store.ingest_alert("fp1", "A", "P1", now=1.0)
    inc = store.update(inc.id, expected_version=1, status="resolved", now=2.0)
    with pytest.raises(InvalidTransition):
        store.update(inc.id, expected_version=2, status="acknowledged", now=3.0)


def test_update_version_conflict_raises(store):
    inc, _ = store.ingest_alert("fp1", "A", "P1", now=1.0)
    with pytest.raises(VersionConflict):
        store.update(inc.id, expected_version=99, status="acknowledged", now=2.0)


def test_update_missing_incident_raises(store):
    with pytest.raises(IncidentNotFound):
        store.update("no-such-id", expected_version=1, status="acknowledged")


def test_update_idempotency_key(db_path):
    store = SQLiteIncidentStore(db_path)
    inc, _ = store.ingest_alert("fp1", "A", "P1", now=1.0)
    r1 = store.update(inc.id, expected_version=1, owner="alice",
                      idempotency_key="upd-1", now=2.0)
    r2 = store.update(inc.id, expected_version=99, owner="bob",
                      idempotency_key="upd-1", now=3.0)
    assert r1.version == r2.version
    assert r2.owner == "alice"


# ---------------------------------------------------------------------------
# events
# ---------------------------------------------------------------------------

def test_events_are_ordered_by_insertion(store):
    inc, _ = store.ingest_alert("fp1", "A", "P1", now=1.0)
    store.update(inc.id, expected_version=1, owner="alice", now=2.0)
    store.update(inc.id, expected_version=2, status="acknowledged", now=3.0)
    evs = store.events(inc.id)
    assert [e.type for e in evs] == ["created", "owner_changed", "status_changed"]


def test_events_immutable_snapshot(store):
    inc, _ = store.ingest_alert("fp1", "A", "P1", now=1.0)
    evs_before = store.events(inc.id)
    store.update(inc.id, expected_version=1, owner="alice", now=2.0)
    # The earlier snapshot must not grow.
    assert len(evs_before) == 1


# ---------------------------------------------------------------------------
# Audit events content
# ---------------------------------------------------------------------------

def test_audit_event_created(db_path):
    store = SQLiteIncidentStore(db_path)
    inc, _ = store.ingest_alert("fp1", "DB down", "P1", source="prometheus", now=1.0)
    evs = store.events(inc.id)
    assert len(evs) == 1
    assert evs[0].type == "created"
    assert evs[0].details["source"] == "prometheus"


def test_audit_event_duplicate_alert(db_path):
    store = SQLiteIncidentStore(db_path, dedupe_window=9999)
    inc, _ = store.ingest_alert("fp1", "DB down", "P1", now=1.0)
    store.ingest_alert("fp1", "DB down", "P1", source="grafana", now=2.0)
    evs = store.events(inc.id)
    assert evs[1].type == "duplicate_alert"
    assert evs[1].details["source"] == "grafana"


# ---------------------------------------------------------------------------
# Escalation
# ---------------------------------------------------------------------------

def test_claim_due_escalation_returns_overdue(db_path):
    store = SQLiteIncidentStore(db_path, lease_seconds=30)
    inc, _ = store.ingest_alert("fp1", "A", "P1", now=1000.0)
    # SLA deadline = 1060; claim at 1061 (overdue)
    claimed = store.claim_due_escalation("worker-1", now=1061.0)
    assert claimed is not None
    assert claimed.id == inc.id


def test_claim_due_escalation_returns_none_when_nothing_due(db_path):
    store = SQLiteIncidentStore(db_path)
    store.ingest_alert("fp1", "A", "P1", now=1000.0)
    # SLA deadline = 1060; claim at 1059 (not yet overdue)
    claimed = store.claim_due_escalation("worker-1", now=1059.0)
    assert claimed is None


def test_complete_escalation(db_path):
    store = SQLiteIncidentStore(db_path, lease_seconds=30)
    inc, _ = store.ingest_alert("fp1", "A", "P1", now=1000.0)
    store.claim_due_escalation("worker-1", now=1061.0)
    result = store.complete_escalation(inc.id, "worker-1", now=1062.0)
    assert result.escalation_level == 1
    # Claim does not bump version; only complete_escalation does.
    assert result.version == 2  # ingest→v1, complete→v2
    # Next deadline = 1062 + 60 (P1 interval)
    assert result.sla_deadline == 1062.0 + 60


def test_complete_escalation_wrong_worker(db_path):
    store = SQLiteIncidentStore(db_path, lease_seconds=30)
    inc, _ = store.ingest_alert("fp1", "A", "P1", now=1000.0)
    store.claim_due_escalation("worker-1", now=1061.0)
    with pytest.raises(ClaimOwnershipError):
        store.complete_escalation(inc.id, "worker-2", now=1062.0)


def test_two_workers_claim_different_incidents(db_path):
    store = SQLiteIncidentStore(db_path, lease_seconds=30)
    inc_a, _ = store.ingest_alert("fp1", "A", "P1", now=1000.0)
    inc_b, _ = store.ingest_alert("fp2", "B", "P1", now=1000.0)
    # Both SLA deadlines = 1060.
    c1 = store.claim_due_escalation("worker-1", now=1061.0)
    c2 = store.claim_due_escalation("worker-2", now=1061.0)
    assert c1 is not None
    assert c2 is not None
    assert c1.id != c2.id


def test_resolved_incident_not_claimed(db_path):
    store = SQLiteIncidentStore(db_path)
    inc, _ = store.ingest_alert("fp1", "A", "P1", now=1000.0)
    store.update(inc.id, expected_version=1, status="resolved", now=1001.0)
    claimed = store.claim_due_escalation("worker-1", now=9999.0)
    assert claimed is None


def test_recover_expired_claims(db_path):
    store = SQLiteIncidentStore(db_path, lease_seconds=30)
    inc, _ = store.ingest_alert("fp1", "A", "P1", now=1000.0)
    store.claim_due_escalation("worker-1", now=1061.0)
    # Lease expires at 1061 + 30 = 1091
    count = store.recover_expired_claims(now=1092.0)
    assert count == 1
    # The incident should be claimable again.
    c2 = store.claim_due_escalation("worker-2", now=1092.0)
    assert c2 is not None


# ---------------------------------------------------------------------------
# Separate store instances share state
# ---------------------------------------------------------------------------

def test_two_stores_same_file(db_path):
    s1 = SQLiteIncidentStore(db_path)
    s2 = SQLiteIncidentStore(db_path)
    inc, _ = s1.ingest_alert("fp1", "A", "P1", now=1.0)
    result = s2.get(inc.id)
    assert result is not None
    assert result.title == "A"


# ---------------------------------------------------------------------------
# Concurrent ingest of the same fingerprint
# ---------------------------------------------------------------------------

def test_concurrent_ingest_same_fingerprint(db_path):
    """Two threads ingesting the same new fingerprint must create exactly one incident."""
    store = SQLiteIncidentStore(db_path, dedupe_window=9999)
    results = []
    errors = []

    def worker():
        try:
            inc, created = store.ingest_alert("fp-race", "Race", "P2", now=1000.0)
            results.append((inc.id, created))
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert not errors
    assert len(results) == 5
    # All share the same incident id.
    ids = {r[0] for r in results}
    assert len(ids) == 1
    # Exactly one creation, the rest are merges.
    created_flags = [r[1] for r in results]
    assert created_flags.count(True) == 1
