from __future__ import annotations

import json
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .service import FreightService

STATIC_DIR = Path(__file__).with_name("static")


def json_default(value: object) -> object:
    if hasattr(value, "value"):
        return value.value
    raise TypeError(type(value).__name__)


def create_server(service: FreightService, host: str = "127.0.0.1", port: int = 8080) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            if parsed.path == "/api/shipments":
                tenant = parse_qs(parsed.query).get("tenant", [""])[0]
                self._json(200, [asdict(item) for item in service.list_shipments(tenant)])
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
                payload = self._body()
                value = service.create_shipment(payload["tenant_id"], payload["reference"])
                self._json(201, asdict(value))
            except (KeyError, ValueError, json.JSONDecodeError) as exc:
                self._json(400, {"error": str(exc)})

        def _body(self) -> dict:
            return json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))

        def _file(self, name: str, content_type: str) -> None:
            body = (STATIC_DIR / name).read_bytes()
            self.send_response(200); self.send_header("Content-Type", content_type); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)

        def _json(self, status: int, value: object) -> None:
            body = json.dumps(value, default=json_default).encode()
            self.send_response(status); self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            return

    return ThreadingHTTPServer((host, port), Handler)
