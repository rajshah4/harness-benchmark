#!/usr/bin/env python3
"""Configure Codex ACP profiles for the same-model benchmark matrix."""

from __future__ import annotations

import json
import urllib.request
from pathlib import Path


BASE_URL = "http://127.0.0.1:18000"
KEY_FILE = Path.home() / ".openhands" / "agent-canvas" / "api-key.txt"
ROOT = Path(__file__).resolve().parent


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


def profile(name: str, profile_id: str, model: str, config_dir: str) -> dict:
    return {
        "schema_version": 2,
        "id": profile_id,
        "name": name,
        "agent_kind": "acp",
        "acp_server": "codex",
        "acp_model": model,
        "acp_prompt_timeout": 1800.0,
        "acp_startup_timeout": 90.0,
        "acp_command": (
            f"env CODEX_HOME={ROOT / 'configs' / config_dir} "
            "npx -y @agentclientprotocol/codex-acp@1.1.7"
        ),
    }


def main() -> None:
    profiles = {
        "ODSC-Codex-GLM52": profile(
            "ODSC-Codex-GLM52",
            "be45715d-4f20-41ad-8198-602278b44831",
            "glm-5.2",
            "codex-glm",
        ),
        "ODSC-Codex-DeepSeekV4": profile(
            "ODSC-Codex-DeepSeekV4",
            "194de147-8d9e-46ef-bb56-14e44670fe03",
            "deepseek-v4-pro",
            "codex-deepseek",
        ),
        "ODSC-Codex-Sonnet": profile(
            "ODSC-Codex-Sonnet",
            "51961987-8014-4bc0-b056-bd47e22f2ba6",
            "claude-sonnet-4-5-20250929",
            "codex-sonnet",
        ),
    }
    for name, value in profiles.items():
        request(f"/api/agent-profiles/{name}", value)
    print(json.dumps({"configured": sorted(profiles)}))


if __name__ == "__main__":
    main()
