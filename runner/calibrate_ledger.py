#!/usr/bin/env python3
"""Run the three-turn provider-ledger calibration for one or more harnesses."""

from __future__ import annotations

import argparse
import json
import tempfile
from datetime import datetime
from pathlib import Path

from run_suite import (
    EVIDENCE_ROOT,
    HARNESS_MODELS,
    KEY_FILE,
    PROFILE_NAMES,
    all_events,
    event_metrics,
    llm_secret_for_harness,
    request_json,
    resolve_curated_agent_settings,
    resolve_profile_ids,
    set_ledger_context,
    wait_for_terminal,
)


def message(turn: int) -> str:
    # Keep the reusable prefix above provider cache-eligibility thresholds even
    # for lightweight harnesses with small system prompts and tool schemas.
    prefix = "CACHE-CALIBRATION-STABLE-PREFIX " * 1200
    return (
        f"{prefix}\n\n"
        f"This is calibration turn {turn}. Do not use tools or edit files. "
        f"Reply with exactly: CALIBRATION-{turn}."
    )


def create_conversation(
    profile_id: str | None,
    agent_settings: dict | None,
    harness: str,
    workspace: Path,
) -> str:
    key = KEY_FILE.read_text(encoding="utf-8").strip()
    payload = {
        "workspace": {"kind": "LocalWorkspace", "working_dir": str(workspace)},
        "initial_message": {
            "role": "user",
            "content": [{"type": "text", "text": message(1)}],
            "run": True,
        },
        "confirmation_policy": {"kind": "NeverConfirm"},
        "max_iterations": 20,
        "stuck_detection": True,
        "autotitle": False,
        "worktree": False,
        "secrets": {"LLM_API_KEY": llm_secret_for_harness(harness, key)},
    }
    if agent_settings is None:
        payload["agent_profile_id"] = profile_id
    else:
        payload["agent_settings"] = agent_settings
    record = request_json(
        "POST",
        "/api/conversations",
        payload,
    )
    return record["id"]


def send_turn(conversation_id: str, turn: int) -> None:
    request_json(
        "POST",
        f"/api/conversations/{conversation_id}/events",
        {
            "role": "user",
            "content": [{"type": "text", "text": message(turn)}],
            "run": True,
        },
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", default=datetime.now().strftime("%Y%m%d-%H%M%S"))
    parser.add_argument("--ledger-proxy-url", required=True)
    parser.add_argument(
        "--harness", action="append",
        choices=(
            "openhands", "openhands-sonnet", "pi", "pi-sonnet",
            "opencode", "opencode-sonnet", "openhands-sonnet46", "pi-sonnet46",
        )
    )
    parser.add_argument(
        "--skill-context",
        choices=("profile-default", "canvas-curated"),
        default="profile-default",
    )
    args = parser.parse_args()
    harnesses = tuple(args.harness or ("openhands", "pi", "opencode"))
    if args.skill_context == "canvas-curated":
        profiles: dict[str, str] = {}
        settings = resolve_curated_agent_settings(harnesses)
    else:
        profiles = resolve_profile_ids(harnesses)
        settings = {}
    results = {
        "run_id": args.run_id,
        "turns": 3,
        "skill_context": args.skill_context,
        "harnesses": {},
    }
    for harness in harnesses:
        with tempfile.TemporaryDirectory(prefix=f"odsc-c2-{harness}-") as temp_dir:
            workspace = Path(temp_dir)
            turns = []
            set_ledger_context(args.ledger_proxy_url, harness, run_id=args.run_id, task_id="C2", phase="turn-1")
            started = datetime.now().astimezone().isoformat()
            agent_settings = settings.get(harness)
            conversation_id = create_conversation(
                profiles.get(harness), agent_settings, harness, workspace
            )
            wait_for_terminal({harness: conversation_id}, 300)
            turns.append({"turn": 1, "submitted_at": started, "terminal_at": datetime.now().astimezone().isoformat()})
            for turn in (2, 3):
                set_ledger_context(args.ledger_proxy_url, harness, run_id=args.run_id, task_id="C2", phase=f"turn-{turn}")
                started = datetime.now().astimezone().isoformat()
                send_turn(conversation_id, turn)
                wait_for_terminal({harness: conversation_id}, 300)
                turns.append({"turn": turn, "submitted_at": started, "terminal_at": datetime.now().astimezone().isoformat()})
            events = all_events(conversation_id)
            results["harnesses"][harness] = {
                "conversation_id": conversation_id,
                "model": HARNESS_MODELS[harness],
                "skill_names": [
                    skill.get("name")
                    for skill in (agent_settings or {}).get("agent_context", {}).get("skills", [])
                    if isinstance(skill, dict)
                ],
                "enable_sub_agents": (agent_settings or {}).get("enable_sub_agents"),
                "agent_kind": (agent_settings or {}).get("agent_kind"),
                "turns": turns,
                "trace": event_metrics(events, harness, workspace),
            }
            set_ledger_context(args.ledger_proxy_url, harness, active=False)
    EVIDENCE_ROOT.mkdir(parents=True, exist_ok=True)
    output = EVIDENCE_ROOT / f"{args.run_id}.json"
    output.write_text(json.dumps(results, indent=2) + "\n", encoding="utf-8")
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
