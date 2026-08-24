#!/usr/bin/env python3
"""Configure native, Pi, and OpenCode DeepSeek V4 benchmark profiles."""

from __future__ import annotations

import json
import urllib.parse
import urllib.request
import uuid
from pathlib import Path


BASE_URL = "http://127.0.0.1:18000"
KEY_FILE = Path.home() / ".openhands" / "agent-canvas" / "api-key.txt"
ROOT = Path(__file__).resolve().parent
MODEL = "openhands/deepseek-v4-pro"


def request(method: str, path: str, payload: dict | None = None) -> dict:
    data = None if payload is None else json.dumps(payload).encode()
    req = urllib.request.Request(
        BASE_URL + path,
        data=data,
        method=method,
        headers={
            "X-Session-API-Key": KEY_FILE.read_text().strip(),
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as response:
        return json.load(response)


def secret_value(path: str) -> str:
    req = urllib.request.Request(
        BASE_URL + path,
        headers={"X-Session-API-Key": KEY_FILE.read_text().strip()},
    )
    with urllib.request.urlopen(req, timeout=30) as response:
        return response.read().decode().strip()


def main() -> None:
    # Reuse the tested native GLM defaults, changing only model and ledger
    # route. Resolve the stored LLM key at runtime and never print it.
    glm = request("GET", "/api/profiles/glm-5.2")["config"]
    llm_key = secret_value("/api/settings/secrets/LLM_API_KEY")
    if not isinstance(llm_key, str) or not llm_key:
        raise RuntimeError("LLM_API_KEY is unavailable")
    glm.update(
        {
            "model": MODEL,
            "api_key": llm_key,
            "base_url": "http://127.0.0.1:4010/v1/openhands-deepseek",
        }
    )
    request(
        "POST",
        "/api/profiles/deepseek-v4",
        {"llm": glm, "include_secrets": True},
    )

    profiles = {
        "DeepSeek-V4": {
            "schema_version": 2,
            "id": str(uuid.UUID("08ce0749-741e-4f69-9be8-1f01fdb3be58")),
            "name": "DeepSeek-V4",
            "agent_kind": "openhands",
            "llm_profile_ref": "deepseek-v4",
            "agent": "CodeActAgent",
            "disabled_skills": [],
            "condenser": {
                "enabled": True,
                "max_size": 240,
                "condenser_kind": "llm_summarizing",
                "keep_first": 2,
                "minimum_progress": 0.1,
                "hard_context_reset_max_retries": 5,
                "hard_context_reset_context_scaling": 0.8,
            },
            "enable_sub_agents": True,
            "enable_switch_llm_tool": False,
            "tool_concurrency_limit": 1,
        },
        "ODSC-Pi-DeepSeekV4": {
            "schema_version": 2,
            "id": "935ad8d1-20e8-49f8-a9b2-71222d4bb1db",
            "name": "ODSC-Pi-DeepSeekV4",
            "agent_kind": "acp",
            "acp_server": "custom",
            "acp_model": MODEL,
            "acp_prompt_timeout": 1800.0,
            "acp_startup_timeout": 90.0,
            "acp_command": (
                f"env PI_CODING_AGENT_DIR={ROOT / 'configs' / 'pi-deepseek'} pi-acp"
            ),
        },
        "ODSC-OpenCode-DeepSeekV4": {
            "schema_version": 2,
            "id": "31f0c8de-af85-4353-a1d8-8fa99d025387",
            "name": "ODSC-OpenCode-DeepSeekV4",
            "agent_kind": "acp",
            "acp_server": "custom",
            "acp_model": MODEL,
            "acp_prompt_timeout": 1800.0,
            "acp_startup_timeout": 90.0,
            "acp_command": (
                f"env OPENCODE_CONFIG={ROOT / 'configs' / 'opencode-deepseek.json'} "
                "ODSC_LLM_BASE_URL=http://127.0.0.1:4010/v1/opencode-deepseek "
                "opencode acp"
            ),
        },
    }
    for name, profile in profiles.items():
        request(
            "POST",
            f"/api/agent-profiles/{urllib.parse.quote(name, safe='')}",
            profile,
        )
    print(json.dumps({"configured": sorted(profiles), "model": MODEL}))


if __name__ == "__main__":
    main()
