"""Command-line interface for the freight control tower.

Global flag  --db  selects the SQLite database file (defaults to freight.db).
All subcommands that need auth accept a  --token  or  --admin-token  flag.

Quick-start example
-------------------
freight-tower --db data.db init
freight-tower --db data.db bootstrap --tenant-id acme --name "ACME Corp" --admin-token secret
freight-tower --db data.db credential --admin-token secret --token op1 --role operator
freight-tower --db data.db shipment create --token op1 --reference ACME-001
freight-tower --db data.db serve --host 0.0.0.0 --port 8080
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import uuid

from .sqlite_store import SQLiteFreightStore
from .web import create_server


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="freight-tower",
        description="Freight exception control tower CLI",
    )
    p.add_argument("--db", default="freight.db", metavar="PATH", help="SQLite database path")

    sub = p.add_subparsers(dest="command", required=True)

    # init
    sub.add_parser("init", help="Initialise the database schema")

    # bootstrap
    bs = sub.add_parser("bootstrap", help="Create a tenant and its first admin credential")
    bs.add_argument("--tenant-id", required=True)
    bs.add_argument("--name", required=True)
    bs.add_argument("--admin-token", required=True)

    # credential
    cr = sub.add_parser("credential", help="Add a credential to the admin's tenant")
    cr.add_argument("--admin-token", required=True)
    cr.add_argument("--token", required=True)
    cr.add_argument("--role", required=True, choices=["viewer", "operator", "admin"])

    # shipment subcommands
    shp = sub.add_parser("shipment", help="Shipment operations")
    shp_sub = shp.add_subparsers(dest="subcommand", required=True)

    shp_create = shp_sub.add_parser("create")
    shp_create.add_argument("--token", required=True)
    shp_create.add_argument("--reference", required=True)

    shp_list = shp_sub.add_parser("list")
    shp_list.add_argument("--token", required=True)
    shp_list.add_argument("--status", default=None)

    shp_get = shp_sub.add_parser("get")
    shp_get.add_argument("--token", required=True)
    shp_get.add_argument("--id", required=True, dest="shipment_id")

    # event ingestion
    ev = sub.add_parser("ingest", help="Ingest a carrier event")
    ev.add_argument("--token", required=True)
    ev.add_argument("--shipment-id", required=True)
    ev.add_argument("--event-id", default=None, help="Globally unique event id (generated if omitted)")
    ev.add_argument("--event-type", required=True,
                    choices=["picked_up", "in_transit", "delayed", "delivered", "cancelled"])
    ev.add_argument("--event-time", type=float, default=None,
                    help="Unix timestamp of the event (defaults to now)")
    ev.add_argument("--location", default=None)
    ev.add_argument("--details", default=None, help="JSON object")

    # exceptions
    exc = sub.add_parser("exceptions", help="List exceptions")
    exc.add_argument("--token", required=True)
    exc.add_argument("--status", default=None)
    exc.add_argument("--severity", default=None)
    exc.add_argument("--assignee", default=None)

    # mutate exception
    mu = sub.add_parser("mutate", help="Mutate an exception (assign/acknowledge/add_note/resolve)")
    mu.add_argument("--token", required=True)
    mu.add_argument("--exception-id", required=True)
    mu.add_argument("--version", required=True, type=int, dest="expected_version")
    mu.add_argument("--action", required=True,
                    choices=["assign", "acknowledge", "add_note", "resolve"])
    mu.add_argument("--actor", default=None)
    mu.add_argument("--assignee", default=None)
    mu.add_argument("--note", default=None)

    # audit
    aud = sub.add_parser("audit", help="View audit trail")
    aud.add_argument("--token", required=True)
    aud.add_argument("--entity-type", default=None)
    aud.add_argument("--entity-id", default=None)

    # SLA rule
    sla = sub.add_parser("sla-rule", help="Set an SLA escalation rule")
    sla.add_argument("--admin-token", required=True)
    sla.add_argument("--severity", required=True)
    sla.add_argument("--delay-seconds", required=True, type=float)

    # tick
    tk = sub.add_parser("tick", help="Enqueue SLA escalations")
    tk.add_argument("--now", type=float, default=None)
    tk.add_argument("--limit", type=int, default=100)

    # worker
    wk = sub.add_parser("worker", help="Process outbox deliveries")
    wk.add_argument("--once", action="store_true", help="Process one delivery then exit")
    wk.add_argument("--interval", type=float, default=2.0, help="Poll interval in seconds")

    # snapshot export
    exp = sub.add_parser("export", help="Export tenant snapshot to JSON")
    exp.add_argument("--admin-token", required=True)
    exp.add_argument("--output", default=None, help="Output file (stdout if omitted)")

    # snapshot import
    imp = sub.add_parser("import", help="Import tenant snapshot from JSON")
    imp.add_argument("--admin-token", required=True)
    imp.add_argument("--input", required=True, help="Input JSON file")

    # serve
    srv = sub.add_parser("serve", help="Start the HTTP server")
    srv.add_argument("--host", default="127.0.0.1")
    srv.add_argument("--port", type=int, default=8080)

    return p


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _pp(obj: object) -> None:
    print(json.dumps(obj, indent=2, default=str))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    p = _build_parser()
    args = p.parse_args(argv)

    store = SQLiteFreightStore(args.db)

    cmd = args.command

    # ------ init ------
    if cmd == "init":
        store.init_schema()
        print("Schema initialised.")
        return 0

    # ------ bootstrap ------
    if cmd == "bootstrap":
        store.bootstrap_tenant(args.tenant_id, args.name, args.admin_token)
        print(f"Tenant '{args.tenant_id}' created with admin token.")
        return 0

    # ------ credential ------
    if cmd == "credential":
        store.create_credential(args.admin_token, args.token, args.role)
        print(f"Credential created with role '{args.role}'.")
        return 0

    # ------ shipment ------
    if cmd == "shipment":
        if args.subcommand == "create":
            result = store.create_shipment(args.token, args.reference)
            _pp({"id": result.id, "reference": result.reference, "status": result.status})
        elif args.subcommand == "list":
            filters = {}
            if args.status:
                filters["status"] = args.status
            items = store.list_shipments(args.token, **filters)
            _pp([{"id": i.id, "reference": i.reference, "status": i.status,
                  "last_location": i.last_location, "version": i.version} for i in items])
        elif args.subcommand == "get":
            result = store.get_shipment(args.token, args.shipment_id)
            _pp({"id": result.id, "reference": result.reference, "status": result.status,
                 "last_location": result.last_location, "version": result.version,
                 "active_exception_id": result.active_exception_id})
        return 0

    # ------ ingest ------
    if cmd == "ingest":
        event_id = args.event_id or str(uuid.uuid4())
        event_time = args.event_time if args.event_time is not None else time.time()
        details = json.loads(args.details) if args.details else None
        result = store.ingest_event(
            args.token,
            args.shipment_id,
            event_id,
            args.event_type,
            event_time,
            args.location,
            details,
        )
        _pp({"id": result.id, "status": result.status, "last_location": result.last_location,
             "active_exception_id": result.active_exception_id, "version": result.version})
        return 0

    # ------ exceptions ------
    if cmd == "exceptions":
        filters = {}
        if args.status:
            filters["status"] = args.status
        if args.severity:
            filters["severity"] = args.severity
        if args.assignee:
            filters["assignee"] = args.assignee
        items = store.list_exceptions(args.token, **filters)
        _pp([{"id": i.id, "shipment_id": i.shipment_id, "status": i.status,
              "severity": i.severity, "assignee": i.assignee, "version": i.version} for i in items])
        return 0

    # ------ mutate ------
    if cmd == "mutate":
        values: dict = {}
        if args.assignee:
            values["assignee"] = args.assignee
        if args.note:
            values["note"] = args.note
        result = store.mutate_exception(
            args.token,
            args.exception_id,
            args.expected_version,
            args.action,
            actor=args.actor,
            **values,
        )
        _pp({"id": result.id, "status": result.status, "version": result.version,
             "assignee": result.assignee})
        return 0

    # ------ audit ------
    if cmd == "audit":
        filters = {}
        if args.entity_type:
            filters["entity_type"] = args.entity_type
        if args.entity_id:
            filters["entity_id"] = args.entity_id
        items = store.audit(args.token, **filters)
        _pp([{"action": a.action, "entity_type": a.entity_type, "entity_id": a.entity_id,
              "actor": a.actor, "created_at": a.created_at} for a in items])
        return 0

    # ------ sla-rule ------
    if cmd == "sla-rule":
        result = store.set_sla_rule(args.admin_token, args.severity, args.delay_seconds)
        print(f"SLA rule set: {result.severity} → {result.delay_seconds}s")
        return 0

    # ------ tick ------
    if cmd == "tick":
        now = args.now if args.now is not None else time.time()
        count = store.tick(now, args.limit)
        print(f"Escalations enqueued: {count}")
        return 0

    # ------ worker ------
    if cmd == "worker":
        worker_id = f"cli-worker-{uuid.uuid4().hex[:8]}"
        print(f"Worker {worker_id} started (once={args.once})")
        while True:
            now = time.time()
            delivery = store.claim_delivery(worker_id, now)
            if delivery:
                print(f"  Processing delivery {delivery.id} ({delivery.event_type})")
                try:
                    # In the CLI worker, we just mark deliveries as delivered.
                    # A real integration would call downstream systems here.
                    store.complete_delivery(delivery.id, worker_id, time.time())
                    print(f"  Completed {delivery.id}")
                except Exception as exc:
                    store.fail_delivery(delivery.id, worker_id, str(exc), time.time())
                    print(f"  Failed {delivery.id}: {exc}", file=sys.stderr)
            if args.once:
                break
            time.sleep(args.interval)
        return 0

    # ------ export ------
    if cmd == "export":
        snapshot = store.export_snapshot(args.admin_token)
        text = json.dumps(snapshot, indent=2, default=str)
        if args.output:
            with open(args.output, "w") as f:
                f.write(text)
            print(f"Snapshot written to {args.output}")
        else:
            print(text)
        return 0

    # ------ import ------
    if cmd == "import":
        with open(args.input) as f:
            snapshot = json.load(f)
        store.import_snapshot(args.admin_token, snapshot)
        print(f"Snapshot imported for tenant '{snapshot.get('tenant_id')}'.")
        return 0

    # ------ serve ------
    if cmd == "serve":
        server = create_server(store, args.host, args.port)
        print(f"Serving on http://{args.host}:{args.port}  (Ctrl-C to stop)")
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()
        return 0

    p.print_help()
    return 1


# Legacy entry point kept for backward compat with existing tests / scripts
def parser() -> argparse.ArgumentParser:
    return _build_parser()


if __name__ == "__main__":
    raise SystemExit(main())
