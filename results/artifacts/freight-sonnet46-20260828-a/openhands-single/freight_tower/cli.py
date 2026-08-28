"""CLI entry point for the freight control tower."""
from __future__ import annotations

import argparse
import json
import sys
import time

from .sqlite_store import SQLiteFreightStore
from .web import create_server


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------

def _add_db(p: argparse.ArgumentParser) -> None:
    p.add_argument("--db", default="freight.db", help="SQLite database path")


def _add_token(p: argparse.ArgumentParser) -> None:
    p.add_argument("--token", required=True, help="Bearer token")


def _add_admin(p: argparse.ArgumentParser) -> None:
    p.add_argument("--admin-token", required=True, dest="admin_token",
                   help="Admin bearer token")


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(
        prog="freight-tower",
        description="Freight exception control tower",
    )
    sub = root.add_subparsers(dest="command", required=True)

    # init
    init = sub.add_parser("init", help="Initialise the database schema")
    _add_db(init)

    # bootstrap
    bs = sub.add_parser("bootstrap", help="Create a tenant and admin credential")
    _add_db(bs)
    bs.add_argument("--tenant-id", required=True, dest="tenant_id")
    bs.add_argument("--name", required=True)
    _add_admin(bs)

    # credential
    cred = sub.add_parser("credential", help="Add a credential to a tenant")
    _add_db(cred)
    _add_admin(cred)
    cred.add_argument("--token", required=True)
    cred.add_argument("--role", required=True, choices=["viewer", "operator", "admin"])

    # serve
    serve = sub.add_parser("serve", help="Start the HTTP server")
    _add_db(serve)
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8080)

    # create-shipment
    cs = sub.add_parser("create-shipment", help="Create a shipment")
    _add_db(cs)
    _add_token(cs)
    cs.add_argument("--reference", required=True)

    # list-shipments
    ls_cmd = sub.add_parser("list-shipments", help="List shipments")
    _add_db(ls_cmd)
    _add_token(ls_cmd)
    ls_cmd.add_argument("--status", default=None)
    ls_cmd.add_argument("--reference", default=None)

    # ingest
    ing = sub.add_parser("ingest", help="Ingest a carrier event")
    _add_db(ing)
    _add_token(ing)
    ing.add_argument("--shipment-id", required=True, dest="shipment_id")
    ing.add_argument("--event-id", required=True, dest="event_id")
    ing.add_argument("--event-type", required=True, dest="event_type",
                     choices=["picked_up", "in_transit", "delayed",
                               "delivered", "cancelled"])
    ing.add_argument("--event-time", required=True, type=float, dest="event_time")
    ing.add_argument("--location", default=None)
    ing.add_argument("--details", default=None)

    # list-exceptions
    le = sub.add_parser("list-exceptions", help="List exceptions")
    _add_db(le)
    _add_token(le)
    le.add_argument("--status", default=None)
    le.add_argument("--severity", default=None)
    le.add_argument("--assignee", default=None)

    # mutate-exception
    me = sub.add_parser("mutate-exception", help="Mutate an exception")
    _add_db(me)
    _add_token(me)
    me.add_argument("--exception-id", required=True, dest="exception_id")
    me.add_argument("--version", required=True, type=int, dest="expected_version")
    me.add_argument("--action", required=True,
                    choices=["assign", "acknowledge", "add_note", "resolve"])
    me.add_argument("--actor", default=None)
    me.add_argument("--assignee", default=None)
    me.add_argument("--note", default=None)

    # audit
    aud = sub.add_parser("audit", help="List audit entries")
    _add_db(aud)
    _add_token(aud)
    aud.add_argument("--resource-type", default=None, dest="resource_type")
    aud.add_argument("--resource-id", default=None, dest="resource_id")
    aud.add_argument("--actor", default=None)
    aud.add_argument("--action", default=None)

    # set-sla-rule
    sla = sub.add_parser("set-sla-rule", help="Configure an SLA escalation rule")
    _add_db(sla)
    _add_admin(sla)
    sla.add_argument("--severity", required=True)
    sla.add_argument("--delay-seconds", required=True, type=int, dest="delay_seconds")

    # tick
    tick = sub.add_parser("tick", help="Trigger SLA escalation check")
    _add_db(tick)
    tick.add_argument("--now", type=float, default=None,
                      help="Unix timestamp (default: current time)")
    tick.add_argument("--limit", type=int, default=100)

    # worker
    wrk = sub.add_parser("worker", help="Run a simple delivery worker loop")
    _add_db(wrk)
    wrk.add_argument("--worker-id", default="cli-worker", dest="worker_id")
    wrk.add_argument("--interval", type=float, default=2.0,
                     help="Poll interval in seconds")

    # export-snapshot
    exp = sub.add_parser("export-snapshot", help="Export a tenant snapshot to JSON")
    _add_db(exp)
    _add_admin(exp)
    exp.add_argument("--out", default=None, help="Output file (default: stdout)")

    # import-snapshot
    imp = sub.add_parser("import-snapshot", help="Atomically restore a tenant snapshot")
    _add_db(imp)
    _add_admin(imp)
    imp.add_argument("--file", required=True, help="Snapshot JSON file")

    return root


# ---------------------------------------------------------------------------
# Serialisation helper
# ---------------------------------------------------------------------------

def _dump(obj: object) -> str:
    import dataclasses

    def _convert(o: object) -> object:
        if dataclasses.is_dataclass(o) and not isinstance(o, type):
            return {
                f.name: _convert(getattr(o, f.name))
                for f in dataclasses.fields(o)  # type: ignore[arg-type]
            }
        if isinstance(o, list):
            return [_convert(i) for i in o]
        if hasattr(o, "value"):
            return o.value
        return o

    return json.dumps(_convert(obj), indent=2, default=str)


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------

def _print_err(msg: str) -> None:
    print(f"Error: {msg}", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    cmd = args.command
    db = getattr(args, "db", "freight.db")
    store = SQLiteFreightStore(db)

    try:
        if cmd == "init":
            store.init_schema()
            print("Schema initialised.")

        elif cmd == "bootstrap":
            result = store.bootstrap_tenant(
                args.tenant_id, args.name, args.admin_token
            )
            print(_dump(result))

        elif cmd == "credential":
            result = store.create_credential(
                args.admin_token, args.token, args.role
            )
            print(_dump(result))

        elif cmd == "serve":
            store.init_schema()
            server = create_server(store, args.host, args.port)
            print(f"Serving on http://{args.host}:{args.port}", file=sys.stderr)
            try:
                server.serve_forever()
            except KeyboardInterrupt:
                pass
            finally:
                server.server_close()

        elif cmd == "create-shipment":
            result = store.create_shipment(args.token, args.reference)
            print(_dump(result))

        elif cmd == "list-shipments":
            results = store.list_shipments(
                args.token, status=args.status, reference=args.reference
            )
            print(_dump(results))

        elif cmd == "ingest":
            result = store.ingest_event(
                args.token,
                args.shipment_id,
                args.event_id,
                args.event_type,
                args.event_time,
                location=args.location,
                details=args.details,
            )
            print(_dump(result))

        elif cmd == "list-exceptions":
            results = store.list_exceptions(
                args.token,
                status=args.status,
                severity=args.severity,
                assignee=args.assignee,
            )
            print(_dump(results))

        elif cmd == "mutate-exception":
            kwargs: dict = {}
            if args.assignee:
                kwargs["assignee"] = args.assignee
            if args.note:
                kwargs["note"] = args.note
            result = store.mutate_exception(
                args.token,
                args.exception_id,
                args.expected_version,
                args.action,
                actor=args.actor,
                **kwargs,
            )
            print(_dump(result))

        elif cmd == "audit":
            results = store.audit(
                args.token,
                resource_type=args.resource_type,
                resource_id=args.resource_id,
                actor=args.actor,
                action=args.action,
            )
            print(_dump(results))

        elif cmd == "set-sla-rule":
            result = store.set_sla_rule(
                args.admin_token, args.severity, args.delay_seconds
            )
            print(_dump(result))

        elif cmd == "tick":
            now = args.now if args.now is not None else time.time()
            n = store.tick(now, args.limit)
            print(f"Enqueued {n} escalation(s).")

        elif cmd == "worker":
            print(
                f"Worker '{args.worker_id}' polling every {args.interval}s "
                "(Ctrl-C to stop)",
                file=sys.stderr,
            )
            while True:
                now = time.time()
                d = store.claim_delivery(args.worker_id, now)
                if d is not None:
                    print(
                        f"Processing delivery {d.id} type={d.delivery_type}",
                        file=sys.stderr,
                    )
                    try:
                        # Workers simply acknowledge; real processors would act here
                        store.complete_delivery(d.id, args.worker_id, time.time())
                        print(f"Completed {d.id}", file=sys.stderr)
                    except Exception as exc:
                        store.fail_delivery(
                            d.id, args.worker_id, str(exc), time.time()
                        )
                        print(f"Failed {d.id}: {exc}", file=sys.stderr)
                else:
                    time.sleep(args.interval)

        elif cmd == "export-snapshot":
            snap = store.export_snapshot(args.admin_token)
            text = json.dumps(snap, indent=2, default=str)
            if args.out:
                with open(args.out, "w") as fh:
                    fh.write(text)
                print(f"Snapshot written to {args.out}")
            else:
                print(text)

        elif cmd == "import-snapshot":
            with open(args.file) as fh:
                snap = json.load(fh)
            store.import_snapshot(args.admin_token, snap)
            print("Snapshot imported successfully.")

    except Exception as exc:
        _print_err(str(exc))
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
