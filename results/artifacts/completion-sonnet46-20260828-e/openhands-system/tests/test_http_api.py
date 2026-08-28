"""Tests for the HTTP JSON API."""

from __future__ import annotations

import json
import threading
import urllib.request
import urllib.error
from pathlib import Path

import pytest

from incident_ops import SQLiteIncidentStore
from incident_ops.web import create_server


# ── server fixture ─────────────────────────────────────────────────────────────

@pytest.fixture
def server_url(tmp_path):
    store = SQLiteIncidentStore(tmp_path / "api_test.db")
    server = create_server(store, host="127.0.0.1", port=0)  # port=0 → OS picks
    port = server.server_address[1]
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{port}"
    server.shutdown()
    t.join(timeout=5)


# ── helpers ────────────────────────────────────────────────────────────────────

def http(method: str, url: str, body: dict | None = None):
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"}
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def POST(url, body):  return http("POST",  url, body)
def GET(url):         return http("GET",   url)
def PATCH(url, body): return http("PATCH", url, body)


# ── POST /api/alerts ────────────────────────────────────────────────────────

def test_post_alert_creates_incident(server_url):
    status, body = POST(f"{server_url}/api/alerts", {
        "fingerprint": "fp1", "title": "DB down", "severity": "P1",
    })
    assert status == 201
    assert body["fingerprint"] == "fp1"
    assert body["status"] == "open"
    assert body["alert_count"] == 1


def test_post_alert_merges_duplicate(server_url):
    POST(f"{server_url}/api/alerts", {"fingerprint": "fp1", "title": "T", "severity": "P2"})
    status, body = POST(f"{server_url}/api/alerts", {"fingerprint": "fp1", "title": "T", "severity": "P2"})
    assert status == 200  # merged, not created
    assert body["alert_count"] == 2


def test_post_alert_invalid_severity(server_url):
    status, body = POST(f"{server_url}/api/alerts", {
        "fingerprint": "fp1", "title": "T", "severity": "INVALID",
    })
    assert status == 400
    assert "error" in body


def test_post_alert_missing_fields(server_url):
    status, body = POST(f"{server_url}/api/alerts", {"fingerprint": "fp1"})
    assert status == 400


# ── GET /api/incidents ──────────────────────────────────────────────────────

def test_get_incidents_returns_list(server_url):
    POST(f"{server_url}/api/alerts", {"fingerprint": "fp1", "title": "A", "severity": "P1"})
    POST(f"{server_url}/api/alerts", {"fingerprint": "fp2", "title": "B", "severity": "P2"})
    status, body = GET(f"{server_url}/api/incidents")
    assert status == 200
    assert isinstance(body, list)
    assert len(body) == 2


def test_get_incidents_status_filter(server_url):
    POST(f"{server_url}/api/alerts", {"fingerprint": "fp1", "title": "A", "severity": "P1"})
    r1, inc = POST(f"{server_url}/api/alerts", {"fingerprint": "fp2", "title": "B", "severity": "P2"})
    PATCH(f"{server_url}/api/incidents/{inc['id']}", {"expected_version": 1, "status": "resolved"})

    status, body = GET(f"{server_url}/api/incidents?status=open")
    assert status == 200
    assert all(i["status"] == "open" for i in body)


def test_get_incidents_severity_filter(server_url):
    POST(f"{server_url}/api/alerts", {"fingerprint": "fp1", "title": "A", "severity": "P1"})
    POST(f"{server_url}/api/alerts", {"fingerprint": "fp2", "title": "B", "severity": "P3"})
    status, body = GET(f"{server_url}/api/incidents?severity=P1")
    assert status == 200
    assert all(i["severity"] == "P1" for i in body)


# ── GET /api/incidents/{id} ─────────────────────────────────────────────────

def test_get_incident_detail_includes_events(server_url):
    _, inc = POST(f"{server_url}/api/alerts", {"fingerprint": "fp1", "title": "X", "severity": "P2"})
    status, body = GET(f"{server_url}/api/incidents/{inc['id']}")
    assert status == 200
    assert "events" in body
    assert isinstance(body["events"], list)
    assert body["events"][0]["type"] == "created"


def test_get_incident_not_found(server_url):
    status, body = GET(f"{server_url}/api/incidents/no-such-id")
    assert status == 404
    assert "error" in body


# ── PATCH /api/incidents/{id} ───────────────────────────────────────────────

def test_patch_assigns_owner(server_url):
    _, inc = POST(f"{server_url}/api/alerts", {"fingerprint": "fp1", "title": "X", "severity": "P3"})
    status, body = PATCH(f"{server_url}/api/incidents/{inc['id']}", {
        "expected_version": 1, "owner": "alice",
    })
    assert status == 200
    assert body["owner"] == "alice"


def test_patch_status_change(server_url):
    _, inc = POST(f"{server_url}/api/alerts", {"fingerprint": "fp1", "title": "X", "severity": "P3"})
    status, body = PATCH(f"{server_url}/api/incidents/{inc['id']}", {
        "expected_version": 1, "status": "acknowledged",
    })
    assert status == 200
    assert body["status"] == "acknowledged"


def test_patch_version_conflict_returns_409(server_url):
    _, inc = POST(f"{server_url}/api/alerts", {"fingerprint": "fp1", "title": "X", "severity": "P3"})
    status, body = PATCH(f"{server_url}/api/incidents/{inc['id']}", {
        "expected_version": 999, "status": "acknowledged",
    })
    assert status == 409
    assert "error" in body


def test_patch_invalid_transition_returns_422(server_url):
    _, inc = POST(f"{server_url}/api/alerts", {"fingerprint": "fp1", "title": "X", "severity": "P3"})
    PATCH(f"{server_url}/api/incidents/{inc['id']}", {"expected_version": 1, "status": "resolved"})
    status, body = PATCH(f"{server_url}/api/incidents/{inc['id']}", {
        "expected_version": 2, "status": "acknowledged",
    })
    assert status == 422
    assert "error" in body


# ── GET /api/summary ────────────────────────────────────────────────────────

def test_summary_counts(server_url):
    POST(f"{server_url}/api/alerts", {"fingerprint": "fp1", "title": "A", "severity": "P1"})
    POST(f"{server_url}/api/alerts", {"fingerprint": "fp2", "title": "B", "severity": "P2"})
    status, body = GET(f"{server_url}/api/summary")
    assert status == 200
    assert body["total"] == 2
    assert body["by_status"]["open"] == 2
    assert body["by_severity"]["P1"] == 1
    assert body["by_severity"]["P2"] == 1


# ── POST /api/escalations/run ───────────────────────────────────────────────

def test_escalation_run_returns_completed(server_url, tmp_path):
    """Escalation run at a time past SLA should return completed incidents."""
    # Use a store directly with a frozen clock so we control timing
    import time
    store = SQLiteIncidentStore(tmp_path / "esc.db")
    early_now = 1000.0
    inc, _ = store.ingest_alert("fp1", "Old issue", "P1", now=early_now)
    # P1 SLA = 60s → deadline 1060.  Run at 9999 should escalate.

    esc_server = create_server(store, host="127.0.0.1", port=0)
    port = esc_server.server_address[1]
    t = threading.Thread(target=esc_server.serve_forever, daemon=True)
    t.start()
    try:
        # Force "now" by manipulating the incident's sla_deadline directly
        # The server uses real time.time() for the worker; the incident is already overdue
        # because sla_deadline = 1060 < current real time
        # Pass max_incidents=1 so the worker stops after one escalation (avoids
        # infinite loop when sla_deadline keeps advancing into the past).
        status, body = POST(f"http://127.0.0.1:{port}/api/escalations/run", {"max_incidents": 1})
        assert status == 200
        assert isinstance(body, list)
        # Should have escalated our incident
        assert any(i["id"] == inc.id for i in body)
    finally:
        esc_server.shutdown()
        t.join(timeout=5)


# ── static files ─────────────────────────────────────────────────────────────

def test_static_index_html(server_url):
    import urllib.request
    with urllib.request.urlopen(f"{server_url}/", timeout=5) as resp:
        assert resp.status == 200
        html = resp.read().decode()
    assert 'data-testid="incident-app"' in html


def test_static_app_js(server_url):
    import urllib.request
    with urllib.request.urlopen(f"{server_url}/app.js", timeout=5) as resp:
        assert resp.status == 200
        js = resp.read().decode()
    assert "incidentOps" in js
    assert "getState" in js
