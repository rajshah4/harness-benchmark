#!/usr/bin/env python3
"""Record content-free provider usage for controlled harness comparisons."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import threading
import time
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from usage_ledger import cache_observation


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
            if not isinstance(event, dict):
                continue
            event_usage = event.get("usage")
            message = event.get("message")
            response = event.get("response")
            if not isinstance(event_usage, dict) and isinstance(message, dict):
                event_usage = message.get("usage")
            # OpenAI Responses streaming emits the final provider usage inside
            # `response.completed.data.response.usage`, rather than at the
            # event root used by Chat Completions and Anthropic streams.
            if not isinstance(event_usage, dict) and isinstance(response, dict):
                event_usage = response.get("usage")
            if isinstance(event_usage, dict):
                usage = {**(usage or {}), **event_usage}
            candidate_id = event.get("id")
            if not isinstance(candidate_id, str) and isinstance(message, dict):
                candidate_id = message.get("id")
            if not isinstance(candidate_id, str) and isinstance(response, dict):
                candidate_id = response.get("id")
            if isinstance(candidate_id, str):
                response_id = candidate_id
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


def response_error_metadata(body: bytes, content_type: str) -> dict[str, Any] | None:
    """Detect provider errors without retaining messages or response content.

    Streaming APIs can return HTTP 200 and then emit an error event. Only short,
    machine-readable classifications are retained; free-form error messages are
    deliberately ignored.
    """
    categorical = re.compile(r"^[A-Za-z0-9_.:/-]{1,128}$")

    def safe_category(value: Any) -> str | None:
        return value if isinstance(value, str) and categorical.fullmatch(value) else None

    def from_payload(payload: Any, *, source: str, event_name: str | None = None) -> dict[str, Any] | None:
        if not isinstance(payload, dict):
            return None
        error = payload.get("error")
        is_error = payload.get("type") == "error" or isinstance(error, dict) or event_name == "error"
        if not is_error:
            return None
        error_object = error if isinstance(error, dict) else {}
        metadata = {
            "detected": True,
            "source": source,
            "event_type": safe_category(payload.get("type")) or safe_category(event_name),
            "error_type": safe_category(error_object.get("type")),
            "error_code": safe_category(error_object.get("code")),
        }
        return {key: value for key, value in metadata.items() if value is not None}

    if "text/event-stream" in content_type:
        event_name = None
        for line in body.decode("utf-8", errors="replace").splitlines():
            if line.startswith("event:"):
                event_name = line.removeprefix("event:").strip()
                continue
            if not line.startswith("data:"):
                continue
            raw_payload = line.removeprefix("data:").strip()
            if not raw_payload or raw_payload == "[DONE]":
                continue
            try:
                payload = json.loads(raw_payload)
            except json.JSONDecodeError:
                if event_name == "error":
                    return {"detected": True, "source": "sse", "event_type": "error"}
                continue
            metadata = from_payload(payload, source="sse", event_name=event_name)
            if metadata:
                return metadata
        return None

    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return None
    return from_payload(payload, source="json")


def request_metadata(body: bytes) -> dict[str, Any]:
    """Return safe request metadata without retaining prompt or tool content."""
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return {"request_json": False}
    if not isinstance(payload, dict):
        return {"request_json": False}
    messages = payload.get("messages", [])
    tools = payload.get("tools", [])

    def encoded(value: Any) -> bytes:
        return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")

    def fingerprint(value: Any) -> str:
        return hashlib.sha256(encoded(value)).hexdigest()

    message_shape = []
    if isinstance(messages, list):
        for message in messages:
            if not isinstance(message, dict):
                message_shape.append({"type": type(message).__name__})
                continue
            content = message.get("content")
            message_shape.append({
                "role": message.get("role"),
                "content_bytes": len(encoded(content)),
                "has_tool_calls": bool(message.get("tool_calls")),
                "tool_call_count": len(message.get("tool_calls", []))
                if isinstance(message.get("tool_calls"), list) else 0,
            })
    system_messages = [
        message for message in messages
        if isinstance(message, dict) and message.get("role") in {"system", "developer"}
    ] if isinstance(messages, list) else []
    stable_prefix = messages[:-1] if isinstance(messages, list) and messages else []
    parameters = {
        key: payload[key]
        for key in (
            "frequency_penalty", "max_completion_tokens", "max_tokens", "n",
            "parallel_tool_calls", "presence_penalty", "reasoning_effort", "seed",
            "stop", "temperature", "tool_choice", "top_p",
        )
        if key in payload
    }
    return {
        "request_json": True,
        "model": payload.get("model"),
        "stream": payload.get("stream", False),
        "message_count": len(messages) if isinstance(messages, list) else None,
        "tool_count": len(tools) if isinstance(tools, list) else None,
        "request_bytes": len(body),
        "messages_bytes": len(encoded(messages)),
        "tools_bytes": len(encoded(tools)),
        "message_shape": message_shape,
        "messages_sha256": fingerprint(messages),
        "stable_prefix_sha256": fingerprint(stable_prefix),
        "stable_prefix_bytes": len(encoded(stable_prefix)),
        "system_sha256": fingerprint(system_messages),
        "system_bytes": len(encoded(system_messages)),
        "tools_sha256": fingerprint(tools),
        "parameters": parameters,
    }


def cache_request_hints(payload: bytes, headers: dict[str, str]) -> dict[str, Any]:
    """Capture cache-routing signals without storing request content or secrets."""
    try:
        parsed = json.loads(payload)
    except json.JSONDecodeError:
        parsed = None

    paths: list[str] = []

    def visit(value: Any, path: str = "$") -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                child_path = f"{path}.{key}"
                if key.lower() in {"cache_control", "cachecontrol", "prompt_cache_key", "promptcachekey"}:
                    paths.append(child_path)
                visit(child, child_path)
        elif isinstance(value, list):
            for index, child in enumerate(value):
                visit(child, f"{path}[{index}]")

    visit(parsed)
    beta = headers.get("anthropic-beta") or headers.get("Anthropic-Beta")
    return {
        "cache_control_paths": paths,
        "anthropic_beta_present": beta is not None,
        "anthropic_beta_mentions_prompt_caching": bool(beta and "prompt-caching" in beta),
    }


def safe_response_metadata(headers: dict[str, str], body: bytes, elapsed_ms: float) -> dict[str, Any]:
    """Retain operational cache evidence without credentials or response content."""
    allowed = {
        "age", "cache-control", "cf-cache-status", "x-cache", "x-cache-hits",
        "x-request-id", "x-ratelimit-limit-tokens", "x-ratelimit-remaining-tokens",
    }
    return {
        "elapsed_ms": round(elapsed_ms, 3),
        "response_bytes": len(body),
        "body_sha256": hashlib.sha256(body).hexdigest(),
        "headers": {
            key.lower(): value for key, value in headers.items() if key.lower() in allowed
        },
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
        endpoint_parts = parts[2:]
        # Anthropic-compatible clients may append their own /v1/messages to a
        # routed base URL that already contains /v1/<harness>.
        if endpoint_parts and endpoint_parts[0] == "v1":
            endpoint_parts = endpoint_parts[1:]
        upstream_path = "/v1/" + "/".join(endpoint_parts)
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
        started = time.monotonic()
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
        provider_error = response_error_metadata(response_body, content_type)
        if provider_error and error_type is None:
            error_type = "ProviderStreamError" if provider_error["source"] == "sse" else "ProviderResponseError"
        record = {
            "schema_version": 3,
            "recorded_at": now(),
            "request_id": request_id,
            "provider_response_id": provider_response_id,
            "harness": harness,
            "context": self.server.ledger.context_for(harness),
            "upstream_path": upstream_path.split("?", 1)[0],
            "request_sha256": hashlib.sha256(body).hexdigest(),
            "request": metadata,
            "cache_request_hints": cache_request_hints(body, headers),
            "response_status": response_status,
            "response_content_type": content_type,
            "response": safe_response_metadata(
                response_headers, response_body, (time.monotonic() - started) * 1000
            ),
            "raw_usage": raw_usage,
            "cache_observation": cache_observation(raw_usage),
            "error_type": error_type,
            "provider_error": provider_error,
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
