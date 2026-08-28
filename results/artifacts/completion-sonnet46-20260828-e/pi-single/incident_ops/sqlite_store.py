"""Durable SQLite-backed incident store."""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from collections.abc import Callable
from typing import Any

from .models import (
    AuditEvent,
    ClaimError,
    Incident,
    IncidentNotFound,
    IncidentStatus,
    InvalidTransition,
    Severity,
    SLA_SECONDS,
    VALID_TRANSITIONS,
    VersionConflict,
)

_DDL = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS incidents (
    id              TEXT PRIMARY KEY,
    fingerprint     TEXT NOT NULL DEFAULT '',
    title           TEXT NOT NULL,
    severity        TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'open',
    owner           TEXT,
    alert_count     INTEGER NOT NULL DEFAULT 1,
    version         INTEGER NOT NULL DEFAULT 1,
    escalation_level INTEGER NOT NULL DEFAULT 0,
    created_at      REAL NOT NULL,
    updated_at      REAL NOT NULL,
    sla_deadline    REAL NOT NULL,
    claim_worker    TEXT,
    claim_expires   REAL
);

CREATE INDEX IF NOT EXISTS idx_incidents_fingerprint ON incidents(fingerprint);
CREATE INDEX IF NOT EXISTS idx_incidents_status ON incidents(status);

CREATE TABLE IF NOT EXISTS audit_events (
    id          TEXT PRIMARY KEY,
    incident_id TEXT NOT NULL REFERENCES incidents(id),
    type        TEXT NOT NULL,
    timestamp   REAL NOT NULL,
    details     TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_audit_incident ON audit_events(incident_id, timestamp);

CREATE TABLE IF NOT EXISTS idempotency_keys (
    key             TEXT PRIMARY KEY,
    incident_id     TEXT NOT NULL,
    created         INTEGER NOT NULL     -- 1 = created new, 0 = duplicate
);
"""


def _row_to_incident(row: sqlite3.Row) -> Incident:
    return Incident(
        id=row["id"],
        fingerprint=row["fingerprint"],
        title=row["title"],
        severity=Severity(row["severity"]),
        status=IncidentStatus(row["status"]),
        owner=row["owner"],
        alert_count=row["alert_count"],
        version=row["version"],
        escalation_level=row["escalation_level"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        sla_deadline=row["sla_deadline"],
    )


def _row_to_event(row: sqlite3.Row) -> AuditEvent:
    return AuditEvent(
        id=row["id"],
        incident_id=row["incident_id"],
        type=row["type"],
        timestamp=row["timestamp"],
        details=json.loads(row["details"]),
    )


class SQLiteIncidentStore:
    """Persistent incident store backed by SQLite.

    Parameters
    ----------
    db_path:
        Path to the SQLite database file (use ":memory:" for testing).
    clock:
        Callable that returns the current Unix timestamp.  Defaults to
        ``time.time``.
    dedupe_window:
        Seconds within which a duplicate alert merges into an existing open
        incident.  Defaults to 300 (five minutes).
    lease_seconds:
        Seconds after which an escalation claim expires.  Defaults to 60.
    """

    def __init__(
        self,
        db_path: str,
        clock: Callable[[], float] = time.time,
        dedupe_window: float = 300.0,
        lease_seconds: float = 60.0,
    ) -> None:
        self._db_path = db_path
        self._clock = clock
        self._dedupe_window = dedupe_window
        self._lease_seconds = lease_seconds
        self._init_db()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path, check_same_thread=False, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    def _init_db(self) -> None:
        conn = self._connect()
        try:
            conn.executescript(_DDL)
            conn.commit()
        finally:
            conn.close()

    def _now(self, now: float | None) -> float:
        return now if now is not None else self._clock()

    def _insert_event(
        self,
        cur: sqlite3.Cursor,
        incident_id: str,
        event_type: str,
        timestamp: float,
        details: dict[str, Any] | None = None,
    ) -> AuditEvent:
        event = AuditEvent(
            id=str(uuid.uuid4()),
            incident_id=incident_id,
            type=event_type,
            timestamp=timestamp,
            details=details or {},
        )
        cur.execute(
            "INSERT INTO audit_events (id, incident_id, type, timestamp, details) "
            "VALUES (?, ?, ?, ?, ?)",
            (event.id, event.incident_id, event.type, event.timestamp,
             json.dumps(event.details)),
        )
        return event

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def ingest_alert(
        self,
        fingerprint: str,
        title: str,
        severity: str | Severity,
        source: str = "unknown",
        details: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
        now: float | None = None,
    ) -> tuple[Incident, bool]:
        """Ingest an alert and return (incident, created_new).

        If *idempotency_key* is given and has been seen before, the stored
        result is returned without any further changes.
        """
        severity = Severity(severity)
        ts = self._now(now)
        conn = self._connect()
        try:
            with conn:
                # Check idempotency key first (inside the transaction)
                if idempotency_key is not None:
                    row = conn.execute(
                        "SELECT incident_id, created FROM idempotency_keys WHERE key=?",
                        (idempotency_key,),
                    ).fetchone()
                    if row is not None:
                        inc = self._get_conn(conn, row["incident_id"])
                        return (inc, bool(row["created"]))  # type: ignore[return-value]

                # Look for an existing open incident with the same fingerprint
                # within the deduplication window.  Use a write-lock via
                # INSERT OR IGNORE to protect against concurrent creation.
                existing = conn.execute(
                    """
                    SELECT * FROM incidents
                    WHERE fingerprint=?
                      AND status != 'resolved'
                    ORDER BY created_at DESC
                    LIMIT 1
                    """,
                    (fingerprint,),
                ).fetchone()

                if existing is not None:
                    age = ts - existing["updated_at"]
                    if age <= self._dedupe_window:
                        # Merge into existing incident
                        new_version = existing["version"] + 1
                        new_count = existing["alert_count"] + 1
                        conn.execute(
                            """
                            UPDATE incidents
                            SET alert_count=?, updated_at=?, version=?
                            WHERE id=?
                            """,
                            (new_count, ts, new_version, existing["id"]),
                        )
                        self._insert_event(
                            conn.cursor(), existing["id"], "duplicate_alert", ts,
                            {"source": source, "details": details, "fingerprint": fingerprint},
                        )
                        if idempotency_key is not None:
                            conn.execute(
                                "INSERT INTO idempotency_keys (key, incident_id, created) VALUES (?,?,?)",
                                (idempotency_key, existing["id"], 0),
                            )
                        row2 = conn.execute(
                            "SELECT * FROM incidents WHERE id=?", (existing["id"],)
                        ).fetchone()
                        return (_row_to_incident(row2), False)

                # Create a brand-new incident
                inc_id = str(uuid.uuid4())
                sla_deadline = ts + SLA_SECONDS[severity.value]
                conn.execute(
                    """
                    INSERT INTO incidents
                        (id, fingerprint, title, severity, status, owner,
                         alert_count, version, escalation_level,
                         created_at, updated_at, sla_deadline)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (inc_id, fingerprint, title, severity.value, "open", None,
                     1, 1, 0, ts, ts, sla_deadline),
                )
                cur = conn.cursor()
                self._insert_event(cur, inc_id, "created", ts,
                                   {"source": source, "details": details, "fingerprint": fingerprint})
                if idempotency_key is not None:
                    conn.execute(
                        "INSERT INTO idempotency_keys (key, incident_id, created) VALUES (?,?,?)",
                        (idempotency_key, inc_id, 1),
                    )
                row3 = conn.execute(
                    "SELECT * FROM incidents WHERE id=?", (inc_id,)
                ).fetchone()
                return (_row_to_incident(row3), True)
        finally:
            conn.close()

    def _get_conn(self, conn: sqlite3.Connection, incident_id: str) -> Incident:
        row = conn.execute(
            "SELECT * FROM incidents WHERE id=?", (incident_id,)
        ).fetchone()
        if row is None:
            raise IncidentNotFound(incident_id)
        return _row_to_incident(row)

    def get(self, incident_id: str) -> Incident | None:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM incidents WHERE id=?", (incident_id,)
            ).fetchone()
            return _row_to_incident(row) if row is not None else None
        finally:
            conn.close()

    def list(
        self,
        status: str | IncidentStatus | None = None,
        severity: str | Severity | None = None,
        owner: str | None = None,
    ) -> list[Incident]:
        query = "SELECT * FROM incidents WHERE 1=1"
        params: list[Any] = []
        if status is not None:
            query += " AND status=?"
            params.append(str(status))
        if severity is not None:
            query += " AND severity=?"
            params.append(str(severity))
        if owner is not None:
            query += " AND owner=?"
            params.append(owner)
        query += " ORDER BY created_at ASC"

        conn = self._connect()
        try:
            rows = conn.execute(query, params).fetchall()
            return [_row_to_incident(r) for r in rows]
        finally:
            conn.close()

    def update(
        self,
        incident_id: str,
        expected_version: int,
        owner: str | None = None,
        status: str | IncidentStatus | None = None,
        idempotency_key: str | None = None,
        now: float | None = None,
    ) -> Incident:
        ts = self._now(now)
        conn = self._connect()
        try:
            with conn:
                if idempotency_key is not None:
                    row = conn.execute(
                        "SELECT incident_id FROM idempotency_keys WHERE key=?",
                        (idempotency_key,),
                    ).fetchone()
                    if row is not None:
                        return self._get_conn(conn, row["incident_id"])

                current = conn.execute(
                    "SELECT * FROM incidents WHERE id=?", (incident_id,)
                ).fetchone()
                if current is None:
                    raise IncidentNotFound(incident_id)
                if current["version"] != expected_version:
                    raise VersionConflict(
                        f"expected version {expected_version}, got {current['version']}"
                    )

                cur_status = IncidentStatus(current["status"])
                new_version = current["version"] + 1
                updated_at = ts
                cur = conn.cursor()

                # Validate and apply status change
                if status is not None:
                    new_status = IncidentStatus(status)
                    if (cur_status, new_status) not in VALID_TRANSITIONS:
                        raise InvalidTransition(
                            f"cannot transition from {cur_status} to {new_status}"
                        )
                    conn.execute(
                        "UPDATE incidents SET status=?, updated_at=?, version=? WHERE id=?",
                        (new_status.value, updated_at, new_version, incident_id),
                    )
                    new_version += 1
                    self._insert_event(
                        cur, incident_id, "status_changed", ts,
                        {"from": cur_status.value, "to": new_status.value},
                    )

                # Apply owner change
                if owner is not None:
                    new_owner = owner.strip() or None
                    conn.execute(
                        "UPDATE incidents SET owner=?, updated_at=?, version=? WHERE id=?",
                        (new_owner, updated_at, new_version, incident_id),
                    )
                    new_version += 1
                    self._insert_event(
                        cur, incident_id, "owner_changed", ts,
                        {"from": current["owner"], "to": new_owner},
                    )

                if idempotency_key is not None:
                    conn.execute(
                        "INSERT INTO idempotency_keys (key, incident_id, created) VALUES (?,?,?)",
                        (idempotency_key, incident_id, 0),
                    )

                return self._get_conn(conn, incident_id)
        finally:
            conn.close()

    def events(self, incident_id: str) -> list[AuditEvent]:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT * FROM audit_events WHERE incident_id=? ORDER BY timestamp ASC",
                (incident_id,),
            ).fetchall()
            return [_row_to_event(r) for r in rows]
        finally:
            conn.close()

    def claim_due_escalation(
        self,
        worker_id: str,
        now: float | None = None,
    ) -> Incident | None:
        ts = self._now(now)
        lease_expires = ts + self._lease_seconds
        conn = self._connect()
        try:
            with conn:
                # Atomically find & claim the oldest overdue unresolved incident
                row = conn.execute(
                    """
                    SELECT * FROM incidents
                    WHERE status != 'resolved'
                      AND sla_deadline <= ?
                      AND (claim_worker IS NULL OR claim_expires <= ?)
                    ORDER BY sla_deadline ASC
                    LIMIT 1
                    """,
                    (ts, ts),
                ).fetchone()
                if row is None:
                    return None
                conn.execute(
                    "UPDATE incidents SET claim_worker=?, claim_expires=? WHERE id=?",
                    (worker_id, lease_expires, row["id"]),
                )
                # Re-read after update to include claim fields if needed
                row2 = conn.execute(
                    "SELECT * FROM incidents WHERE id=?", (row["id"],)
                ).fetchone()
                self._insert_event(
                    conn.cursor(), row["id"], "escalation_claimed", ts,
                    {"worker_id": worker_id},
                )
                return _row_to_incident(row2)
        finally:
            conn.close()

    def complete_escalation(
        self,
        incident_id: str,
        worker_id: str,
        now: float | None = None,
    ) -> Incident:
        ts = self._now(now)
        conn = self._connect()
        try:
            with conn:
                current = conn.execute(
                    "SELECT * FROM incidents WHERE id=?", (incident_id,)
                ).fetchone()
                if current is None:
                    raise IncidentNotFound(incident_id)
                if current["claim_worker"] != worker_id:
                    raise ClaimError(
                        f"worker {worker_id!r} does not own the claim on {incident_id}"
                    )
                severity_str = current["severity"]
                sla_interval = SLA_SECONDS[severity_str]
                new_deadline = ts + sla_interval
                new_level = current["escalation_level"] + 1
                new_version = current["version"] + 1
                conn.execute(
                    """
                    UPDATE incidents
                    SET escalation_level=?, version=?, sla_deadline=?,
                        claim_worker=NULL, claim_expires=NULL, updated_at=?
                    WHERE id=?
                    """,
                    (new_level, new_version, new_deadline, ts, incident_id),
                )
                self._insert_event(
                    conn.cursor(), incident_id, "escalated", ts,
                    {"level": new_level, "worker_id": worker_id, "next_deadline": new_deadline},
                )
                return self._get_conn(conn, incident_id)
        finally:
            conn.close()

    def recover_expired_claims(self, now: float | None = None) -> int:
        ts = self._now(now)
        conn = self._connect()
        try:
            with conn:
                rows = conn.execute(
                    """
                    SELECT id, claim_worker FROM incidents
                    WHERE claim_worker IS NOT NULL AND claim_expires <= ?
                    """,
                    (ts,),
                ).fetchall()
                if not rows:
                    return 0
                cur = conn.cursor()
                for row in rows:
                    conn.execute(
                        "UPDATE incidents SET claim_worker=NULL, claim_expires=NULL WHERE id=?",
                        (row["id"],),
                    )
                    self._insert_event(
                        cur, row["id"], "claim_recovered", ts,
                        {"worker_id": row["claim_worker"]},
                    )
                return len(rows)
        finally:
            conn.close()
