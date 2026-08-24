#!/usr/bin/env python3
"""Add Pi and OpenCode Sonnet profiles to the benchmark Canvas."""

from __future__ import annotations

import json
import urllib.request
from pathlib import Path


BASE_URL = "http://127.0.0.1:18000"
KEY_FILE = Path.home() / ".openhands" / "agent-canvas" / "api-key.txt"
ROOT = Path(__file__).resolve().parent
MODEL = "openhands/claude-sonnet-4-5-20250929"


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
    profiles = {
        "ODSC-Pi-Sonnet": {
            "schema_version": 2,
            "id": "7dd62771-14e7-4c81-a82c-252b9d64b77b",
            "name": "ODSC-Pi-Sonnet",
            "agent_kind": "acp",
            "acp_server": "custom",
            "acp_model": MODEL,
            "acp_prompt_timeout": 1800.0,
            "acp_startup_timeout": 90.0,
            "acp_command": f"env PI_CODING_AGENT_DIR={ROOT / 'configs' / 'pi-sonnet'} pi-acp",
        },
        "ODSC-OpenCode-Sonnet": {
            "schema_version": 2,
            "id": "7d4ba60f-b70b-4238-ac24-5f29321e32bd",
            "name": "ODSC-OpenCode-Sonnet",
            "agent_kind": "acp",
            "acp_server": "custom",
            "acp_model": MODEL,
            "acp_prompt_timeout": 1800.0,
            "acp_startup_timeout": 90.0,
            "acp_command": (
                f"env OPENCODE_CONFIG={ROOT / 'configs' / 'opencode-sonnet.json'} "
                "ODSC_LLM_BASE_URL=http://127.0.0.1:4010/v1/opencode-sonnet opencode acp"
            ),
        },
    }
    for name, profile in profiles.items():
        request(f"/api/agent-profiles/{name}", profile)
    print(json.dumps({"configured": sorted(profiles), "model": MODEL}))


if __name__ == "__main__":
    main()
