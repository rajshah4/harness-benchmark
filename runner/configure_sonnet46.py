#!/usr/bin/env python3
"""Configure native OpenHands with Sonnet 4.6 through the OpenHands provider."""

from __future__ import annotations

import json
import os
import urllib.request
from pathlib import Path


BASE_URL = "http://127.0.0.1:18000"
KEY_FILE = Path.home() / ".openhands" / "agent-canvas" / "api-key.txt"
MODEL = "openhands/claude-sonnet-4-6"


def request(method: str, path: str, payload: dict | None = None):
    req = urllib.request.Request(
        BASE_URL + path,
        data=None if payload is None else json.dumps(payload).encode(),
        method=method,
        headers={
            "X-Session-API-Key": KEY_FILE.read_text().strip(),
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as response:
        body = response.read().decode()
        return json.loads(body) if body.startswith(("{", "[")) else body


def main() -> None:
    api_key = os.environ.get("LLM_API_KEY") or request(
        "GET", "/api/settings/secrets/LLM_API_KEY"
    )
    proxy_base = os.environ.get("ODSC_SONNET46_PROXY_BASE_URL", "").rstrip("/")
    base_url = f"{proxy_base}/v1/openhands-sonnet46" if proxy_base else None
    request("POST", "/api/profiles/sonnet-4.6", {
        "llm": {
            "model": MODEL,
            "api_key": api_key,
            "base_url": base_url,
            "num_retries": 5,
            "timeout": 300,
            "stream": True,
            "drop_params": True,
            "modify_params": True,
            "native_tool_calling": True,
        },
        "include_secrets": True,
    })
    request("POST", "/api/agent-profiles/Sonnet46", {
        "schema_version": 2,
        "id": "ff271abc-03c6-4c93-b9cd-acde691a8973",
        "name": "Sonnet46",
        "agent_kind": "openhands",
        "llm_profile_ref": "sonnet-4.6",
        "agent": "CodeActAgent",
        "enable_sub_agents": False,
        "enable_switch_llm_tool": False,
        "tool_concurrency_limit": 1,
    })
    print(json.dumps({"configured": "Sonnet46", "model": MODEL, "proxy": bool(proxy_base)}))


if __name__ == "__main__":
    main()
