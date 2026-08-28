#!/usr/bin/env python3
"""Independent capability verifier for the freight control tower benchmark."""

from __future__ import annotations

import argparse
import importlib
import json
import subprocess
import sys
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable


class Checks:
    def __init__(self) -> None:
        self.results: list[tuple[str, bool, str]] = []

    def run(self, name: str, check: Callable[[], None]) -> None:
        try:
            check()
        except Exception as exc:
            self.results.append((name, False, f"{type(exc).__name__}: {exc}"))
        else:
            self.results.append((name, True, ""))

    def report(self) -> bool:
        for name, passed, detail in self.results:
            print(f"{'PASS' if passed else 'FAIL'}: {name}")
            if detail:
                print(f"  {detail}")
        passed = sum(row[1] for row in self.results)
        print(f"\n{passed}/{len(self.results)} capabilities passed")
        return passed == len(self.results)


def field(value: Any, name: str) -> Any:
    if isinstance(value, dict):
        return value[name]
    return getattr(value, name)


def enum(value: Any) -> Any:
    return getattr(value, "value", value)


def expect_failure(call: Callable[[], Any], words: tuple[str, ...]) -> None:
    try:
        call()
    except Exception as exc:
        text = f"{type(exc).__name__} {exc}".lower()
        if not any(word in text for word in words):
            raise AssertionError(f"failure was not distinct enough: {text}") from exc
    else:
        raise AssertionError("operation should have failed")


def store_type(workspace: Path) -> type:
    sys.path.insert(0, str(workspace))
    importlib.invalidate_caches()
    return importlib.import_module("freight_tower.sqlite_store").SQLiteFreightStore


def open_store(cls: type, path: Path, **values: Any) -> Any:
    """Open a store through the explicit initialization path allowed by the contract."""
    store = cls(path, **values)
    initializer = getattr(store, "init_schema", None)
    if callable(initializer):
        initializer()
    return store


def setup(store: Any, tenant: str = "acme", token: str = "acme-admin") -> tuple[str, str, str]:
    store.bootstrap_tenant(tenant, tenant.title(), token)
    operator, viewer = f"{tenant}-operator", f"{tenant}-viewer"
    store.create_credential(token, operator, "operator")
    store.create_credential(token, viewer, "viewer")
    return token, operator, viewer


def public_tests(workspace: Path) -> None:
    result = subprocess.run([sys.executable, "-m", "pytest", "-q"], cwd=workspace, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=180)
    if result.returncode:
        raise AssertionError(result.stdout[-1800:])


def persistence_and_restart(cls: type) -> None:
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "tower.db"
        first = open_store(cls, path, clock=lambda: 100.0)
        admin, operator, _ = setup(first)
        shipment = first.create_shipment(operator, "ACME-100")
        first.ingest_event(operator, field(shipment, "id"), "evt-1", "in_transit", 110.0, location="Dallas")
        reopened = open_store(cls, path, clock=lambda: 120.0)
        value = reopened.get_shipment(admin, field(shipment, "id"))
        assert field(value, "reference") == "ACME-100"
        assert field(value, "last_location") == "Dallas"
        assert reopened.audit(admin)


def deterministic_replay(cls: type) -> None:
    with tempfile.TemporaryDirectory() as directory:
        store = open_store(cls, Path(directory) / "tower.db", clock=lambda: 1000.0)
        _, operator, _ = setup(store)
        shipment = store.create_shipment(operator, "ORDER-1")
        sid = field(shipment, "id")
        store.ingest_event(operator, sid, "delivered", "delivered", 300.0, location="Chicago")
        store.ingest_event(operator, sid, "picked", "picked_up", 100.0, location="Austin")
        store.ingest_event(operator, sid, "late-delay", "delayed", 200.0, location="Memphis")
        projected = store.get_shipment(operator, sid)
        assert enum(field(projected, "status")) == "delivered"
        assert field(projected, "last_location") == "Chicago"
        active = [x for x in store.list_exceptions(operator) if field(x, "shipment_id") == sid and enum(field(x, "status")) not in {"resolved", "closed"}]
        assert not active


def concurrent_idempotency(cls: type) -> None:
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "tower.db"
        base = open_store(cls, path)
        _, operator, _ = setup(base)
        sid = field(base.create_shipment(operator, "DUP-1"), "id")
        audit_before = len(base.audit(operator))
        reference = open_store(cls, Path(directory) / "reference.db")
        _, reference_operator, _ = setup(reference, "reference")
        reference_sid = field(reference.create_shipment(reference_operator, "REFERENCE-1"), "id")
        reference_before = len(reference.audit(reference_operator))
        reference.ingest_event(reference_operator, reference_sid, "reference-event", "delayed", 50.0, location="Laredo", details={"reason": "weather"})
        expected_audit_delta = len(reference.audit(reference_operator)) - reference_before
        barrier = threading.Barrier(6)

        def ingest(_: int) -> Any:
            local = open_store(cls, path)
            barrier.wait(timeout=10)
            return local.ingest_event(operator, sid, "same-event", "delayed", 50.0, location="Laredo", details={"reason": "weather"})

        with ThreadPoolExecutor(max_workers=6) as pool:
            list(pool.map(ingest, range(6)))
        audit_after_concurrency = len(base.audit(operator))
        if audit_after_concurrency - audit_before != expected_audit_delta:
            raise AssertionError("concurrent duplicates produced more audit effects than one ingest")
        base.ingest_event(operator, sid, "same-event", "delayed", 50.0, location="Laredo", details={"reason": "weather"})
        if len(base.audit(operator)) != audit_after_concurrency:
            raise AssertionError("a sequential replay added an audit effect")
        assert len([x for x in base.list_exceptions(operator) if field(x, "shipment_id") == sid]) == 1
        expect_failure(lambda: base.ingest_event(operator, sid, "same-event", "delivered", 60.0), ("conflict", "idempot"))


def exception_versions(cls: type) -> None:
    with tempfile.TemporaryDirectory() as directory:
        store = open_store(cls, Path(directory) / "tower.db", clock=lambda: 10.0)
        _, operator, _ = setup(store)
        sid = field(store.create_shipment(operator, "EX-1"), "id")
        store.ingest_event(operator, sid, "delay-1", "delayed", 20.0)
        exception = store.list_exceptions(operator)[0]
        eid, version = field(exception, "id"), field(exception, "version")
        acked = store.mutate_exception(operator, eid, version, "acknowledge", actor="op")
        expect_failure(lambda: store.mutate_exception(operator, eid, version, "resolve", actor="op"), ("stale", "version", "conflict"))
        resolved = store.mutate_exception(operator, eid, field(acked, "version"), "resolve", actor="op")
        assert enum(field(resolved, "status")) in {"resolved", "closed"}
        assert len(store.audit(operator)) >= 4


def tenant_and_rbac(cls: type) -> None:
    with tempfile.TemporaryDirectory() as directory:
        store = open_store(cls, Path(directory) / "tower.db")
        admin, operator, viewer = setup(store, "one")
        two_admin, _, _ = setup(store, "two")
        shipment = store.create_shipment(operator, "SECRET-ONE")
        expect_failure(lambda: store.get_shipment(two_admin, field(shipment, "id")), ("not", "tenant", "forbid", "auth"))
        expect_failure(lambda: store.create_shipment(viewer, "NOPE"), ("forbid", "role", "author"))
        expect_failure(lambda: store.list_shipments("bad-token"), ("auth", "credential", "token"))
        snapshot = store.export_snapshot(admin)
        assert "SECRET-ONE" in json.dumps(snapshot)
        assert "SECRET-ONE" not in json.dumps(store.export_snapshot(two_admin))


def sla_tick(cls: type) -> None:
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "tower.db"
        store = open_store(cls, path)
        admin, operator, _ = setup(store)
        store.set_sla_rule(admin, "P1", 30)
        sid = field(store.create_shipment(operator, "SLA-1"), "id")
        store.ingest_event(operator, sid, "delay-sla", "delayed", 100.0, details={"severity": "P1"})
        assert store.tick(129.0) == 0
        with ThreadPoolExecutor(max_workers=2) as pool:
            counts = list(pool.map(lambda _: open_store(cls, path).tick(131.0), range(2)))
        assert sum(counts) == 1
        deliveries = store.list_deliveries(operator)
        assert len(deliveries) == 1


def outbox_leases_and_retries(cls: type) -> None:
    with tempfile.TemporaryDirectory() as directory:
        store = open_store(cls, Path(directory) / "tower.db", lease_seconds=10, max_attempts=2)
        admin, operator, _ = setup(store)
        store.set_sla_rule(admin, "P1", 1)
        sid = field(store.create_shipment(operator, "OUT-1"), "id")
        store.ingest_event(operator, sid, "delay-out", "delayed", 10.0, details={"severity": "P1"})
        store.tick(12.0)
        first = store.claim_delivery("worker-a", 13.0)
        assert first is not None
        assert store.claim_delivery("worker-b", 13.0) is None
        recovered = store.claim_delivery("worker-b", 24.0)
        assert recovered is not None and field(recovered, "id") == field(first, "id")
        store.fail_delivery(field(first, "id"), "worker-b", "downstream", 24.0)
        retry = store.claim_delivery("worker-c", 10_000.0)
        assert retry is not None
        store.fail_delivery(field(retry, "id"), "worker-c", "still down", 10_000.0)
        dead = store.list_deliveries(admin, status="dead_letter")
        if not dead:
            dead = [x for x in store.list_deliveries(admin) if "dead" in str(enum(field(x, "status"))).lower()]
        assert dead
        store.replay_delivery(admin, field(dead[0], "id"), 20_000.0)
        assert store.claim_delivery("worker-d", 20_000.0) is not None


def surfaces_and_snapshot(workspace: Path, cls: type) -> None:
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "tower.db"
        store = open_store(cls, path)
        admin, operator, _ = setup(store)
        store.create_shipment(operator, "BACKUP-1")
        snapshot = store.export_snapshot(admin)
        other = open_store(cls, Path(directory) / "restore.db")
        other.bootstrap_tenant("acme", "Acme", admin)
        other.import_snapshot(admin, snapshot)
        assert [field(x, "reference") for x in other.list_shipments(admin)] == ["BACKUP-1"]
        help_result = subprocess.run([sys.executable, "-m", "freight_tower.cli", "--help"], cwd=workspace, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=30)
        assert help_result.returncode == 0
        help_text = help_result.stdout.lower()
        for word in ("init", "serve", "ingest", "tick", "worker", "export", "import"):
            if word not in help_text:
                raise AssertionError(f"CLI help missing {word}")
        html = (workspace / "freight_tower" / "static" / "index.html").read_text().lower()
        script = (workspace / "freight_tower" / "static" / "app.js").read_text().lower()
        for word in ("exception", "filter", "status"):
            if word not in html + script:
                raise AssertionError(f"dashboard missing {word}")
        if "innerhtml" in script:
            raise AssertionError("dashboard uses unsafe innerHTML for carrier values")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("workspace", type=Path)
    args = parser.parse_args()
    checks = Checks()
    checks.run("public regression suite", lambda: public_tests(args.workspace))
    try:
        cls = store_type(args.workspace)
    except Exception as exc:
        print(f"FAIL: compatibility API\n  {type(exc).__name__}: {exc}\n\n0/9 checks passed")
        return 1
    checks.run("durable restart", lambda: persistence_and_restart(cls))
    checks.run("deterministic out-of-order replay", lambda: deterministic_replay(cls))
    checks.run("concurrent idempotent ingestion", lambda: concurrent_idempotency(cls))
    checks.run("exception versions and audit", lambda: exception_versions(cls))
    checks.run("tenant isolation and RBAC", lambda: tenant_and_rbac(cls))
    checks.run("durable concurrent SLA tick", lambda: sla_tick(cls))
    checks.run("leased outbox recovery and dead letters", lambda: outbox_leases_and_retries(cls))
    checks.run("CLI, dashboard, and snapshot", lambda: surfaces_and_snapshot(args.workspace, cls))
    return 0 if checks.report() else 1


if __name__ == "__main__":
    raise SystemExit(main())
