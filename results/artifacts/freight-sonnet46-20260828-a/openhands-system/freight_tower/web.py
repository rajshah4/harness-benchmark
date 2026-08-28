"""HTTP API for the freight control tower.

`create_server` accepts a SQLiteFreightStore and returns a ThreadingHTTPServer
that binds but does NOT start serving — call server.serve_forever() explicitly.
This preserves the original contract documented in the starter README.
"""
from __future__ import annotations

import dataclasses
import json
import re
import time
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional
from urllib.parse import parse_qs, urlparse

from .exceptions import (
    AuthError,
    AuthzError,
    ConflictError,
    NotFoundError,
    ValidationError,
    VersionConflictError,
)
from .sqlite_store import SQLiteFreightStore

STATIC_DIR = Path(__file__).with_name("static")

# ---------------------------------------------------------------------------
# Serialisation helpers
# ---------------------------------------------------------------------------

def _default(v: Any) -> Any:
    if dataclasses.is_dataclass(v) and not isinstance(v, type):
        return dataclasses.asdict(v)
    if hasattr(v, "value"):
        return v.value
    raise TypeError(type(v).__name__)


def _dumps(v: Any) -> bytes:
    return json.dumps(v, default=_default).encode()


# ---------------------------------------------------------------------------
# Simple URL router
# ---------------------------------------------------------------------------

class _Route:
    __slots__ = ("method", "_re", "handler")

    def __init__(self, method: str, pattern: str, handler: Any) -> None:
        self.method = method
        self._re = re.compile(pattern + "$")
        self.handler = handler

    def match(self, method: str, path: str) -> Optional[dict]:
        if method != self.method:
            return None
        m = self._re.match(path)
        return m.groupdict() if m else None


# ---------------------------------------------------------------------------
# Route handlers
# ---------------------------------------------------------------------------

def _make_handler(store: SQLiteFreightStore) -> type:
    """Return a request handler class closed over *store*."""

    routes: list[_Route] = []

    def route(method: str, pattern: str):
        def decorator(fn):
            routes.append(_Route(method, pattern, fn))
            return fn
        return decorator

    # ---- Static / health ----

    @route("GET", r"/health")
    def handle_health(req, params):
        req._json(200, {"status": "ok"})

    @route("GET", r"/")
    @route("GET", r"/index\.html")
    def handle_index(req, params):
        req._file("index.html", "text/html; charset=utf-8")

    @route("GET", r"/app\.js")
    def handle_appjs(req, params):
        req._file("app.js", "text/javascript; charset=utf-8")

    @route("GET", r"/styles\.css")
    def handle_css(req, params):
        req._file("styles.css", "text/css; charset=utf-8")

    # ---- Bootstrap / credentials (admin) ----

    @route("POST", r"/api/tenants/bootstrap")
    def handle_bootstrap(req, params):
        body = req._body()
        store.bootstrap_tenant(
            body.get("tenant_id", ""),
            body.get("name", ""),
            body.get("admin_token", ""),
        )
        req._json(201, {"ok": True})

    @route("POST", r"/api/credentials")
    def handle_create_cred(req, params):
        token = req._bearer()
        body = req._body()
        store.create_credential(token, body.get("token", ""), body.get("role", ""))
        req._json(201, {"ok": True})

    # ---- Shipments ----

    @route("POST", r"/api/shipments")
    def handle_create_shipment(req, params):
        token = req._bearer()
        body = req._body()
        result = store.create_shipment(token, body.get("reference", ""))
        req._json(201, dataclasses.asdict(result))

    @route("GET", r"/api/shipments")
    def handle_list_shipments(req, params):
        token = req._bearer()
        qs = parse_qs(urlparse(req.path).query)
        filters = {
            k: qs[k][0]
            for k in ("status", "reference")
            if qs.get(k)
        }
        items = store.list_shipments(token, **filters)
        req._json(200, [dataclasses.asdict(i) for i in items])

    @route("GET", r"/api/shipments/(?P<shipment_id>[^/]+)/events")
    def handle_list_events(req, params):
        # Convenience: return the shipment (events are internal)
        token = req._bearer()
        result = store.get_shipment(token, params["shipment_id"])
        req._json(200, dataclasses.asdict(result))

    @route("POST", r"/api/shipments/(?P<shipment_id>[^/]+)/events")
    def handle_ingest_event(req, params):
        token = req._bearer()
        body = req._body()
        ik = req.headers.get("Idempotency-Key")
        event_id = body.get("event_id") or ik
        if not event_id:
            raise ValidationError("event_id is required (body or Idempotency-Key header)")
        result = store.ingest_event(
            token,
            params["shipment_id"],
            event_id,
            body.get("event_type", ""),
            float(body["event_time"]) if "event_time" in body else None,
            body.get("location"),
            body.get("details"),
        )
        req._json(201, dataclasses.asdict(result))

    @route("GET", r"/api/shipments/(?P<shipment_id>[^/]+)")
    def handle_get_shipment(req, params):
        token = req._bearer()
        result = store.get_shipment(token, params["shipment_id"])
        req._json(200, dataclasses.asdict(result))

    # ---- Exceptions ----

    @route("GET", r"/api/exceptions")
    def handle_list_exceptions(req, params):
        token = req._bearer()
        qs = parse_qs(urlparse(req.path).query)
        filters = {
            k: qs[k][0]
            for k in ("status", "severity", "assignee", "shipment_id")
            if qs.get(k)
        }
        items = store.list_exceptions(token, **filters)
        req._json(200, [dataclasses.asdict(i) for i in items])

    @route("POST", r"/api/exceptions/(?P<exception_id>[^/]+)/mutate")
    def handle_mutate_exception(req, params):
        token = req._bearer()
        body = req._body()
        result = store.mutate_exception(
            token,
            params["exception_id"],
            int(body["expected_version"]),
            body.get("action", ""),
            actor=body.get("actor"),
            **{k: v for k, v in body.items()
               if k not in ("expected_version", "action", "actor")},
        )
        req._json(200, dataclasses.asdict(result))

    @route("GET", r"/api/exceptions/(?P<exception_id>[^/]+)")
    def handle_get_exception(req, params):
        token = req._bearer()
        items = store.list_exceptions(token)
        for exc in items:
            if exc.id == params["exception_id"]:
                req._json(200, dataclasses.asdict(exc))
                return
        raise NotFoundError(f"Exception '{params['exception_id']}' not found")

    # ---- Audit ----

    @route("GET", r"/api/audit")
    def handle_audit(req, params):
        token = req._bearer()
        qs = parse_qs(urlparse(req.path).query)
        filters = {
            k: qs[k][0]
            for k in ("entity_type", "entity_id", "actor", "action")
            if qs.get(k)
        }
        if qs.get("since"):
            filters["since"] = float(qs["since"][0])
        items = store.audit(token, **filters)
        req._json(200, [dataclasses.asdict(i) for i in items])

    # ---- SLA rules ----

    @route("POST", r"/api/sla-rules")
    def handle_set_sla(req, params):
        token = req._bearer()
        body = req._body()
        result = store.set_sla_rule(
            token,
            body.get("severity", ""),
            float(body.get("delay_seconds", 0)),
        )
        req._json(200, dataclasses.asdict(result))

    # ---- Tick ----

    @route("POST", r"/api/tick")
    def handle_tick(req, params):
        body = req._body()
        now = float(body.get("now", time.time()))
        limit = int(body.get("limit", 100))
        count = store.tick(now, limit)
        req._json(200, {"count": count})

    # ---- Outbox deliveries ----

    @route("GET", r"/api/deliveries")
    def handle_list_deliveries(req, params):
        token = req._bearer()
        qs = parse_qs(urlparse(req.path).query)
        filters = {
            k: qs[k][0]
            for k in ("status", "entity_type", "entity_id")
            if qs.get(k)
        }
        items = store.list_deliveries(token, **filters)
        req._json(200, [dataclasses.asdict(i) for i in items])

    @route("POST", r"/api/deliveries/claim")
    def handle_claim(req, params):
        body = req._body()
        worker_id = body.get("worker_id", "worker")
        now = float(body.get("now", time.time()))
        result = store.claim_delivery(worker_id, now)
        req._json(200, dataclasses.asdict(result) if result else None)

    @route("POST", r"/api/deliveries/(?P<delivery_id>[^/]+)/complete")
    def handle_complete(req, params):
        body = req._body()
        result = store.complete_delivery(
            params["delivery_id"],
            body.get("worker_id", ""),
            float(body.get("now", time.time())),
        )
        req._json(200, dataclasses.asdict(result))

    @route("POST", r"/api/deliveries/(?P<delivery_id>[^/]+)/fail")
    def handle_fail(req, params):
        body = req._body()
        result = store.fail_delivery(
            params["delivery_id"],
            body.get("worker_id", ""),
            body.get("error", "unknown error"),
            float(body.get("now", time.time())),
        )
        req._json(200, dataclasses.asdict(result))

    @route("POST", r"/api/deliveries/(?P<delivery_id>[^/]+)/replay")
    def handle_replay(req, params):
        token = req._bearer()
        body = req._body()
        now = float(body.get("now", time.time()))
        result = store.replay_delivery(token, params["delivery_id"], now)
        req._json(200, dataclasses.asdict(result))

    # ---- Snapshots ----

    @route("POST", r"/api/snapshot/export")
    def handle_export(req, params):
        token = req._bearer()
        result = store.export_snapshot(token)
        req._json(200, result)

    @route("POST", r"/api/snapshot/import")
    def handle_import(req, params):
        token = req._bearer()
        body = req._body()
        store.import_snapshot(token, body.get("snapshot", body))
        req._json(200, {"ok": True})

    # ---- Handler class ----

    class Handler(BaseHTTPRequestHandler):
        def _dispatch(self) -> None:
            parsed_path = urlparse(self.path).path
            method = self.command
            for r in routes:
                match_params = r.match(method, parsed_path)
                if match_params is not None:
                    try:
                        r.handler(self, match_params)
                    except (AuthError, AuthzError) as exc:
                        code = 401 if isinstance(exc, AuthError) else 403
                        tag = "auth" if isinstance(exc, AuthError) else "authz"
                        self._json(code, {"error": tag, "message": str(exc)})
                    except NotFoundError as exc:
                        self._json(404, {"error": "not_found", "message": str(exc)})
                    except (ConflictError, VersionConflictError) as exc:
                        tag = "conflict" if isinstance(exc, ConflictError) else "version_conflict"
                        self._json(409, {"error": tag, "message": str(exc)})
                    except ValidationError as exc:
                        self._json(400, {"error": "validation", "message": str(exc)})
                    except (KeyError, TypeError, ValueError) as exc:
                        self._json(400, {"error": "bad_request", "message": str(exc)})
                    except Exception as exc:
                        self._json(500, {"error": "internal", "message": str(exc)})
                    return
            self.send_error(404)

        def do_GET(self) -> None:
            self._dispatch()

        def do_POST(self) -> None:
            self._dispatch()

        def do_PUT(self) -> None:
            self._dispatch()

        def do_DELETE(self) -> None:
            self._dispatch()

        def _bearer(self) -> str:
            auth = self.headers.get("Authorization", "")
            if auth.startswith("Bearer "):
                return auth[7:].strip()
            raise AuthError("Authorization: Bearer <token> header required")

        def _body(self) -> dict:
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length)
            if not raw:
                return {}
            return json.loads(raw)

        def _file(self, name: str, content_type: str) -> None:
            body = (STATIC_DIR / name).read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _json(self, status: int, value: Any) -> None:
            body = _dumps(value)
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt: str, *args: Any) -> None:
            return

    return Handler


def create_server(
    store: SQLiteFreightStore,
    host: str = "127.0.0.1",
    port: int = 8080,
) -> ThreadingHTTPServer:
    """Create and bind the HTTP server but do NOT start serving.

    Call server.serve_forever() to begin accepting requests.
    """
    Handler = _make_handler(store)
    return ThreadingHTTPServer((host, port), Handler)
