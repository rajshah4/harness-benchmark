"""Behavioural tests for SQLiteFreightStore.

All tests use a temporary in-memory-equivalent SQLite file (tmp_path) and
a controlled clock so timestamps are deterministic.  No mocks are used;
every test exercises real code paths against the real SQLite store.
"""
from __future__ import annotations

import json
import time
import uuid
from pathlib import Path

import pytest

from freight_tower.exceptions import (
    AuthError,
    AuthzError,
    ConflictError,
    NotFoundError,
    ValidationError,
    VersionConflictError,
)
from freight_tower.sqlite_store import SQLiteFreightStore


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

class _Clock:
    """Monotonically advancing clock for tests."""
    def __init__(self, start: float = 1_000.0) -> None:
        self._t = start

    def __call__(self) -> float:
        t = self._t
        self._t += 1.0
        return t

    def advance(self, seconds: float) -> None:
        self._t += seconds


@pytest.fixture
def store(tmp_path: Path) -> SQLiteFreightStore:
    clk = _Clock()
    s = SQLiteFreightStore(tmp_path / "test.db", clock=clk, lease_seconds=30, max_attempts=3)
    s.bootstrap_tenant("acme", "ACME Corp", "admin-tok")
    s.create_credential("admin-tok", "op-tok", "operator")
    s.create_credential("admin-tok", "view-tok", "viewer")
    return s


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

def test_invalid_token_raises(store):
    with pytest.raises(AuthError):
        store.list_shipments("bad-token")


def test_viewer_cannot_create_shipment(store):
    with pytest.raises(AuthzError):
        store.create_shipment("view-tok", "V-001")


def test_operator_cannot_bootstrap(store):
    with pytest.raises(AuthzError):
        store.create_credential("op-tok", "new-tok", "viewer")


def test_duplicate_token_same_role_is_idempotent(store):
    # Exact same token + role for the same tenant → idempotent, no exception.
    store.create_credential("admin-tok", "op-tok", "operator")  # already exists
    # Calling again must not raise and must not create a duplicate row.
    creds_before = store.audit("op-tok", action="create_credential")
    store.create_credential("admin-tok", "op-tok", "operator")
    creds_after = store.audit("op-tok", action="create_credential")
    assert len(creds_after) == len(creds_before)  # no new audit entry


def test_duplicate_token_different_role_raises_conflict(store):
    # Same token string but different role → ConflictError (distinct from ValidationError).
    with pytest.raises(ConflictError):
        store.create_credential("admin-tok", "op-tok", "viewer")


def test_duplicate_token_cross_tenant_raises_conflict(tmp_path):
    clk = _Clock()
    s = SQLiteFreightStore(tmp_path / "xt.db", clock=clk)
    s.bootstrap_tenant("t1", "T1", "t1-admin")
    s.bootstrap_tenant("t2", "T2", "t2-admin")
    s.create_credential("t1-admin", "shared-tok", "operator")
    with pytest.raises(ConflictError):
        s.create_credential("t2-admin", "shared-tok", "operator")


def test_bootstrap_token_conflict_raises_conflict_error(tmp_path):
    """bootstrap_tenant must raise ConflictError (not ValidationError) when
    the admin_token is already in use, matching create_credential's behaviour."""
    clk = _Clock()
    s = SQLiteFreightStore(tmp_path / "bt.db", clock=clk)
    s.bootstrap_tenant("first", "First", "shared-admin-tok")
    with pytest.raises(ConflictError):
        s.bootstrap_tenant("second", "Second", "shared-admin-tok")


# ---------------------------------------------------------------------------
# Tenant isolation
# ---------------------------------------------------------------------------

def test_tenant_isolation(tmp_path):
    clk = _Clock()
    s = SQLiteFreightStore(tmp_path / "iso.db", clock=clk)
    s.bootstrap_tenant("t1", "Tenant 1", "tok-t1")
    s.bootstrap_tenant("t2", "Tenant 2", "tok-t2")
    s.create_credential("tok-t1", "op-t1", "operator")
    s.create_credential("tok-t2", "op-t2", "operator")

    s.create_shipment("op-t1", "T1-001")
    s.create_shipment("op-t2", "T2-001")

    t1_ships = s.list_shipments("op-t1")
    t2_ships = s.list_shipments("op-t2")
    assert len(t1_ships) == 1
    assert t1_ships[0].reference == "T1-001"
    assert len(t2_ships) == 1
    assert t2_ships[0].reference == "T2-001"


def test_cross_tenant_shipment_not_visible(store, tmp_path):
    clk = _Clock()
    s2 = SQLiteFreightStore(tmp_path / "s2.db", clock=clk)
    s2.bootstrap_tenant("other", "Other", "other-admin")
    s2.create_credential("other-admin", "other-op", "operator")
    ship = s2.create_shipment("other-op", "OTHER-001")

    with pytest.raises(NotFoundError):
        store.get_shipment("op-tok", ship.id)


# ---------------------------------------------------------------------------
# Shipment creation
# ---------------------------------------------------------------------------

def test_create_and_get_shipment(store):
    ship = store.create_shipment("op-tok", "ACME-001")
    assert ship.status == "created"
    assert ship.version == 1
    fetched = store.get_shipment("op-tok", ship.id)
    assert fetched.id == ship.id
    assert fetched.reference == "ACME-001"


def test_duplicate_reference_rejected(store):
    store.create_shipment("op-tok", "DUP-001")
    with pytest.raises(ValidationError):
        store.create_shipment("op-tok", "DUP-001")


def test_list_shipments_filter_status(store):
    store.create_shipment("op-tok", "S1")
    ship2 = store.create_shipment("op-tok", "S2")
    ev_id = str(uuid.uuid4())
    store.ingest_event("op-tok", ship2.id, ev_id, "delayed", 1001.0)

    delayed = store.list_shipments("op-tok", status="delayed")
    assert len(delayed) == 1
    assert delayed[0].reference == "S2"


# ---------------------------------------------------------------------------
# Event ingestion and projection
# ---------------------------------------------------------------------------

def test_event_projection_basic(store):
    ship = store.create_shipment("op-tok", "P-001")
    result = store.ingest_event("op-tok", ship.id, "ev1", "picked_up", 100.0, location="Memphis")
    assert result.status == "in_transit"
    assert result.last_location == "Memphis"


def test_out_of_order_events(store):
    """Late historical event must not roll back a later delivery."""
    ship = store.create_shipment("op-tok", "OOO-001")
    ship_id = ship.id

    store.ingest_event("op-tok", ship_id, "ev-delay", "delayed", 200.0)
    store.ingest_event("op-tok", ship_id, "ev-delivered", "delivered", 300.0)

    # Now insert a late delayed event timestamped before the delivery
    result = store.ingest_event("op-tok", ship_id, "ev-late-delay", "delayed", 150.0)
    assert result.status == "delivered", "Late delay must not override later delivery"


def test_deterministic_tie_break(store):
    """Two events at the same time — sorted by event_id (UUID string)."""
    ship = store.create_shipment("op-tok", "TIE-001")
    sid = ship.id

    # Insert both at the same event_time; lower UUID string sorts first
    id_a = "aaa-" + "0" * 28
    id_b = "zzz-" + "0" * 28
    store.ingest_event("op-tok", sid, id_b, "in_transit", 500.0, location="B")
    result = store.ingest_event("op-tok", sid, id_a, "delayed", 500.0, location="A")
    # id_a < id_b so delayed(id_a, t=500) is processed first, then in_transit(id_b, t=500)
    assert result.status == "in_transit"


def test_idempotent_event_ingestion(store):
    ship = store.create_shipment("op-tok", "IDEM-001")
    ev_id = str(uuid.uuid4())
    r1 = store.ingest_event("op-tok", ship.id, ev_id, "picked_up", 100.0, location="NYC")
    r2 = store.ingest_event("op-tok", ship.id, ev_id, "picked_up", 100.0, location="NYC")
    assert r1.id == r2.id
    assert r1.version == r2.version


def test_idempotent_event_no_side_effects(store):
    """Replaying the same event must not add audit entries, exceptions, or deliveries."""
    ship = store.create_shipment("op-tok", "IDEM-SIDE")
    ev_id = str(uuid.uuid4())
    store.ingest_event("op-tok", ship.id, ev_id, "delayed", 100.0, location="NYC")

    audit_before = store.audit("op-tok")
    excs_before = store.list_exceptions("op-tok")
    deliveries_before = store.list_deliveries("op-tok")

    # Replay exactly the same event
    r2 = store.ingest_event("op-tok", ship.id, ev_id, "delayed", 100.0, location="NYC")

    assert r2.status == "delayed"
    assert len(store.audit("op-tok")) == len(audit_before)           # no new audit
    assert len(store.list_exceptions("op-tok")) == len(excs_before)  # no new exception
    assert len(store.list_deliveries("op-tok")) == len(deliveries_before)  # no new delivery


def test_conflicting_event_id_raises(store):
    ship = store.create_shipment("op-tok", "CONF-001")
    ev_id = str(uuid.uuid4())
    store.ingest_event("op-tok", ship.id, ev_id, "picked_up", 100.0)
    with pytest.raises(ConflictError):
        store.ingest_event("op-tok", ship.id, ev_id, "delayed", 100.0)


def test_invalid_event_type_raises(store):
    ship = store.create_shipment("op-tok", "BAD-001")
    with pytest.raises(ValidationError):
        store.ingest_event("op-tok", ship.id, "ev-bad", "teleported", 100.0)


# ---------------------------------------------------------------------------
# Exception workflow
# ---------------------------------------------------------------------------

def test_delay_opens_exception(store):
    ship = store.create_shipment("op-tok", "EX-001")
    store.ingest_event("op-tok", ship.id, "ev1", "delayed", 200.0)
    updated = store.get_shipment("op-tok", ship.id)
    assert updated.active_exception_id is not None

    excs = store.list_exceptions("op-tok")
    assert len(excs) == 1
    assert excs[0].status == "open"


def test_delivery_resolves_exception(store):
    ship = store.create_shipment("op-tok", "EX-002")
    store.ingest_event("op-tok", ship.id, "ev1", "delayed", 200.0)
    store.ingest_event("op-tok", ship.id, "ev2", "delivered", 300.0)

    updated = store.get_shipment("op-tok", ship.id)
    assert updated.active_exception_id is None

    excs = store.list_exceptions("op-tok")
    assert excs[0].status == "resolved"


def test_cancellation_resolves_exception(store):
    ship = store.create_shipment("op-tok", "EX-CANCEL")
    store.ingest_event("op-tok", ship.id, "ev1", "delayed", 200.0)
    store.ingest_event("op-tok", ship.id, "ev2", "cancelled", 300.0)
    excs = store.list_exceptions("op-tok")
    assert excs[0].status == "resolved"


def test_delay_after_resolution_creates_new_exception(store):
    ship = store.create_shipment("op-tok", "EX-003")
    store.ingest_event("op-tok", ship.id, "ev1", "delayed", 100.0)
    store.ingest_event("op-tok", ship.id, "ev2", "in_transit", 200.0)
    store.ingest_event("op-tok", ship.id, "ev3", "delayed", 300.0)

    excs = store.list_exceptions("op-tok")
    open_excs = [e for e in excs if e.status == "open"]
    resolved_excs = [e for e in excs if e.status == "resolved"]
    assert len(open_excs) == 1
    assert len(resolved_excs) == 1


def test_exception_idempotent_on_duplicate_delay(store):
    """Same delay epoch → only one exception created."""
    ship = store.create_shipment("op-tok", "EX-IDEM")
    store.ingest_event("op-tok", ship.id, "ev1", "delayed", 100.0)
    # Another delayed event (same epoch continues)
    store.ingest_event("op-tok", ship.id, "ev2", "delayed", 150.0)
    excs = store.list_exceptions("op-tok")
    open_excs = [e for e in excs if e.status == "open"]
    assert len(open_excs) == 1


def test_severity_from_event_details(store):
    ship = store.create_shipment("op-tok", "SEV-001")
    store.ingest_event("op-tok", ship.id, "ev1", "delayed", 100.0,
                       details={"severity": "P1"})
    excs = store.list_exceptions("op-tok")
    assert excs[0].severity == "P1"


# ---------------------------------------------------------------------------
# Optimistic concurrency
# ---------------------------------------------------------------------------

def test_mutate_exception_acknowledge(store):
    ship = store.create_shipment("op-tok", "MUT-001")
    store.ingest_event("op-tok", ship.id, "ev1", "delayed", 100.0)
    exc = store.list_exceptions("op-tok")[0]

    updated = store.mutate_exception("op-tok", exc.id, 1, "acknowledge", actor="alice")
    assert updated.status == "acknowledged"
    assert updated.version == 2


def test_mutate_stale_version_raises(store):
    ship = store.create_shipment("op-tok", "MUT-002")
    store.ingest_event("op-tok", ship.id, "ev1", "delayed", 100.0)
    exc = store.list_exceptions("op-tok")[0]

    store.mutate_exception("op-tok", exc.id, 1, "acknowledge")
    with pytest.raises(VersionConflictError):
        store.mutate_exception("op-tok", exc.id, 1, "resolve")


def test_mutate_resolve(store):
    ship = store.create_shipment("op-tok", "MUT-003")
    store.ingest_event("op-tok", ship.id, "ev1", "delayed", 100.0)
    exc = store.list_exceptions("op-tok")[0]

    updated = store.mutate_exception("op-tok", exc.id, 1, "resolve", actor="bob")
    assert updated.status == "resolved"

    ship_refreshed = store.get_shipment("op-tok", ship.id)
    assert ship_refreshed.active_exception_id is None


def test_mutate_assign_and_add_note(store):
    ship = store.create_shipment("op-tok", "MUT-004")
    store.ingest_event("op-tok", ship.id, "ev1", "delayed", 100.0)
    exc = store.list_exceptions("op-tok")[0]

    updated = store.mutate_exception("op-tok", exc.id, 1, "assign", assignee="carol")
    assert updated.assignee == "carol"

    updated2 = store.mutate_exception("op-tok", exc.id, 2, "add_note", note="Carrier contacted")
    assert len(updated2.notes) == 1
    assert updated2.notes[0].note == "Carrier contacted"


def test_resolve_already_resolved_raises(store):
    ship = store.create_shipment("op-tok", "MUT-005")
    store.ingest_event("op-tok", ship.id, "ev1", "delayed", 100.0)
    exc = store.list_exceptions("op-tok")[0]
    store.mutate_exception("op-tok", exc.id, 1, "resolve")
    with pytest.raises(ValidationError):
        # Need to fetch fresh version
        fresh = store.list_exceptions("op-tok")[0]
        store.mutate_exception("op-tok", fresh.id, 2, "resolve")


def test_viewer_cannot_mutate(store):
    ship = store.create_shipment("op-tok", "AUTH-001")
    store.ingest_event("op-tok", ship.id, "ev1", "delayed", 100.0)
    exc = store.list_exceptions("op-tok")[0]
    with pytest.raises(AuthzError):
        store.mutate_exception("view-tok", exc.id, 1, "acknowledge")


# ---------------------------------------------------------------------------
# Audit trail
# ---------------------------------------------------------------------------

def test_audit_entries_created(store):
    ship = store.create_shipment("op-tok", "AUD-001")
    store.ingest_event("op-tok", ship.id, "ev1", "delayed", 100.0)

    entries = store.audit("op-tok")
    actions = {e.action for e in entries}
    assert "create_shipment" in actions
    assert "ingest_event" in actions
    assert "exception_opened" in actions


def test_audit_filter_by_entity(store):
    ship = store.create_shipment("op-tok", "AUD-002")
    entries = store.audit("op-tok", entity_type="shipment", entity_id=ship.id)
    assert all(e.entity_id == ship.id for e in entries)


# ---------------------------------------------------------------------------
# SLA rules and tick
# ---------------------------------------------------------------------------

def test_sla_rule_zero_delay_accepted(tmp_path):
    """delay_seconds=0 means 'escalate immediately'; must not raise."""
    clk = _Clock()
    s = SQLiteFreightStore(tmp_path / "zero.db", clock=clk)
    s.bootstrap_tenant("z", "Z", "z-admin")
    rule = s.set_sla_rule("z-admin", "P1", delay_seconds=0)
    assert rule.delay_seconds == 0.0


def test_sla_rule_negative_delay_rejected(tmp_path):
    clk = _Clock()
    s = SQLiteFreightStore(tmp_path / "neg.db", clock=clk)
    s.bootstrap_tenant("n", "N", "n-admin")
    with pytest.raises(ValidationError):
        s.set_sla_rule("n-admin", "P1", delay_seconds=-1)


def test_tick_atomicity_and_restart_durability(tmp_path):
    """After a successful tick(), a fresh SQLiteFreightStore opened on the same
    database file must observe escalation_queued_at IS NOT NULL and must return 0
    on a subsequent tick() — demonstrating that the INSERT OR IGNORE and the
    UPDATE escalation_queued_at committed atomically and persisted durably."""
    clk = _Clock(start=0.0)
    s1 = SQLiteFreightStore(tmp_path / "dur.db", clock=clk)
    s1.bootstrap_tenant("d", "D", "d-admin")
    s1.create_credential("d-admin", "d-op", "operator")
    s1.set_sla_rule("d-admin", "P2", delay_seconds=10.0)
    ship = s1.create_shipment("d-op", "D-001")
    s1.ingest_event("d-op", ship.id, "ev1", "delayed", 1.0)

    excs1 = s1.list_exceptions("d-op")
    opened_at = excs1[0].opened_at

    count = s1.tick(now=opened_at + 20.0)
    assert count == 1

    # Open a brand-new store instance on the same file (simulates restart)
    clk2 = _Clock(start=9000.0)
    s2 = SQLiteFreightStore(tmp_path / "dur.db", clock=clk2)

    # Fresh instance must see escalation_queued_at committed by s1
    excs2 = s2.list_exceptions("d-op")
    assert excs2[0].escalation_queued_at is not None, \
        "escalation_queued_at must be visible to a fresh store instance (restart durability)"

    # Fresh instance must not re-escalate
    count2 = s2.tick(now=opened_at + 30.0)
    assert count2 == 0, \
        "tick() on a fresh instance must not re-enqueue an already-escalated exception"


def test_idempotent_ingest_single_audit_entry(store):
    """Two ingest calls for the same event_id must produce exactly one audit
    entry and one outbox delivery, regardless of how many times the event is
    submitted — confirming the _IdempotentReturn sentinel fires before any
    _add_audit / _add_delivery call on the duplicate path."""
    ship = store.create_shipment("op-tok", "IDEM-AUDIT")
    ev_id = str(uuid.uuid4())

    store.ingest_event("op-tok", ship.id, ev_id, "in_transit", 200.0)

    # Capture audit + delivery counts immediately after first ingest
    audits_after_first = store.audit("op-tok", entity_type="shipment",
                                     entity_id=ship.id)
    deliveries_after_first = store.list_deliveries("op-tok",
                                                    entity_id=ship.id)
    ingest_audits_first = [a for a in audits_after_first
                           if a.action == "ingest_event"]
    assert len(ingest_audits_first) == 1

    # Replay same event three times
    for _ in range(3):
        store.ingest_event("op-tok", ship.id, ev_id, "in_transit", 200.0)

    # Counts must not have grown
    audits_after_replay = store.audit("op-tok", entity_type="shipment",
                                      entity_id=ship.id)
    deliveries_after_replay = store.list_deliveries("op-tok",
                                                     entity_id=ship.id)
    ingest_audits_replay = [a for a in audits_after_replay
                            if a.action == "ingest_event"]
    assert len(ingest_audits_replay) == len(ingest_audits_first), \
        "Replaying an identical event must not produce additional audit entries"
    assert len(deliveries_after_replay) == len(deliveries_after_first), \
        "Replaying an identical event must not produce additional outbox deliveries"


def test_tick_enqueues_escalation(tmp_path):
    clk = _Clock(start=0.0)
    store = SQLiteFreightStore(tmp_path / "tick.db", clock=clk)
    store.bootstrap_tenant("tick", "Tick Corp", "tick-admin")
    store.create_credential("tick-admin", "tick-op", "operator")

    store.set_sla_rule("tick-admin", "P2", delay_seconds=300.0)
    ship = store.create_shipment("tick-op", "TICK-001")
    store.ingest_event("tick-op", ship.id, "ev1", "delayed", 100.0)

    excs = store.list_exceptions("tick-op")
    opened_at = excs[0].opened_at

    # Tick before SLA deadline — no escalation
    count = store.tick(now=opened_at + 200.0, limit=100)
    assert count == 0

    # Tick after SLA deadline (opened_at + 300 ≤ now) — one escalation
    count = store.tick(now=opened_at + 301.0, limit=100)
    assert count == 1

    # Tick again — idempotent, no double-escalation
    count2 = store.tick(now=opened_at + 400.0, limit=100)
    assert count2 == 0


def test_tick_skips_acknowledged_exceptions(tmp_path):
    clk = _Clock(start=0.0)
    store = SQLiteFreightStore(tmp_path / "tick2.db", clock=clk)
    store.bootstrap_tenant("t", "T", "adm")
    store.create_credential("adm", "op", "operator")
    store.set_sla_rule("adm", "P2", delay_seconds=10.0)

    ship = store.create_shipment("op", "T-001")
    store.ingest_event("op", ship.id, "ev1", "delayed", 1.0)
    exc = store.list_exceptions("op")[0]
    store.mutate_exception("op", exc.id, 1, "acknowledge")

    count = store.tick(now=exc.opened_at + 100.0, limit=100)
    assert count == 0  # acknowledged → no escalation


# ---------------------------------------------------------------------------
# Outbox delivery
# ---------------------------------------------------------------------------

def test_outbox_create_claim_complete(store):
    ship = store.create_shipment("op-tok", "OUT-001")
    now = time.time()
    delivery = store.claim_delivery("worker-1", now)
    assert delivery is not None
    assert delivery.status == "claimed"
    assert delivery.owner == "worker-1"

    done = store.complete_delivery(delivery.id, "worker-1", now + 1)
    assert done.status == "delivered"


def test_outbox_fail_and_retry(store):
    store.create_shipment("op-tok", "OUT-002")
    now = time.time()
    d = store.claim_delivery("w1", now)
    assert d is not None

    failed = store.fail_delivery(d.id, "w1", "timeout", now)
    assert failed.status == "pending"
    assert failed.next_retry_at > now


def test_backoff_base_configurable(tmp_path):
    """backoff_base controls the retry interval; small values allow tests to
    exercise dead-lettering without advancing now by 60+ seconds."""
    clk = _Clock()
    s = SQLiteFreightStore(
        tmp_path / "backoff.db", clock=clk,
        max_attempts=2, backoff_base=2.0,
    )
    s.bootstrap_tenant("b", "B", "b-admin")
    s.create_credential("b-admin", "b-op", "operator")
    s.create_shipment("b-op", "B-001")

    now = clk._t
    d = s.claim_delivery("w1", now)
    assert d is not None
    failed = s.fail_delivery(d.id, "w1", "err1", now)
    assert failed.status == "pending"
    # First retry should be now + 2.0 (base * 2^0)
    assert abs(failed.next_retry_at - (now + 2.0)) < 0.001

    # Advance just past the retry window and claim again
    now2 = now + 3.0
    d2 = s.claim_delivery("w2", now2)
    assert d2 is not None
    failed2 = s.fail_delivery(d2.id, "w2", "err2", now2)
    # Second attempt: attempts=2 == max_attempts=2 → dead
    assert failed2.status == "dead"


def test_outbox_dead_letter_after_max_attempts(store):
    """After max_attempts failures the delivery is dead-lettered."""
    store.create_shipment("op-tok", "OUT-003")
    now = time.time()

    for _ in range(3):  # max_attempts=3 in fixture
        d = store.claim_delivery("w1", now)
        if d is None:
            # Advance time past retry schedule
            now += 3700
            d = store.claim_delivery("w1", now)
        assert d is not None
        store.fail_delivery(d.id, "w1", "error", now)
        now += 1

    deliveries = store.list_deliveries("op-tok")
    dead = [d for d in deliveries if d.status == "dead"]
    # At least one delivery should be dead-lettered
    assert len(dead) >= 1


def test_outbox_replay_dead_letter(store, tmp_path):
    clk = _Clock()
    s = SQLiteFreightStore(tmp_path / "replay.db", clock=clk, max_attempts=1)
    s.bootstrap_tenant("r", "R", "r-admin")
    s.create_credential("r-admin", "r-op", "operator")
    s.create_shipment("r-op", "R-001")

    now = clk._t
    d = s.claim_delivery("w1", now)
    assert d is not None
    s.fail_delivery(d.id, "w1", "err", now)  # attempts=1 = max → dead

    dead = [d for d in s.list_deliveries("r-op") if d.status == "dead"]
    assert len(dead) == 1

    s.replay_delivery("r-admin", dead[0].id, now + 1)
    replayed = [d for d in s.list_deliveries("r-op") if d.id == dead[0].id]
    assert replayed[0].status == "pending"


def test_outbox_concurrent_workers_no_double_claim(tmp_path):
    """Two claim calls in sequence return different deliveries (or None)."""
    clk = _Clock()
    s = SQLiteFreightStore(tmp_path / "conc.db", clock=clk, lease_seconds=60)
    s.bootstrap_tenant("c", "C", "c-admin")
    s.create_credential("c-admin", "c-op", "operator")
    s.create_shipment("c-op", "C-001")  # creates one delivery

    now = clk._t
    d1 = s.claim_delivery("w1", now)
    d2 = s.claim_delivery("w2", now)
    # w2 should get None because the only pending delivery is claimed by w1
    assert d1 is not None
    assert d2 is None


def test_expired_lease_reclaimable(tmp_path):
    clk = _Clock()
    s = SQLiteFreightStore(tmp_path / "lease.db", clock=clk, lease_seconds=5)
    s.bootstrap_tenant("l", "L", "l-admin")
    s.create_credential("l-admin", "l-op", "operator")
    s.create_shipment("l-op", "L-001")

    now = clk._t
    d1 = s.claim_delivery("w1", now)
    assert d1 is not None

    # Advance past lease expiry
    now += 10
    d2 = s.claim_delivery("w2", now)
    assert d2 is not None
    assert d2.id == d1.id
    assert d2.owner == "w2"


# ---------------------------------------------------------------------------
# Snapshot export / import
# ---------------------------------------------------------------------------

def test_snapshot_roundtrip(store):
    ship = store.create_shipment("op-tok", "SNAP-001")
    store.ingest_event("op-tok", ship.id, "ev1", "delayed", 100.0)

    snapshot = store.export_snapshot("admin-tok")
    assert snapshot["tenant_id"] == "acme"
    assert len(snapshot["shipments"]) == 1

    # Import into a fresh store on the same DB
    store.import_snapshot("admin-tok", snapshot)
    ships_after = store.list_shipments("op-tok")
    assert len(ships_after) == 1
    assert ships_after[0].reference == "SNAP-001"


def test_snapshot_wrong_tenant_rejected(tmp_path):
    clk = _Clock()
    s = SQLiteFreightStore(tmp_path / "snap2.db", clock=clk)
    s.bootstrap_tenant("t1", "T1", "t1-admin")
    s.bootstrap_tenant("t2", "T2", "t2-admin")

    snap = s.export_snapshot("t1-admin")
    with pytest.raises(ValidationError):
        s.import_snapshot("t2-admin", snap)


def test_snapshot_import_is_atomic(store, tmp_path):
    """If the snapshot is for the wrong tenant, the DB stays unchanged."""
    store.create_shipment("op-tok", "BEFORE-001")
    bad_snap = {"version": 1, "tenant_id": "wrong", "tenant": {}, "credentials": [],
                "shipments": [], "carrier_events": [], "exceptions": [],
                "exception_notes": [], "audit_entries": [], "sla_rules": [],
                "outbox_deliveries": []}
    with pytest.raises(ValidationError):
        store.import_snapshot("admin-tok", bad_snap)
    # DB unchanged
    ships = store.list_shipments("op-tok")
    assert any(s.reference == "BEFORE-001" for s in ships)


# ---------------------------------------------------------------------------
# Two-instance durability
# ---------------------------------------------------------------------------

def test_two_instances_same_db(tmp_path):
    """Commits by instance A are visible to instance B."""
    clk1, clk2 = _Clock(), _Clock(start=500.0)
    s1 = SQLiteFreightStore(tmp_path / "shared.db", clock=clk1)
    s1.bootstrap_tenant("shared", "Shared", "sh-admin")
    s1.create_credential("sh-admin", "sh-op", "operator")

    s2 = SQLiteFreightStore(tmp_path / "shared.db", clock=clk2)

    ship = s1.create_shipment("sh-op", "SH-001")
    ships_from_s2 = s2.list_shipments("sh-op")
    assert len(ships_from_s2) == 1
    assert ships_from_s2[0].id == ship.id


# ---------------------------------------------------------------------------
# Init schema idempotency
# ---------------------------------------------------------------------------

def test_init_schema_idempotent(tmp_path):
    clk = _Clock()
    s = SQLiteFreightStore(tmp_path / "idem.db", clock=clk)
    s.bootstrap_tenant("x", "X", "x-admin")
    # Calling init_schema again must not raise or destroy data
    s.init_schema()
    s.init_schema()
    # Data intact
    s.create_credential("x-admin", "x-op", "operator")
    ship = s.create_shipment("x-op", "X-001")
    assert ship.reference == "X-001"
