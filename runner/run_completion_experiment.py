#!/usr/bin/env python3
"""Run the workshop single-agent versus completion-system app experiment."""

from __future__ import annotations

import argparse
import copy
import json
import shutil
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from run_suite import (
    BASE_URL,
    CURATED_CANVAS_SKILLS,
    EVIDENCE_ROOT,
    HARNESS_MODELS,
    KEY_FILE,
    all_events,
    api_key,
    diff_metrics,
    event_metrics,
    hash_file,
    llm_secret_for_harness,
    prepare_workspace,
    request_json,
    resolve_curated_agent_settings,
    run_task,
    save_results,
    set_ledger_context,
    task_prompt,
    verify,
    wait_for_terminal,
    INCIDENT_OPS_DIR,
    FREIGHT_TOWER_DIR,
)
from usage_ledger import normalize_usage, validate_record


MODEL = "openhands/claude-sonnet-4-6"
PROVIDER_REQUEST_MODEL = "claude-sonnet-4-6"
TASK_ID = "incident-operations-center"
SYSTEM_LANES = (
    "openhands-system-implementer",
    "openhands-system-validator",
    "openhands-system-orchestrator",
)
PI_SYSTEM_LANES = (
    "pi-system-implementer",
    "pi-system-validator",
    "pi-system-orchestrator",
)
PI_SYSTEM_CONFIG_ROOT = Path(__file__).resolve().parent / "configs" / "pi-sonnet46-system"


def routed_native_settings(base: dict, lane: str, ledger_proxy_url: str) -> dict:
    settings = copy.deepcopy(base)
    llm = settings.get("llm")
    if not isinstance(llm, dict):
        raise RuntimeError("materialized native OpenHands profile has no LLM settings")
    if llm.get("model") != MODEL:
        raise RuntimeError(f"native profile model mismatch: {llm.get('model')!r}")
    llm.pop("reasoning_effort", None)
    settings["enable_sub_agents"] = False
    llm["base_url"] = f"{ledger_proxy_url.rstrip('/')}/v1/{lane}"
    context = settings.get("agent_context") or {}
    skill_names = [skill.get("name") for skill in context.get("skills", [])]
    if skill_names != list(CURATED_CANVAS_SKILLS):
        raise RuntimeError(f"native skill allow-list mismatch: {skill_names!r}")
    return settings


def routed_pi_settings(base: dict, lane: str, ledger_proxy_url: str) -> dict:
    settings = copy.deepcopy(base)
    if settings.get("agent_kind") != "acp":
        raise RuntimeError("Pi system requires an ACP agent setting")
    command = settings.get("acp_command")
    if not (
        isinstance(command, list)
        and any(
            isinstance(part, str) and part.startswith("PI_CODING_AGENT_DIR=")
            for part in command
        )
    ):
        raise RuntimeError("Pi profile has no PI_CODING_AGENT_DIR command")
    config_dir = PI_SYSTEM_CONFIG_ROOT / lane
    models_path = config_dir / "models.json"
    if not models_path.exists():
        raise RuntimeError(f"missing Pi system lane config: {models_path}")
    settings["acp_command"] = ["env", f"PI_CODING_AGENT_DIR={config_dir}", "pi-acp"]
    context = settings.get("agent_context") or {}
    skill_names = [skill.get("name") for skill in context.get("skills", [])]
    if skill_names != list(CURATED_CANVAS_SKILLS):
        raise RuntimeError(f"Pi skill allow-list mismatch: {skill_names!r}")
    expected_base = f"{ledger_proxy_url.rstrip('/')}/v1/{lane}"
    configured = json.loads(models_path.read_text(encoding="utf-8"))
    actual_base = configured["providers"]["openhands"]["baseUrl"]
    if actual_base != expected_base:
        raise RuntimeError(f"Pi lane proxy mismatch: expected {expected_base}, got {actual_base}")
    return settings


def launch_role(
    *,
    run_id: str,
    role: str,
    prompt: str,
    workspace: Path,
    agent_settings: dict,
) -> str:
    key = api_key()
    payload = {
        "workspace": {"kind": "LocalWorkspace", "working_dir": str(workspace)},
        "agent_settings": agent_settings,
        "initial_message": {
            "role": "user",
            "content": [{"type": "text", "text": prompt}],
            "run": True,
        },
        "confirmation_policy": {"kind": "NeverConfirm"},
        "max_iterations": 1000,
        "stuck_detection": True,
        "autotitle": False,
        "worktree": False,
        "secrets": {"LLM_API_KEY": llm_secret_for_harness(role, key)},
        "observability_metadata": {
            "experiment": "completion-loop-app",
            "run_id": run_id,
            "task": TASK_ID,
            "role": role,
            "model": MODEL,
        },
        "observability_tags": [
            "completion-loop-app",
            f"role:{role}",
            f"task:{TASK_ID}",
        ],
        "observability_span_name": "completion.loop.role",
        "tags": {
            "experiment": "completion-loop-app",
            "task": TASK_ID,
            "role": role,
            "model": MODEL,
        },
    }
    return request_json("POST", "/api/conversations", payload)["id"]


def send_directive(conversation_id: str, directive: str) -> None:
    prompt = (
        "The independent completion loop found remaining product-level gaps. "
        "Investigate and address the directive below without asking for the hidden checks. "
        "Run your own relevant checks, inspect the final diff, and continue until you believe "
        "the product contract is complete.\n\n"
        f"DIRECTIVE:\n{directive}"
    )
    request_json(
        "POST",
        f"/api/conversations/{conversation_id}/events",
        {"role": "user", "content": [{"type": "text", "text": prompt}], "run": True},
    )


def fresh_role_workspace(root: Path, name: str) -> Path:
    path = root / name
    if path.exists():
        raise RuntimeError(f"role workspace already exists: {path}")
    path.mkdir(parents=True)
    return path


def snapshot_candidate(candidate: Path, destination: Path) -> None:
    shutil.copytree(
        candidate,
        destination,
        ignore=shutil.ignore_patterns(".git", ".pytest_cache", "__pycache__", "*.pyc"),
    )


def load_json_contract(path: Path, label: str) -> dict[str, Any]:
    if not path.exists():
        return {"contract_error": f"{label} did not write {path.name}"}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {"contract_error": f"invalid {label} JSON: {type(exc).__name__}"}
    return value if isinstance(value, dict) else {"contract_error": f"{label} JSON is not an object"}


def validator_prompt(round_number: int) -> str:
    return f"""You are the independent validator in completion round {round_number}.

The public product contract is in PUBLIC_TASK.md. A read-only snapshot of the candidate is in candidate/. Controller-owned verification outcomes are in VALIDATION_INPUT.json. Do not modify candidate/. You may inspect it and run bounded checks inside it.

Cluster evidence by missing product capability or root cause. Do not reproduce hidden case details or propose line-by-line patches. Write validation_report.json at the workspace root with exactly this shape:
{{
  "round": {round_number},
  "blocking_findings": [{{"capability": "...", "evidence": "...", "directive": "..."}}],
  "non_blocking_findings": [{{"capability": "...", "evidence": "..."}}],
  "coverage_uncertainty": ["..."],
  "recommendation": "STOP" or "CONTINUE",
  "reason": "..."
}}

STOP is appropriate only when the supplied outcomes pass and your independent inspection finds no material product-contract gap. Finish by reporting the file you wrote."""


def orchestrator_prompt(round_number: int, rounds_remaining: int) -> str:
    return f"""You are the shipping orchestrator in completion round {round_number}.

Read PUBLIC_TASK.md and ORCHESTRATION_INPUT.json. Adjudicate the validator's clustered findings. You—not the controller—decide whether to ship. You have {rounds_remaining} additional implementation round(s) available after this decision.

Write decision.json with exactly this shape:
{{
  "round": {round_number},
  "decision": "STOP" or "CONTINUE",
  "reason": "...",
  "priority_capabilities": ["..."],
  "directive": "a capability-level implementation directive; empty only for STOP"
}}

Reject noise, but do not call a failing required capability complete. Do not expose imagined hidden cases or prescribe a patch. If budget is exhausted, STOP and state that the shipment is budget-limited. Finish by reporting the file you wrote."""


def run_system(
    *,
    run_id: str,
    native_base_settings: dict,
    ledger_proxy_url: str,
    timeout_seconds: int,
    verifier_python: str,
    max_repairs: int,
    system_harness: str = "openhands",
) -> dict[str, Any]:
    if system_harness == "openhands":
        lanes = SYSTEM_LANES
        condition = "openhands-system"
        router = routed_native_settings
    elif system_harness == "pi":
        lanes = PI_SYSTEM_LANES
        condition = "pi-system"
        router = routed_pi_settings
    else:
        raise ValueError(f"unsupported system harness: {system_harness}")
    candidate, baseline_tree = prepare_workspace(run_id, TASK_ID, condition)
    role_root = candidate.parent / f"{condition}-roles"
    role_root.mkdir(parents=True)
    settings = {
        lane: router(native_base_settings, lane, ledger_proxy_url)
        for lane in lanes
    }
    implementer_lane = lanes[0]
    set_ledger_context(
        ledger_proxy_url, implementer_lane,
        run_id=run_id, task_id=TASK_ID, phase="primary",
    )
    started_at = datetime.now().astimezone().isoformat()
    implementer_id = launch_role(
        run_id=run_id,
        role=implementer_lane,
        prompt=task_prompt(TASK_ID),
        workspace=candidate,
        agent_settings=settings[implementer_lane],
    )
    implementer_attempts = [{"phase": "primary", "submitted_at": started_at}]
    record = wait_for_terminal({implementer_lane: implementer_id}, timeout_seconds)[implementer_lane]
    implementer_attempts[-1]["terminal_at"] = datetime.now().astimezone().isoformat()

    rounds: list[dict[str, Any]] = []
    shipped_reason = "round cap reached"
    for round_number in range(max_repairs + 1):
        passed, verifier_output = verify(TASK_ID, candidate, verifier_python)
        round_record: dict[str, Any] = {
            "round": round_number,
            "verification_passed": passed,
            "verification_output": verifier_output,
        }

        validator_workspace = fresh_role_workspace(role_root, f"validator-{round_number}")
        snapshot_candidate(candidate, validator_workspace / "candidate")
        (validator_workspace / "PUBLIC_TASK.md").write_text(
            task_prompt(TASK_ID), encoding="utf-8"
        )
        (validator_workspace / "VALIDATION_INPUT.json").write_text(
            json.dumps(
                {
                    "round": round_number,
                    "verifier_passed": passed,
                    "verifier_output": verifier_output,
                    "candidate_diff": diff_metrics(candidate),
                },
                indent=2,
            ) + "\n",
            encoding="utf-8",
        )
        validator_lane = lanes[1]
        set_ledger_context(
            ledger_proxy_url, validator_lane,
            run_id=run_id, task_id=TASK_ID, phase=f"round-{round_number}",
        )
        validator_id = launch_role(
            run_id=run_id,
            role=validator_lane,
            prompt=validator_prompt(round_number),
            workspace=validator_workspace,
            agent_settings=settings[validator_lane],
        )
        validator_terminal = wait_for_terminal(
            {validator_lane: validator_id}, timeout_seconds
        )[validator_lane]
        validation = load_json_contract(
            validator_workspace / "validation_report.json", "validator"
        )
        round_record["validator"] = {
            "conversation_id": validator_id,
            "conversation_url": f"{BASE_URL}/conversations/{validator_id}",
            "execution_status": validator_terminal.get("execution_status"),
            "report": validation,
            "trace": event_metrics(
                all_events(validator_id), validator_lane, validator_workspace
            ),
        }
        set_ledger_context(ledger_proxy_url, validator_lane, active=False)

        orchestrator_workspace = fresh_role_workspace(role_root, f"orchestrator-{round_number}")
        (orchestrator_workspace / "PUBLIC_TASK.md").write_text(
            task_prompt(TASK_ID), encoding="utf-8"
        )
        (orchestrator_workspace / "ORCHESTRATION_INPUT.json").write_text(
            json.dumps(
                {
                    "round": round_number,
                    "rounds_remaining": max_repairs - round_number,
                    "validator_report": validation,
                    "prior_decisions": [item.get("orchestrator", {}).get("decision") for item in rounds],
                },
                indent=2,
            ) + "\n",
            encoding="utf-8",
        )
        orchestrator_lane = lanes[2]
        set_ledger_context(
            ledger_proxy_url, orchestrator_lane,
            run_id=run_id, task_id=TASK_ID, phase=f"round-{round_number}",
        )
        orchestrator_id = launch_role(
            run_id=run_id,
            role=orchestrator_lane,
            prompt=orchestrator_prompt(round_number, max_repairs - round_number),
            workspace=orchestrator_workspace,
            agent_settings=settings[orchestrator_lane],
        )
        orchestrator_terminal = wait_for_terminal(
            {orchestrator_lane: orchestrator_id}, timeout_seconds
        )[orchestrator_lane]
        decision = load_json_contract(
            orchestrator_workspace / "decision.json", "orchestrator"
        )
        round_record["orchestrator"] = {
            "conversation_id": orchestrator_id,
            "conversation_url": f"{BASE_URL}/conversations/{orchestrator_id}",
            "execution_status": orchestrator_terminal.get("execution_status"),
            "decision": decision,
            "trace": event_metrics(
                all_events(orchestrator_id), orchestrator_lane, orchestrator_workspace
            ),
        }
        set_ledger_context(ledger_proxy_url, orchestrator_lane, active=False)
        rounds.append(round_record)

        if decision.get("decision") == "STOP":
            shipped_reason = str(decision.get("reason") or "orchestrator chose STOP")
            break
        if round_number >= max_repairs:
            shipped_reason = "round cap reached after orchestrator requested CONTINUE"
            break
        directive = decision.get("directive")
        if not isinstance(directive, str) or not directive.strip():
            shipped_reason = "orchestrator CONTINUE decision omitted a directive"
            break
        phase = f"repair-{round_number + 1}"
        set_ledger_context(
            ledger_proxy_url, implementer_lane,
            run_id=run_id, task_id=TASK_ID, phase=phase,
        )
        submitted_at = datetime.now().astimezone().isoformat()
        send_directive(implementer_id, directive)
        record = wait_for_terminal(
            {implementer_lane: implementer_id}, timeout_seconds
        )[implementer_lane]
        implementer_attempts.append(
            {
                "phase": phase,
                "submitted_at": submitted_at,
                "terminal_at": datetime.now().astimezone().isoformat(),
            }
        )

    final_pass, final_output = verify(TASK_ID, candidate, verifier_python)
    if ledger_proxy_url:
        time.sleep(2)
    set_ledger_context(ledger_proxy_url, implementer_lane, active=False)
    implementer_events = all_events(implementer_id)
    return {
        "condition": condition,
        "model": MODEL,
        "workspace": str(candidate),
        "baseline_tree": baseline_tree,
        "execution_status": record.get("execution_status"),
        "implementer": {
            "conversation_id": implementer_id,
            "conversation_url": f"{BASE_URL}/conversations/{implementer_id}",
            "attempt_timing": implementer_attempts,
            "trace": event_metrics(implementer_events, implementer_lane, candidate),
        },
        "rounds": rounds,
        "ship_reason": shipped_reason,
        "verification": {"final_pass": final_pass, "output": final_output},
        "diff": diff_metrics(candidate),
    }


def provider_usage(ledger: Path, run_id: str) -> dict[str, Any]:
    rows = [
        json.loads(line)
        for line in ledger.read_text(encoding="utf-8").splitlines()
        if line
    ]
    rows = [row for row in rows if (row.get("context") or {}).get("run_id") == run_id]
    grouped: dict[str, dict[str, Any]] = {}
    errors: list[str] = []
    reliability_incidents: list[dict[str, Any]] = []
    seen_response_ids: set[str] = set()
    for index, row in enumerate(rows, 1):
        lane = str(row.get("harness", "unlabeled"))
        bucket = grouped.setdefault(
            lane,
            {
                "model_calls": 0,
                "successful_usage_calls": 0,
                "failed_requests": 0,
                "input_tokens": 0,
                "fresh_input_tokens": 0,
                "cache_read_input_tokens": 0,
                "cache_write_input_tokens": 0,
                "output_tokens": 0,
                "provider_total_tokens": 0,
                "request_models": [],
            },
        )
        bucket["model_calls"] += 1
        model = row.get("request", {}).get("model")
        if model not in bucket["request_models"]:
            bucket["request_models"].append(model)
        successful_response = (
            200 <= int(row.get("response_status", 0)) < 300
            and not row.get("provider_error")
        )
        if (
            row.get("error_type")
            or row.get("provider_error")
            or row.get("downstream_error")
            or not 200 <= int(row.get("response_status", 0)) < 300
        ):
            reliability_incidents.append({
                "row": index,
                "harness": lane,
                "response_status": row.get("response_status"),
                "error_type": row.get("error_type"),
                "provider_error": row.get("provider_error"),
                "downstream_error": row.get("downstream_error"),
                "usage_reported": bool(row.get("raw_usage")),
            })
        if not successful_response and not row.get("raw_usage"):
            bucket["failed_requests"] += 1
            continue
        record_errors = validate_record(row)
        errors.extend(f"row {index}: {error}" for error in record_errors)
        if record_errors:
            continue
        response_id = row.get("provider_response_id")
        if response_id in seen_response_ids:
            errors.append(f"row {index}: duplicate provider response ID")
            continue
        seen_response_ids.add(response_id)
        bucket["successful_usage_calls"] += 1
        normalized = normalize_usage(row["raw_usage"])
        for field in (
            "input_tokens", "fresh_input_tokens", "cache_read_input_tokens",
            "cache_write_input_tokens", "output_tokens", "provider_total_tokens",
        ):
            bucket[field] += int(normalized.get(field) or 0)
    for lane, bucket in grouped.items():
        if bucket["request_models"] != [PROVIDER_REQUEST_MODEL]:
            errors.append(f"{lane}: unexpected provider request models {bucket['request_models']!r}")
    return {
        "publishable": bool(rows) and not errors,
        "by_role_or_harness": grouped,
        "accounting_errors": errors,
        "reliability_incidents": reliability_incidents,
    }


def main() -> int:
    global TASK_ID
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", default=datetime.now().strftime("completion-%Y%m%d-%H%M%S"))
    parser.add_argument("--ledger-proxy-url", default="http://127.0.0.1:4020")
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument("--timeout-seconds", type=int, default=2400)
    parser.add_argument("--max-system-repairs", type=int, default=2)
    parser.add_argument(
        "--task-id",
        choices=("incident-operations-center", "freight-control-tower"),
        default=TASK_ID,
    )
    parser.add_argument(
        "--verifier-python",
        default=str(Path(__file__).resolve().parents[1] / ".venv-verifier" / "bin" / "python"),
    )
    args = parser.parse_args()
    TASK_ID = args.task_id
    task_dir = (
        INCIDENT_OPS_DIR
        if TASK_ID == "incident-operations-center"
        else FREIGHT_TOWER_DIR
    )

    harnesses = ("openhands-sonnet46", "pi-sonnet46")
    all_settings = resolve_curated_agent_settings(harnesses)
    native_base = all_settings["openhands-sonnet46"]
    results: dict[str, Any] = {
        "run_id": args.run_id,
        "experiment": "completion-loop-app/v1",
        "model": MODEL,
        "provider": "OpenHands hosted LLM provider",
        "task": TASK_ID,
        "created_at": datetime.now().astimezone().isoformat(),
        "controls": {
            "aws_host": True,
            "skill_source": "OpenHands/OpenHands#16860; @openhands/extensions 0.18.0",
            "skill_names": list(CURATED_CANVAS_SKILLS),
            "task_sha256": hash_file(task_dir / "task.md"),
            "verifier_sha256": hash_file(
                task_dir
                / (
                    "verify_incident_ops.py"
                    if TASK_ID == "incident-operations-center"
                    else "verify_freight_control_tower.py"
                )
            ),
            "system_information_wall": "separate LocalWorkspace directories; operational, not cryptographic",
            "system_max_repairs": args.max_system_repairs,
        },
        "conditions": {},
    }
    save_results(args.run_id, results)

    for harness in harnesses:
        condition = "openhands-single" if harness == "openhands-sonnet46" else "pi-single"
        results["conditions"][condition] = run_task(
            args.run_id,
            TASK_ID,
            (harness,),
            {},
            {harness: all_settings[harness]},
            False,
            args.timeout_seconds,
            0,
            args.ledger_proxy_url,
            2,
            args.verifier_python,
        )["harnesses"][harness]
        results["conditions"][condition]["model"] = HARNESS_MODELS[harness]
        save_results(args.run_id, results)

    results["conditions"]["openhands-system"] = run_system(
        run_id=args.run_id,
        native_base_settings=native_base,
        ledger_proxy_url=args.ledger_proxy_url,
        timeout_seconds=args.timeout_seconds,
        verifier_python=args.verifier_python,
        max_repairs=args.max_system_repairs,
    )
    results["provider_usage"] = provider_usage(args.ledger, args.run_id)
    results["completed_at"] = datetime.now().astimezone().isoformat()
    output = save_results(args.run_id, results)
    print(output)
    return 0 if results["provider_usage"]["publishable"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
