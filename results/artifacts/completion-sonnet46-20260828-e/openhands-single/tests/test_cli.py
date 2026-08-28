"""CLI tests exercising separate processes and the export/import round-trip."""

from __future__ import annotations

import io
import json
import subprocess
import sys
import tempfile
import os
import threading
import time
import urllib.request

import pytest

from incident_ops.cli import main as cli_main
from incident_ops.sqlite_store import SQLiteIncidentStore


PYTHON = sys.executable


def _run(*args, input_text=None, **kwargs) -> subprocess.CompletedProcess:
    """Run a CLI command in a separate process."""
    return subprocess.run(
        [PYTHON, "-m", "incident_ops.cli", *args],
        capture_output=True,
        text=True,
        input=input_text,
        timeout=15,
        **kwargs,
    )


@pytest.fixture
def db(tmp_path):
    return str(tmp_path / "cli_test.db")


# ---------------------------------------------------------------------------
# ingest
# ---------------------------------------------------------------------------

def test_cli_ingest(db):
    result = _run("--db", db, "ingest",
                  json.dumps({"fingerprint": "fp1", "title": "DB down", "severity": "P1"}))
    assert result.returncode == 0
    out = json.loads(result.stdout.strip())
    assert out["created"] is True
    assert out["incident"]["fingerprint"] == "fp1"


def test_cli_ingest_duplicate(db):
    _run("--db", db, "ingest",
         json.dumps({"fingerprint": "fp1", "title": "DB down", "severity": "P1"}))
    result = _run("--db", db, "ingest",
                  json.dumps({"fingerprint": "fp1", "title": "DB down", "severity": "P1"}))
    assert result.returncode == 0
    out = json.loads(result.stdout.strip())
    assert out["created"] is False
    assert out["incident"]["alert_count"] == 2


def test_cli_ingest_bad_json(db):
    result = _run("--db", db, "ingest", "not-json")
    assert result.returncode != 0


def test_cli_ingest_missing_field(db):
    result = _run("--db", db, "ingest",
                  json.dumps({"fingerprint": "fp1", "title": "A"}))
    assert result.returncode != 0


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------

def test_cli_list(db):
    _run("--db", db, "ingest",
         json.dumps({"fingerprint": "fp1", "title": "A", "severity": "P1"}))
    _run("--db", db, "ingest",
         json.dumps({"fingerprint": "fp2", "title": "B", "severity": "P2"}))
    result = _run("--db", db, "list")
    assert result.returncode == 0
    lines = [l for l in result.stdout.strip().split("\n") if l]
    assert len(lines) == 2


def test_cli_list_with_filter(db):
    _run("--db", db, "ingest",
         json.dumps({"fingerprint": "fp1", "title": "A", "severity": "P1"}))
    _run("--db", db, "ingest",
         json.dumps({"fingerprint": "fp2", "title": "B", "severity": "P2"}))
    result = _run("--db", db, "list", "--severity", "P1")
    assert result.returncode == 0
    lines = [l for l in result.stdout.strip().split("\n") if l]
    assert len(lines) == 1
    assert json.loads(lines[0])["severity"] == "P1"


# ---------------------------------------------------------------------------
# update
# ---------------------------------------------------------------------------

def test_cli_update_owner(db):
    ingest = _run("--db", db, "ingest",
                  json.dumps({"fingerprint": "fp1", "title": "A", "severity": "P1"}))
    incident_id = json.loads(ingest.stdout)["incident"]["id"]
    result = _run("--db", db, "update", incident_id,
                  json.dumps({"expected_version": 1, "owner": "alice"}))
    assert result.returncode == 0
    updated = json.loads(result.stdout.strip())
    assert updated["owner"] == "alice"


def test_cli_update_version_conflict(db):
    ingest = _run("--db", db, "ingest",
                  json.dumps({"fingerprint": "fp1", "title": "A", "severity": "P1"}))
    incident_id = json.loads(ingest.stdout)["incident"]["id"]
    result = _run("--db", db, "update", incident_id,
                  json.dumps({"expected_version": 99, "status": "acknowledged"}))
    assert result.returncode != 0


def test_cli_update_not_found(db):
    result = _run("--db", db, "update", "no-such-id",
                  json.dumps({"expected_version": 1}))
    assert result.returncode != 0


# ---------------------------------------------------------------------------
# escalate
# ---------------------------------------------------------------------------

def test_cli_escalate_empty(db):
    # No overdue incidents; should return 0 escalated.
    store = SQLiteIncidentStore(db)
    store.ingest_alert("fp1", "A", "P1", now=time.time() + 9999)  # far future SLA
    result = _run("--db", db, "escalate")
    assert result.returncode == 0
    out = json.loads(result.stdout.strip())
    assert out["escalated"] == 0


# ---------------------------------------------------------------------------
# export / import round-trip
# ---------------------------------------------------------------------------

def test_cli_export_import_roundtrip(tmp_path):
    src_db = str(tmp_path / "src.db")
    dst_db = str(tmp_path / "dst.db")

    # Populate source.
    store = SQLiteIncidentStore(src_db)
    inc, _ = store.ingest_alert("fp1", "DB down", "P1", now=1000.0)
    store.update(inc.id, expected_version=1, owner="alice", now=1001.0)
    store.ingest_alert("fp2", "Net issue", "P2", now=1002.0)

    # Export.
    export_result = _run("--db", src_db, "export")
    assert export_result.returncode == 0
    exported = export_result.stdout

    # Import into fresh DB.
    import_result = _run("--db", dst_db, "import", input_text=exported)
    assert import_result.returncode == 0

    # Verify contents.
    dst_store = SQLiteIncidentStore(dst_db)
    incidents = dst_store.list()
    assert len(incidents) == 2

    inc_a = dst_store.get(inc.id)
    assert inc_a is not None
    assert inc_a.owner == "alice"
    events = dst_store.events(inc.id)
    assert len(events) == 2  # created + owner_changed

    # Idempotent import.
    import_result2 = _run("--db", dst_db, "import", input_text=exported)
    assert import_result2.returncode == 0
    assert len(dst_store.list()) == 2  # no duplication


def test_cli_export_events_ordered(tmp_path):
    db = str(tmp_path / "ord.db")
    store = SQLiteIncidentStore(db)
    inc, _ = store.ingest_alert("fp1", "A", "P1", now=1.0)
    store.update(inc.id, expected_version=1, owner="alice", now=2.0)
    store.update(inc.id, expected_version=2, status="acknowledged", now=3.0)

    result = _run("--db", db, "export")
    assert result.returncode == 0

    records = [json.loads(l) for l in result.stdout.strip().split("\n") if l]
    event_records = [r for r in records if r["type"] == "event"]
    types = [r["data"]["type"] for r in event_records]
    assert types == ["created", "owner_changed", "status_changed"]


def test_cli_import_invalid_json(tmp_path):
    db = str(tmp_path / "bad.db")
    result = _run("--db", db, "import", input_text="not-json\n")
    assert result.returncode != 0


# ---------------------------------------------------------------------------
# No --db flag → error
# ---------------------------------------------------------------------------

def test_cli_requires_db_flag():
    result = _run("list")
    assert result.returncode != 0
