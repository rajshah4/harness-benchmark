"""Behavioral tests for SQLiteFreightStore."""
from __future__ import annotations

import pytest
import threading
import tempfile
import os

from freight_tower.sqlite_store import SQLiteFreightStore
from freight_tower.exceptions import (
    AuthenticationError,
    AuthorizationError,
    IdempotencyConflictError,
    NotFoundError,
    StaleVersionError,
    ValidationError,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def store(tmp_path):
    s = SQLiteFreightStore(str(tmp_path / "test.db"), clock=lambda: 1000.0)
    s.init_schema()
    yield s
    s.close()


@pytest.fixture
def bootstrapped(store):
    """Returns (store, admin_token, op_token, viewer_token)."""
    store.bootstrap_tenant("acme", "ACME Corp", "admin-tok")
    store.create_credential("admin-tok", "op-tok", "operator")
    store.create_credential("admin-tok", "view-tok", "viewer")
    return store, "admin-tok", "op-tok", "view-tok"


# ---------------------------------------------------------------------------
# Schema and tenant bootstrap
# ---------------------------------------------------------------------------

def test_init_schema_is_idempotent(tmp_path):
    s = SQLiteFreightStore(str(tmp_path / "db.sqlite"))
    s.init_schema()
    s.init_schema()  # second call must not fail
    s.close()


def test_bootstrap_tenant(store):
    t = store.bootstrap_tenant("t1", "Tenant One", "tok-admin")
    assert t.id == "t1"
    assert t.name == "Tenant One"


def test_bootstrap_idempotent(store):
    t1 = store.bootstrap_tenant("t1", "Tenant One", "tok-admin")
    t2 = store.bootstrap_tenant("t1", "Tenant One", "tok-admin")
    assert t1.id == t2.id


def test_bootstrap_conflict_token(store):
    store.bootstrap_tenant("t1", "Tenant One", "tok-admin")
    with pytest.raises(ValidationError):
        store.bootstrap_tenant("t1", "Tenant One", "different-token")


def test_two_tenants_isolated(tmp_path):
    """Two instances on the same DB see each other's tenants."""
    path = str(tmp_path / "shared.db")
    s1 = SQLiteFreightStore(path)
    s1.init_schema()
    s2 = SQLiteFreightStore(path)
    s2.init_schema()

    s1.bootstrap_tenant("t1", "Tenant 1", "tok1")
    s2.bootstrap_tenant("t2", "Tenant 2", "tok2")

    # Each can use its own token
    ship = s1.create_shipment("tok1", "T1-001")
    assert ship.tenant_id == "t1"

    ship2 = s2.create_shipment("tok2", "T2-001")
    assert ship2.tenant_id == "t2"

    # Cannot see each other's shipments
    assert s1.list_shipments("tok1") == [ship]
    assert s2.list_shipments("tok2") == [ship2]

    s1.close()
    s2.close()


# ---------------------------------------------------------------------------
# Credentials and roles
# ---------------------------------------------------------------------------

def test_viewer_cannot_create_shipment(bootstrapped):
    store, admin, op, viewer = bootstrapped
    with pytest.raises(AuthorizationError):
        store.create_shipment(viewer, "REF-001")


def test_viewer_can_list_shipments(bootstrapped):
    store, admin, op, viewer = bootstrapped
    store.create_shipment(op, "REF-001")
    ships = store.list_shipments(viewer)
    assert len(ships) == 1


def test_invalid_token_raises(bootstrapped):
    store, admin, op, viewer = bootstrapped
    with pytest.raises(AuthenticationError):
        store.list_shipments("bad-token")


def test_create_credential_invalid_role(bootstrapped):
    store, admin, op, viewer = bootstrapped
    with pytest.raises(ValidationError):
        store.create_credential(admin, "new-tok", "superuser")


def test_operator_cannot_create_credential(bootstrapped):
    store, admin, op, viewer = bootstrapped
    with pytest.raises(AuthorizationError):
        store.create_credential(op, "new-tok", "viewer")


# ---------------------------------------------------------------------------
# Shipments
# ---------------------------------------------------------------------------

def test_create_and_get_shipment(bootstrapped):
    store, admin, op, viewer = bootstrapped
    ship = store.create_shipment(op, "SHIP-001")
    assert ship.status == "created"
    assert ship.version == 1
    fetched = store.get_shipment(viewer, ship.id)
    assert fetched.id == ship.id


def test_get_shipment_not_found(bootstrapped):
    store, admin, op, viewer = bootstrapped
    with pytest.raises(NotFoundError):
        store.get_shipment(viewer, "nonexistent-id")


def test_shipment_reference_idempotent(bootstrapped):
    store, admin, op, viewer = bootstrapped
    s1 = store.create_shipment(op, "DUP-001")
    s2 = store.create_shipment(op, "DUP-001")
    assert s1.id == s2.id


def test_list_shipments_filter_status(bootstrapped):
    store, admin, op, viewer = bootstrapped
    s1 = store.create_shipment(op, "A001")
    store.ingest_event(op, s1.id, "ev1", "delivered", 1001.0)
    s2 = store.create_shipment(op, "A002")
    result = store.list_shipments(viewer, status="delivered")
    assert len(result) == 1
    assert result[0].id == s1.id


# ---------------------------------------------------------------------------
# Carrier event ingestion and projection
# ---------------------------------------------------------------------------

def test_ingest_event_updates_status(bootstrapped):
    store, admin, op, viewer = bootstrapped
    ship = store.create_shipment(op, "S001")
    updated = store.ingest_event(op, ship.id, "ev-001", "in_transit", 1001.0, location="Chicago")
    assert updated.status == "in_transit"
    assert updated.last_location == "Chicago"
    assert updated.version == 2


def test_ingest_out_of_order_events(bootstrapped):
    """Late historical event does not override a later delivery."""
    store, admin, op, viewer = bootstrapped
    ship = store.create_shipment(op, "S-OOO")
    store.ingest_event(op, ship.id, "ev-2", "delivered", 2000.0)
    result = store.ingest_event(op, ship.id, "ev-1", "in_transit", 1000.0)  # older event
    assert result.status == "delivered"  # delivered must remain


def test_ingest_idempotent_same_payload(bootstrapped):
    store, admin, op, viewer = bootstrapped
    ship = store.create_shipment(op, "S002")
    r1 = store.ingest_event(op, ship.id, "ev-A", "in_transit", 1001.0, location="LA")
    r2 = store.ingest_event(op, ship.id, "ev-A", "in_transit", 1001.0, location="LA")
    assert r1.id == r2.id
    assert r1.version == r2.version


def test_ingest_conflict_raises(bootstrapped):
    store, admin, op, viewer = bootstrapped
    ship = store.create_shipment(op, "S003")
    store.ingest_event(op, ship.id, "ev-B", "in_transit", 1001.0)
    with pytest.raises(IdempotencyConflictError):
        store.ingest_event(op, ship.id, "ev-B", "delayed", 1001.0)


def test_delayed_opens_exception(bootstrapped):
    store, admin, op, viewer = bootstrapped
    ship = store.create_shipment(op, "S004")
    store.ingest_event(op, ship.id, "ev-D", "delayed", 1001.0)
    exceptions = store.list_exceptions(viewer)
    assert len(exceptions) == 1
    assert exceptions[0].status == "open"
    assert exceptions[0].shipment_id == ship.id


def test_delivered_resolves_exception(bootstrapped):
    store, admin, op, viewer = bootstrapped
    ship = store.create_shipment(op, "S005")
    store.ingest_event(op, ship.id, "ev-D", "delayed", 1001.0)
    store.ingest_event(op, ship.id, "ev-Dlv", "delivered", 1002.0)
    exceptions = store.list_exceptions(viewer, status="open")
    assert len(exceptions) == 0
    resolved = store.list_exceptions(viewer, status="resolved")
    assert len(resolved) == 1


def test_cancelled_resolves_exception(bootstrapped):
    store, admin, op, viewer = bootstrapped
    ship = store.create_shipment(op, "S006")
    store.ingest_event(op, ship.id, "ev-D2", "delayed", 1001.0)
    store.ingest_event(op, ship.id, "ev-C", "cancelled", 1002.0)
    excs = store.list_exceptions(viewer, status="open")
    assert len(excs) == 0


def test_late_delay_does_not_reopen_after_delivery(bootstrapped):
    """A delayed event arriving after delivery must not reopen exception."""
    store, admin, op, viewer = bootstrapped
    ship = store.create_shipment(op, "LATE-001")
    store.ingest_event(op, ship.id, "ev-dlv", "delivered", 2000.0)
    store.ingest_event(op, ship.id, "ev-dly", "delayed", 1000.0)  # late, earlier timestamp
    # Ship is still delivered
    result = store.get_shipment(viewer, ship.id)
    assert result.status == "delivered"
    # No open exceptions
    excs = store.list_exceptions(viewer, status="open")
    assert len(excs) == 0


# ---------------------------------------------------------------------------
# Exception mutations with optimistic concurrency
# ---------------------------------------------------------------------------

def test_mutate_exception_assign(bootstrapped):
    store, admin, op, viewer = bootstrapped
    ship = store.create_shipment(op, "EX-001")
    store.ingest_event(op, ship.id, "ev-D3", "delayed", 1001.0)
    excs = store.list_exceptions(viewer)
    exc = excs[0]
    updated = store.mutate_exception(op, exc.id, exc.version, "assign", assignee="alice")
    assert updated.assigned_to == "alice"
    assert updated.version == exc.version + 1


def test_mutate_exception_acknowledge(bootstrapped):
    store, admin, op, viewer = bootstrapped
    ship = store.create_shipment(op, "EX-002")
    store.ingest_event(op, ship.id, "ev-D4", "delayed", 1001.0)
    exc = store.list_exceptions(viewer)[0]
    updated = store.mutate_exception(op, exc.id, exc.version, "acknowledge")
    assert updated.status == "acknowledged"


def test_mutate_exception_note(bootstrapped):
    store, admin, op, viewer = bootstrapped
    ship = store.create_shipment(op, "EX-003")
    store.ingest_event(op, ship.id, "ev-D5", "delayed", 1001.0)
    exc = store.list_exceptions(viewer)[0]
    updated = store.mutate_exception(op, exc.id, exc.version, "note", note="Checking with carrier")
    assert "Checking with carrier" in updated.notes


def test_mutate_exception_resolve(bootstrapped):
    store, admin, op, viewer = bootstrapped
    ship = store.create_shipment(op, "EX-004")
    store.ingest_event(op, ship.id, "ev-D6", "delayed", 1001.0)
    exc = store.list_exceptions(viewer)[0]
    updated = store.mutate_exception(op, exc.id, exc.version, "resolve")
    assert updated.status == "resolved"


def test_mutate_exception_stale_version(bootstrapped):
    store, admin, op, viewer = bootstrapped
    ship = store.create_shipment(op, "EX-005")
    store.ingest_event(op, ship.id, "ev-D7", "delayed", 1001.0)
    exc = store.list_exceptions(viewer)[0]
    store.mutate_exception(op, exc.id, exc.version, "acknowledge")
    with pytest.raises(StaleVersionError):
        store.mutate_exception(op, exc.id, exc.version, "resolve")  # version is now stale


def test_viewer_cannot_mutate_exception(bootstrapped):
    store, admin, op, viewer = bootstrapped
    ship = store.create_shipment(op, "EX-006")
    store.ingest_event(op, ship.id, "ev-D8", "delayed", 1001.0)
    exc = store.list_exceptions(viewer)[0]
    with pytest.raises(AuthorizationError):
        store.mutate_exception(viewer, exc.id, exc.version, "acknowledge")


# ---------------------------------------------------------------------------
# Audit trail
# ---------------------------------------------------------------------------

def test_audit_records_actions(bootstrapped):
    store, admin, op, viewer = bootstrapped
    ship = store.create_shipment(op, "AUD-001")
    store.ingest_event(op, ship.id, "ev-A1", "in_transit", 1001.0)
    entries = store.audit(viewer)
    actions = [e.action for e in entries]
    assert "create_shipment" in actions
    assert "ingest_event" in actions


def test_audit_filter_by_resource(bootstrapped):
    store, admin, op, viewer = bootstrapped
    ship = store.create_shipment(op, "AUD-002")
    entries = store.audit(viewer, resource_type="shipment", resource_id=ship.id)
    assert all(e.resource_id == ship.id for e in entries)


# ---------------------------------------------------------------------------
# SLA rules and tick
# ---------------------------------------------------------------------------

def test_set_sla_rule(bootstrapped):
    store, admin, op, viewer = bootstrapped
    store.set_sla_rule(admin, "P1", 300.0)


def test_tick_escalates_due_exception(tmp_path):
    """tick() enqueues escalation for unacknowledged P1 delays past threshold."""
    clock_val = [1000.0]

    def clock():
        return clock_val[0]

    s = SQLiteFreightStore(str(tmp_path / "tick.db"), clock=clock)
    s.init_schema()
    s.bootstrap_tenant("tick-t", "Tick Tenant", "tick-admin")
    s.create_credential("tick-admin", "tick-op", "operator")
    s.set_sla_rule("tick-admin", "P2", 300.0)

    ship = s.create_shipment("tick-op", "TICK-001")

    # Set exception opened_at
    clock_val[0] = 1000.0
    s.ingest_event("tick-op", ship.id, "ev-t1", "delayed", 1000.0)

    # Tick at 1200 (only 200s elapsed) – should not escalate
    n = s.tick(1200.0)
    assert n == 0

    # Tick at 1400 (400s elapsed) – should escalate
    n = s.tick(1400.0)
    assert n == 1

    # Tick again – idempotent
    n = s.tick(1400.0)
    assert n == 0

    deliveries = s.list_deliveries("tick-admin", status="pending")
    escalations = [d for d in deliveries if d.payload.get("event") == "exception.escalated"]
    assert len(escalations) == 1

    s.close()


def test_tick_does_not_escalate_acknowledged(tmp_path):
    clock_val = [1000.0]

    def clock():
        return clock_val[0]

    s = SQLiteFreightStore(str(tmp_path / "tick2.db"), clock=clock)
    s.init_schema()
    s.bootstrap_tenant("t2", "T2", "admin2")
    s.create_credential("admin2", "op2", "operator")
    s.set_sla_rule("admin2", "P2", 300.0)

    ship = s.create_shipment("op2", "TICK-002")
    s.ingest_event("op2", ship.id, "ev-t2", "delayed", 1000.0)

    exc = s.list_exceptions("op2")[0]
    s.mutate_exception("op2", exc.id, exc.version, "acknowledge")

    n = s.tick(2000.0)
    assert n == 0  # acknowledged – no escalation
    s.close()


# ---------------------------------------------------------------------------
# Outbox / deliveries
# ---------------------------------------------------------------------------

def test_claim_and_complete_delivery(bootstrapped):
    store, admin, op, viewer = bootstrapped
    ship = store.create_shipment(op, "D-001")
    delivery = store.claim_delivery("worker-1", 1001.0)
    assert delivery is not None
    assert delivery.owner == "worker-1"
    result = store.complete_delivery(delivery.id, "worker-1", 1002.0)
    assert result.status == "delivered"


def test_claim_delivery_wrong_worker(bootstrapped):
    store, admin, op, viewer = bootstrapped
    store.create_shipment(op, "D-002")
    delivery = store.claim_delivery("worker-1", 1001.0)
    with pytest.raises(AuthorizationError):
        store.complete_delivery(delivery.id, "worker-2", 1002.0)


def test_fail_delivery_retries(tmp_path):
    clock_val = [1000.0]

    def clock():
        return clock_val[0]

    s = SQLiteFreightStore(str(tmp_path / "retry.db"), clock=clock, max_attempts=3)
    s.init_schema()
    s.bootstrap_tenant("rt", "Retry Tenant", "rt-admin")
    s.create_credential("rt-admin", "rt-op", "operator")
    s.create_shipment("rt-op", "R-001")

    d = s.claim_delivery("w1", 1001.0)
    assert d is not None
    r = s.fail_delivery(d.id, "w1", "connection refused", 1002.0)
    assert r.status == "pending"
    assert r.attempts == 1

    # Fail again
    d2 = s.claim_delivery("w1", r.next_attempt_at + 1)
    r2 = s.fail_delivery(d2.id, "w1", "timeout", r.next_attempt_at + 2)
    assert r2.attempts == 2

    # Fail final time -> dead letter
    d3 = s.claim_delivery("w1", r2.next_attempt_at + 1)
    r3 = s.fail_delivery(d3.id, "w1", "fatal", r2.next_attempt_at + 2)
    assert r3.status == "dead"

    s.close()


def test_replay_dead_delivery(tmp_path):
    clock_val = [1000.0]

    def clock():
        return clock_val[0]

    s = SQLiteFreightStore(str(tmp_path / "replay.db"), clock=clock, max_attempts=1)
    s.init_schema()
    s.bootstrap_tenant("rp", "Replay", "rp-admin")
    s.create_credential("rp-admin", "rp-op", "operator")
    s.create_shipment("rp-op", "RP-001")

    d = s.claim_delivery("w1", 1001.0)
    dead = s.fail_delivery(d.id, "w1", "fatal", 1002.0)
    assert dead.status == "dead"

    replayed = s.replay_delivery("rp-admin", dead.id, 1003.0)
    assert replayed.status == "pending"
    assert replayed.attempts == 0

    s.close()


def test_expired_lease_recoverable(tmp_path):
    clock_val = [1000.0]

    def clock():
        return clock_val[0]

    s = SQLiteFreightStore(str(tmp_path / "lease.db"), clock=clock, lease_seconds=30.0)
    s.init_schema()
    s.bootstrap_tenant("ls", "Lease", "ls-admin")
    s.create_credential("ls-admin", "ls-op", "operator")
    s.create_shipment("ls-op", "LS-001")

    d = s.claim_delivery("w1", 1001.0)
    assert d is not None

    # Lease expires at 1031; try to claim at 1032
    d2 = s.claim_delivery("w2", 1032.0)
    assert d2 is not None
    assert d2.owner == "w2"

    s.close()


def test_concurrent_claim_once(tmp_path):
    """Two threads racing to claim should get distinct deliveries or one gets None."""
    clock_val = [1000.0]

    def clock():
        return clock_val[0]

    s = SQLiteFreightStore(str(tmp_path / "conc.db"), clock=clock)
    s.init_schema()
    s.bootstrap_tenant("cc", "Concurrent", "cc-admin")
    s.create_credential("cc-admin", "cc-op", "operator")
    # Create one shipment -> one delivery
    s.create_shipment("cc-op", "CC-001")

    results = []
    barrier = threading.Barrier(2)

    def claim():
        barrier.wait()
        d = s.claim_delivery(f"w-{threading.get_ident()}", 1001.0)
        results.append(d)

    t1 = threading.Thread(target=claim)
    t2 = threading.Thread(target=claim)
    t1.start(); t2.start()
    t1.join(); t2.join()

    non_none = [r for r in results if r is not None]
    ids = {r.id for r in non_none}
    # At most one unique delivery claimed
    assert len(ids) <= 1

    s.close()


# ---------------------------------------------------------------------------
# Snapshot export / import
# ---------------------------------------------------------------------------

def test_export_import_snapshot(tmp_path):
    path1 = str(tmp_path / "orig.db")
    path2 = str(tmp_path / "restored.db")

    s1 = SQLiteFreightStore(path1)
    s1.init_schema()
    s1.bootstrap_tenant("snap-t", "Snap Tenant", "snap-admin")
    s1.create_credential("snap-admin", "snap-op", "operator")
    s1.create_shipment("snap-op", "SNAP-001")
    s1.create_shipment("snap-op", "SNAP-002")

    snapshot = s1.export_snapshot("snap-admin")
    assert snapshot["version"] == 1
    assert snapshot["tenant"]["id"] == "snap-t"
    assert len(snapshot["shipments"]) == 2

    s2 = SQLiteFreightStore(path2)
    s2.init_schema()
    s2.bootstrap_tenant("snap-t", "Snap Tenant", "snap-admin")
    s2.import_snapshot("snap-admin", snapshot)

    ships = s2.list_shipments("snap-admin")
    assert len(ships) == 2

    s1.close()
    s2.close()


def test_import_wrong_tenant_raises(tmp_path):
    s1 = SQLiteFreightStore(str(tmp_path / "s1.db"))
    s1.init_schema()
    s1.bootstrap_tenant("t-one", "T One", "admin-one")

    s2 = SQLiteFreightStore(str(tmp_path / "s2.db"))
    s2.init_schema()
    s2.bootstrap_tenant("t-two", "T Two", "admin-two")

    snap = s1.export_snapshot("admin-one")
    with pytest.raises(AuthorizationError):
        s2.import_snapshot("admin-two", snap)  # tenant id mismatch

    s1.close()
    s2.close()


# ---------------------------------------------------------------------------
# Tenant isolation
# ---------------------------------------------------------------------------

def test_tenant_isolation_shipment_access(tmp_path):
    path = str(tmp_path / "iso.db")
    s = SQLiteFreightStore(path)
    s.init_schema()
    s.bootstrap_tenant("iso-1", "ISO One", "iso-admin-1")
    s.bootstrap_tenant("iso-2", "ISO Two", "iso-admin-2")

    ship = s.create_shipment("iso-admin-1", "ISO-001")

    with pytest.raises(NotFoundError):
        s.get_shipment("iso-admin-2", ship.id)

    s.close()


def test_tenant_isolation_exception_access(tmp_path):
    path = str(tmp_path / "iso2.db")
    s = SQLiteFreightStore(path)
    s.init_schema()
    s.bootstrap_tenant("t-a", "T A", "admin-a")
    s.bootstrap_tenant("t-b", "T B", "admin-b")
    s.create_credential("admin-a", "op-a", "operator")
    s.create_credential("admin-b", "op-b", "operator")

    ship = s.create_shipment("op-a", "A-001")
    s.ingest_event("op-a", ship.id, "ev-xx", "delayed", 1001.0)

    exc_a = s.list_exceptions("admin-a")
    exc_b = s.list_exceptions("admin-b")
    assert len(exc_a) == 1
    assert len(exc_b) == 0

    s.close()
