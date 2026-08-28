"""HTTP API integration tests.

The server is started in a background thread per test and shut down after.
All waits are bounded to avoid hanging if the server fails to start.
"""

from __future__ import annotations

import json
import threading
import urllib.request
import urllib.error

import pytest

from incident_ops.sqlite_store import SQLiteIncidentStore
from incident_ops.web import create_server


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def http(tmp_path):
    """Start a server on a random port; yield base URL; shut down after."""
    db = str(tmp_path / "api_test.db")
    store = SQLiteIncidentStore(db)
    server = create_server(store, host="127.0.0.1", port=0)
    host, port = server.server_address
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://{host}:{port}"
    yield base, store
    server.shutdown()
    thread.join(timeout=5)


def _get(url):
    with urllib.request.urlopen(url, timeout=5) as r:
        return r.status, json.loads(r.read())


def _post(url, body):
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        url, data=data, method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def _patch(url, body):
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        url, data=data, method="PATCH",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


# ---------------------------------------------------------------------------
# Static files
# ---------------------------------------------------------------------------

def test_root_returns_html(http):
    base, _ = http
    with urllib.request.urlopen(base + "/", timeout=5) as r:
        assert r.status == 200
        ct = r.headers.get("Content-Type", "")
        assert "text/html" in ct


# ---------------------------------------------------------------------------
# POST /api/alerts
# ---------------------------------------------------------------------------

def test_post_alert_creates_incident(http):
    base, _ = http
    status, body = _post(base + "/api/alerts", {
        "fingerprint": "fp1", "title": "DB down", "severity": "P1",
    })
    assert status == 201
    assert body["fingerprint"] == "fp1"
    assert body["status"] == "open"
    assert body["alert_count"] == 1


def test_post_alert_duplicate_merges(http):
    base, _ = http
    _post(base + "/api/alerts", {"fingerprint": "fp2", "title": "A", "severity": "P2"})
    status, body = _post(base + "/api/alerts", {"fingerprint": "fp2", "title": "A", "severity": "P2"})
    assert status == 200
    assert body["alert_count"] == 2


def test_post_alert_missing_field_returns_400(http):
    base, _ = http
    status, body = _post(base + "/api/alerts", {"fingerprint": "fp3", "title": "A"})
    assert status == 400
    assert "error" in body


def test_post_alert_bad_severity_returns_400(http):
    base, _ = http
    status, body = _post(base + "/api/alerts", {"fingerprint": "fp4", "title": "A", "severity": "P9"})
    assert status == 400
    assert "error" in body


# ---------------------------------------------------------------------------
# GET /api/incidents
# ---------------------------------------------------------------------------

def test_get_incidents_returns_list(http):
    base, store = http
    store.ingest_alert("fp1", "A", "P1", now=1.0)
    store.ingest_alert("fp2", "B", "P2", now=2.0)
    status, body = _get(base + "/api/incidents")
    assert status == 200
    assert isinstance(body, list)
    assert len(body) == 2


def test_get_incidents_filter_by_status(http):
    base, store = http
    inc, _ = store.ingest_alert("fp1", "A", "P1", now=1.0)
    store.ingest_alert("fp2", "B", "P2", now=2.0)
    store.update(inc.id, expected_version=1, status="resolved", now=3.0)
    status, body = _get(base + "/api/incidents?status=resolved")
    assert status == 200
    assert len(body) == 1
    assert body[0]["status"] == "resolved"


def test_get_incidents_filter_by_severity(http):
    base, store = http
    store.ingest_alert("fp1", "A", "P1", now=1.0)
    store.ingest_alert("fp2", "B", "P2", now=2.0)
    status, body = _get(base + "/api/incidents?severity=P1")
    assert status == 200
    assert len(body) == 1
    assert body[0]["severity"] == "P1"


# ---------------------------------------------------------------------------
# GET /api/incidents/{id}
# ---------------------------------------------------------------------------

def test_get_incident_detail_includes_events(http):
    base, store = http
    inc, _ = store.ingest_alert("fp1", "A", "P1", now=1.0)
    store.update(inc.id, expected_version=1, owner="alice", now=2.0)
    status, body = _get(base + f"/api/incidents/{inc.id}")
    assert status == 200
    assert "events" in body
    assert len(body["events"]) == 2  # created + owner_changed


def test_get_incident_not_found_returns_404(http):
    base, _ = http
    try:
        _get(base + "/api/incidents/no-such-id")
        assert False, "should have raised"
    except urllib.error.HTTPError as e:
        assert e.code == 404


# ---------------------------------------------------------------------------
# PATCH /api/incidents/{id}
# ---------------------------------------------------------------------------

def test_patch_acknowledge(http):
    base, store = http
    inc, _ = store.ingest_alert("fp1", "A", "P1", now=1.0)
    status, body = _patch(
        base + f"/api/incidents/{inc.id}",
        {"expected_version": 1, "status": "acknowledged"},
    )
    assert status == 200
    assert body["status"] == "acknowledged"
    assert "events" in body


def test_patch_assign_owner(http):
    base, store = http
    inc, _ = store.ingest_alert("fp1", "A", "P1", now=1.0)
    status, body = _patch(
        base + f"/api/incidents/{inc.id}",
        {"expected_version": 1, "owner": "bob"},
    )
    assert status == 200
    assert body["owner"] == "bob"


def test_patch_version_conflict_returns_409(http):
    base, store = http
    inc, _ = store.ingest_alert("fp1", "A", "P1", now=1.0)
    status, body = _patch(
        base + f"/api/incidents/{inc.id}",
        {"expected_version": 99, "status": "acknowledged"},
    )
    assert status == 409
    assert "error" in body


def test_patch_invalid_transition_returns_422(http):
    base, store = http
    inc, _ = store.ingest_alert("fp1", "A", "P1", now=1.0)
    store.update(inc.id, expected_version=1, status="resolved")
    status, body = _patch(
        base + f"/api/incidents/{inc.id}",
        {"expected_version": 2, "status": "acknowledged"},
    )
    assert status == 422
    assert "error" in body


def test_patch_missing_expected_version_returns_400(http):
    base, store = http
    inc, _ = store.ingest_alert("fp1", "A", "P1", now=1.0)
    status, body = _patch(
        base + f"/api/incidents/{inc.id}",
        {"status": "acknowledged"},
    )
    assert status == 400


def test_patch_not_found_returns_404(http):
    base, _ = http
    status, body = _patch(
        base + "/api/incidents/no-such-id",
        {"expected_version": 1, "status": "acknowledged"},
    )
    assert status == 404


# ---------------------------------------------------------------------------
# GET /api/summary
# ---------------------------------------------------------------------------

def test_summary_counts(http):
    base, store = http
    inc, _ = store.ingest_alert("fp1", "A", "P1", now=1.0)
    store.ingest_alert("fp2", "B", "P2", now=2.0)
    store.update(inc.id, expected_version=1, status="resolved", now=3.0)
    status, body = _get(base + "/api/summary")
    assert status == 200
    assert body["total"] == 2
    assert body.get("open", 0) == 1
    assert body.get("resolved", 0) == 1
    assert body.get("P1", 0) == 1
    assert body.get("P2", 0) == 1


# ---------------------------------------------------------------------------
# POST /api/escalations/run
# ---------------------------------------------------------------------------

def test_escalation_run_endpoint(http):
    base, store = http
    store.ingest_alert("fp1", "A", "P1", now=1000.0)
    # Simulate overdue by setting now explicitly through the endpoint
    # (the endpoint uses real clock, so we test with an empty result).
    status, body = _post(base + "/api/escalations/run", {})
    assert status == 200
    assert "escalated" in body
