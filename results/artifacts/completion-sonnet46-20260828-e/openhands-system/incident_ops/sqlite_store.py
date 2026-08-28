"""Durable SQLite-backed incident store."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from collections.abc import Callable
from pathlib import Path

from .models import AuditEvent, Incident, IncidentStatus, Severity

# ---------------------------------------------------------------------------
# SLA windows per severity (seconds)
# ---------------------------------------------------------------------------

SLA_SECONDS: dict[Severity, int] = {
    Severity.P1: 60,
    Severity.P2: 300,
    Severity.P3: 900,
    Severity.P4: 3600,
}

# Valid operator status transitions
_VALID_TRANSITIONS: frozenset[tuple[IncidentStatus, IncidentStatus]] = frozenset(
    {
        (IncidentStatus.OPEN, IncidentStatus.ACKNOWLEDGED),
        (IncidentStatus.OPEN, IncidentStatus.RESOLVED),
        (IncidentStatus.ACKNOWLEDGED, IncidentStatus.RESOLVED),
    }
)

# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class IncidentNotFound(KeyError):
    def __init__(self, incident_id: str) -> None:
        super().__init__(incident_id)
        self.incident_id = incident_id

    def __str__(self) -> str:
        return f"incident not found: {self.incident_id}"


class VersionConflict(Exception):
    def __init__(self, incident_id: str, expected: int, actual: int) -> None:
        super().__init__(
            f"version conflict on {incident_id}: expected {expected}, got {actual}"
        )
        self.incident_id = incident_id
        self.expected = expected
        self.actual = actual


class InvalidTransition(ValueError):
    def __init__(self, from_status: IncidentStatus, to_status: IncidentStatus) -> None:
        super().__init__(
            f"invalid transition: {from_status.value} → {to_status.value}"
        )
        self.from_status = from_status
        self.to_status = to_status


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

_DDL = """
CREATE TABLE IF NOT EXISTS incidents (
    id               TEXT PRIMARY KEY,
    fingerprint      TEXT NOT NULL DEFAULT '',
    title            TEXT NOT NULL,
    severity         TEXT NOT NULL,
    status           TEXT NOT NULL DEFAULT 'open',
    owner            TEXT,
    alert_count      INTEGER NOT NULL DEFAULT 1,
    version          INTEGER NOT NULL DEFAULT 1,
    escalation_level INTEGER NOT NULL DEFAULT 0,
    created_at       REAL NOT NULL,
    updated_at       REAL NOT NULL,
    sla_deadline     REAL NOT NULL,
    claimed_by       TEXT,
    claim_expires_at REAL
);

CREATE TABLE IF NOT EXISTS audit_events (
    id          TEXT PRIMARY KEY,
    incident_id TEXT NOT NULL REFERENCES incidents(id),
    type        TEXT NOT NULL,
    timestamp   REAL NOT NULL,
    details     TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_audit_incident_ts
    ON audit_events (incident_id, timestamp);

CREATE TABLE IF NOT EXISTS idempotency_keys (
    key         TEXT PRIMARY KEY,
    incident_id TEXT NOT NULL,
    created     INTEGER NOT NULL DEFAULT 0
);
"""

# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


class SQLiteIncidentStore:
    """Durable incident store backed by a SQLite database file."""

    def __init__(
        self,
        db_path: str | Path,
        clock: Callable[[], float] = time.time,
        dedupe_window: float = 300.0,
        lease_seconds: float = 30.0,
    ) -> None:
        self._db_path = str(Path(db_path).resolve())
        self._clock = clock
        self._dedupe_window = dedupe_window
        self._lease_seconds = lease_seconds
        self._local = threading.local()
        self._init_db()

    # ------------------------------------------------------------------
    # Connection helpers
    # ------------------------------------------------------------------

    def _conn(self) -> sqlite3.Connection:
        """Return (or lazily create) a per-thread connection."""
        if not hasattr(self._local, "conn") or self._local.conn is None:
            conn = sqlite3.connect(self._db_path, timeout=15, isolation_level=None)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            self._local.conn = conn
        return self._local.conn

    def _init_db(self) -> None:
        conn = self._conn()
        conn.executescript(_DDL)

    # ------------------------------------------------------------------
    # Row helpers
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
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            sla_deadline=row["sla_deadline"],
        )

    def _row_to_event(self, row: sqlite3.Row) -> AuditEvent:
        return AuditEvent(
            id=row["id"],
            incident_id=row["incident_id"],
            type=row["type"],
            timestamp=row["timestamp"],
            details=json.loads(row["details"]),
        )

    def _fetch_incident(self, conn: sqlite3.Connection, incident_id: str) -> Incident:
        row = conn.execute(
            "SELECT * FROM incidents WHERE id = ?", (incident_id,)
        ).fetchone()
        if row is None:
            raise IncidentNotFound(incident_id)
        return self._row_to_incident(row)

    def _new_event(
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
            (str(uuid.uuid4()), incident_id, event_type, timestamp, json.dumps(details)),
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
        details: dict | None = None,
        idempotency_key: str | None = None,
        now: float | None = None,
    ) -> tuple[Incident, bool]:
        """Ingest an alert; returns (incident, created)."""
        ts = now if now is not None else self._clock()
        sev = Severity(severity)
        extra = details or {}

        conn = self._conn()
        conn.execute("BEGIN IMMEDIATE")
        try:
            # Idempotency check
            if idempotency_key:
                idem = conn.execute(
                    "SELECT incident_id, created FROM idempotency_keys WHERE key = ?",
                    (idempotency_key,),
                ).fetchone()
                if idem:
                    incident = self._fetch_incident(conn, idem["incident_id"])
                    conn.execute("COMMIT")
                    return incident, bool(idem["created"])

            # Find newest unresolved incident for this fingerprint
            existing = conn.execute(
                """SELECT id, updated_at FROM incidents
                   WHERE fingerprint = ? AND status != 'resolved'
                   ORDER BY created_at DESC LIMIT 1""",
                (fingerprint,),
            ).fetchone()

            if existing is not None and (ts - existing["updated_at"]) <= self._dedupe_window:
                # Merge into existing
                incident_id = existing["id"]
                conn.execute(
                    "UPDATE incidents SET alert_count = alert_count + 1,"
                    " updated_at = ?, version = version + 1 WHERE id = ?",
                    (ts, incident_id),
                )
                new_count = conn.execute(
                    "SELECT alert_count FROM incidents WHERE id = ?", (incident_id,)
                ).fetchone()["alert_count"]
                self._new_event(
                    conn,
                    incident_id,
                    "duplicate_alert",
                    ts,
                    {"source": source, "fingerprint": fingerprint, "alert_count": new_count, **extra},
                )
                created = False
            else:
                # Create new incident
                incident_id = str(uuid.uuid4())
                sla_deadline = ts + SLA_SECONDS[sev]
                conn.execute(
                    """INSERT INTO incidents
                       (id, fingerprint, title, severity, status, owner,
                        alert_count, version, escalation_level,
                        created_at, updated_at, sla_deadline)
                       VALUES (?, ?, ?, ?, 'open', NULL, 1, 1, 0, ?, ?, ?)""",
                    (incident_id, fingerprint, title, sev.value, ts, ts, sla_deadline),
                )
                self._new_event(
                    conn,
                    incident_id,
                    "created",
                    ts,
                    {"source": source, "fingerprint": fingerprint, **extra},
                )
                created = True

            if idempotency_key:
                conn.execute(
                    "INSERT OR IGNORE INTO idempotency_keys (key, incident_id, created)"
                    " VALUES (?, ?, ?)",
                    (idempotency_key, incident_id, int(created)),
                )

            incident = self._fetch_incident(conn, incident_id)
            conn.execute("COMMIT")
            return incident, created

        except Exception:
            conn.execute("ROLLBACK")
            raise

    def get(self, incident_id: str) -> Incident | None:
        row = self._conn().execute(
            "SELECT * FROM incidents WHERE id = ?", (incident_id,)
        ).fetchone()
        return self._row_to_incident(row) if row else None

    def list(
        self,
        status: str | None = None,
        severity: str | None = None,
        owner: str | None = None,
    ) -> list[Incident]:
        clauses: list[str] = []
        params: list[object] = []
        if status is not None:
            clauses.append("status = ?")
            params.append(status)
        if severity is not None:
            clauses.append("severity = ?")
            params.append(severity)
        if owner is not None:
            clauses.append("owner = ?")
            params.append(owner)

        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        rows = self._conn().execute(
            f"SELECT * FROM incidents {where} ORDER BY created_at ASC",
            params,
        ).fetchall()
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
        ts = now if now is not None else self._clock()
        conn = self._conn()
        conn.execute("BEGIN IMMEDIATE")
        try:
            # Idempotency check
            if idempotency_key:
                idem = conn.execute(
                    "SELECT incident_id FROM idempotency_keys WHERE key = ?",
                    (idempotency_key,),
                ).fetchone()
                if idem:
                    incident = self._fetch_incident(conn, idem["incident_id"])
                    conn.execute("COMMIT")
                    return incident

            row = conn.execute(
                "SELECT * FROM incidents WHERE id = ?", (incident_id,)
            ).fetchone()
            if row is None:
                raise IncidentNotFound(incident_id)
            if row["version"] != expected_version:
                raise VersionConflict(incident_id, expected_version, row["version"])

            if status is not None:
                current = IncidentStatus(row["status"])
                new_s = IncidentStatus(status)
                if (current, new_s) not in _VALID_TRANSITIONS:
                    raise InvalidTransition(current, new_s)

            changes: dict[str, object] = {"version": row["version"] + 1, "updated_at": ts}
            events: list[tuple[str, dict]] = []

            if owner is not None:
                new_owner = owner.strip() or None
                if new_owner != row["owner"]:
                    changes["owner"] = new_owner
                    events.append(("owner_changed", {"from": row["owner"], "to": new_owner}))

            if status is not None:
                new_s = IncidentStatus(status)
                current = IncidentStatus(row["status"])
                if new_s != current:
                    changes["status"] = new_s.value
                    events.append(("status_changed", {"from": current.value, "to": new_s.value}))

            set_clause = ", ".join(f"{k} = ?" for k in changes)
            conn.execute(
                f"UPDATE incidents SET {set_clause} WHERE id = ?",
                [*changes.values(), incident_id],
            )

            for event_type, event_details in events:
                self._new_event(conn, incident_id, event_type, ts, event_details)

            if idempotency_key:
                conn.execute(
                    "INSERT OR IGNORE INTO idempotency_keys (key, incident_id, created)"
                    " VALUES (?, ?, 0)",
                    (idempotency_key, incident_id),
                )

            incident = self._fetch_incident(conn, incident_id)
            conn.execute("COMMIT")
            return incident

        except Exception:
            conn.execute("ROLLBACK")
            raise

    def events(self, incident_id: str) -> list[AuditEvent]:
        rows = self._conn().execute(
            "SELECT * FROM audit_events WHERE incident_id = ? ORDER BY timestamp ASC",
            (incident_id,),
        ).fetchall()
        return [self._row_to_event(r) for r in rows]

    def claim_due_escalation(
        self, worker_id: str, now: float | None = None
    ) -> Incident | None:
        """Atomically claim the oldest overdue unresolved incident."""
        ts = now if now is not None else self._clock()
        conn = self._conn()
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute(
                """UPDATE incidents
                   SET claimed_by = ?, claim_expires_at = ?
                   WHERE id = (
                       SELECT id FROM incidents
                       WHERE status != 'resolved'
                         AND claimed_by IS NULL
                         AND sla_deadline <= ?
                       ORDER BY sla_deadline ASC
                       LIMIT 1
                   )
                   RETURNING *""",
                (worker_id, ts + self._lease_seconds, ts),
            ).fetchone()

            if row is not None:
                self._new_event(
                    conn,
                    row["id"],
                    "escalation_claimed",
                    ts,
                    {"worker_id": worker_id},
                )

            conn.execute("COMMIT")
            return self._row_to_incident(row) if row else None

        except Exception:
            conn.execute("ROLLBACK")
            raise

    def complete_escalation(
        self, incident_id: str, worker_id: str, now: float | None = None
    ) -> Incident:
        """Complete a previously claimed escalation."""
        ts = now if now is not None else self._clock()
        conn = self._conn()
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute(
                "SELECT * FROM incidents WHERE id = ?", (incident_id,)
            ).fetchone()
            if row is None:
                raise IncidentNotFound(incident_id)
            if row["claimed_by"] != worker_id:
                raise ValueError(
                    f"incident {incident_id} is not claimed by worker {worker_id}"
                )

            sev = Severity(row["severity"])
            new_deadline = row["sla_deadline"] + SLA_SECONDS[sev]
            new_level = row["escalation_level"] + 1
            new_version = row["version"] + 1

            conn.execute(
                """UPDATE incidents
                   SET escalation_level = ?, version = ?, sla_deadline = ?,
                       claimed_by = NULL, claim_expires_at = NULL, updated_at = ?
                   WHERE id = ?""",
                (new_level, new_version, new_deadline, ts, incident_id),
            )
            self._new_event(
                conn,
                incident_id,
                "escalated",
                ts,
                {
                    "worker_id": worker_id,
                    "escalation_level": new_level,
                    "next_deadline": new_deadline,
                },
            )

            incident = self._fetch_incident(conn, incident_id)
            conn.execute("COMMIT")
            return incident

        except Exception:
            conn.execute("ROLLBACK")
            raise

    def recover_expired_claims(self, now: float | None = None) -> int:
        """Clear expired claims so another worker may proceed. Returns count cleared."""
        ts = now if now is not None else self._clock()
        conn = self._conn()
        conn.execute("BEGIN IMMEDIATE")
        try:
            expired = conn.execute(
                """SELECT id, claimed_by FROM incidents
                   WHERE claimed_by IS NOT NULL AND claim_expires_at <= ?""",
                (ts,),
            ).fetchall()

            if expired:
                ids = [r["id"] for r in expired]
                conn.execute(
                    f"UPDATE incidents SET claimed_by = NULL, claim_expires_at = NULL"
                    f" WHERE id IN ({','.join('?' * len(ids))})",
                    ids,
                )
                for r in expired:
                    self._new_event(
                        conn,
                        r["id"],
                        "claim_recovered",
                        ts,
                        {"worker_id": r["claimed_by"]},
                    )

            conn.execute("COMMIT")
            return len(expired)

        except Exception:
            conn.execute("ROLLBACK")
            raise
