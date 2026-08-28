"""HTTP server for the incident operations centre."""

from __future__ import annotations

import json
import uuid
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .escalation import EscalationWorker
from .models import AuditEvent, Incident, IncidentStatus, Severity
from .service import IncidentService
from .sqlite_store import (
    IncidentNotFound,
    InvalidTransition,
    SQLiteIncidentStore,
    VersionConflict,
)

STATIC_DIR = Path(__file__).with_name("static")

_INCIDENT_PREFIX = "/api/incidents/"


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


# Legacy JSON encoder for IncidentService (in-memory) responses.
def _json_default(value: object) -> object:
    if hasattr(value, "value"):
        return value.value
    raise TypeError(f"cannot encode {type(value).__name__}")


def create_server(
    store: SQLiteIncidentStore | IncidentService,
    host: str = "127.0.0.1",
    port: int = 8080,
) -> ThreadingHTTPServer:
    """Construct and bind a server without starting its request loop."""

    # Determine whether we have a durable store or the legacy in-memory service.
    is_sqlite = isinstance(store, SQLiteIncidentStore)

    class Handler(BaseHTTPRequestHandler):
        # ----------------------------------------------------------------
        # Routing
        # ----------------------------------------------------------------

        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            path = parsed.path
            qs = parse_qs(parsed.query)

            if path == "/api/summary" and is_sqlite:
                self._handle_summary()
            elif path == "/api/incidents":
                self._handle_list(qs)
            elif path.startswith(_INCIDENT_PREFIX) and is_sqlite:
                self._handle_get(path[len(_INCIDENT_PREFIX):])
            elif path in {"/", "/index.html"}:
                self._file("index.html", "text/html; charset=utf-8")
            elif path == "/app.js":
                self._file("app.js", "text/javascript; charset=utf-8")
            elif path == "/styles.css":
                self._file("styles.css", "text/css; charset=utf-8")
            else:
                self.send_error(404)

        def do_POST(self) -> None:
            path = urlparse(self.path).path
            if path == "/api/alerts" and is_sqlite:
                self._handle_ingest()
            elif path == "/api/incidents" and not is_sqlite:
                self._handle_legacy_create()
            elif path == "/api/escalations/run" and is_sqlite:
                self._handle_escalation_run()
            else:
                self.send_error(404)

        def do_PATCH(self) -> None:
            path = urlparse(self.path).path
            if path.startswith(_INCIDENT_PREFIX) and is_sqlite:
                self._handle_update(path[len(_INCIDENT_PREFIX):])
            else:
                self.send_error(404)

        # ----------------------------------------------------------------
        # Route handlers — durable store
        # ----------------------------------------------------------------

        def _handle_ingest(self) -> None:
            try:
                payload = self._body()
                fp = payload["fingerprint"]
                title = payload["title"]
                sev = Severity(payload["severity"])
            except (KeyError, ValueError, json.JSONDecodeError) as exc:
                self._json(400, {"error": str(exc)})
                return
            source = payload.get("source", "unknown")
            details = payload.get("details") or {}
            idem = payload.get("idempotency_key")
            try:
                incident, created = store.ingest_alert(
                    fp, title, sev, source=source, details=details, idempotency_key=idem
                )
            except Exception as exc:
                self._json(400, {"error": str(exc)})
                return
            self._json(201 if created else 200, _incident_dict(incident))

        def _handle_list(self, qs: dict) -> None:
            if is_sqlite:
                status = (qs.get("status") or [None])[0]
                severity = (qs.get("severity") or [None])[0]
                owner = (qs.get("owner") or [None])[0]
                incidents = store.list(status=status, severity=severity, owner=owner)
                self._json(200, [_incident_dict(i) for i in incidents])
            else:
                self._json(200, [asdict(i) for i in store.list()], default=_json_default)

        def _handle_get(self, incident_id: str) -> None:
            incident = store.get(incident_id)
            if incident is None:
                self._json(404, {"error": f"incident not found: {incident_id}"})
                return
            data = _incident_dict(incident)
            data["events"] = [_event_dict(e) for e in store.events(incident_id)]
            self._json(200, data)

        def _handle_update(self, incident_id: str) -> None:
            try:
                payload = self._body()
                expected_version = int(payload["expected_version"])
            except (KeyError, ValueError, json.JSONDecodeError) as exc:
                self._json(400, {"error": str(exc)})
                return
            owner = payload.get("owner")
            status = payload.get("status")
            idem = payload.get("idempotency_key")
            try:
                incident = store.update(
                    incident_id,
                    expected_version,
                    owner=owner,
                    status=status,
                    idempotency_key=idem,
                )
            except VersionConflict as exc:
                self._json(409, {"error": str(exc)})
                return
            except IncidentNotFound as exc:
                self._json(404, {"error": str(exc)})
                return
            except (InvalidTransition, ValueError) as exc:
                self._json(422, {"error": str(exc)})
                return
            self._json(200, _incident_dict(incident))

        def _handle_escalation_run(self) -> None:
            try:
                payload = self._body() if int(self.headers.get("Content-Length", "0")) else {}
            except json.JSONDecodeError:
                payload = {}
            max_n = payload.get("max_incidents")
            worker_id = payload.get("worker_id") or str(uuid.uuid4())
            worker = EscalationWorker(store, worker_id=worker_id)
            completed = worker.run_until_idle(
                max_incidents=int(max_n) if max_n is not None else None
            )
            self._json(200, [_incident_dict(i) for i in completed])

        def _handle_summary(self) -> None:
            incidents = store.list()
            by_status: dict[str, int] = {}
            by_severity: dict[str, int] = {}
            for inc in incidents:
                by_status[inc.status.value] = by_status.get(inc.status.value, 0) + 1
                by_severity[inc.severity.value] = by_severity.get(inc.severity.value, 0) + 1
            self._json(
                200,
                {
                    "total": len(incidents),
                    "by_status": by_status,
                    "by_severity": by_severity,
                },
            )

        # ----------------------------------------------------------------
        # Legacy route handler
        # ----------------------------------------------------------------

        def _handle_legacy_create(self) -> None:
            try:
                payload = self._body()
                incident = store.create(payload["title"], payload["severity"])
            except (KeyError, ValueError, json.JSONDecodeError) as exc:
                self._json(400, {"error": str(exc)})
                return
            self._json(201, asdict(incident), default=_json_default)

        # ----------------------------------------------------------------
        # Helpers
        # ----------------------------------------------------------------

        def _body(self) -> dict:
            length = int(self.headers.get("Content-Length", "0"))
            return json.loads(self.rfile.read(length))

        def _file(self, name: str, content_type: str) -> None:
            body = (STATIC_DIR / name).read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _json(
            self, status: int, value: object, default=None
        ) -> None:
            body = json.dumps(value, default=default).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            return

    return ThreadingHTTPServer((host, port), Handler)
