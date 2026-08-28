#!/usr/bin/env python3
"""Configure Pi with Sonnet 4.6 through the OpenHands provider."""

from __future__ import annotations

import json
import urllib.request
from pathlib import Path


BASE_URL = "http://127.0.0.1:18000"
KEY_FILE = Path.home() / ".openhands" / "agent-canvas" / "api-key.txt"
ROOT = Path(__file__).resolve().parent
MODEL = "openhands/claude-sonnet-4-6"


def request(path: str, payload: dict) -> None:
    req = urllib.request.Request(
        BASE_URL + path,
        data=json.dumps(payload).encode(),
        method="POST",
        headers={
            "X-Session-API-Key": KEY_FILE.read_text().strip(),
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=30):
        pass


def main() -> None:
    profile = {
        "schema_version": 2,
        "id": "022c2b85-7a88-4010-ae6f-a5e119b9c21f",
        "name": "ODSC-Pi-Sonnet46",
        "agent_kind": "acp",
        "acp_server": "custom",
        "acp_model": MODEL,
        "acp_prompt_timeout": 2400.0,
        "acp_startup_timeout": 90.0,
        "acp_command": f"env PI_CODING_AGENT_DIR={ROOT / 'configs' / 'pi-sonnet46'} pi-acp",
    }
    request("/api/agent-profiles/ODSC-Pi-Sonnet46", profile)
    print(json.dumps({"configured": profile["name"], "model": MODEL}))


if __name__ == "__main__":
    main()
