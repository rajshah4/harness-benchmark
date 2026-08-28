"""HTTP server for the incident operations center."""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from .models import (
    AuditEvent,
    Incident,
    IncidentNotFound,
    IncidentStatus,
    InvalidTransition,
    Severity,
    VersionConflict,
)
from .service import IncidentService

STATIC_DIR = Path(__file__).with_name("static")

_INCIDENT_RE = re.compile(r"^/api/incidents/([^/?]+)$")


def _incident_to_dict(incident: Incident) -> dict[str, Any]:
    d = asdict(incident)
    d["severity"] = incident.severity.value
    d["status"] = incident.status.value
    return d


def _event_to_dict(event: AuditEvent) -> dict[str, Any]:
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
    service: IncidentService | None = None,
    host: str = "127.0.0.1",
    port: int = 8080,
    store: Any = None,
) -> ThreadingHTTPServer:
    """Construct and bind a server without starting its request loop."""

    # Accept either a legacy IncidentService or a SQLiteIncidentStore directly
    _store = store
    _service = service

    class Handler(BaseHTTPRequestHandler):

        # ------------------------------------------------------------------ #
        # Routing                                                              #
        # ------------------------------------------------------------------ #

        def do_GET(self) -> None:
            path = self.path.split("?")[0]
            query = self.path[len(path):]

            if path == "/api/incidents":
                self._handle_list_incidents(query)
            elif m := _INCIDENT_RE.match(path):
                self._handle_get_incident(m.group(1))
            elif path == "/api/summary":
                self._handle_summary()
            elif path in {"/", "/index.html"}:
                self._file("index.html", "text/html; charset=utf-8")
            elif path == "/app.js":
                self._file("app.js", "text/javascript; charset=utf-8")
            elif path == "/styles.css":
                self._file("styles.css", "text/css; charset=utf-8")
            else:
                self.send_error(404)

        def do_POST(self) -> None:
            path = self.path.split("?")[0]
            if path == "/api/alerts":
                self._handle_ingest_alert()
            elif path == "/api/incidents":
                # Legacy route kept for backward compatibility
                self._handle_create_incident_legacy()
            elif path == "/api/escalations/run":
                self._handle_escalation_run()
            else:
                self.send_error(404)

        def do_PATCH(self) -> None:
            path = self.path.split("?")[0]
            m = _INCIDENT_RE.match(path)
            if m:
                self._handle_update_incident(m.group(1))
            else:
                self.send_error(404)

        # ------------------------------------------------------------------ #
        # Handlers                                                             #
        # ------------------------------------------------------------------ #

        def _handle_list_incidents(self, query: str) -> None:
            params = _parse_qs(query)
            if _store is not None:
                try:
                    incidents = _store.list(
                        status=params.get("status"),
                        severity=params.get("severity"),
                        owner=params.get("owner"),
                    )
                except ValueError as exc:
                    self._json(400, {"error": str(exc)})
                    return
            elif _service is not None:
                incidents = _service.list()
            else:
                incidents = []
            self._json(200, [_incident_to_dict(i) for i in incidents])

        def _handle_get_incident(self, incident_id: str) -> None:
            if _store is not None:
                incident = _store.get(incident_id)
            elif _service is not None:
                incident = _service.store.get(incident_id)
            else:
                incident = None
            if incident is None:
                self._json(404, {"error": f"incident {incident_id!r} not found"})
                return
            d = _incident_to_dict(incident)
            if _store is not None:
                d["events"] = [_event_to_dict(e) for e in _store.events(incident_id)]
            self._json(200, d)

        def _handle_ingest_alert(self) -> None:
            if _store is None:
                self._json(501, {"error": "store not configured"})
                return
            try:
                body = self._body()
                fingerprint = body.get("fingerprint") or str(uuid.uuid4())
                title = body.get("title", "").strip()
                if not title:
                    self._json(400, {"error": "title is required"})
                    return
                severity = body.get("severity", "P3")
                Severity(severity)  # validate
                incident, created = _store.ingest_alert(
                    fingerprint=fingerprint,
                    title=title,
                    severity=severity,
                    source=body.get("source", "unknown"),
                    details=body.get("details"),
                    idempotency_key=body.get("idempotency_key"),
                )
            except (KeyError, json.JSONDecodeError) as exc:
                self._json(400, {"error": str(exc)})
                return
            except ValueError as exc:
                self._json(400, {"error": str(exc)})
                return
            status_code = 201 if created else 200
            self._json(status_code, _incident_to_dict(incident))

        def _handle_create_incident_legacy(self) -> None:
            """Original POST /api/incidents endpoint."""
            try:
                body = self._body()
                if _store is not None:
                    title = body.get("title", "").strip()
                    if not title:
                        self._json(400, {"error": "title is required"})
                        return
                    severity = body.get("severity", "P3")
                    Severity(severity)
                    incident, created = _store.ingest_alert(
                        fingerprint=str(uuid.uuid4()),
                        title=title,
                        severity=severity,
                    )
                elif _service is not None:
                    incident = _service.create(body["title"], body["severity"])
                else:
                    self._json(501, {"error": "no store"})
                    return
            except (KeyError, ValueError, json.JSONDecodeError) as exc:
                self._json(400, {"error": str(exc)})
                return
            self._json(201, _incident_to_dict(incident))

        def _handle_update_incident(self, incident_id: str) -> None:
            if _store is None:
                self._json(501, {"error": "store not configured"})
                return
            try:
                body = self._body()
            except json.JSONDecodeError as exc:
                self._json(400, {"error": f"invalid JSON: {exc}"})
                return

            expected_version = body.get("expected_version")
            if expected_version is None:
                self._json(400, {"error": "expected_version is required"})
                return
            if not isinstance(expected_version, int):
                self._json(400, {"error": "expected_version must be an integer"})
                return

            try:
                incident = _store.update(
                    incident_id=incident_id,
                    expected_version=expected_version,
                    owner=body.get("owner"),
                    status=body.get("status"),
                    idempotency_key=body.get("idempotency_key"),
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

            self._json(200, _incident_to_dict(incident))

        def _handle_escalation_run(self) -> None:
            if _store is None:
                self._json(501, {"error": "store not configured"})
                return
            try:
                body = self._body() if int(self.headers.get("Content-Length", "0")) > 0 else {}
            except json.JSONDecodeError:
                body = {}

            from .escalation import EscalationWorker
            worker_id = body.get("worker_id", "http-trigger")
            max_incidents = body.get("max_incidents")
            worker = EscalationWorker(_store, worker_id)
            escalated = worker.run_until_idle(
                max_incidents=max_incidents if isinstance(max_incidents, int) else None
            )
            self._json(200, {"escalated": [_incident_to_dict(i) for i in escalated]})

        def _handle_summary(self) -> None:
            if _store is not None:
                all_incidents = _store.list()
            elif _service is not None:
                all_incidents = _service.list()
            else:
                all_incidents = []

            total = len(all_incidents)
            by_status: dict[str, int] = {}
            by_severity: dict[str, int] = {}
            for inc in all_incidents:
                s = inc.status.value if hasattr(inc.status, "value") else str(inc.status)
                by_status[s] = by_status.get(s, 0) + 1
                sv = inc.severity.value if hasattr(inc.severity, "value") else str(inc.severity)
                by_severity[sv] = by_severity.get(sv, 0) + 1

            self._json(200, {
                "total": total,
                "by_status": by_status,
                "by_severity": by_severity,
            })

        # ------------------------------------------------------------------ #
        # Utilities                                                            #
        # ------------------------------------------------------------------ #

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

        def _json(self, status: int, value: object) -> None:
            body = json.dumps(value, default=json_value).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            return

    return ThreadingHTTPServer((host, port), Handler)


def _parse_qs(query: str) -> dict[str, str]:
    """Parse a query string like ?foo=bar&baz=qux into a dict."""
    import urllib.parse
    if query.startswith("?"):
        query = query[1:]
    if not query:
        return {}
    result = {}
    for part in query.split("&"):
        if "=" in part:
            k, v = part.split("=", 1)
            result[urllib.parse.unquote_plus(k)] = urllib.parse.unquote_plus(v)
    return result
