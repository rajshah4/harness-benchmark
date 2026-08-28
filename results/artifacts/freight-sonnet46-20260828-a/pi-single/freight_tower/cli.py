"""Command-line interface for the freight control tower.

Usage examples::

    # Initialise a database and create the first admin
    freight-tower --db freight.db init
    freight-tower --db freight.db bootstrap --tenant acme --name "ACME Corp" --token my-admin-token

    # Manage credentials
    freight-tower --db freight.db create-credential --admin-token my-admin-token --token op-tok --role operator

    # Shipments
    freight-tower --db freight.db create-shipment --token my-admin-token --reference ACME-001
    freight-tower --db freight.db list-shipments --token my-admin-token
    freight-tower --db freight.db get-shipment --token my-admin-token --id <shipment-id>

    # Carrier events
    freight-tower --db freight.db ingest-event --token op-tok --shipment-id <id> \\
        --event-id ev-001 --event-type in_transit --event-time 1700000000 --location Chicago

    # Exceptions
    freight-tower --db freight.db list-exceptions --token my-admin-token
    freight-tower --db freight.db mutate-exception --token op-tok --id <exc-id> \\
        --version 1 --action acknowledge

    # SLA and ticking
    freight-tower --db freight.db set-sla-rule --admin-token my-admin-token --severity P1 --delay 300
    freight-tower --db freight.db tick

    # Outbox worker
    freight-tower --db freight.db worker --token my-admin-token

    # Audit
    freight-tower --db freight.db audit --token my-admin-token

    # Snapshot
    freight-tower --db freight.db export-snapshot --admin-token my-admin-token --output snap.json
    freight-tower --db freight.db import-snapshot --admin-token my-admin-token --input snap.json

    # HTTP server
    freight-tower --db freight.db serve --host 127.0.0.1 --port 8080
"""
from __future__ import annotations

import argparse
import json
import sys
import time

from .sqlite_store import SQLiteFreightStore
from .web import create_server


def _store(args: argparse.Namespace) -> SQLiteFreightStore:
    s = SQLiteFreightStore(args.db)
    s.init_schema()
    return s


def _print(value: object) -> None:
    print(json.dumps(value, default=str, indent=2))


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="freight-tower", description="Freight Control Tower CLI")
    p.add_argument("--db", default="freight.db", help="SQLite database path")
    sub = p.add_subparsers(dest="command", required=True)

    # init
    sub.add_parser("init", help="Initialise the database schema")

    # bootstrap
    bs = sub.add_parser("bootstrap", help="Bootstrap a tenant with an admin credential")
    bs.add_argument("--tenant", required=True)
    bs.add_argument("--name", required=True)
    bs.add_argument("--token", required=True, help="Admin token")

    # create-credential
    cc = sub.add_parser("create-credential", help="Create a new credential")
    cc.add_argument("--admin-token", required=True)
    cc.add_argument("--token", required=True)
    cc.add_argument("--role", required=True, choices=["viewer", "operator", "admin"])

    # create-shipment
    cs = sub.add_parser("create-shipment", help="Create a shipment")
    cs.add_argument("--token", required=True)
    cs.add_argument("--reference", required=True)

    # get-shipment
    gs = sub.add_parser("get-shipment", help="Get a shipment by id")
    gs.add_argument("--token", required=True)
    gs.add_argument("--id", required=True)

    # list-shipments
    ls_p = sub.add_parser("list-shipments", help="List shipments")
    ls_p.add_argument("--token", required=True)
    ls_p.add_argument("--status", default=None)

    # ingest-event
    ie = sub.add_parser("ingest-event", help="Ingest a carrier event")
    ie.add_argument("--token", required=True)
    ie.add_argument("--shipment-id", required=True)
    ie.add_argument("--event-id", required=True)
    ie.add_argument("--event-type", required=True)
    ie.add_argument("--event-time", type=float, default=None)
    ie.add_argument("--location", default=None)
    ie.add_argument("--details", default=None)

    # list-exceptions
    le = sub.add_parser("list-exceptions", help="List exceptions")
    le.add_argument("--token", required=True)
    le.add_argument("--status", default=None)
    le.add_argument("--severity", default=None)

    # mutate-exception
    me = sub.add_parser("mutate-exception", help="Mutate an exception")
    me.add_argument("--token", required=True)
    me.add_argument("--id", required=True)
    me.add_argument("--version", type=int, required=True)
    me.add_argument("--action", required=True, choices=["assign", "acknowledge", "note", "resolve"])
    me.add_argument("--assignee", default=None)
    me.add_argument("--note", default=None)
    me.add_argument("--actor", default=None)

    # audit
    au = sub.add_parser("audit", help="Show audit log")
    au.add_argument("--token", required=True)
    au.add_argument("--resource-type", default=None)
    au.add_argument("--resource-id", default=None)

    # set-sla-rule
    sr = sub.add_parser("set-sla-rule", help="Set an SLA escalation rule")
    sr.add_argument("--admin-token", required=True)
    sr.add_argument("--severity", required=True, choices=["P1", "P2", "P3"])
    sr.add_argument("--delay", type=float, required=True, help="Seconds before escalation")

    # tick
    tk = sub.add_parser("tick", help="Claim due SLA escalations")
    tk.add_argument("--limit", type=int, default=100)

    # worker
    wk = sub.add_parser("worker", help="Run a simple outbox delivery worker (prints payloads)")
    wk.add_argument("--worker-id", default=None)
    wk.add_argument("--interval", type=float, default=2.0)

    # list-deliveries
    ld = sub.add_parser("list-deliveries", help="List outbox deliveries")
    ld.add_argument("--token", required=True)
    ld.add_argument("--status", default=None)

    # replay-delivery
    rd = sub.add_parser("replay-delivery", help="Replay a dead-lettered delivery")
    rd.add_argument("--admin-token", required=True)
    rd.add_argument("--delivery-id", required=True)

    # export-snapshot
    ex = sub.add_parser("export-snapshot", help="Export tenant snapshot")
    ex.add_argument("--admin-token", required=True)
    ex.add_argument("--output", default="-", help="Output file path (- for stdout)")

    # import-snapshot
    im = sub.add_parser("import-snapshot", help="Import tenant snapshot")
    im.add_argument("--admin-token", required=True)
    im.add_argument("--input", required=True, help="Input file path")

    # serve
    sv = sub.add_parser("serve", help="Start the HTTP server")
    sv.add_argument("--host", default="127.0.0.1")
    sv.add_argument("--port", type=int, default=8080)

    return p


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    cmd = args.command

    if cmd == "init":
        s = SQLiteFreightStore(args.db)
        s.init_schema()
        s.close()
        print("Schema initialised.")
        return 0

    if cmd == "serve":
        s = _store(args)
        server = create_server(store=s, host=args.host, port=args.port)
        print(f"Serving on http://{args.host}:{args.port}")
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()
            s.close()
        return 0

    s = _store(args)
    try:
        if cmd == "bootstrap":
            t = s.bootstrap_tenant(args.tenant, args.name, args.token)
            _print({"id": t.id, "name": t.name})

        elif cmd == "create-credential":
            c = s.create_credential(args.admin_token, args.token, args.role)
            _print({"id": c.id, "role": c.role})

        elif cmd == "create-shipment":
            ship = s.create_shipment(args.token, args.reference)
            _print({"id": ship.id, "reference": ship.reference, "status": ship.status})

        elif cmd == "get-shipment":
            ship = s.get_shipment(args.token, args.id)
            _print({"id": ship.id, "reference": ship.reference, "status": ship.status,
                    "last_location": ship.last_location, "version": ship.version})

        elif cmd == "list-shipments":
            filters = {}
            if args.status:
                filters["status"] = args.status
            ships = s.list_shipments(args.token, **filters)
            _print([{"id": sh.id, "reference": sh.reference, "status": sh.status,
                     "last_location": sh.last_location, "version": sh.version} for sh in ships])

        elif cmd == "ingest-event":
            et = args.event_time or time.time()
            result = s.ingest_event(
                args.token,
                args.shipment_id,
                args.event_id,
                args.event_type,
                et,
                location=args.location,
                details=args.details,
            )
            _print({"id": result.id, "status": result.status, "last_location": result.last_location,
                    "version": result.version})

        elif cmd == "list-exceptions":
            filters = {}
            if args.status:
                filters["status"] = args.status
            if args.severity:
                filters["severity"] = args.severity
            excs = s.list_exceptions(args.token, **filters)
            _print([{"id": e.id, "shipment_id": e.shipment_id, "status": e.status,
                     "severity": e.severity, "version": e.version} for e in excs])

        elif cmd == "mutate-exception":
            kwargs: dict = {}
            if args.assignee:
                kwargs["assignee"] = args.assignee
            if args.note:
                kwargs["note"] = args.note
            result = s.mutate_exception(
                args.token, args.id, args.version, args.action, actor=args.actor, **kwargs
            )
            _print({"id": result.id, "status": result.status, "version": result.version})

        elif cmd == "audit":
            filters = {}
            if args.resource_type:
                filters["resource_type"] = args.resource_type
            if args.resource_id:
                filters["resource_id"] = args.resource_id
            entries = s.audit(args.token, **filters)
            _print([{"id": e.id, "action": e.action, "resource_type": e.resource_type,
                     "resource_id": e.resource_id, "actor": e.actor, "created_at": e.created_at} for e in entries])

        elif cmd == "set-sla-rule":
            s.set_sla_rule(args.admin_token, args.severity, args.delay)
            print(f"SLA rule set: {args.severity} => {args.delay}s")

        elif cmd == "tick":
            n = s.tick(time.time(), args.limit)
            print(f"Escalations enqueued: {n}")

        elif cmd == "worker":
            import uuid as _uuid
            wid = args.worker_id or str(_uuid.uuid4())
            print(f"Worker {wid} started. Press Ctrl-C to stop.")
            while True:
                try:
                    now = time.time()
                    delivery = s.claim_delivery(wid, now)
                    if delivery:
                        print(f"Delivering {delivery.id}: {json.dumps(delivery.payload)}")
                        s.complete_delivery(delivery.id, wid, time.time())
                    else:
                        time.sleep(args.interval)
                except KeyboardInterrupt:
                    break
            print("Worker stopped.")

        elif cmd == "list-deliveries":
            filters = {}
            if args.status:
                filters["status"] = args.status
            ds = s.list_deliveries(args.token, **filters)
            _print([{"id": d.id, "status": d.status, "attempts": d.attempts,
                     "idempotency_key": d.idempotency_key} for d in ds])

        elif cmd == "replay-delivery":
            d = s.replay_delivery(args.admin_token, args.delivery_id, time.time())
            _print({"id": d.id, "status": d.status})

        elif cmd == "export-snapshot":
            snap = s.export_snapshot(args.admin_token)
            if args.output == "-":
                print(json.dumps(snap, indent=2))
            else:
                with open(args.output, "w") as f:
                    json.dump(snap, f, indent=2)
                print(f"Snapshot written to {args.output}")

        elif cmd == "import-snapshot":
            with open(args.input) as f:
                snap = json.load(f)
            s.import_snapshot(args.admin_token, snap)
            print("Snapshot imported.")

    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    finally:
        s.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
