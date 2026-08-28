"""Command line entry point for the incident operations system."""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from dataclasses import asdict

from .escalation import EscalationWorker
from .models import AuditEvent, Incident
from .sqlite_store import SQLiteIncidentStore
from .web import create_server


def _incident_dict(incident: Incident) -> dict:
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
        "created_at": incident.created_at,
        "updated_at": incident.updated_at,
        "sla_deadline": incident.sla_deadline,
    }


def _event_dict(event: AuditEvent) -> dict:
    return {
        "id": event.id,
        "incident_id": event.incident_id,
        "type": event.type,
        "timestamp": event.timestamp,
        "details": event.details,
    }


def _print_json(value: object) -> None:
    print(json.dumps(value))


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m incident_ops.cli")
    p.add_argument("--db", metavar="PATH", required=True, help="SQLite database path")

    sub = p.add_subparsers(dest="command", required=True)

    serve_p = sub.add_parser("serve")
    serve_p.add_argument("--host", default="127.0.0.1")
    serve_p.add_argument("--port", type=int, default=8080)

    ingest_p = sub.add_parser("ingest")
    ingest_p.add_argument("alert_json", metavar="JSON_ALERT")

    list_p = sub.add_parser("list")
    list_p.add_argument("--status")
    list_p.add_argument("--severity")
    list_p.add_argument("--owner")

    update_p = sub.add_parser("update")
    update_p.add_argument("incident_id", metavar="INCIDENT_ID")
    update_p.add_argument("update_json", metavar="JSON_UPDATE")

    escalate_p = sub.add_parser("escalate")
    escalate_p.add_argument("--worker-id", default=None)
    escalate_p.add_argument("--max-incidents", type=int, default=None)

    sub.add_parser("export")
    sub.add_parser("import")

    return p


def main() -> int:
    args = parser().parse_args()
    store = SQLiteIncidentStore(args.db)

    if args.command == "serve":
        server = create_server(store, args.host, args.port)
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
            print(f"error: invalid JSON: {exc}", file=sys.stderr)
            return 1
        try:
            incident, created = store.ingest_alert(
                fingerprint=payload["fingerprint"],
                title=payload["title"],
                severity=payload["severity"],
                source=payload.get("source", "unknown"),
                details=payload.get("details"),
                idempotency_key=payload.get("idempotency_key"),
            )
        except Exception as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        _print_json({"incident": _incident_dict(incident), "created": created})
        return 0

    if args.command == "list":
        incidents = store.list(
            status=args.status, severity=args.severity, owner=args.owner
        )
        for inc in incidents:
            _print_json(_incident_dict(inc))
        return 0

    if args.command == "update":
        try:
            payload = json.loads(args.update_json)
        except json.JSONDecodeError as exc:
            print(f"error: invalid JSON: {exc}", file=sys.stderr)
            return 1
        try:
            incident = store.update(
                args.incident_id,
                expected_version=int(payload["expected_version"]),
                owner=payload.get("owner"),
                status=payload.get("status"),
                idempotency_key=payload.get("idempotency_key"),
            )
        except Exception as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        _print_json(_incident_dict(incident))
        return 0

    if args.command == "escalate":
        worker_id = args.worker_id or str(uuid.uuid4())
        worker = EscalationWorker(store, worker_id=worker_id)
        completed = worker.run_until_idle(max_incidents=args.max_incidents)
        for inc in completed:
            _print_json(_incident_dict(inc))
        return 0

    if args.command == "export":
        for inc in store.list():
            print(json.dumps({"type": "incident", "data": _incident_dict(inc)}))
            for event in store.events(inc.id):
                print(json.dumps({"type": "audit_event", "data": _event_dict(event)}))
        return 0

    if args.command == "import":
        conn = store._conn()
        conn.execute("BEGIN IMMEDIATE")
        try:
            for lineno, raw_line in enumerate(sys.stdin, 1):
                raw_line = raw_line.strip()
                if not raw_line:
                    continue
                try:
                    record = json.loads(raw_line)
                    rec_type = record["type"]
                    data = record["data"]
                except (json.JSONDecodeError, KeyError) as exc:
                    print(f"error: line {lineno}: {exc}", file=sys.stderr)
                    conn.execute("ROLLBACK")
                    return 1

                if rec_type == "incident":
                    conn.execute(
                        """INSERT OR IGNORE INTO incidents
                           (id, fingerprint, title, severity, status, owner,
                            alert_count, version, escalation_level,
                            created_at, updated_at, sla_deadline)
                           VALUES (:id,:fingerprint,:title,:severity,:status,:owner,
                                   :alert_count,:version,:escalation_level,
                                   :created_at,:updated_at,:sla_deadline)""",
                        data,
                    )
                elif rec_type == "audit_event":
                    conn.execute(
                        """INSERT OR IGNORE INTO audit_events
                           (id, incident_id, type, timestamp, details)
                           VALUES (:id, :incident_id, :type, :timestamp, :details)""",
                        {**data, "details": json.dumps(data.get("details", {}))},
                    )
                else:
                    print(
                        f"error: line {lineno}: unknown record type {rec_type!r}",
                        file=sys.stderr,
                    )
                    conn.execute("ROLLBACK")
                    return 1

            conn.execute("COMMIT")
        except Exception as exc:
            conn.execute("ROLLBACK")
            print(f"error: {exc}", file=sys.stderr)
            return 1
        return 0

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

