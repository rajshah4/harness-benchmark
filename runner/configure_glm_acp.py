#!/usr/bin/env python3
"""Configure reproducible Pi and OpenCode GLM profiles for the benchmark."""

from __future__ import annotations

import json
import urllib.request
from pathlib import Path


BASE_URL = "http://127.0.0.1:18000"
KEY_FILE = Path.home() / ".openhands" / "agent-canvas" / "api-key.txt"
ROOT = Path(__file__).resolve().parent
MODEL = "openhands/glm-5.2"


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
        "ODSC-Pi-GLM52": {
            "schema_version": 2,
            "id": "c4f965b9-f871-4e1f-a9d9-fdfb48d9caf9",
            "name": "ODSC-Pi-GLM52",
            "agent_kind": "acp",
            "acp_server": "custom",
            "acp_model": MODEL,
            "acp_prompt_timeout": 1800.0,
            "acp_startup_timeout": 90.0,
            "acp_command": (
                f"env PI_CODING_AGENT_DIR={ROOT / 'configs' / 'pi-glm'} pi-acp"
            ),
        },
        "ODSC-OpenCode-GLM52": {
            "schema_version": 2,
            "id": "3aa4ff2d-7194-4193-a644-ee193f3fede6",
            "name": "ODSC-OpenCode-GLM52",
            "agent_kind": "acp",
            "acp_server": "custom",
            "acp_model": MODEL,
            "acp_prompt_timeout": 1800.0,
            "acp_startup_timeout": 90.0,
            "acp_command": (
                f"env OPENCODE_CONFIG={ROOT / 'configs' / 'opencode-glm.json'} "
                "ODSC_LLM_BASE_URL=http://127.0.0.1:4010/v1/opencode opencode acp"
            ),
        },
    }
    for name, profile in profiles.items():
        request(f"/api/agent-profiles/{name}", profile)
    print(json.dumps({"configured": sorted(profiles), "model": MODEL}))


if __name__ == "__main__":
    main()
