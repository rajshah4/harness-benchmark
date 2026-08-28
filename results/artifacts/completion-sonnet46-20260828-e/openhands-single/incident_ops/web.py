"""Standard-library HTTP server for the incident operations interface."""

from __future__ import annotations

import json
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .escalation import EscalationWorker
from .exceptions import IncidentNotFound, InvalidTransition, VersionConflict
from .models import AuditEvent, Incident, Severity
from .service import IncidentService
from .sqlite_store import SQLiteIncidentStore

STATIC_DIR = Path(__file__).with_name("static")


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
        "sla_deadline": incident.sla_deadline,
        "created_at": incident.created_at,
        "updated_at": incident.updated_at,
    }


def _event_dict(event: AuditEvent) -> dict:
    return {
        "id": event.id,
        "incident_id": event.incident_id,
        "type": event.type,
        "timestamp": event.timestamp,
        "details": event.details,
    }


def json_value(value: object) -> object:
    if hasattr(value, "value"):
        return value.value
    raise TypeError(f"cannot encode {type(value).__name__}")


def create_server(
    store: SQLiteIncidentStore | IncidentService | None = None,
    host: str = "127.0.0.1",
    port: int = 8080,
    service: IncidentService | None = None,
) -> ThreadingHTTPServer:
    """Construct and bind a server without starting its request loop.

    Pass a :class:`~incident_ops.sqlite_store.SQLiteIncidentStore` as the first
    argument for durable storage.  An :class:`~incident_ops.service.IncidentService`
    is accepted for backward compatibility with the in-memory API.
    """
    # Normalise: decide which backend objects are available.
    sqlite_store: SQLiteIncidentStore | None = None
    mem_service: IncidentService | None = service

    if isinstance(store, SQLiteIncidentStore):
        sqlite_store = store
    elif isinstance(store, IncidentService):
        mem_service = store
    # If store is None and service was supplied via keyword, mem_service is set.

    class Handler(BaseHTTPRequestHandler):
        # ------------------------------------------------------------------ #
        # GET routing
        # ------------------------------------------------------------------ #
        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            path = parsed.path.rstrip("/") or "/"

            if path in {"/", "/index.html"}:
                self._file("index.html", "text/html; charset=utf-8")
                return
            if path == "/app.js":
                self._file("app.js", "text/javascript; charset=utf-8")
                return
            if path == "/styles.css":
                self._file("styles.css", "text/css; charset=utf-8")
                return

            if path == "/api/incidents":
                self._handle_list(parsed)
                return
            if path.startswith("/api/incidents/"):
                incident_id = path[len("/api/incidents/"):]
                self._handle_detail(incident_id)
                return
            if path == "/api/summary":
                self._handle_summary()
                return

            self.send_error(404)

        # ------------------------------------------------------------------ #
        # POST routing
        # ------------------------------------------------------------------ #
        def do_POST(self) -> None:
            parsed = urlparse(self.path)
            path = parsed.path.rstrip("/") or "/"

            if path == "/api/alerts":
                self._handle_ingest()
                return
            if path == "/api/incidents":
                # Backward-compatible creation endpoint.
                self._handle_legacy_create()
                return
            if path == "/api/escalations/run":
                self._handle_escalation_run()
                return

            self.send_error(404)

        # ------------------------------------------------------------------ #
        # PATCH routing
        # ------------------------------------------------------------------ #
        def do_PATCH(self) -> None:
            parsed = urlparse(self.path)
            path = parsed.path.rstrip("/") or "/"
            if path.startswith("/api/incidents/"):
                incident_id = path[len("/api/incidents/"):]
                self._handle_update(incident_id)
                return
            self.send_error(404)

        # ------------------------------------------------------------------ #
        # Route handlers
        # ------------------------------------------------------------------ #
        def _handle_list(self, parsed) -> None:
            qs = parse_qs(parsed.query)
            status = (qs.get("status") or [None])[0]
            severity = (qs.get("severity") or [None])[0]
            owner = (qs.get("owner") or [None])[0]

            if sqlite_store is not None:
                try:
                    items = sqlite_store.list(
                        status=status, severity=severity, owner=owner
                    )
                except ValueError as exc:
                    self._json(400, {"error": str(exc)})
                    return
                self._json(200, [_incident_dict(i) for i in items])
            elif mem_service is not None:
                self._json(200, [asdict(i) for i in mem_service.list()])
            else:
                self._json(200, [])

        def _handle_detail(self, incident_id: str) -> None:
            if sqlite_store is None:
                self.send_error(404)
                return
            incident = sqlite_store.get(incident_id)
            if incident is None:
                self._json(404, {"error": f"incident {incident_id!r} not found"})
                return
            data = _incident_dict(incident)
            data["events"] = [_event_dict(e) for e in sqlite_store.events(incident_id)]
            self._json(200, data)

        def _handle_summary(self) -> None:
            if sqlite_store is None:
                self._json(200, {"total": 0})
                return
            incidents = sqlite_store.list()
            summary: dict[str, int] = {"total": len(incidents)}
            for inc in incidents:
                summary[inc.status.value] = summary.get(inc.status.value, 0) + 1
                summary[inc.severity.value] = summary.get(inc.severity.value, 0) + 1
            self._json(200, summary)

        def _handle_ingest(self) -> None:
            if sqlite_store is None:
                self._json(503, {"error": "no durable store configured"})
                return
            try:
                payload = self._body()
                fingerprint = payload["fingerprint"]
                title = payload["title"]
                severity = payload["severity"]
                Severity(severity)  # validate
            except json.JSONDecodeError as exc:
                self._json(400, {"error": f"invalid JSON: {exc}"})
                return
            except KeyError as exc:
                self._json(400, {"error": f"missing field: {exc}"})
                return
            except ValueError as exc:
                self._json(400, {"error": str(exc)})
                return

            try:
                incident, created = sqlite_store.ingest_alert(
                    fingerprint=fingerprint,
                    title=title,
                    severity=severity,
                    source=payload.get("source", "unknown"),
                    details=payload.get("details"),
                    idempotency_key=payload.get("idempotency_key"),
                )
            except ValueError as exc:
                self._json(400, {"error": str(exc)})
                return
            self._json(201 if created else 200, _incident_dict(incident))

        def _handle_legacy_create(self) -> None:
            if sqlite_store is not None:
                # Map the old-style create into ingest_alert.
                try:
                    payload = self._body()
                    title = payload["title"]
                    severity = payload["severity"]
                    Severity(severity)
                    fingerprint = f"legacy:{title}:{severity}"
                    incident, created = sqlite_store.ingest_alert(
                        fingerprint=fingerprint, title=title, severity=severity
                    )
                except json.JSONDecodeError as exc:
                    self._json(400, {"error": f"invalid JSON: {exc}"})
                    return
                except (KeyError, ValueError) as exc:
                    self._json(400, {"error": str(exc)})
                    return
                self._json(201, _incident_dict(incident))
            elif mem_service is not None:
                try:
                    payload = self._body()
                    incident = mem_service.create(payload["title"], payload["severity"])
                except (KeyError, ValueError, json.JSONDecodeError) as exc:
                    self._json(400, {"error": str(exc)})
                    return
                self._json(201, asdict(incident))
            else:
                self._json(503, {"error": "no store configured"})

        def _handle_update(self, incident_id: str) -> None:
            if sqlite_store is None:
                self.send_error(404)
                return
            try:
                payload = self._body()
                expected_version = int(payload["expected_version"])
            except json.JSONDecodeError as exc:
                self._json(400, {"error": f"invalid JSON: {exc}"})
                return
            except (KeyError, TypeError, ValueError) as exc:
                self._json(400, {"error": f"missing or invalid field: {exc}"})
                return

            owner = payload.get("owner")
            status = payload.get("status")
            idempotency_key = payload.get("idempotency_key")

            # Validate severity if someone accidentally sent it (not used here).
            if status is not None:
                from .models import IncidentStatus
                try:
                    IncidentStatus(status)
                except ValueError:
                    self._json(400, {"error": f"invalid status: {status!r}"})
                    return

            try:
                incident = sqlite_store.update(
                    incident_id=incident_id,
                    expected_version=expected_version,
                    owner=owner,
                    status=status,
                    idempotency_key=idempotency_key,
                )
            except IncidentNotFound:
                self._json(404, {"error": f"incident {incident_id!r} not found"})
                return
            except VersionConflict as exc:
                self._json(409, {"error": str(exc)})
                return
            except InvalidTransition as exc:
                self._json(422, {"error": str(exc)})
                return
            except ValueError as exc:
                self._json(400, {"error": str(exc)})
                return

            data = _incident_dict(incident)
            data["events"] = [_event_dict(e) for e in sqlite_store.events(incident_id)]
            self._json(200, data)

        def _handle_escalation_run(self) -> None:
            if sqlite_store is None:
                self._json(503, {"error": "no durable store configured"})
                return
            try:
                payload = self._body() if self._content_length() > 0 else {}
            except json.JSONDecodeError as exc:
                self._json(400, {"error": f"invalid JSON: {exc}"})
                return
            worker_id = payload.get("worker_id", "http-worker")
            max_incidents = payload.get("max_incidents")
            if max_incidents is not None:
                try:
                    max_incidents = int(max_incidents)
                except (TypeError, ValueError):
                    self._json(400, {"error": "max_incidents must be an integer"})
                    return

            worker = EscalationWorker(sqlite_store, worker_id)
            escalated = worker.run_until_idle(max_incidents=max_incidents)
            self._json(200, {"escalated": [_incident_dict(i) for i in escalated]})

        # ------------------------------------------------------------------ #
        # Helpers
        # ------------------------------------------------------------------ #
        def _content_length(self) -> int:
            return int(self.headers.get("Content-Length", "0"))

        def _body(self) -> dict:
            return json.loads(self.rfile.read(self._content_length()))

        def _file(self, name: str, content_type: str) -> None:
            body = (STATIC_DIR / name).read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _json(self, status: int, value: object) -> None:
            body = json.dumps(value, default=json_value).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            return

    return ThreadingHTTPServer((host, port), Handler)
