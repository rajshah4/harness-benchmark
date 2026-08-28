"""HTTP API server for the freight control tower."""
from __future__ import annotations

import dataclasses
import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .exceptions import (
    AuthenticationError,
    AuthorizationError,
    IdempotencyConflict,
    NotFoundError,
    ValidationError,
    VersionConflict,
)
from .sqlite_store import SQLiteFreightStore

STATIC_DIR = Path(__file__).with_name("static")


def _to_json(obj: object) -> object:
    """Recursively convert domain objects to JSON-serialisable structures."""
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        result = {}
        for f in dataclasses.fields(obj):  # type: ignore[arg-type]
            v = getattr(obj, f.name)
            result[f.name] = _to_json(v)
        return result
    if isinstance(obj, list):
        return [_to_json(i) for i in obj]
    if hasattr(obj, "value"):  # StrEnum
        return obj.value
    return obj


def create_server(
    store: SQLiteFreightStore,
    host: str = "127.0.0.1",
    port: int = 8080,
) -> ThreadingHTTPServer:
    """Create an HTTP server wrapping *store*.  Does NOT start serving."""

    class Handler(BaseHTTPRequestHandler):
        # ----------------------------------------------------------------
        # Dispatch
        # ----------------------------------------------------------------

        def do_GET(self) -> None:
            self._dispatch("GET")

        def do_POST(self) -> None:
            self._dispatch("POST")

        def do_PUT(self) -> None:
            self._dispatch("PUT")

        def do_DELETE(self) -> None:
            self._dispatch("DELETE")

        def _dispatch(self, method: str) -> None:
            parsed = urlparse(self.path)
            path = parsed.path
            qs = parse_qs(parsed.query)
            try:
                self._route(method, path, qs)
            except AuthenticationError as exc:
                self._json(401, {"error": str(exc)})
            except AuthorizationError as exc:
                self._json(403, {"error": str(exc)})
            except NotFoundError as exc:
                self._json(404, {"error": str(exc)})
            except ValidationError as exc:
                self._json(400, {"error": str(exc)})
            except IdempotencyConflict as exc:
                self._json(409, {"error": str(exc), "type": "idempotency_conflict"})
            except VersionConflict as exc:
                self._json(409, {"error": str(exc), "type": "version_conflict"})
            except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
                self._json(400, {"error": f"Bad request: {exc}"})
            except Exception as exc:
                self._json(500, {"error": f"Internal error: {type(exc).__name__}: {exc}"})

        def _route(self, method: str, path: str, qs: dict) -> None:
            tok = self._token()

            # ----- health (no auth) -----
            if method == "GET" and path == "/api/health":
                self._json(200, {"status": "ok"})
                return

            # ----- static files -----
            if method == "GET" and path in {"/", "/index.html"}:
                self._file("index.html", "text/html; charset=utf-8")
                return
            if method == "GET" and path == "/app.js":
                self._file("app.js", "text/javascript; charset=utf-8")
                return
            if method == "GET" and path == "/styles.css":
                self._file("styles.css", "text/css; charset=utf-8")
                return

            # ----- tenant bootstrap -----
            if method == "POST" and path == "/api/tenants":
                body = self._body()
                result = store.bootstrap_tenant(
                    body["tenant_id"], body["name"], body["admin_token"]
                )
                self._json(201, result)
                return

            # ----- credentials -----
            if method == "POST" and path == "/api/credentials":
                body = self._body()
                result = store.create_credential(
                    tok, body["token"], body["role"]
                )
                self._json(201, result)
                return

            # ----- shipments -----
            m = re.match(r"^/api/shipments/([^/]+)/events$", path)
            if m and method == "POST":
                sid = m.group(1)
                body = self._body()
                result = store.ingest_event(
                    tok,
                    sid,
                    body["event_id"],
                    body["event_type"],
                    float(body["event_time"]),
                    location=body.get("location"),
                    details=body.get("details"),
                )
                self._json(200, _to_json(result))
                return

            m = re.match(r"^/api/shipments/([^/]+)$", path)
            if m and method == "GET":
                self._json(200, _to_json(store.get_shipment(tok, m.group(1))))
                return

            if path == "/api/shipments":
                if method == "GET":
                    filters = {k: qs.get(k, [None])[0] for k in ("status", "reference")}
                    self._json(200, _to_json(store.list_shipments(tok, **filters)))
                    return
                if method == "POST":
                    body = self._body()
                    result = store.create_shipment(tok, body["reference"])
                    self._json(201, _to_json(result))
                    return

            # ----- exceptions -----
            m = re.match(r"^/api/exceptions/([^/]+)/mutate$", path)
            if m and method == "POST":
                exc_id = m.group(1)
                body = self._body()
                result = store.mutate_exception(
                    tok,
                    exc_id,
                    int(body["expected_version"]),
                    body["action"],
                    actor=body.get("actor"),
                    **{
                        k: body[k]
                        for k in ("assignee", "note")
                        if k in body
                    },
                )
                self._json(200, _to_json(result))
                return

            m = re.match(r"^/api/exceptions/([^/]+)$", path)
            if m and method == "GET":
                self._json(200, _to_json(store.get_exception(tok, m.group(1))))
                return

            if path == "/api/exceptions" and method == "GET":
                filters = {
                    k: qs.get(k, [None])[0]
                    for k in ("status", "severity", "assignee", "shipment_id")
                }
                self._json(200, _to_json(store.list_exceptions(tok, **filters)))
                return

            # ----- audit -----
            if path == "/api/audit" and method == "GET":
                filters = {
                    k: qs.get(k, [None])[0]
                    for k in ("resource_type", "resource_id", "actor", "action")
                }
                self._json(200, _to_json(store.audit(tok, **filters)))
                return

            # ----- SLA rules -----
            if path == "/api/sla-rules":
                if method == "GET":
                    self._json(200, _to_json(store.list_sla_rules(tok)))
                    return
                if method == "POST":
                    body = self._body()
                    result = store.set_sla_rule(
                        tok, body["severity"], int(body["delay_seconds"])
                    )
                    self._json(200, _to_json(result))
                    return

            # ----- tick -----
            if path == "/api/tick" and method == "POST":
                body = self._body() if self._content_length() else {}
                import time as _time
                now = float(body.get("now", _time.time()))
                limit = int(body.get("limit", 100))
                count = store.tick(now, limit)
                self._json(200, {"enqueued": count})
                return

            # ----- deliveries -----
            m = re.match(r"^/api/deliveries/([^/]+)/complete$", path)
            if m and method == "POST":
                import time as _time
                body = self._body() if self._content_length() else {}
                result = store.complete_delivery(
                    m.group(1), body.get("worker_id", "http"),
                    float(body.get("now", _time.time())),
                )
                self._json(200, _to_json(result))
                return

            m = re.match(r"^/api/deliveries/([^/]+)/fail$", path)
            if m and method == "POST":
                import time as _time
                body = self._body()
                result = store.fail_delivery(
                    m.group(1),
                    body.get("worker_id", "http"),
                    body.get("error", ""),
                    float(body.get("now", _time.time())),
                )
                self._json(200, _to_json(result))
                return

            m = re.match(r"^/api/deliveries/([^/]+)/replay$", path)
            if m and method == "POST":
                import time as _time
                body = self._body() if self._content_length() else {}
                result = store.replay_delivery(
                    tok, m.group(1), float(body.get("now", _time.time()))
                )
                self._json(200, _to_json(result))
                return

            if path == "/api/deliveries/claim" and method == "POST":
                import time as _time
                body = self._body() if self._content_length() else {}
                result = store.claim_delivery(
                    body.get("worker_id", "http"),
                    float(body.get("now", _time.time())),
                )
                self._json(200, _to_json(result) if result else None)
                return

            if path == "/api/deliveries" and method == "GET":
                filters = {
                    k: qs.get(k, [None])[0]
                    for k in ("status", "delivery_type")
                }
                self._json(200, _to_json(store.list_deliveries(tok, **filters)))
                return

            # ----- snapshot -----
            if path == "/api/snapshot" and method == "GET":
                self._json(200, store.export_snapshot(tok))
                return

            if path == "/api/snapshot" and method == "POST":
                body = self._body()
                store.import_snapshot(tok, body)
                self._json(200, {"imported": True})
                return

            self._json(404, {"error": f"No route for {method} {path}"})

        # ----------------------------------------------------------------
        # Helpers
        # ----------------------------------------------------------------

        def _token(self) -> str:
            auth = self.headers.get("Authorization", "")
            if auth.startswith("Bearer "):
                return auth[7:]
            return ""

        def _content_length(self) -> int:
            return int(self.headers.get("Content-Length", "0"))

        def _body(self) -> dict:
            raw = self.rfile.read(self._content_length())
            return json.loads(raw) if raw else {}

        def _file(self, name: str, content_type: str) -> None:
            body = (STATIC_DIR / name).read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _json(self, status: int, value: object) -> None:
            body = json.dumps(value, default=str).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt: str, *args: object) -> None:
            return  # silence default stderr logging

    return ThreadingHTTPServer((host, port), Handler)
