#!/usr/bin/env python3
"""Run the harness comparison suite through local Agent Canvas."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BENCHMARK = ROOT / "benchmark"
P09 = BENCHMARK / "short-suite"
P09_REPO = P09 / "toy-repo"
P09_TASKS = P09 / "tasks.json"
P09_CHECKER = P09 / "check_task.py"
RATE_LIMITER_DIR = BENCHMARK / "rate-limiter"
SPREAD_PLATE_DIR = BENCHMARK / "spread-plate"
DURABLE_QUEUE_DIR = BENCHMARK / "durable-job-queue"
INCIDENT_OPS_DIR = BENCHMARK / "incident-operations-center"
RUNS_ROOT = ROOT / "benchmark-runs"
EVIDENCE_ROOT = ROOT / "results" / "raw" / "generated"
PI_SESSION_ROOTS = (
    Path.home() / ".pi" / "agent" / "sessions",
    Path.home() / ".pi" / "agent-ledger" / "sessions",
)
OPENCODE_DB = Path.home() / ".local" / "share" / "opencode" / "opencode.db"

BASE_URL = "http://localhost:8000"
LAMINAR_SQL_URL = "https://api.lmnr.ai/v1/sql/query"
SECRET_LOOKUP_URL = "http://localhost:18000/api/settings/secrets/LLM_API_KEY"
LAMINAR_SECRET_LOOKUP_URL = (
    "http://localhost:18000/api/settings/secrets/LAMINAR_API_KEY"
)
KEY_FILE = Path.home() / ".openhands" / "agent-canvas" / "api-key.txt"

PROFILE_NAMES = {
    "openhands": "GLM",
    "pi": "ODSC-Pi-GLM52",
    "opencode": "ODSC-OpenCode-GLM52",
    "codex": "Codex",
    "openhands-sonnet": "Sonnet",
    "pi-sonnet": "ODSC-Pi-Sonnet",
    "opencode-sonnet": "ODSC-OpenCode-Sonnet",
    "openhands-deepseek": "DeepSeek-V4",
    "pi-deepseek": "ODSC-Pi-DeepSeekV4",
    "opencode-deepseek": "ODSC-OpenCode-DeepSeekV4",
    "codex-glm": "ODSC-Codex-GLM52",
    "codex-deepseek": "ODSC-Codex-DeepSeekV4",
    "codex-sonnet": "ODSC-Codex-Sonnet",
}
DEFAULT_HARNESSES = ("openhands", "pi", "opencode")
HARNESS_MODELS = {
    "openhands": "glm-5.2",
    "pi": "glm-5.2",
    "opencode": "glm-5.2",
    "codex": "gpt-5.5",
    "openhands-sonnet": "openhands/claude-sonnet-4-5-20250929",
    "pi-sonnet": "openhands/claude-sonnet-4-5-20250929",
    "opencode-sonnet": "openhands/claude-sonnet-4-5-20250929",
    "openhands-deepseek": "openhands/deepseek-v4-pro",
    "pi-deepseek": "openhands/deepseek-v4-pro",
    "opencode-deepseek": "openhands/deepseek-v4-pro",
    "codex-glm": "glm-5.2",
    "codex-deepseek": "deepseek-v4-pro",
    "codex-sonnet": "claude-sonnet-4-5-20250929",
}

# OpenHands/extensions 0.18.0, consumed by Agent Canvas at the merge commit for
# OpenHands/OpenHands#16860. Keep this explicit so benchmark evidence records
# the exact allow-list instead of silently following a later catalog release.
CURATED_CANVAS_SKILLS = (
    "add-skill",
    "agent-canvas-environment",
    "agent-memory",
    "agent-sdk-builder",
    "code-review",
    "docker",
    "github",
    "openhands-api",
    "openhands-automation",
    "openhands-sdk",
    "skill-creator",
)

# Agent Server's stuck detector has already stopped the agent loop.  Treat it
# as terminal so a benchmark records the failed attempt and the sequential
# matrix can proceed instead of waiting for its wall-clock timeout.
TERMINAL_STATUSES = {"finished", "error", "stopped", "stuck"}

CODEX_CONFIG_DIRS = {
    "codex-glm": Path(__file__).resolve().parent / "configs" / "codex-glm",
    "codex-deepseek": Path(__file__).resolve().parent
    / "configs"
    / "codex-deepseek",
    "codex-sonnet": Path(__file__).resolve().parent / "configs" / "codex-sonnet",
}


def api_key() -> str:
    value = KEY_FILE.read_text(encoding="utf-8").strip()
    if not value:
        raise RuntimeError("Agent Canvas API key is empty")
    return value


def secret_source(name: str, lookup_url: str, key: str) -> dict[str, object]:
    """Use an injected value on clean hosts, with legacy lookup as fallback."""
    value = os.environ.get(name)
    if value:
        return {"kind": "StaticSecret", "value": value}
    return {
        "kind": "LookupSecret",
        "url": lookup_url,
        "headers": {"X-Session-API-Key": key},
    }


def llm_secret_for_harness(harness: str, key: str) -> dict[str, object]:
    return secret_source("LLM_API_KEY", SECRET_LOOKUP_URL, key)


def secret_value(name: str, lookup_url: str, key: str) -> str:
    """Resolve a runtime secret without writing it to benchmark evidence."""
    value = os.environ.get(name)
    if value:
        return value
    request = urllib.request.Request(
        lookup_url,
        method="GET",
        headers={"X-Session-API-Key": key},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        value = response.read().decode("utf-8").strip()
    if not value:
        raise RuntimeError(f"Agent Canvas secret {name} is empty")
    return value


def request_json(method: str, path: str, payload: dict | None = None) -> dict:
    key = api_key()
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        f"{BASE_URL}{path}",
        data=data,
        method=method,
        headers={
            "X-Session-API-Key": key,
            "Content-Type": "application/json",
        },
    )
    attempts = 4 if method == "GET" else 1
    timeout = 120 if method == "POST" else 30
    for attempt in range(1, attempts + 1):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except (TimeoutError, urllib.error.URLError):
            if attempt == attempts:
                raise
            time.sleep(2 * attempt)
    raise AssertionError("request retry loop exited unexpectedly")


def set_ledger_context(
    ledger_proxy_url: str | None,
    harness: str,
    *,
    run_id: str | None = None,
    task_id: str | None = None,
    phase: str | None = None,
    active: bool = True,
) -> None:
    """Label content-free proxy records without sending benchmark text."""
    if not ledger_proxy_url:
        return
    payload: dict[str, str | bool] = {"harness": harness, "active": active}
    if active:
        if not (run_id and task_id and phase):
            raise ValueError("active ledger context requires run, task, and phase")
        payload.update({"run_id": run_id, "task_id": task_id, "phase": phase})
    request = urllib.request.Request(
        f"{ledger_proxy_url.rstrip('/')}/__odsc/context",
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        if response.status != 200:
            raise RuntimeError(f"ledger context update failed: HTTP {response.status}")


def run_command(command: list[str], cwd: Path, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        command,
        cwd=cwd,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=check,
    )


def load_p09_prompts() -> dict[str, str]:
    tasks = json.loads(P09_TASKS.read_text(encoding="utf-8"))
    prompts = {task["id"]: task["prompt"] for task in tasks}
    prompts["p09-task-07"] += (
        "\n\nAcceptance criteria: move at least one helper into a new module whose "
        "filename matches toyapp/report_*.py, leave no function in toyapp/reports.py "
        "longer than 90 lines, and keep tests/test_reports.py passing."
    )
    return prompts


def task_prompt(task_id: str) -> str:
    if task_id == "task08-rate-limiter":
        base = (RATE_LIMITER_DIR / "task.md").read_text(encoding="utf-8")
    elif task_id == "artifactsbench-spread-plate":
        base = (SPREAD_PLATE_DIR / "task.md").read_text(encoding="utf-8")
    elif task_id == "durable-job-queue":
        base = (DURABLE_QUEUE_DIR / "task.md").read_text(encoding="utf-8")
    elif task_id == "incident-operations-center":
        base = (INCIDENT_OPS_DIR / "task.md").read_text(encoding="utf-8")
    else:
        base = load_p09_prompts()[task_id]
    return (
        f"{base}\n\n"
        "Do not modify tests. Work only inside the current repository. Before finishing, "
        "run the relevant tests or behavioral checks and inspect the final git status and diff. "
        "Report files changed, checks run, and any remaining uncertainty."
    )


def prepare_workspace(run_id: str, task_id: str, harness: str) -> tuple[Path, str]:
    workspace = RUNS_ROOT / run_id / task_id / harness
    if workspace.exists():
        raise RuntimeError(f"workspace already exists: {workspace}")
    workspace.mkdir(parents=True)
    if task_id == "durable-job-queue":
        for item in (DURABLE_QUEUE_DIR / "starter").iterdir():
            destination = workspace / item.name
            if item.is_dir():
                shutil.copytree(item, destination)
            else:
                shutil.copy2(item, destination)
    elif task_id == "incident-operations-center":
        for item in (INCIDENT_OPS_DIR / "starter").iterdir():
            destination = workspace / item.name
            if item.is_dir():
                shutil.copytree(item, destination)
            else:
                shutil.copy2(item, destination)
    elif task_id not in {"task08-rate-limiter", "artifactsbench-spread-plate"}:
        for item in P09_REPO.iterdir():
            if item.name in {".pytest_cache", "__pycache__"}:
                continue
            destination = workspace / item.name
            if item.is_dir():
                shutil.copytree(item, destination, ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache"))
            else:
                shutil.copy2(item, destination)
    run_command(["git", "init", "-q"], workspace)
    run_command(["git", "config", "user.name", "ODSC Harness Benchmark"], workspace)
    run_command(["git", "config", "user.email", "benchmark@example.invalid"], workspace)
    run_command(["git", "add", "."], workspace)
    run_command(["git", "commit", "--allow-empty", "-q", "-m", "benchmark baseline"], workspace)
    tree = run_command(["git", "rev-parse", "HEAD^{tree}"], workspace).stdout.strip()
    return workspace, tree


def launch(
    run_id: str,
    task_id: str,
    harness: str,
    profile_id: str | None,
    agent_settings: dict | None,
    workspace: Path,
    enable_laminar: bool,
) -> str:
    if (profile_id is None) == (agent_settings is None):
        raise ValueError("launch requires exactly one agent source")
    key = api_key()
    payload = {
        "workspace": {"kind": "LocalWorkspace", "working_dir": str(workspace)},
        "initial_message": {
            "role": "user",
            "content": [{"type": "text", "text": task_prompt(task_id)}],
            "run": True,
        },
        "confirmation_policy": {"kind": "NeverConfirm"},
        "max_iterations": 1000,
        "stuck_detection": True,
        "autotitle": False,
        "worktree": False,
        "secrets": {
            "LLM_API_KEY": llm_secret_for_harness(harness, key)
        },
        "observability_metadata": {
            "experiment": "odsc-harness-suite",
            "run_id": run_id,
            "task": task_id,
            "harness": harness,
            "model": HARNESS_MODELS[harness],
        },
        "observability_tags": [
            "odsc-harness-suite",
            f"harness:{harness}",
            f"task:{task_id}",
        ],
        "observability_span_name": "odsc.harness.run",
        "tags": {
            "experiment": "odsc-harness-suite",
            "task": task_id,
            "harness": harness,
            "model": HARNESS_MODELS[harness],
        },
    }
    # Agent Server selects an ACP authentication method from the subprocess
    # environment before it starts Codex. Supplying CODEX_HOME only through an
    # `env ...` command prefix is too late: the server can see a host ChatGPT
    # login first and choose subscription authentication even though the child
    # uses a custom provider. Pass the model-specific directory as an ordinary
    # conversation environment value so both sides resolve the same config.
    if harness in CODEX_CONFIG_DIRS:
        payload["secrets"]["CODEX_HOME"] = {
            "kind": "StaticSecret",
            "value": str(CODEX_CONFIG_DIRS[harness]),
        }
    if profile_id is not None:
        payload["agent_profile_id"] = profile_id
    else:
        payload["agent_settings"] = agent_settings
    if enable_laminar:
        payload["secrets"]["LMNR_PROJECT_API_KEY"] = secret_source(
            "LAMINAR_API_KEY", LAMINAR_SECRET_LOOKUP_URL, key
        )
    return request_json("POST", "/api/conversations", payload)["id"]


def resolve_profile_ids(harnesses: tuple[str, ...]) -> dict[str, str]:
    profiles = request_json("GET", "/api/agent-profiles")["profiles"]
    ids_by_name = {
        profile["name"]: profile["id"]
        for profile in profiles
        if profile.get("id")
    }
    missing = [
        PROFILE_NAMES[harness]
        for harness in harnesses
        if PROFILE_NAMES[harness] not in ids_by_name
    ]
    if missing:
        raise RuntimeError(f"Agent Canvas profiles are missing: {', '.join(missing)}")
    return {
        harness: ids_by_name[PROFILE_NAMES[harness]]
        for harness in harnesses
    }


def materialize_profile(profile_name: str) -> dict:
    encoded = urllib.parse.quote(profile_name, safe="")
    response = request_json("POST", f"/api/agent-profiles/{encoded}/materialize", {})
    if not response.get("valid"):
        raise RuntimeError(
            f"Agent Canvas profile {profile_name!r} is invalid: "
            f"{response.get('errors') or 'unknown materialization error'}"
        )
    settings = response.get("resolved_settings")
    if not isinstance(settings, dict):
        raise RuntimeError(
            f"Agent Canvas profile {profile_name!r} did not return resolved settings"
        )
    return settings


def resolve_curated_agent_settings(harnesses: tuple[str, ...]) -> dict[str, dict]:
    """Build inline settings with the same current Canvas skill context.

    Saved Agent Profiles are intentionally a server-side launch boundary. On
    Agent Server 1.42.1, a native OpenHands profile materializes all public
    skills while ACP profiles materialize none. For a controlled comparison we
    source the current serialized catalog from the native profile, select the
    Canvas 11-skill allow-list, and attach the identical objects to every
    materialized harness setting.
    """
    catalog_settings = materialize_profile(PROFILE_NAMES["openhands"])
    catalog_context = catalog_settings.get("agent_context") or {}
    catalog_skills = catalog_context.get("skills") or []
    skills_by_name = {
        skill.get("name"): skill
        for skill in catalog_skills
        if isinstance(skill, dict) and isinstance(skill.get("name"), str)
    }
    missing = [name for name in CURATED_CANVAS_SKILLS if name not in skills_by_name]
    if missing:
        raise RuntimeError(
            "Current Agent Canvas skill catalog is missing curated defaults: "
            + ", ".join(missing)
        )
    curated = [copy.deepcopy(skills_by_name[name]) for name in CURATED_CANVAS_SKILLS]
    key = api_key()

    resolved: dict[str, dict] = {}
    for harness in harnesses:
        settings = copy.deepcopy(materialize_profile(PROFILE_NAMES[harness]))
        if settings.get("agent_kind") == "openhands":
            llm = settings.get("llm")
            if not isinstance(llm, dict):
                raise RuntimeError(
                    f"OpenHands profile {PROFILE_NAMES[harness]!r} has no LLM settings"
                )
            # The materialize endpoint deliberately redacts the saved API key.
            # Inline launches must resolve it just as Canvas does before posting
            # agent_settings. This value remains in memory and is never stored
            # in the result artifact or provider ledger.
            llm["api_key"] = secret_value("LLM_API_KEY", SECRET_LOOKUP_URL, key)
        context = settings.get("agent_context")
        if not isinstance(context, dict):
            context = {}
            settings["agent_context"] = context
        context.update(
            {
                "skills": copy.deepcopy(curated),
                "load_public_skills": False,
                "load_user_skills": True,
                "load_project_skills": True,
                "disabled_skills": [],
            }
        )
        resolved[harness] = settings
    return resolved


def wait_for_terminal(conversation_ids: dict[str, str], timeout_seconds: int) -> dict[str, dict]:
    deadline = time.monotonic() + timeout_seconds
    records: dict[str, dict] = {}
    last_summary = ""
    while time.monotonic() < deadline:
        states = []
        for harness, conversation_id in conversation_ids.items():
            record = request_json("GET", f"/api/conversations/{conversation_id}")
            records[harness] = record
            states.append(f"{harness}={record.get('execution_status')}")
        summary = ", ".join(states)
        if summary != last_summary:
            print(f"  {summary}", flush=True)
            last_summary = summary
        if all(record.get("execution_status") in TERMINAL_STATUSES for record in records.values()):
            return records
        time.sleep(5)
    raise TimeoutError(f"conversations did not finish within {timeout_seconds}s: {last_summary}")


def verify(task_id: str, workspace: Path, verifier_python: str) -> tuple[bool, str]:
    if task_id == "task08-rate-limiter":
        command = [verifier_python, str(RATE_LIMITER_DIR / "verify_task08.py"), str(workspace)]
    elif task_id == "artifactsbench-spread-plate":
        command = [verifier_python, str(SPREAD_PLATE_DIR / "verify_spread_plate.py"), str(workspace)]
    elif task_id == "durable-job-queue":
        command = [verifier_python, str(DURABLE_QUEUE_DIR / "verify_durable_queue.py"), str(workspace)]
    elif task_id == "incident-operations-center":
        command = [
            verifier_python,
            str(INCIDENT_OPS_DIR / "verify_incident_ops.py"),
            str(workspace),
        ]
    else:
        command = [
            verifier_python,
            str(P09_CHECKER),
            "--task",
            task_id,
            "--repo",
            str(workspace),
        ]
    result = run_command(command, ROOT, check=False)
    return result.returncode == 0, result.stdout[-6000:]


def send_repair(conversation_id: str, verifier_output: str) -> None:
    text = (
        "The instructor-owned verifier failed after your first attempt. Fix the implementation, "
        "run the relevant checks again, and inspect the final diff. Do not modify tests.\n\n"
        f"Verifier output:\n{verifier_output[-4000:]}"
    )
    request_json(
        "POST",
        f"/api/conversations/{conversation_id}/events",
        {"role": "user", "content": [{"type": "text", "text": text}], "run": True},
    )


def all_events(conversation_id: str) -> list[dict]:
    items: list[dict] = []
    page_id: str | None = None
    while True:
        query = {"limit": "25"}
        if page_id:
            query["page_id"] = page_id
        data = request_json(
            "GET",
            f"/api/conversations/{conversation_id}/events/search?{urllib.parse.urlencode(query)}",
        )
        items.extend(data.get("items", []))
        page_id = data.get("next_page_id")
        if not page_id:
            return items


def pi_usage(workspace: Path) -> dict | None:
    candidates = []
    workspace_marker = str(workspace).replace("/", "-")
    for session_root in PI_SESSION_ROOTS:
        for path in session_root.rglob("*.jsonl"):
            if workspace_marker not in str(path.parent):
                continue
            candidates.append(path)
    if not candidates:
        return None
    path = max(candidates, key=lambda item: item.stat().st_mtime)
    messages = []
    session_id = None
    for line in path.read_text(encoding="utf-8").splitlines():
        event = json.loads(line)
        if event.get("type") == "session" and event.get("id"):
            session_id = event["id"]
        message = event.get("message", {})
        if event.get("type") == "message" and message.get("role") == "assistant" and message.get("usage"):
            messages.append(message)
    if not messages:
        return None
    return {
        "prompt_tokens": sum(message["usage"].get("input", 0) for message in messages),
        "completion_tokens": sum(message["usage"].get("output", 0) for message in messages),
        "cache_read_tokens": sum(message["usage"].get("cacheRead", 0) for message in messages),
        "cache_write_tokens": sum(message["usage"].get("cacheWrite", 0) for message in messages),
        "reasoning_tokens": sum(message["usage"].get("reasoning", 0) for message in messages),
        "accounted_total_tokens": sum(message["usage"].get("totalTokens", 0) for message in messages),
        "assistant_turns": len(messages),
        "telemetry_session_id": session_id,
        "source": str(path),
    }


def opencode_usage(workspace: Path) -> dict | None:
    """Read aggregate usage from OpenCode's own session record.

    Agent Canvas receives usage attached to the latest ACP relay message. That
    is useful for a live display, but it is not the aggregate for the session.
    OpenCode's local database maintains the full-session counters.
    """
    if not OPENCODE_DB.exists():
        return None
    connection = sqlite3.connect(f"file:{OPENCODE_DB}?mode=ro", uri=True)
    try:
        row = connection.execute(
            """
            SELECT id, tokens_input, tokens_output, tokens_reasoning,
                   tokens_cache_read, tokens_cache_write
            FROM session
            WHERE directory = ?
            ORDER BY time_updated DESC
            LIMIT 1
            """,
            (str(workspace),),
        ).fetchone()
    finally:
        connection.close()
    if row is None:
        return None
    session_id, prompt, completion, reasoning, cache_read, cache_write = row
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "cache_read_tokens": cache_read,
        "cache_write_tokens": cache_write,
        "reasoning_tokens": reasoning,
        "accounted_total_tokens": prompt + completion + cache_read + cache_write,
        "telemetry_session_id": session_id,
        "source": f"opencode-session-db:{session_id}",
    }


def laminar_sql(query: str) -> list[dict]:
    project_key = os.environ.get("LAMINAR_API_KEY")
    if not project_key:
        key_request = urllib.request.Request(
            LAMINAR_SECRET_LOOKUP_URL,
            method="GET",
            headers={"X-Session-API-Key": api_key()},
        )
        with urllib.request.urlopen(key_request, timeout=30) as response:
            project_key = response.read().decode("utf-8").strip()
    request = urllib.request.Request(
        LAMINAR_SQL_URL,
        data=json.dumps({"query": query}).encode("utf-8"),
        method="POST",
        headers={
            "Authorization": f"Bearer {project_key}",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.loads(response.read().decode("utf-8")).get("data", [])


def collect_laminar(results: dict, wait_seconds: int) -> dict:
    """Collect provider-reported trace totals for every completed session."""
    session_map: dict[str, dict[str, str]] = {}
    for task in results["tasks"]:
        for harness, result in task["harnesses"].items():
            if harness in {"openhands", "codex"}:
                session_id = result["conversation_id"]
            else:
                session_id = result["trace"]["usage"].get("telemetry_session_id")
            if session_id:
                result["laminar_session_id"] = session_id
                session_map[session_id] = {
                    "task": task["task_id"],
                    "harness": harness,
                }
    if not session_map:
        return {"status": "no-session-ids", "sessions": []}
    if wait_seconds:
        time.sleep(wait_seconds)
    quoted_ids = ", ".join(
        "'" + session_id.replace("'", "''") + "'" for session_id in session_map
    )
    query = f"""
        SELECT
            session_id,
            count() AS trace_count,
            sum(input_tokens) AS input_tokens,
            sum(output_tokens) AS output_tokens,
            sum(total_tokens) AS total_tokens,
            sum(cache_read_input_tokens) AS cache_read_input_tokens,
            sum(cache_creation_input_tokens) AS cache_creation_input_tokens,
            sum(reasoning_tokens) AS reasoning_tokens,
            sum(total_cost) AS total_cost
        FROM traces
        WHERE start_time > now() - INTERVAL 1 DAY
          AND session_id IN ({quoted_ids})
        GROUP BY session_id
        ORDER BY session_id
    """
    rows = laminar_sql(query)
    for row in rows:
        row.update(session_map.get(row.get("session_id", ""), {}))
    return {
        "status": "complete" if len(rows) == len(session_map) else "partial",
        "expected_sessions": len(session_map),
        "observed_sessions": len(rows),
        "sessions": rows,
    }


def event_metrics(events: list[dict], harness: str, workspace: Path) -> dict:
    timestamps = [event["timestamp"] for event in events if event.get("timestamp")]
    elapsed_seconds = None
    if len(timestamps) >= 2:
        elapsed_seconds = round(
            (
                datetime.fromisoformat(max(timestamps).replace("Z", "+00:00"))
                - datetime.fromisoformat(min(timestamps).replace("Z", "+00:00"))
            ).total_seconds(),
            3,
        )
    status_updates = [
        event.get("value")
        for event in events
        if event.get("key") == "execution_status"
    ]
    metric_updates = [
        event.get("value", {}).get("usage_to_metrics", {})
        for event in events
        if event.get("key") == "stats"
    ]
    metric_updates = [item for item in metric_updates if item]
    native_metrics = metric_updates[-1] if metric_updates else {}
    metric_name = "default" if "default" in native_metrics else next(iter(native_metrics), None)
    selected_metrics = native_metrics.get(metric_name, {}) if metric_name else {}
    accumulated = selected_metrics.get("accumulated_token_usage", {})
    usage = {
        "prompt_tokens": accumulated.get("prompt_tokens", 0),
        "completion_tokens": accumulated.get("completion_tokens", 0),
        "cache_read_tokens": accumulated.get("cache_read_tokens", 0),
        "cache_write_tokens": accumulated.get("cache_write_tokens", 0),
        "reasoning_tokens": accumulated.get("reasoning_tokens", 0),
        "accounted_total_tokens": (
            accumulated.get("prompt_tokens", 0)
            + accumulated.get("completion_tokens", 0)
            + accumulated.get("cache_read_tokens", 0)
        ),
        "source": "agent-server-native-metrics",
    }
    if harness.startswith("openhands"):
        tool_calls = sum(event.get("kind") == "ActionEvent" for event in events)
    else:
        tool_calls = len(
            {
                event.get("tool_call_id")
                for event in events
                if event.get("kind") == "ACPToolCallEvent" and event.get("tool_call_id")
            }
        )
    models = sorted(
        {
            event.get("value", {}).get("llm", {}).get("model")
            for event in events
            if event.get("key") == "agent"
            and event.get("value", {}).get("llm", {}).get("model")
        }
    )
    system_prompt_chars = next(
        (
            len(event.get("system_prompt", {}).get("text", ""))
            for event in events
            if event.get("kind") == "SystemPromptEvent"
        ),
        0,
    )
    return {
        "event_count": len(events),
        "started_at": min(timestamps) if timestamps else None,
        "finished_at": max(timestamps) if timestamps else None,
        "elapsed_seconds": elapsed_seconds,
        "terminal_status": status_updates[-1] if status_updates else None,
        "tool_calls": tool_calls,
        "models": models,
        "visible_system_prompt_chars": system_prompt_chars,
        "usage": usage,
        "native_metrics": {
            "usage_id": metric_name,
            "metrics": selected_metrics,
            "all_usage_to_metrics": native_metrics,
        },
    }


def diff_metrics(workspace: Path) -> dict:
    status = run_command(["git", "status", "--short"], workspace).stdout.splitlines()
    diff = run_command(["git", "diff", "--numstat", "HEAD"], workspace).stdout.splitlines()
    additions = 0
    deletions = 0
    for line in diff:
        added, removed, _ = line.split("\t", 2)
        if added.isdigit():
            additions += int(added)
        if removed.isdigit():
            deletions += int(removed)
    untracked = [line[3:] for line in status if line.startswith("?? ")]
    for relative in untracked:
        path = workspace / relative
        if path.is_file():
            additions += len(path.read_text(encoding="utf-8", errors="ignore").splitlines())
    return {
        "changed_files": status,
        "additions": additions,
        "deletions": deletions,
    }


def hash_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def save_results(run_id: str, results: dict) -> Path:
    EVIDENCE_ROOT.mkdir(parents=True, exist_ok=True)
    path = EVIDENCE_ROOT / f"{run_id}.json"
    path.write_text(json.dumps(results, indent=2) + "\n", encoding="utf-8")
    return path


def refresh_results(path: Path) -> None:
    """Refresh locally sourced usage for a completed run."""
    results = json.loads(path.read_text(encoding="utf-8"))
    for task in results["tasks"]:
        for harness, result in task["harnesses"].items():
            workspace = Path(result["workspace"])
            if harness.startswith("pi"):
                result["trace"]["usage"] = pi_usage(workspace) or result["trace"]["usage"]
            elif harness.startswith("opencode"):
                result["trace"]["usage"] = (
                    opencode_usage(workspace) or result["trace"]["usage"]
                )
    path.write_text(json.dumps(results, indent=2) + "\n", encoding="utf-8")


def run_task(
    run_id: str,
    task_id: str,
    harnesses: tuple[str, ...],
    profile_ids: dict[str, str],
    agent_settings: dict[str, dict] | None,
    enable_laminar: bool,
    timeout_seconds: int,
    repair_rounds: int,
    ledger_proxy_url: str | None,
    ledger_settle_seconds: int,
    verifier_python: str,
) -> dict:
    print(f"\n{task_id}", flush=True)
    workspaces: dict[str, Path] = {}
    trees: dict[str, str] = {}
    for harness in harnesses:
        workspace, tree = prepare_workspace(run_id, task_id, harness)
        workspaces[harness] = workspace
        trees[harness] = tree
    if len(set(trees.values())) != 1:
        raise RuntimeError(f"starter trees differ for {task_id}: {trees}")

    conversation_ids = {}
    attempt_timing: dict[str, list[dict[str, str]]] = {harness: [] for harness in harnesses}
    for harness in harnesses:
        set_ledger_context(
            ledger_proxy_url,
            harness,
            run_id=run_id,
            task_id=task_id,
            phase="primary",
        )
        submitted_at = datetime.now().astimezone().isoformat()
        conversation_ids[harness] = launch(
            run_id,
            task_id,
            harness,
            profile_ids.get(harness),
            agent_settings.get(harness) if agent_settings else None,
            workspaces[harness],
            enable_laminar,
        )
        attempt_timing[harness].append({"phase": "primary", "submitted_at": submitted_at})
    print(f"  conversations={conversation_ids}", flush=True)
    records = wait_for_terminal(conversation_ids, timeout_seconds)
    primary_terminal_at = datetime.now().astimezone().isoformat()
    for harness in harnesses:
        attempt_timing[harness][0]["terminal_at"] = primary_terminal_at

    checks = {}
    for harness, workspace in workspaces.items():
        passed, output = verify(task_id, workspace, verifier_python)
        checks[harness] = {
            "first_pass": passed,
            "final_pass": passed,
            "repair_rounds": 0,
            "output": output,
        }

    for round_number in range(1, repair_rounds + 1):
        failing = [harness for harness, check in checks.items() if not check["final_pass"]]
        if not failing:
            break
        print(f"  repair round {round_number}: {', '.join(failing)}", flush=True)
        for harness in failing:
            set_ledger_context(
                ledger_proxy_url,
                harness,
                run_id=run_id,
                task_id=task_id,
                phase=f"repair-{round_number}",
            )
            submitted_at = datetime.now().astimezone().isoformat()
            send_repair(conversation_ids[harness], checks[harness]["output"])
            attempt_timing[harness].append(
                {"phase": f"repair-{round_number}", "submitted_at": submitted_at}
            )
        wait_for_terminal(
            {harness: conversation_ids[harness] for harness in failing},
            timeout_seconds,
        )
        repair_terminal_at = datetime.now().astimezone().isoformat()
        for harness in failing:
            attempt_timing[harness][-1]["terminal_at"] = repair_terminal_at
            passed, output = verify(task_id, workspaces[harness], verifier_python)
            checks[harness]["final_pass"] = passed
            checks[harness]["repair_rounds"] = round_number
            checks[harness]["output"] = output

    harness_results = {}
    for harness, conversation_id in conversation_ids.items():
        events = all_events(conversation_id)
        harness_results[harness] = {
            "conversation_id": conversation_id,
            "conversation_url": f"{BASE_URL}/conversations/{conversation_id}",
            "execution_status": records[harness].get("execution_status"),
            "workspace": str(workspaces[harness]),
            "baseline_tree": trees[harness],
            "verification": checks[harness],
            "attempt_timing": attempt_timing[harness],
            "trace": event_metrics(events, harness, workspaces[harness]),
            "diff": diff_metrics(workspaces[harness]),
        }
    if ledger_proxy_url and ledger_settle_seconds:
        time.sleep(ledger_settle_seconds)
    for harness in harnesses:
        set_ledger_context(ledger_proxy_url, harness, active=False)
    return {
        "task_id": task_id,
        "prompt": task_prompt(task_id),
        "harnesses": harness_results,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", default=datetime.now().strftime("%Y%m%d-%H%M%S"))
    parser.add_argument("--task", action="append")
    parser.add_argument("--timeout-seconds", type=int, default=900)
    parser.add_argument("--repair-rounds", type=int, default=1)
    parser.add_argument(
        "--harness",
        action="append",
        choices=tuple(PROFILE_NAMES),
        help="Run only the selected harness. Repeat to select multiple harnesses.",
    )
    parser.add_argument(
        "--include-codex",
        action="store_true",
        help="Add the existing Codex ACP profile using GPT-5.5.",
    )
    parser.add_argument(
        "--skill-context",
        choices=("profile-default", "canvas-curated"),
        default="profile-default",
        help=(
            "Use saved-profile skill behavior, or materialize each profile and "
            "inject Agent Canvas's pinned 11-skill current default into every harness."
        ),
    )
    parser.add_argument(
        "--allow-laminar-content-export",
        action="store_true",
        help=(
            "Send prompts, responses, code, and tool details to Laminar. "
            "The tested redaction environment flag does not suppress these fields."
        ),
    )
    parser.add_argument("--refresh-results", type=Path)
    parser.add_argument("--laminar-wait-seconds", type=int, default=30)
    parser.add_argument(
        "--ledger-proxy-url",
        help="Local provider-ledger proxy, for example http://127.0.0.1:4010.",
    )
    parser.add_argument(
        "--ledger-settle-seconds",
        type=int,
        default=45,
        help="Keep a provider-ledger task label active for delayed final responses.",
    )
    parser.add_argument(
        "--verifier-python",
        default=sys.executable,
        help="Python interpreter used for instructor-owned verifiers.",
    )
    args = parser.parse_args()

    if args.refresh_results:
        refresh_results(args.refresh_results)
        print(f"Refreshed {args.refresh_results}")
        return 0

    suite = json.loads((Path(__file__).with_name("suite.json")).read_text(encoding="utf-8"))
    task_ids = args.task or [task["id"] for task in suite["tasks"]]
    if args.harness:
        selected = args.harness + (["codex"] if args.include_codex else [])
        harnesses = tuple(dict.fromkeys(selected))
    else:
        harnesses = DEFAULT_HARNESSES + (("codex",) if args.include_codex else ())
    if args.skill_context == "canvas-curated":
        profile_ids: dict[str, str] = {}
        agent_settings = resolve_curated_agent_settings(harnesses)
    else:
        profile_ids = resolve_profile_ids(harnesses)
        agent_settings = None
    results = {
        "run_id": args.run_id,
        "suite_id": suite["suite_id"],
        "model": suite["model"],
        "telemetry": (
            {
                "backend": "laminar-otel",
                "project_secret": "LAMINAR_API_KEY",
                "trace_metadata": [
                    "experiment",
                    "run_id",
                    "task",
                    "harness",
                    "model",
                ],
                "acp_token_source": "internal-subprocess-llm-spans",
                "transport": "http",
                "trace_content": "prompts-responses-code-and-tools",
                "explicit_content_export": True,
            }
            if args.allow_laminar_content_export
            else {
                "backend": "disabled",
                "reason": "Laminar content export was not explicitly allowed.",
            }
        ),
        "harness_models": {harness: HARNESS_MODELS[harness] for harness in harnesses},
        "provider_ledger": {
            "enabled": bool(args.ledger_proxy_url),
            "proxy_url": args.ledger_proxy_url,
            "schema": "provider_ledger_proxy/v1" if args.ledger_proxy_url else None,
        },
        "agent_canvas_version": "1.15.0",
        "agent_server_version": "1.42.1",
        "skill_context": {
            "mode": args.skill_context,
            "source": (
                "OpenHands/OpenHands#16860; @openhands/extensions 0.18.0"
                if args.skill_context == "canvas-curated"
                else "agent-profile server defaults"
            ),
            "count": (
                len(CURATED_CANVAS_SKILLS)
                if args.skill_context == "canvas-curated"
                else None
            ),
            "names": (
                list(CURATED_CANVAS_SKILLS)
                if args.skill_context == "canvas-curated"
                else None
            ),
        },
        "created_at": datetime.now().astimezone().isoformat(),
        "task_sha256": {
            "p09": hash_file(P09_TASKS),
            "rate_limiter": hash_file(RATE_LIMITER_DIR / "task.md"),
            "artifactsbench_spread_plate": hash_file(SPREAD_PLATE_DIR / "task.md"),
            "durable_job_queue": hash_file(DURABLE_QUEUE_DIR / "task.md"),
            "incident_operations_center": hash_file(INCIDENT_OPS_DIR / "task.md"),
        },
        "verifier_sha256": {
            "p09": hash_file(P09_CHECKER),
            "rate_limiter": hash_file(RATE_LIMITER_DIR / "verify_task08.py"),
            "artifactsbench_spread_plate": hash_file(SPREAD_PLATE_DIR / "verify_spread_plate.py"),
            "durable_job_queue": hash_file(DURABLE_QUEUE_DIR / "verify_durable_queue.py"),
            "incident_operations_center": hash_file(INCIDENT_OPS_DIR / "verify_incident_ops.py"),
        },
        "tasks": [],
    }
    output_path = save_results(args.run_id, results)
    for task_id in task_ids:
        results["tasks"].append(
            run_task(
                args.run_id,
                task_id,
                harnesses,
                profile_ids,
                agent_settings,
                args.allow_laminar_content_export,
                args.timeout_seconds,
                args.repair_rounds,
                args.ledger_proxy_url,
                args.ledger_settle_seconds,
                args.verifier_python,
            )
        )
        save_results(args.run_id, results)
    if args.allow_laminar_content_export:
        try:
            refresh_results(output_path)
            results = json.loads(output_path.read_text(encoding="utf-8"))
            results["laminar"] = collect_laminar(results, args.laminar_wait_seconds)
        except Exception as exc:
            results["laminar"] = {
                "status": "error",
                "error": f"{type(exc).__name__}: {exc}",
            }
    else:
        results["laminar"] = {"status": "disabled"}
    save_results(args.run_id, results)
    print(f"\nSaved {output_path}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
