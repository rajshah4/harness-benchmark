#!/usr/bin/env python3
"""Export accepted Agent Canvas event streams with defensive redaction."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from typing import Any


ACCEPTED_RUN_PREFIXES = (
    "20260824-aws-long-projects-v2-",
    "20260824-aws-incident-v2-",
)

SENSITIVE_KEYS = {
    "api_key",
    "authorization",
    "aws_access_key_id",
    "aws_secret_access_key",
    "aws_session_token",
    "cookie",
    "credentials",
    "password",
    "private_key",
    "refresh_token",
    "secret",
    "secrets",
    "set-cookie",
    "x-session-api-key",
}

PATH_PATTERN = re.compile(
    r"/(?:home/ubuntu|Users/rajiv\.shah)/[^\s\"']+"
)
ASSIGNMENT_PATTERN = re.compile(
    r"(?i)\b([A-Z0-9_]*(?:API_KEY|ACCESS_TOKEN|REFRESH_TOKEN|PASSWORD|"
    r"PRIVATE_KEY|SECRET_ACCESS_KEY|SESSION_TOKEN)[A-Z0-9_]*\s*[=:]\s*)"
    r"(?:\"[^\"]*\"|'[^']*'|[^\s,;]+)"
)
BEARER_PATTERN = re.compile(r"(?i)(authorization\s*[:=]\s*bearer\s+)[^\s\"']+")
HEADER_PATTERN = re.compile(
    r"(?i)((?:x-session-api-key|x-api-key|api-key)\s*:\s*)"
    r"(?!\$|<|\[REDACTED\])[^\s\"']+"
)
TOKEN_PATTERNS = (
    re.compile(r"sk-[A-Za-z0-9_-]{16,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"AIza[0-9A-Za-z_-]{30,}"),
    re.compile(r"xox[baprs]-[0-9A-Za-z-]{10,}"),
    re.compile(r"lmnr_[A-Za-z0-9_-]{16,}"),
    re.compile(
        r"eyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\."
        r"[A-Za-z0-9_-]{10,}"
    ),
)


def redact_text(value: str) -> str:
    value = PATH_PATTERN.sub("<local-workspace>", value)
    value = ASSIGNMENT_PATTERN.sub(r"\1[REDACTED]", value)
    value = BEARER_PATTERN.sub(r"\1[REDACTED]", value)
    value = HEADER_PATTERN.sub(r"\1[REDACTED]", value)
    for pattern in TOKEN_PATTERNS:
        value = pattern.sub("[REDACTED]", value)
    return value


def sanitize(value: Any, parent_key: str | None = None) -> Any:
    if parent_key and parent_key.lower() in SENSITIVE_KEYS:
        return "[REDACTED]"
    if isinstance(value, dict):
        return {key: sanitize(item, key) for key, item in value.items()}
    if isinstance(value, list):
        return [sanitize(item) for item in value]
    if isinstance(value, str):
        return redact_text(value)
    return value


def export_trace(conversation_dir: Path, destination: Path) -> dict[str, Any]:
    meta = json.loads((conversation_dir / "meta.json").read_text(encoding="utf-8"))
    metadata = meta["observability_metadata"]
    run_id = metadata["run_id"]
    output_path = destination / f"{run_id}.jsonl"
    digest = hashlib.sha256()
    event_count = 0

    with output_path.open("w", encoding="utf-8") as output:
        for event_path in sorted((conversation_dir / "events").glob("event-*.json")):
            event = json.loads(event_path.read_text(encoding="utf-8"))
            line = json.dumps(sanitize(event), sort_keys=True, separators=(",", ":"))
            encoded = f"{line}\n".encode()
            output.write(encoded.decode())
            digest.update(encoded)
            event_count += 1

    return {
        "file": output_path.name,
        "run_id": run_id,
        "task": metadata["task"],
        "harness": metadata["harness"],
        "model": metadata["model"],
        "event_count": event_count,
        "sha256": digest.hexdigest(),
    }


def merge_manifest_entries(
    existing: list[dict[str, Any]], exported: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Replace matching run IDs, preserve other entries, and sort deterministically."""
    exported_run_ids = {item["run_id"] for item in exported}
    merged = [item for item in existing if item["run_id"] not in exported_run_ids]
    merged.extend(exported)
    return sorted(merged, key=lambda item: item["run_id"])


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path, help="Agent Canvas state directory")
    parser.add_argument("destination", type=Path, help="Trace output directory")
    parser.add_argument(
        "--run-id",
        action="append",
        default=[],
        help="Export an exact run ID. Repeat to export multiple runs.",
    )
    parser.add_argument(
        "--merge-manifest",
        action="store_true",
        help="Merge exported traces into an existing manifest by run ID.",
    )
    args = parser.parse_args()
    args.destination.mkdir(parents=True, exist_ok=True)

    manifest = []
    for conversation_dir in sorted(args.source.iterdir()):
        meta_path = conversation_dir / "meta.json"
        if not meta_path.exists():
            continue
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        run_id = meta.get("observability_metadata", {}).get("run_id", "")
        selected = run_id in args.run_id if args.run_id else run_id.startswith(
            ACCEPTED_RUN_PREFIXES
        )
        if not selected:
            continue
        manifest.append(export_trace(conversation_dir, args.destination))

    manifest_path = args.destination / "manifest.json"
    exported_count = len(manifest)
    if args.merge_manifest and manifest_path.exists():
        existing = json.loads(manifest_path.read_text(encoding="utf-8"))["traces"]
        manifest = merge_manifest_entries(existing, manifest)
    else:
        manifest.sort(key=lambda item: item["run_id"])
    manifest_path.write_text(
        json.dumps({"schema_version": 1, "traces": manifest}, indent=2) + "\n",
        encoding="utf-8",
    )
    print(
        f"Exported {exported_count} traces; "
        f"manifest now contains {len(manifest)} traces in {args.destination}"
    )


if __name__ == "__main__":
    main()
