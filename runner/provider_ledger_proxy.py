#!/usr/bin/env python3
"""Record content-free provider usage for controlled harness comparisons."""

from __future__ import annotations

import argparse
import hashlib
import json
import threading
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def response_usage_metadata(body: bytes, content_type: str) -> tuple[str | None, dict[str, Any] | None]:
    """Extract usage only. Response text and tool output are never persisted."""
    if "text/event-stream" in content_type:
        usage = None
        response_id = None
        for line in body.decode("utf-8", errors="replace").splitlines():
            if not line.startswith("data:"):
                continue
            payload = line.removeprefix("data:").strip()
            if not payload or payload == "[DONE]":
                continue
            try:
                event = json.loads(payload)
            except json.JSONDecodeError:
                continue
            if isinstance(event, dict) and isinstance(event.get("usage"), dict):
                usage = event["usage"]
                response_id = event.get("id") if isinstance(event.get("id"), str) else None
        return response_id, usage
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return None, None
    if isinstance(payload, dict) and isinstance(payload.get("usage"), dict):
        response_id = payload.get("id") if isinstance(payload.get("id"), str) else None
        return response_id, payload["usage"]
    return None, None


def usage_from_response(body: bytes, content_type: str) -> dict[str, Any] | None:
    """Return usage only for callers that do not need the provider response ID."""
    return response_usage_metadata(body, content_type)[1]


def request_metadata(body: bytes) -> dict[str, Any]:
    """Return safe request metadata without retaining prompt or tool content."""
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return {"request_json": False}
    if not isinstance(payload, dict):
        return {"request_json": False}
    return {
        "request_json": True,
        "model": payload.get("model"),
        "stream": payload.get("stream", False),
        "message_count": len(payload.get("messages", []))
        if isinstance(payload.get("messages"), list)
        else None,
        "tool_count": len(payload.get("tools", []))
        if isinstance(payload.get("tools"), list)
        else None,
    }


class Ledger:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()
        self.contexts: dict[str, dict[str, str]] = {}

    def set_context(self, harness: str, context: dict[str, str] | None) -> None:
        with self.lock:
            if context is None:
                self.contexts.pop(harness, None)
            else:
                self.contexts[harness] = context

    def context_for(self, harness: str) -> dict[str, str] | None:
        with self.lock:
            value = self.contexts.get(harness)
            return dict(value) if value else None

    def append(self, record: dict[str, Any]) -> None:
        with self.lock:
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, sort_keys=True) + "\n")


class ProxyHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server: "LedgerProxyServer"

    def log_message(self, format: str, *args: object) -> None:
        return

    def _read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length", "0"))
        return self.rfile.read(length)

    def _json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path == "/healthz":
            self._json(200, {"status": "ok"})
            return
        self._json(404, {"detail": "not found"})

    def do_POST(self) -> None:
        if self.path == "/__odsc/context":
            self._set_context()
            return
        self._forward()

    def _set_context(self) -> None:
        try:
            payload = json.loads(self._read_body())
        except json.JSONDecodeError:
            self._json(400, {"detail": "invalid JSON"})
            return
        harness = payload.get("harness") if isinstance(payload, dict) else None
        if not isinstance(harness, str) or not harness:
            self._json(400, {"detail": "harness is required"})
            return
        if payload.get("active") is False:
            self.server.ledger.set_context(harness, None)
        else:
            context = {
                key: value
                for key, value in payload.items()
                if key in {"run_id", "task_id", "phase"} and isinstance(value, str)
            }
            self.server.ledger.set_context(harness, context)
        self._json(200, {"status": "ok"})

    def _forward(self) -> None:
        parsed = urlsplit(self.path)
        parts = [part for part in parsed.path.split("/") if part]
        if len(parts) < 3 or parts[0] != "v1":
            self._json(404, {"detail": "expected /v1/<harness>/<endpoint>"})
            return
        harness = parts[1]
        upstream_path = "/v1/" + "/".join(parts[2:])
        if parsed.query:
            upstream_path += "?" + parsed.query
        body = self._read_body()
        request_id = str(uuid.uuid4())
        metadata = request_metadata(body)
        headers = {
            key: value
            for key, value in self.headers.items()
            if key.lower()
            not in {"host", "content-length", "connection", "accept-encoding"}
        }
        upstream_url = self.server.upstream_base + upstream_path
        response_body = b""
        response_headers: dict[str, str] = {}
        response_status = 502
        error_type = None
        try:
            request = Request(upstream_url, data=body, method="POST", headers=headers)
            with urlopen(request, timeout=self.server.timeout_seconds) as response:
                response_status = response.status
                response_headers = dict(response.headers.items())
                response_body = response.read()
        except HTTPError as error:
            response_status = error.code
            response_headers = dict(error.headers.items())
            response_body = error.read()
            error_type = type(error).__name__
        except Exception as error:  # pragma: no cover. Network errors vary by OS.
            error_type = type(error).__name__
            response_body = json.dumps({"detail": "upstream request failed"}).encode("utf-8")
            response_headers = {"Content-Type": "application/json"}

        content_type = response_headers.get("Content-Type", "")
        provider_response_id, raw_usage = response_usage_metadata(response_body, content_type)
        record = {
            "schema_version": 1,
            "recorded_at": now(),
            "request_id": request_id,
            "provider_response_id": provider_response_id,
            "harness": harness,
            "context": self.server.ledger.context_for(harness),
            "upstream_path": upstream_path.split("?", 1)[0],
            "request_sha256": hashlib.sha256(body).hexdigest(),
            "request": metadata,
            "response_status": response_status,
            "response_content_type": content_type,
            "raw_usage": raw_usage,
            "error_type": error_type,
        }
        self.server.ledger.append(record)

        self.send_response(response_status)
        excluded = {"content-length", "connection", "transfer-encoding", "content-encoding"}
        for key, value in response_headers.items():
            if key.lower() not in excluded:
                self.send_header(key, value)
        self.send_header("Content-Length", str(len(response_body)))
        self.end_headers()
        self.wfile.write(response_body)


class LedgerProxyServer(ThreadingHTTPServer):
    def __init__(self, address: tuple[str, int], upstream_base: str, ledger: Ledger, timeout: int):
        super().__init__(address, ProxyHandler)
        self.upstream_base = upstream_base.rstrip("/").removesuffix("/v1")
        self.ledger = ledger
        self.timeout_seconds = timeout


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bind", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=4010)
    parser.add_argument("--upstream-base", required=True)
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument("--timeout-seconds", type=int, default=600)
    args = parser.parse_args()
    server = LedgerProxyServer(
        (args.bind, args.port), args.upstream_base, Ledger(args.ledger), args.timeout_seconds
    )
    print(json.dumps({"bind": args.bind, "port": args.port, "ledger": str(args.ledger)}), flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
