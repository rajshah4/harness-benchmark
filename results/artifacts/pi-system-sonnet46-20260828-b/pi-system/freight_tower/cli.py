"""
Freight Control Tower CLI

Usage
-----
  freight-tower --db PATH init [--admin-token TOKEN] TENANT_ID NAME
  freight-tower --db PATH serve [--host HOST] [--port PORT]
  freight-tower --db PATH credentials create TOKEN ROLE --admin-token TOKEN
  freight-tower --db PATH shipments create REFERENCE --token TOKEN
  freight-tower --db PATH shipments list [--status STATUS] --token TOKEN
  freight-tower --db PATH events ingest SHIPMENT_ID EVENT_ID EVENT_TYPE EVENT_TIME [--location LOC] --token TOKEN
  freight-tower --db PATH exceptions list [--status STATUS] --token TOKEN
  freight-tower --db PATH exceptions mutate EXCEPTION_ID ACTION EXPECTED_VERSION --token TOKEN [--assignee A] [--note N]
  freight-tower --db PATH audit [--entity-type T] [--entity-id ID] --token TOKEN
  freight-tower --db PATH rules set SEVERITY DELAY_SECONDS --admin-token TOKEN
  freight-tower --db PATH tick NOW [--limit N]
  freight-tower --db PATH worker run --worker-id ID [--once]
  freight-tower --db PATH snapshot export --admin-token TOKEN
  freight-tower --db PATH snapshot import SNAPSHOT_FILE --admin-token TOKEN
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from .service import FreightService
from .sqlite_store import SQLiteFreightStore
from .web import create_server


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _store(args: argparse.Namespace) -> SQLiteFreightStore:
    db = getattr(args, "db", ":memory:")
    return SQLiteFreightStore(db)


def _print(value: object) -> None:
    if hasattr(value, "__dict__") or hasattr(value, "_fields"):
        print(json.dumps(value.__dict__, default=str, indent=2))
    else:
        print(json.dumps(value, default=str, indent=2))


def _obj_to_dict(obj: object) -> object:
    """Convert dataclass-like objects to dicts recursively."""
    if hasattr(obj, "__dataclass_fields__"):
        return {k: _obj_to_dict(v) for k, v in obj.__dict__.items()}
    if isinstance(obj, list):
        return [_obj_to_dict(i) for i in obj]
    return obj


def _show(value: object) -> None:
    print(json.dumps(_obj_to_dict(value), default=str, indent=2))


# ---------------------------------------------------------------------------
# Subcommand handlers
# ---------------------------------------------------------------------------


def cmd_init(args: argparse.Namespace) -> int:
    store = _store(args)
    store.bootstrap_tenant(args.tenant_id, args.name, args.admin_token)
    print(f"Tenant '{args.tenant_id}' initialised with admin token '{args.admin_token}'.")
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    db = getattr(args, "db", ":memory:")
    if db == ":memory:":
        service: FreightService | SQLiteFreightStore = FreightService()
        print("WARNING: using in-memory store (data will be lost on exit)")
    else:
        service = SQLiteFreightStore(db)
    server = create_server(service, args.host, args.port)
    print(f"Listening on {args.host}:{args.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


def cmd_credentials_create(args: argparse.Namespace) -> int:
    store = _store(args)
    store.create_credential(args.admin_token, args.token, args.role)
    print(f"Credential created: token='{args.token}' role='{args.role}'")
    return 0


def cmd_shipments_create(args: argparse.Namespace) -> int:
    store = _store(args)
    ship = store.create_shipment(args.token, args.reference)
    _show(ship)
    return 0


def cmd_shipments_list(args: argparse.Namespace) -> int:
    store = _store(args)
    filters = {}
    if args.status:
        filters["status"] = args.status
    ships = store.list_shipments(args.token, **filters)
    _show(ships)
    return 0


def cmd_events_ingest(args: argparse.Namespace) -> int:
    store = _store(args)
    ship = store.ingest_event(
        args.token,
        args.shipment_id,
        args.event_id,
        args.event_type,
        float(args.event_time),
        location=getattr(args, "location", None),
        details=getattr(args, "details", None),
    )
    _show(ship)
    return 0


def cmd_exceptions_list(args: argparse.Namespace) -> int:
    store = _store(args)
    filters = {}
    for key in ("status", "severity", "assignee", "shipment_id"):
        val = getattr(args, key, None)
        if val:
            filters[key] = val
    excs = store.list_exceptions(args.token, **filters)
    _show(excs)
    return 0


def cmd_exceptions_mutate(args: argparse.Namespace) -> int:
    store = _store(args)
    values = {}
    if getattr(args, "assignee", None):
        values["assignee"] = args.assignee
    if getattr(args, "note", None):
        values["note"] = args.note
    exc = store.mutate_exception(
        args.token,
        args.exception_id,
        int(args.expected_version),
        args.action,
        actor=getattr(args, "actor", None),
        **values,
    )
    _show(exc)
    return 0


def cmd_audit(args: argparse.Namespace) -> int:
    store = _store(args)
    filters = {}
    for key in ("entity_type", "entity_id", "action"):
        val = getattr(args, key.replace("-", "_"), None)
        if val:
            filters[key] = val
    entries = store.audit(args.token, **filters)
    _show(entries)
    return 0


def cmd_rules_set(args: argparse.Namespace) -> int:
    store = _store(args)
    rule = store.set_sla_rule(args.admin_token, args.severity, int(args.delay_seconds))
    _show(rule)
    return 0


def cmd_tick(args: argparse.Namespace) -> int:
    store = _store(args)
    now = float(args.now) if args.now != "now" else time.time()
    count = store.tick(now, limit=int(args.limit))
    print(f"Escalations enqueued: {count}")
    return 0


def cmd_worker_run(args: argparse.Namespace) -> int:
    store = _store(args)
    worker_id = args.worker_id
    once = getattr(args, "once", False)
    print(f"Worker '{worker_id}' started (once={once})")
    while True:
        now = time.time()
        delivery = store.claim_delivery(worker_id, now)
        if delivery is None:
            if once:
                print("No pending deliveries.")
                break
            time.sleep(1)
            continue
        print(f"  Processing {delivery.id} ({delivery.event_type})…", end=" ")
        try:
            # Simulate delivery (in a real system, call a webhook/queue here)
            store.complete_delivery(delivery.id, worker_id, time.time())
            print("OK")
        except Exception as exc:
            print(f"FAIL: {exc}")
            store.fail_delivery(delivery.id, worker_id, str(exc), time.time())
        if once:
            break
    return 0


def cmd_snapshot_export(args: argparse.Namespace) -> int:
    store = _store(args)
    snap = store.export_snapshot(args.admin_token)
    out = json.dumps(snap, default=str, indent=2)
    if hasattr(args, "output") and args.output:
        Path(args.output).write_text(out)
        print(f"Snapshot written to {args.output}")
    else:
        print(out)
    return 0


def cmd_snapshot_import(args: argparse.Namespace) -> int:
    store = _store(args)
    snap = json.loads(Path(args.snapshot_file).read_text())
    store.import_snapshot(args.admin_token, snap)
    print("Snapshot imported successfully.")
    return 0


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="freight-tower",
                                description="Freight Control Tower CLI")
    p.add_argument("--db", default=":memory:", metavar="PATH",
                   help="SQLite database path (default: :memory:)")
    sub = p.add_subparsers(dest="command", required=True)

    # init
    init_p = sub.add_parser("init", help="Bootstrap a tenant")
    init_p.add_argument("tenant_id")
    init_p.add_argument("name")
    init_p.add_argument("--admin-token", required=True, dest="admin_token")

    # serve
    serve_p = sub.add_parser("serve", help="Start the HTTP server")
    serve_p.add_argument("--host", default="127.0.0.1")
    serve_p.add_argument("--port", type=int, default=8080)

    # credentials
    cred_p = sub.add_parser("credentials", help="Credential management")
    cred_sub = cred_p.add_subparsers(dest="cred_command", required=True)
    cred_create = cred_sub.add_parser("create")
    cred_create.add_argument("token")
    cred_create.add_argument("role", choices=["viewer", "operator", "admin"])
    cred_create.add_argument("--admin-token", required=True, dest="admin_token")

    # shipments
    ship_p = sub.add_parser("shipments", help="Shipment management")
    ship_sub = ship_p.add_subparsers(dest="ship_command", required=True)
    ship_create = ship_sub.add_parser("create")
    ship_create.add_argument("reference")
    ship_create.add_argument("--token", required=True)
    ship_list = ship_sub.add_parser("list")
    ship_list.add_argument("--token", required=True)
    ship_list.add_argument("--status", default="")

    # events
    ev_p = sub.add_parser("events", help="Carrier event ingestion")
    ev_sub = ev_p.add_subparsers(dest="ev_command", required=True)
    ev_ingest = ev_sub.add_parser("ingest")
    ev_ingest.add_argument("shipment_id")
    ev_ingest.add_argument("event_id")
    ev_ingest.add_argument("event_type",
                           choices=["picked_up", "in_transit", "delayed", "delivered", "cancelled"])
    ev_ingest.add_argument("event_time", metavar="EVENT_TIME",
                           help="Unix timestamp (float)")
    ev_ingest.add_argument("--location", default=None)
    ev_ingest.add_argument("--details", default=None)
    ev_ingest.add_argument("--token", required=True)

    # exceptions
    exc_p = sub.add_parser("exceptions", help="Exception workflow")
    exc_sub = exc_p.add_subparsers(dest="exc_command", required=True)
    exc_list = exc_sub.add_parser("list")
    exc_list.add_argument("--token", required=True)
    for k in ("status", "severity", "assignee", "shipment_id"):
        exc_list.add_argument(f"--{k}", default="")
    exc_mut = exc_sub.add_parser("mutate")
    exc_mut.add_argument("exception_id")
    exc_mut.add_argument("action", choices=["assign", "acknowledge", "note", "resolve"])
    exc_mut.add_argument("expected_version", type=int)
    exc_mut.add_argument("--token", required=True)
    exc_mut.add_argument("--assignee", default=None)
    exc_mut.add_argument("--note", default=None)
    exc_mut.add_argument("--actor", default=None)

    # audit
    audit_p = sub.add_parser("audit", help="Audit log")
    audit_p.add_argument("--token", required=True)
    audit_p.add_argument("--entity-type", default="")
    audit_p.add_argument("--entity-id", default="")
    audit_p.add_argument("--action", default="")

    # rules
    rules_p = sub.add_parser("rules", help="SLA rule management")
    rules_sub = rules_p.add_subparsers(dest="rules_command", required=True)
    rules_set = rules_sub.add_parser("set")
    rules_set.add_argument("severity", choices=["P1", "P2", "P3"])
    rules_set.add_argument("delay_seconds", type=int)
    rules_set.add_argument("--admin-token", required=True, dest="admin_token")

    # tick
    tick_p = sub.add_parser("tick", help="Process SLA escalations")
    tick_p.add_argument("now", nargs="?", default="now",
                        help="Unix timestamp or 'now' (default)")
    tick_p.add_argument("--limit", type=int, default=100)

    # worker
    worker_p = sub.add_parser("worker", help="Outbox delivery worker")
    worker_sub = worker_p.add_subparsers(dest="worker_command", required=True)
    worker_run = worker_sub.add_parser("run")
    worker_run.add_argument("--worker-id", default="default-worker", dest="worker_id")
    worker_run.add_argument("--once", action="store_true")

    # snapshot
    snap_p = sub.add_parser("snapshot", help="Tenant snapshot export/import")
    snap_sub = snap_p.add_subparsers(dest="snap_command", required=True)
    snap_export = snap_sub.add_parser("export")
    snap_export.add_argument("--admin-token", required=True, dest="admin_token")
    snap_export.add_argument("--output", default="")
    snap_import = snap_sub.add_parser("import")
    snap_import.add_argument("snapshot_file")
    snap_import.add_argument("--admin-token", required=True, dest="admin_token")

    return p


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def main() -> int:
    args = parser().parse_args()
    try:
        if args.command == "init":
            return cmd_init(args)
        if args.command == "serve":
            return cmd_serve(args)
        if args.command == "credentials":
            if args.cred_command == "create":
                return cmd_credentials_create(args)
        if args.command == "shipments":
            if args.ship_command == "create":
                return cmd_shipments_create(args)
            if args.ship_command == "list":
                return cmd_shipments_list(args)
        if args.command == "events":
            if args.ev_command == "ingest":
                return cmd_events_ingest(args)
        if args.command == "exceptions":
            if args.exc_command == "list":
                return cmd_exceptions_list(args)
            if args.exc_command == "mutate":
                return cmd_exceptions_mutate(args)
        if args.command == "audit":
            return cmd_audit(args)
        if args.command == "rules":
            if args.rules_command == "set":
                return cmd_rules_set(args)
        if args.command == "tick":
            return cmd_tick(args)
        if args.command == "worker":
            if args.worker_command == "run":
                return cmd_worker_run(args)
        if args.command == "snapshot":
            if args.snap_command == "export":
                return cmd_snapshot_export(args)
            if args.snap_command == "import":
                return cmd_snapshot_import(args)
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    print(f"Unknown command: {args.command}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
