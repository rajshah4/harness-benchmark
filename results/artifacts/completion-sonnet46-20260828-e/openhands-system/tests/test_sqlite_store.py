"""Tests for SQLiteIncidentStore."""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from incident_ops import (
    AuditEvent,
    Incident,
    IncidentNotFound,
    IncidentStatus,
    InvalidTransition,
    Severity,
    SQLiteIncidentStore,
    VersionConflict,
)


# ── fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "test.db"


@pytest.fixture
def clock():
    tick = [100.0]

    def _clock():
        v = tick[0]
        tick[0] += 1.0
        return v

    return _clock


@pytest.fixture
def store(db_path, clock):
    return SQLiteIncidentStore(db_path, clock=clock, dedupe_window=300, lease_seconds=30)


# ── ingest_alert ──────────────────────────────────────────────────────────────

def test_ingest_creates_new_incident(store):
    inc, created = store.ingest_alert("fp1", "DB errors", "P1")
    assert created is True
    assert inc.fingerprint == "fp1"
    assert inc.title == "DB errors"
    assert inc.severity == Severity.P1
    assert inc.status == IncidentStatus.OPEN
    assert inc.alert_count == 1
    assert inc.version == 1
    assert inc.escalation_level == 0
    assert inc.sla_deadline == inc.created_at + 60  # P1 = 60s


def test_ingest_merges_duplicate_within_window(store):
    inc1, created1 = store.ingest_alert("fp1", "DB errors", "P1")
    inc2, created2 = store.ingest_alert("fp1", "DB errors again", "P1")

    assert created1 is True
    assert created2 is False
    assert inc1.id == inc2.id
    assert inc2.alert_count == 2
    assert inc2.version == 2


def test_ingest_outside_window_creates_new(db_path):
    ticks = iter([1000.0, 2000.0])  # 1000s apart, window=300
    store = SQLiteIncidentStore(db_path, clock=lambda: next(ticks), dedupe_window=300)
    inc1, created1 = store.ingest_alert("fp1", "First", "P2")
    inc2, created2 = store.ingest_alert("fp1", "Second", "P2")

    assert created1 is True
    assert created2 is True
    assert inc1.id != inc2.id


def test_ingest_resolved_incident_creates_new(store):
    inc1, _ = store.ingest_alert("fp1", "First", "P2")
    store.update(inc1.id, expected_version=1, status="resolved")
    inc2, created = store.ingest_alert("fp1", "Second", "P2")

    assert created is True
    assert inc1.id != inc2.id


def test_ingest_idempotency_key(store):
    inc1, c1 = store.ingest_alert("fp1", "A", "P3", idempotency_key="k1")
    inc2, c2 = store.ingest_alert("fp1", "A", "P3", idempotency_key="k1")

    assert inc1.id == inc2.id
    assert c1 == c2
    # version must not have changed
    assert inc1.version == inc2.version


def test_ingest_audit_event_created(store):
    inc, _ = store.ingest_alert("fp1", "Disk full", "P2", source="prometheus")
    events = store.events(inc.id)
    assert len(events) == 1
    assert events[0].type == "created"
    assert events[0].details["source"] == "prometheus"


def test_ingest_audit_event_duplicate(store):
    inc, _ = store.ingest_alert("fp1", "Disk full", "P2")
    store.ingest_alert("fp1", "Disk full", "P2")
    events = store.events(inc.id)
    assert len(events) == 2
    assert events[1].type == "duplicate_alert"
    assert events[1].details["alert_count"] == 2


# ── get / list ────────────────────────────────────────────────────────────────

def test_get_returns_none_for_unknown(store):
    assert store.get("no-such-id") is None


def test_list_order_and_filters(store):
    a, _ = store.ingest_alert("a", "Alpha", "P1")
    b, _ = store.ingest_alert("b", "Beta", "P2")
    store.update(b.id, expected_version=1, status="acknowledged")

    all_incidents = store.list()
    assert [i.id for i in all_incidents] == [a.id, b.id]

    open_only = store.list(status="open")
    assert len(open_only) == 1 and open_only[0].id == a.id

    p2_only = store.list(severity="P2")
    assert len(p2_only) == 1 and p2_only[0].id == b.id


def test_list_by_owner(store):
    a, _ = store.ingest_alert("a", "Alpha", "P1")
    store.update(a.id, expected_version=1, owner="alice")
    store.ingest_alert("b", "Beta", "P2")  # no owner

    owned = store.list(owner="alice")
    assert len(owned) == 1 and owned[0].id == a.id


# ── update ────────────────────────────────────────────────────────────────────

def test_update_assigns_owner(store):
    inc, _ = store.ingest_alert("fp1", "Issue", "P3")
    updated = store.update(inc.id, expected_version=1, owner="bob")
    assert updated.owner == "bob"
    assert updated.version == 2


def test_update_status_open_to_acknowledged(store):
    inc, _ = store.ingest_alert("fp1", "Issue", "P3")
    updated = store.update(inc.id, expected_version=1, status="acknowledged")
    assert updated.status == IncidentStatus.ACKNOWLEDGED


def test_update_status_open_to_resolved(store):
    inc, _ = store.ingest_alert("fp1", "Issue", "P3")
    updated = store.update(inc.id, expected_version=1, status="resolved")
    assert updated.status == IncidentStatus.RESOLVED


def test_update_status_acknowledged_to_resolved(store):
    inc, _ = store.ingest_alert("fp1", "Issue", "P3")
    store.update(inc.id, expected_version=1, status="acknowledged")
    updated = store.update(inc.id, expected_version=2, status="resolved")
    assert updated.status == IncidentStatus.RESOLVED


def test_update_invalid_transition(store):
    inc, _ = store.ingest_alert("fp1", "Issue", "P3")
    store.update(inc.id, expected_version=1, status="resolved")
    with pytest.raises(InvalidTransition):
        store.update(inc.id, expected_version=2, status="acknowledged")


def test_update_version_conflict(store):
    inc, _ = store.ingest_alert("fp1", "Issue", "P3")
    with pytest.raises(VersionConflict) as exc_info:
        store.update(inc.id, expected_version=999, status="acknowledged")
    assert exc_info.value.expected == 999


def test_update_not_found(store):
    with pytest.raises(IncidentNotFound):
        store.update("no-such-id", expected_version=1)


def test_update_appends_audit_events(store):
    inc, _ = store.ingest_alert("fp1", "Issue", "P3")
    store.update(inc.id, expected_version=1, owner="carol", status="acknowledged")
    events = store.events(inc.id)
    event_types = [e.type for e in events]
    assert "owner_changed" in event_types
    assert "status_changed" in event_types


def test_update_idempotency(store):
    inc, _ = store.ingest_alert("fp1", "Issue", "P3")
    r1 = store.update(inc.id, expected_version=1, owner="dave", idempotency_key="u1")
    r2 = store.update(inc.id, expected_version=1, owner="dave", idempotency_key="u1")
    assert r1.version == r2.version


# ── immutability ──────────────────────────────────────────────────────────────

def test_returned_incident_is_snapshot(store):
    inc, _ = store.ingest_alert("fp1", "Issue", "P1")
    snapshot = store.get(inc.id)
    # Update changes DB; snapshot must be unchanged
    store.update(inc.id, expected_version=1, owner="someone")
    assert snapshot.owner is None
    assert snapshot.version == 1


# ── separate store instances see same state ───────────────────────────────────

def test_separate_stores_share_state(db_path):
    s1 = SQLiteIncidentStore(db_path)
    s2 = SQLiteIncidentStore(db_path)
    inc, _ = s1.ingest_alert("fp1", "Shared", "P2")
    found = s2.get(inc.id)
    assert found is not None
    assert found.id == inc.id


# ── concurrent ingestion ──────────────────────────────────────────────────────

def test_concurrent_ingestion_single_incident(db_path):
    """Two threads ingesting the same fingerprint must produce only one incident."""
    results = []
    errors = []

    def ingest():
        try:
            s = SQLiteIncidentStore(db_path, dedupe_window=300)
            r = s.ingest_alert("concurrent-fp", "Title", "P2")
            results.append(r)
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=ingest) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert not errors, errors
    ids = {r[0].id for r in results}
    # All should see the same incident (either created or merged)
    assert len(ids) == 1


# ── escalation store methods ──────────────────────────────────────────────────

def test_claim_due_escalation_returns_overdue(db_path):
    now = 1000.0
    store = SQLiteIncidentStore(db_path, clock=lambda: now, lease_seconds=30)
    inc, _ = store.ingest_alert("fp1", "Issue", "P1", now=now)
    # P1 SLA = 60s, so deadline = 1060; claim at 1065 (overdue)
    claimed = store.claim_due_escalation("w1", now=1065.0)
    assert claimed is not None
    assert claimed.id == inc.id


def test_claim_returns_none_when_not_overdue(db_path):
    now = 1000.0
    store = SQLiteIncidentStore(db_path, clock=lambda: now)
    store.ingest_alert("fp1", "Issue", "P1", now=now)
    # Claim before SLA deadline (1050 < 1060)
    claimed = store.claim_due_escalation("w1", now=1050.0)
    assert claimed is None


def test_claim_multiple_workers_exclusive(db_path):
    now = 1000.0
    store = SQLiteIncidentStore(db_path, clock=lambda: now, lease_seconds=300)
    store.ingest_alert("fp1", "A", "P1", now=now)
    store.ingest_alert("fp2", "B", "P1", now=now)

    c1 = store.claim_due_escalation("w1", now=2000.0)
    c2 = store.claim_due_escalation("w2", now=2000.0)
    assert c1 is not None
    assert c2 is not None
    assert c1.id != c2.id


def test_complete_escalation(db_path):
    now = 1000.0
    store = SQLiteIncidentStore(db_path, clock=lambda: now, lease_seconds=300)
    inc, _ = store.ingest_alert("fp1", "Issue", "P2", now=now)
    # P2 SLA = 300s, deadline = 1300; claim at 2000
    store.claim_due_escalation("w1", now=2000.0)
    result = store.complete_escalation(inc.id, "w1", now=2001.0)
    assert result.escalation_level == 1
    assert result.version == inc.version + 1  # claim does not bump version; only complete_escalation does
    assert result.sla_deadline == 1300.0 + 300  # advanced


def test_complete_escalation_wrong_worker(db_path):
    now = 1000.0
    store = SQLiteIncidentStore(db_path, clock=lambda: now, lease_seconds=300)
    inc, _ = store.ingest_alert("fp1", "Issue", "P2", now=now)
    store.claim_due_escalation("w1", now=2000.0)
    with pytest.raises(ValueError):
        store.complete_escalation(inc.id, "wrong-worker", now=2001.0)


def test_recover_expired_claims(db_path):
    now = 1000.0
    store = SQLiteIncidentStore(db_path, clock=lambda: now, lease_seconds=10)
    inc, _ = store.ingest_alert("fp1", "Issue", "P1", now=now)
    store.claim_due_escalation("w1", now=2000.0)  # claim expires at 2010
    # Recover at 2020 (after expiry)
    recovered = store.recover_expired_claims(now=2020.0)
    assert recovered == 1

    # Incident should now be claimable again
    c2 = store.claim_due_escalation("w2", now=2020.0)
    assert c2 is not None
    assert c2.id == inc.id


def test_recover_expired_appends_audit_event(db_path):
    now = 1000.0
    store = SQLiteIncidentStore(db_path, clock=lambda: now, lease_seconds=10)
    inc, _ = store.ingest_alert("fp1", "Issue", "P1", now=now)
    store.claim_due_escalation("w1", now=2000.0)
    store.recover_expired_claims(now=2020.0)
    events = store.events(inc.id)
    event_types = [e.type for e in events]
    assert "claim_recovered" in event_types


def test_resolved_incident_not_claimed(db_path):
    now = 1000.0
    store = SQLiteIncidentStore(db_path, clock=lambda: now)
    inc, _ = store.ingest_alert("fp1", "Issue", "P1", now=now)
    store.update(inc.id, expected_version=1, status="resolved")
    claimed = store.claim_due_escalation("w1", now=9999.0)
    assert claimed is None


def test_escalation_claim_appends_audit_event(db_path):
    now = 1000.0
    store = SQLiteIncidentStore(db_path, clock=lambda: now, lease_seconds=300)
    inc, _ = store.ingest_alert("fp1", "Issue", "P1", now=now)
    store.claim_due_escalation("w1", now=2000.0)
    events = store.events(inc.id)
    event_types = [e.type for e in events]
    assert "escalation_claimed" in event_types


def test_escalated_audit_event(db_path):
    now = 1000.0
    store = SQLiteIncidentStore(db_path, clock=lambda: now, lease_seconds=300)
    inc, _ = store.ingest_alert("fp1", "Issue", "P1", now=now)
    store.claim_due_escalation("w1", now=2000.0)
    store.complete_escalation(inc.id, "w1", now=2001.0)
    events = store.events(inc.id)
    event_types = [e.type for e in events]
    assert "escalated" in event_types


# ── SLA deadlines ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("severity,expected_sla", [
    ("P1", 60),
    ("P2", 300),
    ("P3", 900),
    ("P4", 3600),
])
def test_sla_deadline_set_by_severity(db_path, severity, expected_sla):
    now = 5000.0
    store = SQLiteIncidentStore(db_path, clock=lambda: now)
    inc, _ = store.ingest_alert("fp1", "Issue", severity, now=now)
    assert inc.sla_deadline == now + expected_sla


# ── export / import roundtrip (exercised via store directly) ──────────────────

def test_export_import_roundtrip(tmp_path):
    src = SQLiteIncidentStore(tmp_path / "src.db")
    inc, _ = src.ingest_alert("fp1", "Alpha", "P2")
    src.update(inc.id, expected_version=1, owner="alice", status="acknowledged")

    # Export
    lines = []
    for i in src.list():
        from incident_ops.cli import _incident_dict, _event_dict
        import json
        lines.append(json.dumps({"type": "incident", "data": _incident_dict(i)}))
        for ev in src.events(i.id):
            lines.append(json.dumps({"type": "audit_event", "data": _event_dict(ev)}))

    # Import into new DB
    import json
    dst = SQLiteIncidentStore(tmp_path / "dst.db")
    conn = dst._conn()
    conn.execute("BEGIN IMMEDIATE")
    for line in lines:
        rec = json.loads(line)
        data = rec["data"]
        if rec["type"] == "incident":
            conn.execute(
                """INSERT OR IGNORE INTO incidents
                   (id, fingerprint, title, severity, status, owner,
                    alert_count, version, escalation_level,
                    created_at, updated_at, sla_deadline)
                   VALUES (:id,:fingerprint,:title,:severity,:status,:owner,
                           :alert_count,:version,:escalation_level,
                           :created_at,:updated_at,:sla_deadline)""",
                data,
            )
        else:
            conn.execute(
                """INSERT OR IGNORE INTO audit_events
                   (id, incident_id, type, timestamp, details)
                   VALUES (:id, :incident_id, :type, :timestamp, :details)""",
                {**data, "details": json.dumps(data.get("details", {}))},
            )
    conn.execute("COMMIT")

    dst_inc = dst.get(inc.id)
    assert dst_inc is not None
    assert dst_inc.owner == "alice"
    assert dst_inc.status == IncidentStatus.ACKNOWLEDGED

    dst_events = dst.events(inc.id)
    src_events = src.events(inc.id)
    assert len(dst_events) == len(src_events)
    assert [e.type for e in dst_events] == [e.type for e in src_events]
