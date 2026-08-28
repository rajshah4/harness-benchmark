"""
Behavioral tests for SQLiteFreightStore covering all contract requirements.
"""
from __future__ import annotations

import threading
import time

import pytest

from freight_tower.sqlite_store import SQLiteFreightStore
from freight_tower.exceptions import (
    AuthError,
    AuthzError,
    ConflictError,
    NotFoundError,
    ValidationError,
    VersionError,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_store(**kwargs) -> SQLiteFreightStore:
    """Return an in-memory store with a deterministic clock."""
    ticks = iter(range(1, 100_000))
    return SQLiteFreightStore(":memory:", clock=lambda: float(next(ticks)), **kwargs)


def bootstrap(store: SQLiteFreightStore, tenant_id="acme", name="Acme Corp", admin_token="admin-token"):
    store.bootstrap_tenant(tenant_id, name, admin_token)
    return admin_token


def setup_tenant(store, tenant_id="acme"):
    admin = f"{tenant_id}-admin"
    op = f"{tenant_id}-op"
    viewer = f"{tenant_id}-viewer"
    bootstrap(store, tenant_id, f"{tenant_id} Corp", admin)
    store.create_credential(admin, op, "operator")
    store.create_credential(admin, viewer, "viewer")
    return admin, op, viewer


# ---------------------------------------------------------------------------
# Tenant / credential
# ---------------------------------------------------------------------------


class TestTenantBootstrap:
    def test_bootstrap_creates_tenant_and_admin(self):
        store = make_store()
        store.bootstrap_tenant("acme", "Acme", "tok-admin")
        # Can authenticate as admin
        cred = store._authenticate("tok-admin")
        assert cred["role"] == "admin"
        assert cred["tenant_id"] == "acme"

    def test_bootstrap_idempotent(self):
        store = make_store()
        store.bootstrap_tenant("acme", "Acme", "tok")
        store.bootstrap_tenant("acme", "Acme", "tok")  # second call OK

    def test_bootstrap_conflict_different_token(self):
        store = make_store()
        store.bootstrap_tenant("acme", "Acme", "tok-a")
        with pytest.raises(ConflictError):
            store.bootstrap_tenant("acme", "Acme", "tok-b")

    def test_bootstrap_missing_fields(self):
        store = make_store()
        with pytest.raises(ValidationError):
            store.bootstrap_tenant("", "Acme", "tok")
        with pytest.raises(ValidationError):
            store.bootstrap_tenant("acme", "", "tok")
        with pytest.raises(ValidationError):
            store.bootstrap_tenant("acme", "Acme", "")


class TestCredentials:
    def test_create_viewer_and_operator(self):
        store = make_store()
        admin, op, viewer = setup_tenant(store)
        # viewer cannot mutate
        cred_v = store._authenticate(viewer)
        assert cred_v["role"] == "viewer"
        cred_o = store._authenticate(op)
        assert cred_o["role"] == "operator"

    def test_invalid_role_rejected(self):
        store = make_store()
        admin = bootstrap(store)
        with pytest.raises(ValidationError):
            store.create_credential(admin, "tok", "superuser")

    def test_non_admin_cannot_create_credential(self):
        store = make_store()
        admin, op, viewer = setup_tenant(store)
        with pytest.raises(AuthzError):
            store.create_credential(op, "new-tok", "viewer")
        with pytest.raises(AuthzError):
            store.create_credential(viewer, "new-tok", "viewer")

    def test_bad_token_auth_error(self):
        store = make_store()
        with pytest.raises(AuthError):
            store._authenticate("no-such-token")

    def test_duplicate_token_conflict(self):
        store = make_store()
        admin = bootstrap(store)
        store.create_credential(admin, "viewer-tok", "viewer")
        with pytest.raises(ConflictError):
            store.create_credential(admin, "viewer-tok", "operator")

    def test_duplicate_token_idempotent_same_role(self):
        store = make_store()
        admin = bootstrap(store)
        store.create_credential(admin, "viewer-tok", "viewer")
        store.create_credential(admin, "viewer-tok", "viewer")  # OK


# ---------------------------------------------------------------------------
# Shipments
# ---------------------------------------------------------------------------


class TestShipments:
    def test_create_and_get_shipment(self):
        store = make_store()
        admin, op, viewer = setup_tenant(store)
        s = store.create_shipment(op, "SHIP-1")
        assert s.reference == "SHIP-1"
        assert s.status == "created"
        assert s.version == 1
        got = store.get_shipment(viewer, s.id)
        assert got.id == s.id

    def test_viewer_cannot_create_shipment(self):
        store = make_store()
        admin, op, viewer = setup_tenant(store)
        with pytest.raises(AuthzError):
            store.create_shipment(viewer, "SHIP-1")

    def test_create_shipment_idempotent_on_reference(self):
        store = make_store()
        admin, op, viewer = setup_tenant(store)
        s1 = store.create_shipment(op, "SHIP-1")
        s2 = store.create_shipment(op, "SHIP-1")
        assert s1.id == s2.id

    def test_tenant_isolation(self):
        store = make_store()
        admin1, op1, viewer1 = setup_tenant(store, "t1")
        admin2, op2, viewer2 = setup_tenant(store, "t2")
        s1 = store.create_shipment(op1, "REF-1")
        store.create_shipment(op2, "REF-2")
        # tenant1 sees only its own
        ships = store.list_shipments(op1)
        assert len(ships) == 1
        assert ships[0].id == s1.id
        # tenant1 cannot access tenant2's shipment by guessing id
        t2_ships = store.list_shipments(op2)
        with pytest.raises(NotFoundError):
            store.get_shipment(op1, t2_ships[0].id)

    def test_list_shipments_filter_by_status(self):
        store = make_store()
        admin, op, viewer = setup_tenant(store)
        s = store.create_shipment(op, "SHIP-1")
        store.ingest_event(op, s.id, "ev-1", "delayed", 100.0)
        ships = store.list_shipments(op, status="delayed")
        assert len(ships) == 1
        ships_created = store.list_shipments(op, status="created")
        assert len(ships_created) == 0

    def test_invalid_token_list(self):
        store = make_store()
        with pytest.raises(AuthError):
            store.list_shipments("bad-token")


# ---------------------------------------------------------------------------
# Event ingestion and projection
# ---------------------------------------------------------------------------


class TestEventIngestion:
    def test_basic_status_progression(self):
        store = make_store()
        admin, op, viewer = setup_tenant(store)
        s = store.create_shipment(op, "SHIP-1")
        s = store.ingest_event(op, s.id, "ev-1", "picked_up", 100.0, location="NYC")
        assert s.status == "in_transit"
        assert s.last_location == "NYC"
        s = store.ingest_event(op, s.id, "ev-2", "in_transit", 200.0, location="CHI")
        assert s.status == "in_transit"
        assert s.last_location == "CHI"
        s = store.ingest_event(op, s.id, "ev-3", "delayed", 300.0)
        assert s.status == "delayed"
        s = store.ingest_event(op, s.id, "ev-4", "delivered", 400.0)
        assert s.status == "delivered"

    def test_out_of_order_events_deterministic(self):
        """Late event must not override newer delivered status."""
        store = make_store()
        admin, op, _ = setup_tenant(store)
        s = store.create_shipment(op, "SHIP-2")
        # Deliver first
        store.ingest_event(op, s.id, "ev-del", "delivered", 200.0)
        # Now send a delayed event with earlier timestamp
        s = store.ingest_event(op, s.id, "ev-late", "delayed", 100.0)
        assert s.status == "delivered"  # delivery must not be rolled back

    def test_out_of_order_location_update(self):
        """Location from any event in the ordered projection should be applied."""
        store = make_store()
        admin, op, _ = setup_tenant(store)
        s = store.create_shipment(op, "SHIP-3")
        # Ingest in reverse order
        store.ingest_event(op, s.id, "ev-b", "in_transit", 200.0, location="LA")
        s = store.ingest_event(op, s.id, "ev-a", "picked_up", 100.0, location="NYC")
        # After replay: ev-a (100), ev-b (200) → status=in_transit, loc=LA
        assert s.status == "in_transit"
        assert s.last_location == "LA"

    def test_idempotent_event(self):
        """Same event_id + same payload returns current projection without duplicate."""
        store = make_store()
        admin, op, _ = setup_tenant(store)
        s = store.create_shipment(op, "SHIP-4")
        s1 = store.ingest_event(op, s.id, "ev-x", "in_transit", 100.0)
        s2 = store.ingest_event(op, s.id, "ev-x", "in_transit", 100.0)
        assert s1.id == s2.id
        assert s1.version == s2.version
        # Only one event stored
        events = store._conn().execute(
            "SELECT COUNT(*) FROM carrier_events WHERE event_id='ev-x'"
        ).fetchone()[0]
        assert events == 1

    def test_conflict_on_different_payload(self):
        store = make_store()
        admin, op, _ = setup_tenant(store)
        s = store.create_shipment(op, "SHIP-5")
        store.ingest_event(op, s.id, "ev-c", "in_transit", 100.0)
        with pytest.raises(ConflictError):
            store.ingest_event(op, s.id, "ev-c", "delayed", 100.0)  # same id, different type

    def test_invalid_event_type(self):
        store = make_store()
        admin, op, _ = setup_tenant(store)
        s = store.create_shipment(op, "SHIP-6")
        with pytest.raises(ValidationError):
            store.ingest_event(op, s.id, "ev-bad", "unknown_type", 100.0)

    def test_viewer_cannot_ingest(self):
        store = make_store()
        admin, op, viewer = setup_tenant(store)
        s = store.create_shipment(op, "SHIP-7")
        with pytest.raises(AuthzError):
            store.ingest_event(viewer, s.id, "ev-1", "in_transit", 100.0)

    def test_version_increments_per_event(self):
        store = make_store()
        admin, op, _ = setup_tenant(store)
        s = store.create_shipment(op, "SHIP-8")
        assert s.version == 1
        s = store.ingest_event(op, s.id, "ev-1", "in_transit", 100.0)
        assert s.version == 2
        s = store.ingest_event(op, s.id, "ev-2", "delayed", 200.0)
        assert s.version == 3

    def test_cancelled_not_rolled_back_by_late_delayed(self):
        store = make_store()
        admin, op, _ = setup_tenant(store)
        s = store.create_shipment(op, "SHIP-9")
        store.ingest_event(op, s.id, "ev-cancel", "cancelled", 300.0)
        s = store.ingest_event(op, s.id, "ev-delayed", "delayed", 50.0)
        assert s.status == "cancelled"


# ---------------------------------------------------------------------------
# Exception workflow
# ---------------------------------------------------------------------------


class TestExceptionWorkflow:
    def test_delayed_opens_exception(self):
        store = make_store()
        admin, op, viewer = setup_tenant(store)
        s = store.create_shipment(op, "SHIP-EX1")
        store.ingest_event(op, s.id, "ev-1", "delayed", 100.0)
        excs = store.list_exceptions(op)
        assert len(excs) == 1
        exc = excs[0]
        assert exc.status == "open"
        assert exc.shipment_id == s.id

    def test_delivery_resolves_exception(self):
        store = make_store()
        admin, op, viewer = setup_tenant(store)
        s = store.create_shipment(op, "SHIP-EX2")
        store.ingest_event(op, s.id, "ev-1", "delayed", 100.0)
        store.ingest_event(op, s.id, "ev-2", "delivered", 200.0)
        excs = store.list_exceptions(op, status="open")
        assert len(excs) == 0
        resolved = store.list_exceptions(op, status="resolved")
        assert len(resolved) == 1

    def test_reopen_exception_after_resolution(self):
        """A new delay after delivery resolves may open a fresh exception."""
        store = make_store()
        admin, op, _ = setup_tenant(store)
        s = store.create_shipment(op, "SHIP-EX3")
        store.ingest_event(op, s.id, "ev-1", "delayed", 100.0)
        store.ingest_event(op, s.id, "ev-2", "delivered", 200.0)
        # Late arriving delayed that is historical (before delivered) – must NOT reopen
        s2 = store.ingest_event(op, s.id, "ev-3-late", "delayed", 50.0)
        assert s2.status == "delivered"
        open_excs = store.list_exceptions(op, status="open")
        assert len(open_excs) == 0

    def test_mutate_assign(self):
        store = make_store()
        admin, op, viewer = setup_tenant(store)
        s = store.create_shipment(op, "SHIP-EX4")
        store.ingest_event(op, s.id, "ev-1", "delayed", 100.0)
        exc = store.list_exceptions(op)[0]
        updated = store.mutate_exception(op, exc.id, exc.version, "assign", assignee="ops-user-1")
        assert updated.assignee == "ops-user-1"
        assert updated.version == exc.version + 1

    def test_mutate_acknowledge(self):
        store = make_store()
        admin, op, _ = setup_tenant(store)
        s = store.create_shipment(op, "SHIP-EX5")
        store.ingest_event(op, s.id, "ev-1", "delayed", 100.0)
        exc = store.list_exceptions(op)[0]
        updated = store.mutate_exception(op, exc.id, exc.version, "acknowledge")
        assert updated.status == "acknowledged"

    def test_mutate_note(self):
        store = make_store()
        admin, op, _ = setup_tenant(store)
        s = store.create_shipment(op, "SHIP-EX6")
        store.ingest_event(op, s.id, "ev-1", "delayed", 100.0)
        exc = store.list_exceptions(op)[0]
        updated = store.mutate_exception(op, exc.id, exc.version, "note", note="Investigating")
        assert "Investigating" in updated.notes

    def test_mutate_resolve(self):
        store = make_store()
        admin, op, _ = setup_tenant(store)
        s = store.create_shipment(op, "SHIP-EX7")
        store.ingest_event(op, s.id, "ev-1", "delayed", 100.0)
        exc = store.list_exceptions(op)[0]
        updated = store.mutate_exception(op, exc.id, exc.version, "resolve")
        assert updated.status == "resolved"

    def test_stale_version_raises_version_error(self):
        store = make_store()
        admin, op, _ = setup_tenant(store)
        s = store.create_shipment(op, "SHIP-EX8")
        store.ingest_event(op, s.id, "ev-1", "delayed", 100.0)
        exc = store.list_exceptions(op)[0]
        # First mutation
        store.mutate_exception(op, exc.id, exc.version, "acknowledge")
        # Stale version
        with pytest.raises(VersionError):
            store.mutate_exception(op, exc.id, exc.version, "resolve")

    def test_viewer_cannot_mutate_exception(self):
        store = make_store()
        admin, op, viewer = setup_tenant(store)
        s = store.create_shipment(op, "SHIP-EX9")
        store.ingest_event(op, s.id, "ev-1", "delayed", 100.0)
        exc = store.list_exceptions(viewer)[0]
        with pytest.raises(AuthzError):
            store.mutate_exception(viewer, exc.id, exc.version, "acknowledge")

    def test_only_one_active_exception_per_shipment(self):
        store = make_store()
        admin, op, _ = setup_tenant(store)
        s = store.create_shipment(op, "SHIP-EX10")
        store.ingest_event(op, s.id, "ev-1", "delayed", 100.0)
        store.ingest_event(op, s.id, "ev-2", "delayed", 200.0)  # second delayed
        open_excs = store.list_exceptions(op, status="open")
        assert len(open_excs) == 1


# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------


class TestAudit:
    def test_audit_entries_created(self):
        store = make_store()
        admin, op, _ = setup_tenant(store)
        s = store.create_shipment(op, "SHIP-A1")
        entries = store.audit(op)
        assert any(e.action == "create_shipment" for e in entries)

    def test_audit_tenant_isolation(self):
        store = make_store()
        admin1, op1, _ = setup_tenant(store, "t1")
        admin2, op2, _ = setup_tenant(store, "t2")
        store.create_shipment(op1, "REF-1")
        entries_t1 = store.audit(op1)
        entries_t2 = store.audit(op2)
        assert len(entries_t1) > 0
        assert all(e.tenant_id == "t1" for e in entries_t1)
        assert all(e.tenant_id == "t2" for e in entries_t2)

    def test_audit_filter_by_entity(self):
        store = make_store()
        admin, op, _ = setup_tenant(store)
        s = store.create_shipment(op, "SHIP-A2")
        entries = store.audit(op, entity_type="shipment", entity_id=s.id)
        assert all(e.entity_id == s.id for e in entries)


# ---------------------------------------------------------------------------
# SLA rules and tick
# ---------------------------------------------------------------------------


class TestSlaAndTick:
    def test_set_sla_rule(self):
        store = make_store()
        admin, op, _ = setup_tenant(store)
        rule = store.set_sla_rule(admin, "P1", 60)
        assert rule.severity == "P1"
        assert rule.delay_seconds == 60

    def test_sla_upsert(self):
        store = make_store()
        admin, op, _ = setup_tenant(store)
        store.set_sla_rule(admin, "P1", 60)
        rule = store.set_sla_rule(admin, "P1", 120)
        assert rule.delay_seconds == 120

    def test_invalid_severity(self):
        store = make_store()
        admin = bootstrap(store)
        with pytest.raises(ValidationError):
            store.set_sla_rule(admin, "P9", 60)

    def test_non_admin_cannot_set_rule(self):
        store = make_store()
        admin, op, viewer = setup_tenant(store)
        with pytest.raises(AuthzError):
            store.set_sla_rule(op, "P1", 60)

    def test_tick_enqueues_escalation(self):
        store = make_store()
        admin, op, _ = setup_tenant(store)
        store.set_sla_rule(admin, "P2", 10)  # 10 second rule
        s = store.create_shipment(op, "SHIP-SLA1")
        store.ingest_event(op, s.id, "ev-1", "delayed", 1.0)
        # Exception opened_at is some timestamp; tick at opened_at + 11
        exc = store.list_exceptions(op)[0]
        count = store.tick(exc.opened_at + 11, limit=100)
        assert count == 1
        # Tick again – idempotent
        count2 = store.tick(exc.opened_at + 20, limit=100)
        assert count2 == 0

    def test_tick_does_not_escalate_acknowledged(self):
        store = make_store()
        admin, op, _ = setup_tenant(store)
        store.set_sla_rule(admin, "P2", 10)
        s = store.create_shipment(op, "SHIP-SLA2")
        store.ingest_event(op, s.id, "ev-1", "delayed", 1.0)
        exc = store.list_exceptions(op)[0]
        store.mutate_exception(op, exc.id, exc.version, "acknowledge")
        # acknowledged status means tick should NOT escalate
        count = store.tick(exc.opened_at + 20, limit=100)
        assert count == 0

    def test_tick_does_not_escalate_resolved(self):
        store = make_store()
        admin, op, _ = setup_tenant(store)
        store.set_sla_rule(admin, "P2", 10)
        s = store.create_shipment(op, "SHIP-SLA3")
        store.ingest_event(op, s.id, "ev-1", "delayed", 1.0)
        exc = store.list_exceptions(op)[0]
        store.mutate_exception(op, exc.id, exc.version, "resolve")
        count = store.tick(exc.opened_at + 20, limit=100)
        assert count == 0

    def test_tick_not_yet_due(self):
        store = make_store()
        admin, op, _ = setup_tenant(store)
        store.set_sla_rule(admin, "P2", 3600)  # 1 hour rule
        s = store.create_shipment(op, "SHIP-SLA4")
        store.ingest_event(op, s.id, "ev-1", "delayed", 1.0)
        exc = store.list_exceptions(op)[0]
        count = store.tick(exc.opened_at + 5, limit=100)
        assert count == 0


# ---------------------------------------------------------------------------
# Outbox deliveries
# ---------------------------------------------------------------------------


class TestOutboxDelivery:
    def test_claim_and_complete(self):
        store = make_store()
        admin, op, _ = setup_tenant(store)
        store.create_shipment(op, "SHIP-D1")
        delivery = store.claim_delivery("worker-1", 9999.0)
        assert delivery is not None
        assert delivery.owner == "worker-1"
        store.complete_delivery(delivery.id, "worker-1", 10000.0)
        # Verify delivered
        deliveries = store.list_deliveries(admin, status="delivered")
        assert any(d.id == delivery.id for d in deliveries)

    def test_claim_fail_retry_and_dead_letter(self):
        store = make_store(max_attempts=2)
        admin, op, _ = setup_tenant(store)
        store.create_shipment(op, "SHIP-D2")
        now = 9999.0
        d = store.claim_delivery("w1", now)
        assert d is not None
        store.fail_delivery(d.id, "w1", "network error", now)
        # Should be in failed state awaiting retry
        d2 = store.claim_delivery("w1", now + 100)
        assert d2 is not None
        store.fail_delivery(d2.id, "w1", "still broken", now + 100)
        # Now dead-lettered (max_attempts=2)
        dead = store.list_deliveries(admin, status="dead")
        assert any(d.id == d2.id for d in dead)

    def test_replay_dead_letter(self):
        store = make_store(max_attempts=1)
        admin, op, _ = setup_tenant(store)
        store.create_shipment(op, "SHIP-D3")
        now = 9999.0
        d = store.claim_delivery("w1", now)
        store.fail_delivery(d.id, "w1", "error", now)
        dead = store.list_deliveries(admin, status="dead")
        assert len(dead) >= 1
        dead_id = dead[0].id
        replayed = store.replay_delivery(admin, dead_id, now + 1)
        assert replayed.status == "pending"

    def test_concurrent_workers_no_double_claim(self):
        """Two threads should not claim the same delivery."""
        store = make_store()
        admin, op, _ = setup_tenant(store)
        store.create_shipment(op, "SHIP-D4")
        # Only one delivery enqueued
        claimed = []
        errors = []
        barrier = threading.Barrier(2)

        def worker(wid):
            try:
                barrier.wait()
                d = store.claim_delivery(wid, 99999.0)
                if d:
                    claimed.append((wid, d.id))
            except Exception as e:
                errors.append(e)

        t1 = threading.Thread(target=worker, args=("w1",))
        t2 = threading.Thread(target=worker, args=("w2",))
        t1.start(); t2.start()
        t1.join(); t2.join()
        assert not errors
        # At most one claim
        assert len(claimed) <= 1

    def test_expired_lease_recoverable(self):
        store = make_store(lease_seconds=1)
        admin, op, _ = setup_tenant(store)
        store.create_shipment(op, "SHIP-D5")
        now = 1000.0
        d = store.claim_delivery("w1", now)
        assert d is not None
        # Lease expires after 1 second
        d2 = store.claim_delivery("w2", now + 10)  # well past lease
        assert d2 is not None
        assert d2.owner == "w2"

    def test_non_admin_cannot_replay(self):
        store = make_store(max_attempts=1)
        admin, op, viewer = setup_tenant(store)
        store.create_shipment(op, "SHIP-D6")
        now = 9999.0
        d = store.claim_delivery("w1", now)
        store.fail_delivery(d.id, "w1", "err", now)
        dead = store.list_deliveries(admin, status="dead")[0]
        with pytest.raises(AuthzError):
            store.replay_delivery(op, dead.id, now + 1)


# ---------------------------------------------------------------------------
# Snapshot export/import
# ---------------------------------------------------------------------------


class TestSnapshot:
    def test_export_and_import_round_trip(self):
        store = make_store()
        admin, op, _ = setup_tenant(store)
        s = store.create_shipment(op, "SHIP-SNAP1")
        store.ingest_event(op, s.id, "ev-1", "delayed", 100.0)
        snap = store.export_snapshot(admin)
        assert snap["tenant"]["id"] == "acme"
        assert len(snap["shipments"]) == 1

        # Import into a fresh store
        store2 = make_store()
        store2.bootstrap_tenant("acme", "Acme Corp", admin)
        store2.import_snapshot(admin, snap)

        ships = store2.list_shipments(op)
        assert len(ships) == 1
        assert ships[0].reference == "SHIP-SNAP1"
        assert ships[0].status == "delayed"

    def test_import_wrong_tenant_rejected(self):
        store = make_store()
        admin1, op1, _ = setup_tenant(store, "t1")
        admin2, op2, _ = setup_tenant(store, "t2")
        snap = store.export_snapshot(admin1)
        # admin2 cannot import t1's snapshot
        with pytest.raises(AuthzError):
            store.import_snapshot(admin2, snap)

    def test_non_admin_cannot_export(self):
        store = make_store()
        admin, op, viewer = setup_tenant(store)
        with pytest.raises(AuthzError):
            store.export_snapshot(op)

    def test_import_is_atomic(self):
        """Import replaces all existing data atomically."""
        store = make_store()
        admin, op, _ = setup_tenant(store)
        s1 = store.create_shipment(op, "SHIP-ORIG")
        # Build a snapshot with a different shipment
        snap = store.export_snapshot(admin)
        # Add another shipment and import the old snapshot
        store.create_shipment(op, "SHIP-NEW")
        store.import_snapshot(admin, snap)
        # After import, only the snapshot's shipment exists
        ships = store.list_shipments(op)
        refs = [s.reference for s in ships]
        assert "SHIP-ORIG" in refs
        assert "SHIP-NEW" not in refs


# ---------------------------------------------------------------------------
# Durability / shared database
# ---------------------------------------------------------------------------


class TestDurability:
    def test_two_instances_share_data(self, tmp_path):
        """Two store instances on the same file see each other's commits."""
        db = str(tmp_path / "test.db")
        store1 = SQLiteFreightStore(db)
        store2 = SQLiteFreightStore(db)
        store1.bootstrap_tenant("shared", "Shared", "admin-shared")
        store1.create_credential("admin-shared", "op-shared", "operator")

        s = store1.create_shipment("op-shared", "SHIP-DUR1")
        # store2 sees it
        ships = store2.list_shipments("op-shared")
        assert len(ships) == 1
        assert ships[0].id == s.id

    def test_schema_init_idempotent(self, tmp_path):
        """Calling _init_schema twice doesn't crash."""
        db = str(tmp_path / "test2.db")
        store1 = SQLiteFreightStore(db)
        store2 = SQLiteFreightStore(db)
        store1._init_schema()
        store2._init_schema()
