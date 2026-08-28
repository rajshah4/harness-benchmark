"""Command line entry point for the incident-operations system."""

from __future__ import annotations

import argparse
import json
import sys
import uuid

from .escalation import EscalationWorker
from .exceptions import IncidentNotFound, InvalidTransition, VersionConflict
from .models import Severity
from .sqlite_store import SQLiteIncidentStore
from .web import create_server


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m incident_ops.cli",
        description="Incident Operations Center CLI",
    )
    p.add_argument("--db", required=True, metavar="PATH", help="SQLite database path")

    sub = p.add_subparsers(dest="command", required=True)

    # serve ---------------------------------------------------------------
    srv = sub.add_parser("serve", help="Start the HTTP server")
    srv.add_argument("--host", default="127.0.0.1")
    srv.add_argument("--port", type=int, default=8080)

    # ingest --------------------------------------------------------------
    ingest = sub.add_parser("ingest", help="Ingest a JSON alert")
    ingest.add_argument(
        "alert_json",
        metavar="JSON_ALERT",
        help='JSON object with fingerprint, title, severity, and optional fields',
    )

    # list ----------------------------------------------------------------
    ls = sub.add_parser("list", help="List incidents")
    ls.add_argument("--status", help="Filter by status")
    ls.add_argument("--severity", help="Filter by severity")
    ls.add_argument("--owner", help="Filter by owner")

    # update --------------------------------------------------------------
    upd = sub.add_parser("update", help="Update an incident")
    upd.add_argument("incident_id", metavar="INCIDENT_ID")
    upd.add_argument(
        "update_json",
        metavar="JSON_UPDATE",
        help='JSON with expected_version and optional owner/status fields',
    )

    # escalate ------------------------------------------------------------
    esc = sub.add_parser("escalate", help="Run the escalation worker")
    esc.add_argument("--worker-id", default=f"cli-worker-{uuid.uuid4().hex[:8]}")
    esc.add_argument("--max-incidents", type=int, default=None)

    # export --------------------------------------------------------------
    sub.add_parser("export", help="Export all data as newline-delimited JSON")

    # import --------------------------------------------------------------
    sub.add_parser("import", help="Import newline-delimited JSON from stdin")

    return p


def _emit(obj: object) -> None:
    print(json.dumps(obj))


def _err(msg: str) -> int:
    print(json.dumps({"error": msg}), file=sys.stderr)
    return 1


def main(argv: list[str] | None = None) -> int:
    p = _build_parser()
    args = p.parse_args(argv)
    store = SQLiteIncidentStore(args.db)

    if args.command == "serve":
        server = create_server(store, args.host, args.port)
        print(
            json.dumps(
                {"listening": f"http://{args.host}:{args.port}"}
            )
        )
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()
        return 0

    if args.command == "ingest":
        try:
            payload = json.loads(args.alert_json)
        except json.JSONDecodeError as exc:
            return _err(f"invalid JSON: {exc}")
        if not isinstance(payload, dict):
            return _err("alert must be a JSON object")
        try:
            incident, created = store.ingest_alert(
                fingerprint=payload["fingerprint"],
                title=payload["title"],
                severity=payload["severity"],
                source=payload.get("source", "unknown"),
                details=payload.get("details"),
                idempotency_key=payload.get("idempotency_key"),
            )
        except KeyError as exc:
            return _err(f"missing field: {exc}")
        except ValueError as exc:
            return _err(str(exc))
        _emit({"created": created, "incident": _incident_dict(incident)})
        return 0

    if args.command == "list":
        try:
            items = store.list(
                status=args.status,
                severity=args.severity,
                owner=args.owner,
            )
        except ValueError as exc:
            return _err(str(exc))
        for item in items:
            _emit(_incident_dict(item))
        return 0

    if args.command == "update":
        try:
            payload = json.loads(args.update_json)
        except json.JSONDecodeError as exc:
            return _err(f"invalid JSON: {exc}")
        if not isinstance(payload, dict):
            return _err("update must be a JSON object")
        try:
            expected_version = int(payload["expected_version"])
        except (KeyError, TypeError, ValueError) as exc:
            return _err(f"expected_version is required: {exc}")
        try:
            incident = store.update(
                incident_id=args.incident_id,
                expected_version=expected_version,
                owner=payload.get("owner"),
                status=payload.get("status"),
                idempotency_key=payload.get("idempotency_key"),
            )
        except IncidentNotFound:
            return _err(f"incident not found: {args.incident_id!r}")
        except VersionConflict as exc:
            return _err(f"version conflict: {exc}")
        except InvalidTransition as exc:
            return _err(f"invalid transition: {exc}")
        except ValueError as exc:
            return _err(str(exc))
        _emit(_incident_dict(incident))
        return 0

    if args.command == "escalate":
        worker = EscalationWorker(store, args.worker_id)
        escalated = worker.run_until_idle(max_incidents=args.max_incidents)
        _emit({"escalated": len(escalated), "incidents": [_incident_dict(i) for i in escalated]})
        return 0

    if args.command == "export":
        _export(store)
        return 0

    if args.command == "import":
        return _import(store)

    return 0  # unreachable — argparse required=True


# ---------------------------------------------------------------------------
# Serialisation helpers
# ---------------------------------------------------------------------------

def _incident_dict(incident) -> dict:
    return {
        "id": incident.id,
        "fingerprint": incident.fingerprint,
        "title": incident.title,
        "severity": incident.severity.value,
        "status": incident.status.value,
        "owner": incident.owner,
        "alert_count": incident.alert_count,
        "version": incident.version,
        "escalation_level": incident.escalation_level,
        "sla_deadline": incident.sla_deadline,
        "created_at": incident.created_at,
        "updated_at": incident.updated_at,
    }


def _event_dict(event) -> dict:
    return {
        "id": event.id,
        "incident_id": event.incident_id,
        "type": event.type,
        "timestamp": event.timestamp,
        "details": event.details,
    }


def _export(store: SQLiteIncidentStore) -> None:
    """Write newline-delimited JSON to stdout (incidents then their events)."""
    for incident in store.list():
        print(json.dumps({"type": "incident", "data": _incident_dict(incident)}))
        for event in store.events(incident.id):
            print(json.dumps({"type": "event", "data": _event_dict(event)}))


def _import(store: SQLiteIncidentStore) -> int:
    """Read newline-delimited JSON from stdin and upsert records."""
    conn = store._conn()
    errors = 0
    for lineno, raw in enumerate(sys.stdin, start=1):
        raw = raw.strip()
        if not raw:
            continue
        try:
            record = json.loads(raw)
        except json.JSONDecodeError as exc:
            print(json.dumps({"error": f"line {lineno}: invalid JSON: {exc}"}), file=sys.stderr)
            errors += 1
            continue

        rtype = record.get("type")
        data = record.get("data", {})

        if rtype == "incident":
            try:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute(
                    """
                    INSERT OR IGNORE INTO incidents
                        (id, fingerprint, title, severity, status, owner,
                         alert_count, version, escalation_level,
                         sla_deadline, created_at, updated_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    [
                        data["id"], data["fingerprint"], data["title"],
                        data["severity"], data["status"], data.get("owner"),
                        data.get("alert_count", 1), data.get("version", 1),
                        data.get("escalation_level", 0),
                        data.get("sla_deadline", 0.0),
                        data["created_at"], data["updated_at"],
                    ],
                )
                conn.execute("COMMIT")
            except Exception as exc:
                conn.execute("ROLLBACK")
                print(
                    json.dumps({"error": f"line {lineno}: could not import incident: {exc}"}),
                    file=sys.stderr,
                )
                errors += 1

        elif rtype == "event":
            try:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute(
                    """
                    INSERT OR IGNORE INTO audit_events
                        (id, incident_id, type, timestamp, details)
                    VALUES (?,?,?,?,?)
                    """,
                    [
                        data["id"], data["incident_id"], data["type"],
                        data["timestamp"], json.dumps(data.get("details", {})),
                    ],
                )
                conn.execute("COMMIT")
            except Exception as exc:
                conn.execute("ROLLBACK")
                print(
                    json.dumps({"error": f"line {lineno}: could not import event: {exc}"}),
                    file=sys.stderr,
                )
                errors += 1
        else:
            print(
                json.dumps({"error": f"line {lineno}: unknown record type {rtype!r}"}),
                file=sys.stderr,
            )
            errors += 1

    return 0 if errors == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())

