"""HTTP server for the freight control tower.

Provides a JSON REST API and a browser dashboard.
"""
from __future__ import annotations

import json
import time
from dataclasses import asdict, fields, is_dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from .exceptions import (
    AuthenticationError,
    AuthorizationError,
    IdempotencyConflictError,
    NotFoundError,
    StaleVersionError,
    ValidationError,
)
from .service import FreightService
from .sqlite_store import SQLiteFreightStore

STATIC_DIR = Path(__file__).with_name("static")


# ---------------------------------------------------------------------------
# Serialisation helpers
# ---------------------------------------------------------------------------

def _to_dict(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return {f.name: _to_dict(getattr(value, f.name)) for f in fields(value)}
    if isinstance(value, list):
        return [_to_dict(v) for v in value]
    if hasattr(value, "value"):  # StrEnum etc.
        return value.value
    return value


def _json_encode(value: Any) -> bytes:
    return json.dumps(_to_dict(value)).encode()


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------

class _Handler(BaseHTTPRequestHandler):
    store: SQLiteFreightStore

    # ------------------------------------------------------------------
    # Routing helpers
    # ------------------------------------------------------------------

    def _bearer(self) -> str | None:
        auth = self.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            return auth[7:].strip()
        return None

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length)
        return json.loads(raw) if raw else {}

    def _json(self, status: int, value: Any) -> None:
        body = _json_encode(value)
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _file(self, name: str, content_type: str) -> None:
        body = (STATIC_DIR / name).read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _error(self, status: int, message: str) -> None:
        self._json(status, {"error": message})

    def log_message(self, format: str, *args: object) -> None:  # silence access log
        return

    # ------------------------------------------------------------------
    # Exception → HTTP status mapping
    # ------------------------------------------------------------------

    def _handle_exc(self, exc: Exception) -> None:
        if isinstance(exc, AuthenticationError):
            self._error(401, str(exc))
        elif isinstance(exc, AuthorizationError):
            self._error(403, str(exc))
        elif isinstance(exc, NotFoundError):
            self._error(404, str(exc))
        elif isinstance(exc, StaleVersionError):
            self._error(409, str(exc))
        elif isinstance(exc, IdempotencyConflictError):
            self._error(409, str(exc))
        elif isinstance(exc, ValidationError):
            self._error(400, str(exc))
        elif isinstance(exc, (KeyError, json.JSONDecodeError)):
            self._error(400, f"Bad request: {exc}")
        else:
            self._error(500, str(exc))

    # ------------------------------------------------------------------
    # GET routing
    # ------------------------------------------------------------------

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        qs = parse_qs(parsed.query)

        # Static files (no auth required)
        if path in {"", "/", "/index.html"}:
            self._file("index.html", "text/html; charset=utf-8")
            return
        if path == "/app.js":
            self._file("app.js", "text/javascript; charset=utf-8")
            return
        if path == "/styles.css":
            self._file("styles.css", "text/css; charset=utf-8")
            return
        if path == "/api/health":
            self._json(200, {"status": "ok"})
            return

        token = self._bearer()
        if not token:
            self._error(401, "Bearer token required")
            return

        try:
            if path == "/api/shipments":
                filters = {}
                if "status" in qs:
                    filters["status"] = qs["status"][0]
                self._json(200, self.store.list_shipments(token, **filters))

            elif path.startswith("/api/shipments/"):
                sid = path[len("/api/shipments/"):]
                if "/events" in sid:
                    self._error(404, "Use POST to ingest events")
                    return
                self._json(200, self.store.get_shipment(token, sid))

            elif path == "/api/exceptions":
                filters = {k: qs[k][0] for k in ("status", "severity", "assigned_to", "shipment_id") if k in qs}
                self._json(200, self.store.list_exceptions(token, **filters))

            elif path.startswith("/api/exceptions/"):
                exc_id = path[len("/api/exceptions/"):]
                excs = self.store.list_exceptions(token)
                match = [e for e in excs if e.id == exc_id]
                if not match:
                    raise NotFoundError(f"Exception '{exc_id}' not found")
                self._json(200, match[0])

            elif path == "/api/audit":
                filters = {k: qs[k][0] for k in ("resource_type", "resource_id", "action") if k in qs}
                self._json(200, self.store.audit(token, **filters))

            elif path == "/api/deliveries":
                filters = {}
                if "status" in qs:
                    filters["status"] = qs["status"][0]
                self._json(200, self.store.list_deliveries(token, **filters))

            else:
                self._error(404, "Not found")

        except Exception as exc:
            self._handle_exc(exc)

    # ------------------------------------------------------------------
    # POST routing
    # ------------------------------------------------------------------

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")

        token = self._bearer()
        if not token:
            self._error(401, "Bearer token required")
            return

        try:
            body = self._body()

            if path == "/api/tenants/bootstrap":
                t = self.store.bootstrap_tenant(
                    body["tenant_id"], body["name"], body["admin_token"]
                )
                self._json(201, {"id": t.id, "name": t.name, "created_at": t.created_at})

            elif path == "/api/credentials":
                c = self.store.create_credential(token, body["token"], body["role"])
                self._json(201, {"id": c.id, "role": c.role, "tenant_id": c.tenant_id})

            elif path == "/api/shipments":
                ship = self.store.create_shipment(token, body["reference"])
                self._json(201, ship)

            elif path.startswith("/api/shipments/") and path.endswith("/events"):
                sid = path[len("/api/shipments/"):-len("/events")]
                result = self.store.ingest_event(
                    token,
                    sid,
                    body["event_id"],
                    body["event_type"],
                    float(body["event_time"]),
                    location=body.get("location"),
                    details=body.get("details"),
                )
                self._json(200, result)

            elif path.startswith("/api/exceptions/") and path.endswith("/mutate"):
                exc_id = path[len("/api/exceptions/"):-len("/mutate")]
                result = self.store.mutate_exception(
                    token,
                    exc_id,
                    int(body["expected_version"]),
                    body["action"],
                    actor=body.get("actor"),
                    **{k: v for k, v in body.items() if k not in ("expected_version", "action", "actor")},
                )
                self._json(200, result)

            elif path == "/api/sla-rules":
                self.store.set_sla_rule(token, body["severity"], float(body["delay_seconds"]))
                self._json(200, {"ok": True})

            elif path == "/api/tick":
                now = float(body.get("now", time.time()))
                limit = int(body.get("limit", 100))
                n = self.store.tick(now, limit)
                self._json(200, {"escalated": n})

            elif path == "/api/worker/claim":
                now = float(body.get("now", time.time()))
                d = self.store.claim_delivery(body["worker_id"], now)
                self._json(200, d if d else None)

            elif path == "/api/worker/complete":
                now = float(body.get("now", time.time()))
                d = self.store.complete_delivery(body["delivery_id"], body["worker_id"], now)
                self._json(200, d)

            elif path == "/api/worker/fail":
                now = float(body.get("now", time.time()))
                d = self.store.fail_delivery(body["delivery_id"], body["worker_id"], body["error"], now)
                self._json(200, d)

            elif path == "/api/deliveries/replay":
                now = float(body.get("now", time.time()))
                d = self.store.replay_delivery(token, body["delivery_id"], now)
                self._json(200, d)

            elif path == "/api/snapshot/export":
                snap = self.store.export_snapshot(token)
                self._json(200, snap)

            elif path == "/api/snapshot/import":
                self.store.import_snapshot(token, body["snapshot"])
                self._json(200, {"ok": True})

            else:
                self._error(404, "Not found")

        except Exception as exc:
            self._handle_exc(exc)

    # ------------------------------------------------------------------
    # PATCH routing (exception mutations via PATCH)
    # ------------------------------------------------------------------

    def do_PATCH(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")

        token = self._bearer()
        if not token:
            self._error(401, "Bearer token required")
            return

        try:
            body = self._body()
            if path.startswith("/api/exceptions/"):
                exc_id = path[len("/api/exceptions/"):]
                result = self.store.mutate_exception(
                    token,
                    exc_id,
                    int(body["expected_version"]),
                    body["action"],
                    actor=body.get("actor"),
                    **{k: v for k, v in body.items() if k not in ("expected_version", "action", "actor")},
                )
                self._json(200, result)
            else:
                self._error(404, "Not found")
        except Exception as exc:
            self._handle_exc(exc)


def create_server(
    service: FreightService | SQLiteFreightStore | None = None,
    host: str = "127.0.0.1",
    port: int = 8080,
    store: SQLiteFreightStore | None = None,
) -> ThreadingHTTPServer:
    """Create a ThreadingHTTPServer.

    Accepts either the legacy ``service`` (FreightService) or the new
    ``store`` (SQLiteFreightStore).  If both are provided, ``store`` wins.
    """
    actual_store: SQLiteFreightStore | None = store or (service if isinstance(service, SQLiteFreightStore) else None)

    # Legacy path: wrap FreightService to satisfy the interface minimally
    if actual_store is None and isinstance(service, FreightService):
        # Minimal compatibility shim – just use the old in-memory service for
        # the two legacy routes.
        class _LegacyHandler(BaseHTTPRequestHandler):
            _svc = service

            def do_GET(self) -> None:
                parsed = urlparse(self.path)
                if parsed.path == "/api/shipments":
                    tenant = parse_qs(parsed.query).get("tenant", [""])[0]
                    from dataclasses import asdict
                    rows = [asdict(item) for item in self._svc.list_shipments(tenant)]
                    body = json.dumps(rows).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                elif parsed.path in {"/", "/index.html"}:
                    self._file("index.html", "text/html; charset=utf-8")
                elif parsed.path == "/app.js":
                    self._file("app.js", "text/javascript; charset=utf-8")
                elif parsed.path == "/styles.css":
                    self._file("styles.css", "text/css; charset=utf-8")
                else:
                    self.send_error(404)

            def do_POST(self) -> None:
                if self.path != "/api/shipments":
                    self.send_error(404)
                    return
                try:
                    raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                    payload = json.loads(raw)
                    from dataclasses import asdict
                    value = self._svc.create_shipment(payload["tenant_id"], payload["reference"])
                    body = json.dumps(asdict(value)).encode()
                    self.send_response(201)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                except (KeyError, ValueError, json.JSONDecodeError) as exc:
                    body = json.dumps({"error": str(exc)}).encode()
                    self.send_response(400)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)

            def _file(self, name: str, content_type: str) -> None:
                data = (STATIC_DIR / name).read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, format: str, *args: object) -> None:
                return

        return ThreadingHTTPServer((host, port), _LegacyHandler)

    # Full SQLite-backed handler
    class Handler(_Handler):
        store = actual_store  # type: ignore[assignment]

    return ThreadingHTTPServer((host, port), Handler)
