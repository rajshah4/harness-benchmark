"""
HTTP API server for the freight control tower.

Provides:
  - REST endpoints under /api/v1/
  - Health endpoint GET /health (no auth)
  - Static file serving for the dashboard
  - Bearer token authentication
  - JSON request/response with Idempotency-Key support
"""
from __future__ import annotations

import json
import time
from dataclasses import asdict, is_dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from .exceptions import (
    AuthError,
    AuthzError,
    ConflictError,
    FreightError,
    NotFoundError,
    ValidationError,
    VersionError,
)
from .service import FreightService
from .sqlite_store import SQLiteFreightStore

STATIC_DIR = Path(__file__).with_name("static")


# ---------------------------------------------------------------------------
# JSON serialisation helpers
# ---------------------------------------------------------------------------

def _to_json(value: Any) -> Any:
    """Recursively convert dataclasses / enums / lists to JSON-able types."""
    if is_dataclass(value) and not isinstance(value, type):
        return {k: _to_json(v) for k, v in asdict(value).items()}
    if isinstance(value, list):
        return [_to_json(i) for i in value]
    if hasattr(value, "value"):  # StrEnum / Enum
        return value.value
    return value


def _json_dumps(value: Any) -> bytes:
    return json.dumps(_to_json(value), default=str).encode()


# ---------------------------------------------------------------------------
# Error → HTTP status mapping
# ---------------------------------------------------------------------------

ERROR_STATUS: dict[type, int] = {
    AuthError: 401,
    AuthzError: 403,
    NotFoundError: 404,
    ValidationError: 400,
    ConflictError: 409,
    VersionError: 409,
}


def _error_status(exc: Exception) -> int:
    for cls, code in ERROR_STATUS.items():
        if isinstance(exc, cls):
            return code
    return 500


# ---------------------------------------------------------------------------
# Request helper
# ---------------------------------------------------------------------------

class _Req:
    """Thin wrapper around BaseHTTPRequestHandler with convenience methods."""

    def __init__(self, handler: BaseHTTPRequestHandler) -> None:
        self._h = handler
        parsed = urlparse(handler.path)
        self.path = parsed.path
        self.query = parse_qs(parsed.query)
        self._body_cache: bytes | None = None

    def bearer(self) -> str:
        auth = self._h.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            return auth[7:]
        raise AuthError("Missing or invalid Authorization header")

    def body(self) -> dict:
        if self._body_cache is None:
            length = int(self._h.headers.get("Content-Length", "0"))
            self._body_cache = self._h.rfile.read(length)
        if not self._body_cache:
            return {}
        return json.loads(self._body_cache)

    def qs(self, key: str, default: str = "") -> str:
        return self.query.get(key, [default])[0]

    def idempotency_key(self) -> str | None:
        return self._h.headers.get("Idempotency-Key")


# ---------------------------------------------------------------------------
# Handler factory
# ---------------------------------------------------------------------------

def create_server(
    service: FreightService | SQLiteFreightStore,
    host: str = "127.0.0.1",
    port: int = 8080,
) -> ThreadingHTTPServer:
    """Create but do not start the HTTP server."""

    store = service  # may be FreightService (legacy) or SQLiteFreightStore

    class Handler(BaseHTTPRequestHandler):

        # ----------------------------------------------------------------
        # Main dispatch
        # ----------------------------------------------------------------

        def do_GET(self) -> None:
            req = _Req(self)
            try:
                if req.path == "/health":
                    self._json(200, {"status": "ok", "time": time.time()})
                elif req.path.startswith("/api/"):
                    self._api_get(req)
                elif req.path in {"/", "/index.html"}:
                    self._file("index.html", "text/html; charset=utf-8")
                elif req.path == "/app.js":
                    self._file("app.js", "text/javascript; charset=utf-8")
                elif req.path == "/styles.css":
                    self._file("styles.css", "text/css; charset=utf-8")
                else:
                    self.send_error(404)
            except Exception as exc:
                self._handle_error(exc)

        def do_POST(self) -> None:
            req = _Req(self)
            try:
                self._api_post(req)
            except Exception as exc:
                self._handle_error(exc)

        def do_PATCH(self) -> None:
            req = _Req(self)
            try:
                self._api_patch(req)
            except Exception as exc:
                self._handle_error(exc)

        # ----------------------------------------------------------------
        # GET routing
        # ----------------------------------------------------------------

        def _api_get(self, req: _Req) -> None:
            token = req.bearer()
            p = req.path

            # Legacy compatibility: /api/shipments (old FreightService)
            if p == "/api/shipments" and isinstance(store, FreightService):
                tenant = req.qs("tenant")
                self._json(200, [asdict(s) for s in store.list_shipments(tenant)])
                return

            # New API v1
            if p == "/api/v1/shipments":
                filters = {}
                if s := req.qs("status"):
                    filters["status"] = s
                if r := req.qs("reference"):
                    filters["reference"] = r
                ships = store.list_shipments(token, **filters)
                self._json(200, ships)

            elif p.startswith("/api/v1/shipments/") and "/events" not in p:
                sid = p.split("/")[-1]
                self._json(200, store.get_shipment(token, sid))

            elif p == "/api/v1/exceptions":
                filters = {}
                for key in ("status", "severity", "assignee", "shipment_id"):
                    if v := req.qs(key):
                        filters[key] = v
                self._json(200, store.list_exceptions(token, **filters))

            elif p == "/api/v1/audit":
                filters = {}
                for key in ("entity_type", "entity_id", "action"):
                    if v := req.qs(key):
                        filters[key] = v
                self._json(200, store.audit(token, **filters))

            elif p == "/api/v1/deliveries":
                filters = {}
                for key in ("status", "event_type"):
                    if v := req.qs(key):
                        filters[key] = v
                self._json(200, store.list_deliveries(token, **filters))

            elif p == "/api/v1/snapshot/export":
                snap = store.export_snapshot(token)
                self._json(200, snap)

            else:
                self.send_error(404)

        # ----------------------------------------------------------------
        # POST routing
        # ----------------------------------------------------------------

        def _api_post(self, req: _Req) -> None:
            p = req.path

            # Legacy
            if p == "/api/shipments" and isinstance(store, FreightService):
                body = req.body()
                value = store.create_shipment(body["tenant_id"], body["reference"])
                self._json(201, asdict(value))
                return

            token = req.bearer()
            body = req.body()

            if p == "/api/v1/shipments":
                ref = body.get("reference", "")
                s = store.create_shipment(token, ref)
                self._json(201, s)

            elif p.endswith("/events") and "/shipments/" in p:
                # POST /api/v1/shipments/{id}/events
                parts = p.split("/")
                sid = parts[parts.index("shipments") + 1]
                s = store.ingest_event(
                    token, sid,
                    event_id=body.get("event_id", ""),
                    event_type=body.get("event_type", ""),
                    event_time=body.get("event_time", 0.0),
                    location=body.get("location"),
                    details=body.get("details"),
                )
                self._json(200, s)

            elif p == "/api/v1/sla-rules":
                rule = store.set_sla_rule(
                    token,
                    severity=body.get("severity", ""),
                    delay_seconds=body.get("delay_seconds", 0),
                )
                self._json(200, rule)

            elif p == "/api/v1/tick":
                now = body.get("now", time.time())
                limit = body.get("limit", 100)
                count = store.tick(now, limit)
                self._json(200, {"escalations_enqueued": count})

            elif p == "/api/v1/worker/claim":
                worker_id = body.get("worker_id", "anon")
                now = body.get("now", time.time())
                d = store.claim_delivery(worker_id, now)
                self._json(200, d)

            elif p == "/api/v1/worker/complete":
                d = store.complete_delivery(
                    body.get("delivery_id", ""),
                    body.get("worker_id", ""),
                    body.get("now", time.time()),
                )
                self._json(200, {"ok": True})

            elif p == "/api/v1/worker/fail":
                store.fail_delivery(
                    body.get("delivery_id", ""),
                    body.get("worker_id", ""),
                    body.get("error", ""),
                    body.get("now", time.time()),
                )
                self._json(200, {"ok": True})

            elif p.endswith("/replay") and "/deliveries/" in p:
                parts = p.split("/")
                did = parts[parts.index("deliveries") + 1]
                now = body.get("now", time.time())
                d = store.replay_delivery(token, did, now)
                self._json(200, d)

            elif p == "/api/v1/snapshot/import":
                store.import_snapshot(token, body.get("snapshot", body))
                self._json(200, {"ok": True})

            elif p == "/api/v1/tenants":
                store.bootstrap_tenant(
                    body.get("tenant_id", ""),
                    body.get("name", ""),
                    body.get("admin_token", ""),
                )
                self._json(201, {"ok": True})

            elif p == "/api/v1/credentials":
                store.create_credential(
                    token,
                    body.get("token", ""),
                    body.get("role", ""),
                )
                self._json(201, {"ok": True})

            else:
                self.send_error(404)

        # ----------------------------------------------------------------
        # PATCH routing (exception mutations)
        # ----------------------------------------------------------------

        def _api_patch(self, req: _Req) -> None:
            p = req.path
            if "/exceptions/" not in p:
                self.send_error(404)
                return
            token = req.bearer()
            body = req.body()
            parts = p.split("/")
            exc_id = parts[parts.index("exceptions") + 1]
            updated = store.mutate_exception(
                token,
                exc_id,
                expected_version=body.get("expected_version", -1),
                action=body.get("action", ""),
                actor=body.get("actor"),
                **{k: v for k, v in body.items()
                   if k not in ("expected_version", "action", "actor")},
            )
            self._json(200, updated)

        # ----------------------------------------------------------------
        # Low-level helpers
        # ----------------------------------------------------------------

        def _file(self, name: str, content_type: str) -> None:
            body = (STATIC_DIR / name).read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _json(self, status: int, value: Any) -> None:
            body = _json_dumps(value)
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _handle_error(self, exc: Exception) -> None:
            status = _error_status(exc)
            exc_type = type(exc).__name__
            msg = str(exc) if not isinstance(exc, FreightError) else str(exc)
            self._json(status, {"error": exc_type, "message": msg})

        def log_message(self, fmt: str, *args: object) -> None:
            return

    return ThreadingHTTPServer((host, port), Handler)
