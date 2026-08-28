"""Durable SQLite-backed incident store."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from collections.abc import Callable
from pathlib import Path

from .exceptions import (
    ClaimOwnershipError,
    IncidentNotFound,
    InvalidTransition,
    VersionConflict,
)
from .models import AuditEvent, Incident, IncidentStatus, Severity

SLA_SECONDS: dict[Severity, int] = {
    Severity.P1: 60,
    Severity.P2: 300,
    Severity.P3: 900,
    Severity.P4: 3600,
}

_VALID_TRANSITIONS: dict[IncidentStatus, set[IncidentStatus]] = {
    IncidentStatus.OPEN: {IncidentStatus.ACKNOWLEDGED, IncidentStatus.RESOLVED},
    IncidentStatus.ACKNOWLEDGED: {IncidentStatus.RESOLVED},
    IncidentStatus.RESOLVED: set(),
}

_DDL = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS incidents (
    id              TEXT PRIMARY KEY,
    fingerprint     TEXT NOT NULL,
    title           TEXT NOT NULL,
    severity        TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'open',
    owner           TEXT,
    alert_count     INTEGER NOT NULL DEFAULT 1,
    version         INTEGER NOT NULL DEFAULT 1,
    escalation_level INTEGER NOT NULL DEFAULT 0,
    sla_deadline    REAL NOT NULL,
    created_at      REAL NOT NULL,
    updated_at      REAL NOT NULL,
    claimed_by      TEXT,
    claim_expires_at REAL
);

CREATE TABLE IF NOT EXISTS audit_events (
    id          TEXT PRIMARY KEY,
    incident_id TEXT NOT NULL,
    type        TEXT NOT NULL,
    timestamp   REAL NOT NULL,
    details     TEXT NOT NULL DEFAULT '{}',
    FOREIGN KEY (incident_id) REFERENCES incidents(id)
);

CREATE TABLE IF NOT EXISTS idempotency_keys (
    key         TEXT PRIMARY KEY,
    incident_id TEXT NOT NULL,
    was_new     INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_incidents_fingerprint
    ON incidents (fingerprint, status, updated_at);

CREATE INDEX IF NOT EXISTS idx_incidents_escalation
    ON incidents (status, sla_deadline);

CREATE INDEX IF NOT EXISTS idx_events_incident
    ON audit_events (incident_id);
"""


class SQLiteIncidentStore:
    """Durable incident store backed by a SQLite database.

    Multiple store instances opened on the same file share state.  Within a
    single process each thread gets its own SQLite connection; writes use
    ``BEGIN IMMEDIATE`` transactions so concurrent ingest and escalation are
    safely serialized.
    """

    def __init__(
        self,
        db_path: str | Path,
        clock: Callable[[], float] = time.time,
        dedupe_window: float = 300.0,
        lease_seconds: float = 30.0,
    ) -> None:
        self._db_path = str(db_path)
        self._clock = clock
        self._dedupe_window = dedupe_window
        self._lease_seconds = lease_seconds
        self._local: threading.local = threading.local()
        self._init_db()

    # ------------------------------------------------------------------
    # Connection management
    # ------------------------------------------------------------------

    def _conn(self) -> sqlite3.Connection:
        conn: sqlite3.Connection | None = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self._db_path, isolation_level=None)
            conn.row_factory = sqlite3.Row
            # WAL and FK enforcement are connection-level settings.
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            self._local.conn = conn
        return conn

    def _init_db(self) -> None:
        conn = self._conn()
        # executescript commits any pending transaction first, which is fine
        # here because we're in isolation_level=None (autocommit) mode.
        conn.executescript(_DDL)

    # ------------------------------------------------------------------
    # Row → model helpers
    # ------------------------------------------------------------------

    def _row_to_incident(self, row: sqlite3.Row) -> Incident:
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
            sla_deadline=row["sla_deadline"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def _load(self, conn: sqlite3.Connection, incident_id: str) -> Incident | None:
        row = conn.execute(
            "SELECT * FROM incidents WHERE id = ?", [incident_id]
        ).fetchone()
        return self._row_to_incident(row) if row else None

    def _require(self, conn: sqlite3.Connection, incident_id: str) -> Incident:
        incident = self._load(conn, incident_id)
        if incident is None:
            raise IncidentNotFound(incident_id)
        return incident

    def _row_to_event(self, row: sqlite3.Row) -> AuditEvent:
        return AuditEvent(
            id=row["id"],
            incident_id=row["incident_id"],
            type=row["type"],
            timestamp=row["timestamp"],
            details=json.loads(row["details"]),
        )

    def _append_event(
        self,
        conn: sqlite3.Connection,
        incident_id: str,
        event_type: str,
        timestamp: float,
        details: dict,
    ) -> None:
        conn.execute(
            "INSERT INTO audit_events (id, incident_id, type, timestamp, details)"
            " VALUES (?, ?, ?, ?, ?)",
            [str(uuid.uuid4()), incident_id, event_type, timestamp, json.dumps(details)],
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def ingest_alert(
        self,
        fingerprint: str,
        title: str,
        severity: str | Severity,
        source: str = "unknown",
        details: object = None,
        idempotency_key: str | None = None,
        now: float | None = None,
    ) -> tuple[Incident, bool]:
        """Ingest an alert, creating a new incident or merging a duplicate.

        Returns ``(incident, True)`` when a new incident is created and
        ``(incident, False)`` when merged into an existing one.
        """
        ts = now if now is not None else self._clock()
        sev = Severity(severity)
        conn = self._conn()
        conn.execute("BEGIN IMMEDIATE")
        try:
            if idempotency_key:
                idem_row = conn.execute(
                    "SELECT incident_id, was_new FROM idempotency_keys WHERE key = ?",
                    [idempotency_key],
                ).fetchone()
                if idem_row:
                    incident = self._require(conn, idem_row["incident_id"])
                    conn.execute("COMMIT")
                    return incident, bool(idem_row["was_new"])

            # Find newest unresolved incident for this fingerprint within the
            # deduplication window.
            existing = conn.execute(
                """
                SELECT id FROM incidents
                WHERE fingerprint = ?
                  AND status != 'resolved'
                  AND updated_at >= ?
                ORDER BY created_at DESC
                LIMIT 1
                """,
                [fingerprint, ts - self._dedupe_window],
            ).fetchone()

            if existing:
                inc_id = existing["id"]
                inc_row = conn.execute(
                    "SELECT * FROM incidents WHERE id = ?", [inc_id]
                ).fetchone()
                new_version = inc_row["version"] + 1
                new_count = inc_row["alert_count"] + 1
                conn.execute(
                    "UPDATE incidents SET alert_count=?, version=?, updated_at=?"
                    " WHERE id=?",
                    [new_count, new_version, ts, inc_id],
                )
                self._append_event(
                    conn, inc_id, "duplicate_alert", ts,
                    {"source": source, "details": details},
                )
                if idempotency_key:
                    conn.execute(
                        "INSERT OR IGNORE INTO idempotency_keys VALUES (?,?,0)",
                        [idempotency_key, inc_id],
                    )
                incident = self._require(conn, inc_id)
                conn.execute("COMMIT")
                return incident, False
            else:
                inc_id = str(uuid.uuid4())
                sla_deadline = ts + SLA_SECONDS[sev]
                conn.execute(
                    """
                    INSERT INTO incidents
                        (id, fingerprint, title, severity, status, owner,
                         alert_count, version, escalation_level,
                         sla_deadline, created_at, updated_at)
                    VALUES (?,?,?,?,'open',NULL,1,1,0,?,?,?)
                    """,
                    [inc_id, fingerprint, title, sev.value, sla_deadline, ts, ts],
                )
                self._append_event(
                    conn, inc_id, "created", ts,
                    {"source": source, "title": title,
                     "severity": sev.value, "details": details},
                )
                if idempotency_key:
                    conn.execute(
                        "INSERT OR IGNORE INTO idempotency_keys VALUES (?,?,1)",
                        [idempotency_key, inc_id],
                    )
                incident = self._require(conn, inc_id)
                conn.execute("COMMIT")
                return incident, True
        except Exception:
            conn.execute("ROLLBACK")
            raise

    def get(self, incident_id: str) -> Incident | None:
        return self._load(self._conn(), incident_id)

    def list(
        self,
        status: str | None = None,
        severity: str | None = None,
        owner: str | None = None,
    ) -> list[Incident]:
        conditions: list[str] = []
        params: list[object] = []
        if status is not None:
            conditions.append("status = ?")
            params.append(status)
        if severity is not None:
            conditions.append("severity = ?")
            params.append(severity)
        if owner is not None:
            conditions.append("owner = ?")
            params.append(owner)
        where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
        sql = f"SELECT * FROM incidents {where} ORDER BY created_at ASC"
        rows = self._conn().execute(sql, params).fetchall()
        return [self._row_to_incident(r) for r in rows]

    def update(
        self,
        incident_id: str,
        expected_version: int,
        owner: str | None = None,
        status: str | None = None,
        idempotency_key: str | None = None,
        now: float | None = None,
    ) -> Incident:
        """Apply owner/status changes and return the updated incident.

        Raises :exc:`VersionConflict` when *expected_version* is stale and
        :exc:`InvalidTransition` for disallowed status changes.
        """
        ts = now if now is not None else self._clock()
        conn = self._conn()
        conn.execute("BEGIN IMMEDIATE")
        try:
            if idempotency_key:
                idem_row = conn.execute(
                    "SELECT incident_id FROM idempotency_keys WHERE key = ?",
                    [idempotency_key],
                ).fetchone()
                if idem_row:
                    incident = self._require(conn, idem_row["incident_id"])
                    conn.execute("COMMIT")
                    return incident

            inc_row = conn.execute(
                "SELECT * FROM incidents WHERE id = ?", [incident_id]
            ).fetchone()
            if inc_row is None:
                raise IncidentNotFound(incident_id)

            if inc_row["version"] != expected_version:
                raise VersionConflict(
                    f"expected version {expected_version},"
                    f" current is {inc_row['version']}"
                )

            current_status = IncidentStatus(inc_row["status"])
            new_status: IncidentStatus | None = None
            if status is not None:
                new_status = IncidentStatus(status)
                if new_status not in _VALID_TRANSITIONS[current_status]:
                    raise InvalidTransition(
                        f"cannot transition from {current_status} to {new_status}"
                    )

            new_version = inc_row["version"] + 1
            updates: dict[str, object] = {"version": new_version, "updated_at": ts}
            if owner is not None:
                updates["owner"] = owner.strip() or None
            if new_status is not None:
                updates["status"] = new_status.value

            set_clause = ", ".join(f"{k} = ?" for k in updates)
            conn.execute(
                f"UPDATE incidents SET {set_clause} WHERE id = ?",
                [*updates.values(), incident_id],
            )

            # Audit events — only emit when the value actually changes.
            current_owner: str | None = inc_row["owner"]
            if owner is not None:
                normalized = owner.strip() or None
                if normalized != current_owner:
                    self._append_event(
                        conn, incident_id, "owner_changed", ts,
                        {"from": current_owner, "to": normalized},
                    )

            if new_status is not None:
                self._append_event(
                    conn, incident_id, "status_changed", ts,
                    {"from": current_status.value, "to": new_status.value},
                )

            if idempotency_key:
                conn.execute(
                    "INSERT OR IGNORE INTO idempotency_keys VALUES (?,?,0)",
                    [idempotency_key, incident_id],
                )

            incident = self._require(conn, incident_id)
            conn.execute("COMMIT")
            return incident
        except Exception:
            conn.execute("ROLLBACK")
            raise

    def events(self, incident_id: str) -> list[AuditEvent]:
        """Return audit events for an incident in insertion order."""
        rows = self._conn().execute(
            "SELECT * FROM audit_events WHERE incident_id = ? ORDER BY rowid ASC",
            [incident_id],
        ).fetchall()
        return [self._row_to_event(r) for r in rows]

    def claim_due_escalation(
        self, worker_id: str, now: float | None = None
    ) -> Incident | None:
        """Atomically claim the oldest overdue unresolved incident.

        Returns ``None`` when there is nothing to escalate.  Separate workers
        are guaranteed to claim different incidents.
        """
        ts = now if now is not None else self._clock()
        conn = self._conn()
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute(
                """
                SELECT id FROM incidents
                WHERE status != 'resolved'
                  AND sla_deadline <= ?
                  AND (claimed_by IS NULL OR claim_expires_at <= ?)
                ORDER BY sla_deadline ASC
                LIMIT 1
                """,
                [ts, ts],
            ).fetchone()
            if row is None:
                conn.execute("COMMIT")
                return None

            inc_id = row["id"]
            expires_at = ts + self._lease_seconds
            conn.execute(
                "UPDATE incidents SET claimed_by=?, claim_expires_at=? WHERE id=?",
                [worker_id, expires_at, inc_id],
            )
            self._append_event(
                conn, inc_id, "escalation_claimed", ts,
                {"worker_id": worker_id, "expires_at": expires_at},
            )
            incident = self._require(conn, inc_id)
            conn.execute("COMMIT")
            return incident
        except Exception:
            conn.execute("ROLLBACK")
            raise

    def complete_escalation(
        self, incident_id: str, worker_id: str, now: float | None = None
    ) -> Incident:
        """Complete a claimed escalation, advancing the level and deadline."""
        ts = now if now is not None else self._clock()
        conn = self._conn()
        conn.execute("BEGIN IMMEDIATE")
        try:
            inc_row = conn.execute(
                "SELECT * FROM incidents WHERE id = ?", [incident_id]
            ).fetchone()
            if inc_row is None:
                raise IncidentNotFound(incident_id)
            if inc_row["claimed_by"] != worker_id:
                raise ClaimOwnershipError(
                    f"incident {incident_id} is not claimed by {worker_id!r}"
                )

            sev = Severity(inc_row["severity"])
            new_level = inc_row["escalation_level"] + 1
            new_version = inc_row["version"] + 1
            new_deadline = ts + SLA_SECONDS[sev]

            conn.execute(
                """
                UPDATE incidents
                SET escalation_level=?, version=?, sla_deadline=?, updated_at=?,
                    claimed_by=NULL, claim_expires_at=NULL
                WHERE id=?
                """,
                [new_level, new_version, new_deadline, ts, incident_id],
            )
            self._append_event(
                conn, incident_id, "escalated", ts,
                {"level": new_level, "worker_id": worker_id},
            )
            incident = self._require(conn, incident_id)
            conn.execute("COMMIT")
            return incident
        except Exception:
            conn.execute("ROLLBACK")
            raise

    def recover_expired_claims(self, now: float | None = None) -> int:
        """Clear expired worker leases so another worker can claim them.

        Returns the number of incidents whose claims were cleared.
        """
        ts = now if now is not None else self._clock()
        conn = self._conn()
        conn.execute("BEGIN IMMEDIATE")
        try:
            rows = conn.execute(
                "SELECT id FROM incidents WHERE claimed_by IS NOT NULL"
                " AND claim_expires_at <= ?",
                [ts],
            ).fetchall()
            count = 0
            for row in rows:
                conn.execute(
                    "UPDATE incidents SET claimed_by=NULL, claim_expires_at=NULL"
                    " WHERE id=?",
                    [row["id"]],
                )
                self._append_event(
                    conn, row["id"], "claim_recovered", ts, {}
                )
                count += 1
            conn.execute("COMMIT")
            return count
        except Exception:
            conn.execute("ROLLBACK")
            raise
