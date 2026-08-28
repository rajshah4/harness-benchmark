"""Command line entry point for the incident operations center."""

from __future__ import annotations

import argparse
import json
import sys

from .service import IncidentService
from .web import create_server


def _json_out(value: object) -> None:
    """Print a JSON value to stdout."""
    from dataclasses import asdict
    from .models import Incident, AuditEvent

    def _default(obj: object) -> object:
        if hasattr(obj, "value"):
            return obj.value
        if hasattr(obj, "__dataclass_fields__"):
            return asdict(obj)  # type: ignore[arg-type]
        raise TypeError(f"cannot encode {type(obj).__name__}")

    print(json.dumps(value, default=_default))


def _incident_dict(incident: object) -> dict:
    from dataclasses import asdict
    d = asdict(incident)  # type: ignore[arg-type]
    d["severity"] = incident.severity.value  # type: ignore[attr-defined]
    d["status"] = incident.status.value  # type: ignore[attr-defined]
    return d


def _event_dict(event: object) -> dict:
    return {
        "id": event.id,  # type: ignore[attr-defined]
        "incident_id": event.incident_id,  # type: ignore[attr-defined]
        "type": event.type,  # type: ignore[attr-defined]
        "timestamp": event.timestamp,  # type: ignore[attr-defined]
        "details": event.details,  # type: ignore[attr-defined]
    }


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(prog="python -m incident_ops.cli")
    result.add_argument("--db", metavar="PATH", required=False, help="SQLite database path")

    subcommands = result.add_subparsers(dest="command", required=True)

    # serve
    serve = subcommands.add_parser("serve", help="Start the HTTP server")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8080)

    # ingest
    ingest = subcommands.add_parser("ingest", help="Ingest an alert (JSON)")
    ingest.add_argument("alert_json", metavar="JSON_ALERT")

    # list
    lst = subcommands.add_parser("list", help="List incidents")
    lst.add_argument("--status", default=None)
    lst.add_argument("--severity", default=None)
    lst.add_argument("--owner", default=None)

    # update
    upd = subcommands.add_parser("update", help="Update an incident")
    upd.add_argument("incident_id", metavar="INCIDENT_ID")
    upd.add_argument("update_json", metavar="JSON_UPDATE")

    # escalate
    esc = subcommands.add_parser("escalate", help="Run escalation worker")
    esc.add_argument("--worker-id", default="cli-worker")
    esc.add_argument("--max-incidents", type=int, default=None)

    # export
    subcommands.add_parser("export", help="Export incidents to NDJSON on stdout")

    # import
    subcommands.add_parser("import", help="Import incidents from NDJSON on stdin")

    return result


def main() -> int:
    args = parser().parse_args()

    # Commands that require a database
    if args.command in {"serve", "ingest", "list", "update", "escalate", "export", "import"}:
        if args.db:
            from .sqlite_store import SQLiteIncidentStore
            store = SQLiteIncidentStore(args.db)
        else:
            store = None

    if args.command == "serve":
        if store is not None:
            server = create_server(store=store, host=getattr(args, "host", "127.0.0.1"), port=args.port)
        else:
            server = create_server(service=IncidentService(), host=getattr(args, "host", "127.0.0.1"), port=args.port)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()
        return 0

    if args.command == "ingest":
        if store is None:
            print("error: --db is required for ingest", file=sys.stderr)
            return 1
        try:
            payload = json.loads(args.alert_json)
        except json.JSONDecodeError as exc:
            print(f"error: invalid JSON: {exc}", file=sys.stderr)
            return 1
        try:
            incident, created = store.ingest_alert(
                fingerprint=payload.get("fingerprint", ""),
                title=payload.get("title", ""),
                severity=payload.get("severity", "P3"),
                source=payload.get("source", "unknown"),
                details=payload.get("details"),
                idempotency_key=payload.get("idempotency_key"),
            )
        except Exception as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        _json_out({**_incident_dict(incident), "created": created})
        return 0

    if args.command == "list":
        if store is None:
            print("error: --db is required for list", file=sys.stderr)
            return 1
        try:
            incidents = store.list(
                status=args.status,
                severity=args.severity,
                owner=args.owner,
            )
        except Exception as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        for inc in incidents:
            _json_out(_incident_dict(inc))
        return 0

    if args.command == "update":
        if store is None:
            print("error: --db is required for update", file=sys.stderr)
            return 1
        try:
            payload = json.loads(args.update_json)
        except json.JSONDecodeError as exc:
            print(f"error: invalid JSON: {exc}", file=sys.stderr)
            return 1
        expected_version = payload.get("expected_version")
        if expected_version is None:
            print("error: expected_version is required", file=sys.stderr)
            return 1
        try:
            incident = store.update(
                incident_id=args.incident_id,
                expected_version=expected_version,
                owner=payload.get("owner"),
                status=payload.get("status"),
                idempotency_key=payload.get("idempotency_key"),
            )
        except Exception as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        _json_out(_incident_dict(incident))
        return 0

    if args.command == "escalate":
        if store is None:
            print("error: --db is required for escalate", file=sys.stderr)
            return 1
        from .escalation import EscalationWorker
        worker = EscalationWorker(store, args.worker_id)
        incidents = worker.run_until_idle(max_incidents=args.max_incidents)
        for inc in incidents:
            _json_out(_incident_dict(inc))
        return 0

    if args.command == "export":
        if store is None:
            print("error: --db is required for export", file=sys.stderr)
            return 1
        incidents = store.list()
        for inc in incidents:
            d = _incident_dict(inc)
            d["_events"] = [_event_dict(e) for e in store.events(inc.id)]
            print(json.dumps(d))
        return 0

    if args.command == "import":
        if store is None:
            print("error: --db is required for import", file=sys.stderr)
            return 1
        import sqlite3
        conn = sqlite3.connect(store._db_path, check_same_thread=False, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        imported = 0
        for line_no, line in enumerate(sys.stdin, 1):
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                print(f"error on line {line_no}: {exc}", file=sys.stderr)
                conn.close()
                return 1
            events = record.pop("_events", [])
            inc_id = record.get("id")
            if not inc_id:
                print(f"error on line {line_no}: missing id", file=sys.stderr)
                conn.close()
                return 1
            with conn:
                existing = conn.execute(
                    "SELECT id FROM incidents WHERE id=?", (inc_id,)
                ).fetchone()
                if existing is None:
                    conn.execute(
                        """
                        INSERT OR IGNORE INTO incidents
                            (id, fingerprint, title, severity, status, owner,
                             alert_count, version, escalation_level,
                             created_at, updated_at, sla_deadline)
                        VALUES (:id,:fingerprint,:title,:severity,:status,:owner,
                                :alert_count,:version,:escalation_level,
                                :created_at,:updated_at,:sla_deadline)
                        """,
                        {
                            "id": inc_id,
                            "fingerprint": record.get("fingerprint", ""),
                            "title": record.get("title", ""),
                            "severity": record.get("severity", "P3"),
                            "status": record.get("status", "open"),
                            "owner": record.get("owner"),
                            "alert_count": record.get("alert_count", 1),
                            "version": record.get("version", 1),
                            "escalation_level": record.get("escalation_level", 0),
                            "created_at": record.get("created_at", 0),
                            "updated_at": record.get("updated_at", 0),
                            "sla_deadline": record.get("sla_deadline", 0),
                        },
                    )
                    imported += 1
                for ev in events:
                    conn.execute(
                        "INSERT OR IGNORE INTO audit_events (id, incident_id, type, timestamp, details) VALUES (?,?,?,?,?)",
                        (
                            ev.get("id", ""),
                            inc_id,
                            ev.get("type", ""),
                            ev.get("timestamp", 0),
                            json.dumps(ev.get("details", {})),
                        ),
                    )
        conn.close()
        _json_out({"imported": imported})
        return 0

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
