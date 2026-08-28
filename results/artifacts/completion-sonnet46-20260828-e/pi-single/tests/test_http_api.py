"""HTTP API tests."""
from __future__ import annotations

import json
import threading
import time
import urllib.request
import urllib.error

import pytest

from incident_ops.sqlite_store import SQLiteIncidentStore
from incident_ops.web import create_server


# ---------------------------------------------------------------------------
# Fixture: server running in a background thread
# ---------------------------------------------------------------------------

@pytest.fixture
def server_url(tmp_path):
    store = SQLiteIncidentStore(str(tmp_path / "http.db"), dedupe_window=300, lease_seconds=60)
    server = create_server(store=store, host="127.0.0.1", port=0)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{port}"
    yield url
    server.shutdown()
    thread.join(timeout=5)


def _get(url, path, timeout=5):
    try:
        with urllib.request.urlopen(url + path, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def _post(url, path, body, timeout=5):
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        url + path,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def _patch(url, path, body, timeout=5):
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        url + path,
        data=data,
        headers={"Content-Type": "application/json"},
        method="PATCH",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_post_alerts_creates_incident(server_url):
    status, body = _post(server_url, "/api/alerts", {
        "fingerprint": "fp1", "title": "DB down", "severity": "P1"
    })
    assert status == 201
    assert body["fingerprint"] == "fp1"
    assert body["title"] == "DB down"
    assert body["status"] == "open"


def test_post_alerts_duplicate_returns_200(server_url):
    _post(server_url, "/api/alerts", {"fingerprint": "fp2", "title": "Disk full", "severity": "P2"})
    status, body = _post(server_url, "/api/alerts", {"fingerprint": "fp2", "title": "Disk full", "severity": "P2"})
    assert status == 200
    assert body["alert_count"] == 2


def test_post_alerts_invalid_severity(server_url):
    status, body = _post(server_url, "/api/alerts", {"fingerprint": "fp3", "title": "X", "severity": "P9"})
    assert status == 400
    assert "error" in body


def test_post_alerts_missing_title(server_url):
    status, body = _post(server_url, "/api/alerts", {"fingerprint": "fp4", "severity": "P1"})
    assert status == 400


def test_get_incidents_list(server_url):
    _post(server_url, "/api/alerts", {"fingerprint": "fpA", "title": "A", "severity": "P1"})
    _post(server_url, "/api/alerts", {"fingerprint": "fpB", "title": "B", "severity": "P2"})
    status, body = _get(server_url, "/api/incidents")
    assert status == 200
    assert isinstance(body, list)
    assert len(body) >= 2


def test_get_incidents_filter(server_url):
    _post(server_url, "/api/alerts", {"fingerprint": "fpC", "title": "C", "severity": "P3"})
    status, body = _get(server_url, "/api/incidents?severity=P3")
    assert status == 200
    assert all(i["severity"] == "P3" for i in body)


def test_get_incident_detail(server_url):
    _, created = _post(server_url, "/api/alerts", {"fingerprint": "fpD", "title": "D", "severity": "P2"})
    inc_id = created["id"]
    status, body = _get(server_url, f"/api/incidents/{inc_id}")
    assert status == 200
    assert body["id"] == inc_id
    assert "events" in body
    assert isinstance(body["events"], list)


def test_get_incident_not_found(server_url):
    status, body = _get(server_url, "/api/incidents/nonexistent-id")
    assert status == 404


def test_patch_incident_acknowledge(server_url):
    _, inc = _post(server_url, "/api/alerts", {"fingerprint": "fpE", "title": "E", "severity": "P1"})
    status, body = _patch(server_url, f"/api/incidents/{inc['id']}", {
        "expected_version": inc["version"], "status": "acknowledged"
    })
    assert status == 200
    assert body["status"] == "acknowledged"


def test_patch_incident_version_conflict(server_url):
    _, inc = _post(server_url, "/api/alerts", {"fingerprint": "fpF", "title": "F", "severity": "P1"})
    # Update once
    _patch(server_url, f"/api/incidents/{inc['id']}", {
        "expected_version": inc["version"], "status": "acknowledged"
    })
    # Second update with stale version → 409
    status, body = _patch(server_url, f"/api/incidents/{inc['id']}", {
        "expected_version": inc["version"], "status": "resolved"
    })
    assert status == 409
    assert "error" in body


def test_patch_incident_invalid_transition(server_url):
    _, inc = _post(server_url, "/api/alerts", {"fingerprint": "fpG", "title": "G", "severity": "P1"})
    # Resolve first
    _, updated = _patch(server_url, f"/api/incidents/{inc['id']}", {
        "expected_version": inc["version"], "status": "resolved"
    })
    # Try acknowledge after resolved → 422
    status, body = _patch(server_url, f"/api/incidents/{inc['id']}", {
        "expected_version": updated["version"], "status": "acknowledged"
    })
    assert status == 422


def test_get_summary(server_url):
    _post(server_url, "/api/alerts", {"fingerprint": "fpH", "title": "H", "severity": "P1"})
    status, body = _get(server_url, "/api/summary")
    assert status == 200
    assert "total" in body
    assert body["total"] >= 1
    assert "by_status" in body
    assert "by_severity" in body


def test_post_escalations_run(server_url):
    status, body = _post(server_url, "/api/escalations/run", {})
    assert status == 200
    assert "escalated" in body


def test_static_files(server_url):
    with urllib.request.urlopen(server_url + "/", timeout=5) as resp:
        assert resp.status == 200
        content = resp.read()
        assert b"incident" in content.lower()
