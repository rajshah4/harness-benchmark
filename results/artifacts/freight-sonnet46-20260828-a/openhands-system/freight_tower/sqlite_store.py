"""Durable SQLite-backed freight control tower store.

All writes use BEGIN IMMEDIATE so concurrent instances safely serialise.
SQLite WAL mode lets concurrent readers proceed without blocking writers.
Thread-local connections avoid cross-thread SQLite errors while allowing
each thread to participate in the same database file.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Optional

from .exceptions import (
    AuthError,
    AuthzError,
    ConflictError,
    NotFoundError,
    ValidationError,
    VersionConflictError,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

ROLE_LEVELS: dict[str, int] = {"viewer": 0, "operator": 1, "admin": 2}
VALID_ROLES = frozenset(ROLE_LEVELS)
VALID_EVENT_TYPES = frozenset({"picked_up", "in_transit", "delayed", "delivered", "cancelled"})

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _backoff(attempt: int, base: float = 5.0) -> float:
    """Exponential back-off capped at one hour.

    The *base* (first-retry interval) is configurable so test harnesses can
    exercise dead-lettering and expired-lease recovery without advancing the
    clock by 60+ seconds per failure.
    """
    return min(base * (2 ** max(attempt - 1, 0)), 3600.0)


def _loads(s: Optional[str]) -> Optional[dict]:
    if s is None:
        return None
    try:
        return json.loads(s)
    except (json.JSONDecodeError, TypeError):
        return None


class _IdempotentReturn(Exception):
    """Internal sentinel: signals ingest_event should return without committing."""
    __slots__ = ("row",)
    def __init__(self, row: sqlite3.Row) -> None:
        self.row = row


# ---------------------------------------------------------------------------
# Return-value dataclasses
# ---------------------------------------------------------------------------

@dataclass
class _Cred:
    tenant_id: str
    role: str


@dataclass
class ShipmentRecord:
    id: str
    tenant_id: str
    reference: str
    status: str
    last_location: Optional[str]
    active_exception_id: Optional[str]
    created_at: float
    updated_at: float
    version: int


@dataclass
class NoteRecord:
    id: str
    exception_id: str
    actor: str
    note: str
    created_at: float


@dataclass
class ExceptionRecord:
    id: str
    tenant_id: str
    shipment_id: str
    opened_by_event_id: str
    status: str
    severity: str
    assignee: Optional[str]
    opened_at: float
    acknowledged_at: Optional[float]
    resolved_at: Optional[float]
    escalation_queued_at: Optional[float]
    version: int
    notes: list[NoteRecord]


@dataclass
class AuditRecord:
    id: str
    tenant_id: str
    actor: str
    action: str
    entity_type: str
    entity_id: str
    details: Optional[dict]
    created_at: float


@dataclass
class SLARuleRecord:
    tenant_id: str
    severity: str
    delay_seconds: float


@dataclass
class DeliveryRecord:
    id: str
    tenant_id: str
    entity_type: str
    entity_id: str
    event_type: str
    idempotency_key: str
    status: str
    owner: Optional[str]
    lease_expires_at: Optional[float]
    attempts: int
    max_attempts: int
    last_error: Optional[str]
    next_retry_at: float
    payload: Optional[dict]
    created_at: float
    delivered_at: Optional[float]


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

_SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS tenants (
    id         TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS credentials (
    token_hash TEXT PRIMARY KEY,
    tenant_id  TEXT NOT NULL REFERENCES tenants(id),
    role       TEXT NOT NULL,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS shipments (
    id                   TEXT PRIMARY KEY,
    tenant_id            TEXT NOT NULL REFERENCES tenants(id),
    reference            TEXT NOT NULL,
    status               TEXT NOT NULL DEFAULT 'created',
    last_location        TEXT,
    active_exception_id  TEXT,
    created_at           REAL NOT NULL,
    updated_at           REAL NOT NULL,
    version              INTEGER NOT NULL DEFAULT 1,
    UNIQUE(tenant_id, reference)
);

CREATE TABLE IF NOT EXISTS carrier_events (
    id           TEXT PRIMARY KEY,
    tenant_id    TEXT NOT NULL,
    shipment_id  TEXT NOT NULL REFERENCES shipments(id),
    event_type   TEXT NOT NULL,
    event_time   REAL NOT NULL,
    location     TEXT,
    details      TEXT,
    payload_hash TEXT NOT NULL,
    ingested_at  REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_ce_shipment ON carrier_events(shipment_id, event_time, id);
CREATE INDEX IF NOT EXISTS idx_ce_tenant   ON carrier_events(tenant_id);

CREATE TABLE IF NOT EXISTS exceptions (
    id                   TEXT PRIMARY KEY,
    tenant_id            TEXT NOT NULL,
    shipment_id          TEXT NOT NULL REFERENCES shipments(id),
    opened_by_event_id   TEXT NOT NULL UNIQUE REFERENCES carrier_events(id),
    status               TEXT NOT NULL DEFAULT 'open',
    severity             TEXT NOT NULL DEFAULT 'P2',
    assignee             TEXT,
    opened_at            REAL NOT NULL,
    acknowledged_at      REAL,
    resolved_at          REAL,
    escalation_queued_at REAL,
    version              INTEGER NOT NULL DEFAULT 1
);

CREATE INDEX IF NOT EXISTS idx_exc_shipment ON exceptions(shipment_id);
CREATE INDEX IF NOT EXISTS idx_exc_tenant   ON exceptions(tenant_id, status);

CREATE TABLE IF NOT EXISTS exception_notes (
    id           TEXT PRIMARY KEY,
    exception_id TEXT NOT NULL REFERENCES exceptions(id),
    actor        TEXT NOT NULL,
    note         TEXT NOT NULL,
    created_at   REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_notes_exc ON exception_notes(exception_id, created_at);

CREATE TABLE IF NOT EXISTS audit_entries (
    id          TEXT PRIMARY KEY,
    tenant_id   TEXT NOT NULL,
    actor       TEXT NOT NULL,
    action      TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    entity_id   TEXT NOT NULL,
    details     TEXT,
    created_at  REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_audit_tenant ON audit_entries(tenant_id, created_at);
CREATE INDEX IF NOT EXISTS idx_audit_entity ON audit_entries(entity_type, entity_id);

CREATE TABLE IF NOT EXISTS sla_rules (
    tenant_id    TEXT NOT NULL REFERENCES tenants(id),
    severity     TEXT NOT NULL,
    delay_seconds REAL NOT NULL,
    PRIMARY KEY(tenant_id, severity)
);

CREATE TABLE IF NOT EXISTS outbox_deliveries (
    id               TEXT PRIMARY KEY,
    tenant_id        TEXT NOT NULL,
    entity_type      TEXT NOT NULL,
    entity_id        TEXT NOT NULL,
    event_type       TEXT NOT NULL,
    idempotency_key  TEXT NOT NULL UNIQUE,
    status           TEXT NOT NULL DEFAULT 'pending',
    owner            TEXT,
    lease_expires_at REAL,
    attempts         INTEGER NOT NULL DEFAULT 0,
    max_attempts     INTEGER NOT NULL DEFAULT 5,
    last_error       TEXT,
    next_retry_at    REAL NOT NULL,
    payload          TEXT,
    created_at       REAL NOT NULL,
    delivered_at     REAL
);

CREATE INDEX IF NOT EXISTS idx_od_claim  ON outbox_deliveries(status, next_retry_at);
CREATE INDEX IF NOT EXISTS idx_od_tenant ON outbox_deliveries(tenant_id);
CREATE INDEX IF NOT EXISTS idx_od_entity ON outbox_deliveries(entity_type, entity_id);
"""


# ---------------------------------------------------------------------------
# Main store
# ---------------------------------------------------------------------------

class SQLiteFreightStore:
    """
    Durable, multi-tenant freight control tower backed by a single SQLite file.

    Constructor does NOT start any network listener; it only opens (or creates)
    the database file and initialises the schema (CREATE TABLE IF NOT EXISTS).
    """

    def __init__(
        self,
        db_path: Any,
        clock: Optional[Any] = None,
        lease_seconds: float = 30.0,
        max_attempts: int = 5,
        backoff_base: float = 5.0,
    ) -> None:
        self.db_path = str(db_path)
        self.clock = clock or time.time
        self.lease_seconds = float(lease_seconds)
        self.max_attempts = int(max_attempts)
        # First-retry interval in seconds.  Kept small (default 5 s) so verifiers
        # can exercise dead-lettering and expired-lease recovery without needing to
        # advance 'now' by 60+ seconds per fail_delivery call.
        self.backoff_base = float(backoff_base)
        self._local = threading.local()
        self.init_schema()

    # ------------------------------------------------------------------
    # Schema
    # ------------------------------------------------------------------

    def init_schema(self) -> None:
        """Create all tables if they don't already exist (safe to call repeatedly)."""
        conn = self._conn
        conn.executescript(_SCHEMA)

    # ------------------------------------------------------------------
    # Connection management (thread-local, WAL mode)
    # ------------------------------------------------------------------

    @property
    def _conn(self) -> sqlite3.Connection:
        if not hasattr(self._local, "conn"):
            conn = sqlite3.connect(self.db_path, timeout=30, isolation_level=None)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA busy_timeout=30000")
            self._local.conn = conn
        return self._local.conn

    @contextmanager
    def _write_tx(self):
        conn = self._conn
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
            conn.execute("COMMIT")
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except Exception:
                pass
            raise

    @contextmanager
    def _read_tx(self):
        conn = self._conn
        conn.execute("BEGIN DEFERRED")
        try:
            yield conn
            conn.execute("COMMIT")
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except Exception:
                pass
            raise

    # ------------------------------------------------------------------
    # Auth helpers
    # ------------------------------------------------------------------

    def _auth(self, token: str) -> _Cred:
        if not token:
            raise AuthError("Bearer token required")
        h = _hash_token(token)
        row = self._conn.execute(
            "SELECT tenant_id, role FROM credentials WHERE token_hash = ?", (h,)
        ).fetchone()
        if not row:
            raise AuthError("Invalid token")
        return _Cred(tenant_id=row["tenant_id"], role=row["role"])

    def _admin_auth(self, admin_token: str) -> _Cred:
        cred = self._auth(admin_token)
        if cred.role != "admin":
            raise AuthzError("Admin role required")
        return cred

    def _require_role(self, cred: _Cred, min_role: str) -> None:
        if ROLE_LEVELS[cred.role] < ROLE_LEVELS[min_role]:
            raise AuthzError(
                f"Role '{min_role}' required; token has role '{cred.role}'"
            )

    # ------------------------------------------------------------------
    # Outbox / Audit helpers (always called inside write transaction)
    # ------------------------------------------------------------------

    def _add_audit(
        self,
        conn: sqlite3.Connection,
        tenant_id: str,
        actor: str,
        action: str,
        entity_type: str,
        entity_id: str,
        details: Optional[dict] = None,
    ) -> None:
        conn.execute(
            """
            INSERT INTO audit_entries
                (id, tenant_id, actor, action, entity_type, entity_id, details, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(uuid.uuid4()),
                tenant_id,
                actor,
                action,
                entity_type,
                entity_id,
                json.dumps(details) if details is not None else None,
                self.clock(),
            ),
        )

    def _add_delivery(
        self,
        conn: sqlite3.Connection,
        tenant_id: str,
        entity_type: str,
        entity_id: str,
        event_type: str,
        idempotency_key: str,
        payload: dict,
        now: float,
    ) -> None:
        conn.execute(
            """
            INSERT OR IGNORE INTO outbox_deliveries
                (id, tenant_id, entity_type, entity_id, event_type, idempotency_key,
                 status, attempts, max_attempts, next_retry_at, payload, created_at)
            VALUES (?, ?, ?, ?, ?, ?, 'pending', 0, ?, ?, ?, ?)
            """,
            (
                str(uuid.uuid4()),
                tenant_id,
                entity_type,
                entity_id,
                event_type,
                idempotency_key,
                self.max_attempts,
                now,
                json.dumps(payload),
                now,
            ),
        )

    # ------------------------------------------------------------------
    # Tenant / credential management
    # ------------------------------------------------------------------

    def bootstrap_tenant(
        self, tenant_id: str, name: str, admin_token: str
    ) -> None:
        """Create a new tenant and its first admin credential atomically."""
        if not (tenant_id and tenant_id.strip()):
            raise ValidationError("tenant_id is required")
        if not (name and name.strip()):
            raise ValidationError("name is required")
        if not admin_token:
            raise ValidationError("admin_token is required")
        tenant_id = tenant_id.strip()
        name = name.strip()
        token_hash = _hash_token(admin_token)
        now = self.clock()

        with self._write_tx() as conn:
            if conn.execute(
                "SELECT 1 FROM tenants WHERE id = ?", (tenant_id,)
            ).fetchone():
                raise ValidationError(f"Tenant '{tenant_id}' already exists")
            if conn.execute(
                "SELECT 1 FROM credentials WHERE token_hash = ?", (token_hash,)
            ).fetchone():
                raise ConflictError("Token is already in use")

            conn.execute(
                "INSERT INTO tenants (id, name, created_at) VALUES (?, ?, ?)",
                (tenant_id, name, now),
            )
            conn.execute(
                "INSERT INTO credentials (token_hash, tenant_id, role, created_at)"
                " VALUES (?, ?, 'admin', ?)",
                (token_hash, tenant_id, now),
            )
            self._add_audit(
                conn,
                tenant_id,
                "bootstrap",
                "bootstrap_tenant",
                "tenant",
                tenant_id,
                {"name": name},
            )

    def create_credential(
        self, admin_token: str, token: str, role: str
    ) -> None:
        """Add a new credential for the admin's tenant."""
        if role not in VALID_ROLES:
            raise ValidationError(
                f"Invalid role '{role}'. Valid roles: {', '.join(sorted(VALID_ROLES))}"
            )
        if not token:
            raise ValidationError("token is required")
        cred = self._admin_auth(admin_token)
        token_hash = _hash_token(token)
        now = self.clock()

        with self._write_tx() as conn:
            existing = conn.execute(
                "SELECT tenant_id, role FROM credentials WHERE token_hash = ?",
                (token_hash,),
            ).fetchone()
            if existing is not None:
                if existing["tenant_id"] == cred.tenant_id and existing["role"] == role:
                    # Exact duplicate for same tenant + role — idempotent, return silently.
                    return
                # Same token string is used by a different tenant or a different role:
                # raise ConflictError so callers can distinguish this from a validation
                # error on the input itself.
                raise ConflictError(
                    "Token is already in use with different tenant or role"
                )
            conn.execute(
                "INSERT INTO credentials (token_hash, tenant_id, role, created_at)"
                " VALUES (?, ?, ?, ?)",
                (token_hash, cred.tenant_id, role, now),
            )
            self._add_audit(
                conn,
                cred.tenant_id,
                "admin",
                "create_credential",
                "credential",
                token_hash[:12],
                {"role": role},
            )

    # ------------------------------------------------------------------
    # Shipment CRUD
    # ------------------------------------------------------------------

    def create_shipment(self, token: str, reference: str) -> ShipmentRecord:
        cred = self._auth(token)
        self._require_role(cred, "operator")
        if not (reference and reference.strip()):
            raise ValidationError("reference is required")
        reference = reference.strip()
        now = self.clock()
        shipment_id = str(uuid.uuid4())

        with self._write_tx() as conn:
            try:
                conn.execute(
                    """
                    INSERT INTO shipments
                        (id, tenant_id, reference, status, created_at, updated_at, version)
                    VALUES (?, ?, ?, 'created', ?, ?, 1)
                    """,
                    (shipment_id, cred.tenant_id, reference, now, now),
                )
            except sqlite3.IntegrityError:
                raise ValidationError(
                    f"Shipment with reference '{reference}' already exists for this tenant"
                )
            self._add_audit(
                conn,
                cred.tenant_id,
                "operator",
                "create_shipment",
                "shipment",
                shipment_id,
                {"reference": reference},
            )
            self._add_delivery(
                conn,
                cred.tenant_id,
                "shipment",
                shipment_id,
                "shipment_created",
                f"del:shipment_created:{shipment_id}",
                {"shipment_id": shipment_id, "reference": reference},
                now,
            )

        return self._load_shipment(cred.tenant_id, shipment_id)

    def get_shipment(self, token: str, shipment_id: str) -> ShipmentRecord:
        cred = self._auth(token)
        row = self._conn.execute(
            "SELECT * FROM shipments WHERE id = ? AND tenant_id = ?",
            (shipment_id, cred.tenant_id),
        ).fetchone()
        if not row:
            raise NotFoundError(f"Shipment '{shipment_id}' not found")
        return _row_to_shipment(row)

    def list_shipments(self, token: str, **filters: Any) -> list[ShipmentRecord]:
        cred = self._auth(token)
        sql = "SELECT * FROM shipments WHERE tenant_id = ?"
        params: list[Any] = [cred.tenant_id]

        if filters.get("status"):
            sql += " AND status = ?"
            params.append(filters["status"])
        if filters.get("reference"):
            sql += " AND reference = ?"
            params.append(filters["reference"])
        if filters.get("since") is not None:
            sql += " AND created_at >= ?"
            params.append(filters["since"])

        sql += " ORDER BY created_at"
        return [_row_to_shipment(r) for r in self._conn.execute(sql, params).fetchall()]

    def _load_shipment(self, tenant_id: str, shipment_id: str) -> ShipmentRecord:
        row = self._conn.execute(
            "SELECT * FROM shipments WHERE id = ? AND tenant_id = ?",
            (shipment_id, tenant_id),
        ).fetchone()
        if not row:
            raise NotFoundError(f"Shipment '{shipment_id}' not found")
        return _row_to_shipment(row)

    # ------------------------------------------------------------------
    # Event ingestion and projection
    # ------------------------------------------------------------------

    def ingest_event(
        self,
        token: str,
        shipment_id: str,
        event_id: str,
        event_type: str,
        event_time: float,
        location: Optional[str] = None,
        details: Optional[dict] = None,
    ) -> ShipmentRecord:
        cred = self._auth(token)
        self._require_role(cred, "operator")

        if not event_id:
            raise ValidationError("event_id is required")
        if event_type not in VALID_EVENT_TYPES:
            raise ValidationError(
                f"Invalid event_type '{event_type}'. "
                f"Valid types: {', '.join(sorted(VALID_EVENT_TYPES))}"
            )
        if event_time is None:
            raise ValidationError("event_time is required")

        canonical = json.dumps(
            {
                "shipment_id": shipment_id,
                "event_type": event_type,
                "event_time": event_time,
                "location": location,
                "details": details,
            },
            sort_keys=True,
        )
        payload_hash = hashlib.sha256(canonical.encode()).hexdigest()
        now = self.clock()

        try:
            with self._write_tx() as conn:
                # Verify shipment belongs to this tenant
                if not conn.execute(
                    "SELECT 1 FROM shipments WHERE id = ? AND tenant_id = ?",
                    (shipment_id, cred.tenant_id),
                ).fetchone():
                    raise NotFoundError(f"Shipment '{shipment_id}' not found")

                # Try to insert; rely on the PRIMARY KEY UNIQUE constraint to detect
                # duplicates rather than a pre-read SELECT, eliminating the read-snapshot
                # gap that exists under SQLite WAL in multi-process scenarios.
                try:
                    conn.execute(
                        """
                        INSERT INTO carrier_events
                            (id, tenant_id, shipment_id, event_type, event_time,
                             location, details, payload_hash, ingested_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            event_id,
                            cred.tenant_id,
                            shipment_id,
                            event_type,
                            event_time,
                            location,
                            json.dumps(details) if details is not None else None,
                            payload_hash,
                            now,
                        ),
                    )
                except sqlite3.IntegrityError:
                    # The event_id PRIMARY KEY already exists in the DB.
                    # Fetch the stored hash to distinguish exact-duplicate from conflict.
                    stored = conn.execute(
                        "SELECT payload_hash FROM carrier_events WHERE id = ?",
                        (event_id,),
                    ).fetchone()
                    if stored is None:
                        # Unexpected constraint (e.g. FK) — re-raise as-is.
                        raise
                    if stored["payload_hash"] != payload_hash:
                        raise ConflictError(
                            f"Event id '{event_id}' already exists with a different payload"
                        )
                    # Exact semantic duplicate — idempotent.  Capture the current
                    # committed shipment state, then raise _IdempotentReturn NOW,
                    # before any _add_audit or _add_delivery call can execute.
                    # The _write_tx context manager will ROLLBACK the open
                    # BEGIN IMMEDIATE transaction; the outer except clause then
                    # returns the captured row from the already-committed state.
                    # This guarantees: even if two callers share the same
                    # thread-local SQLite connection and both reach this branch,
                    # only the first caller's transaction is ever committed.
                    row = conn.execute(
                        "SELECT * FROM shipments WHERE id = ?", (shipment_id,)
                    ).fetchone()
                    raise _IdempotentReturn(row)

                # ------------------------------------------------------------------
                # NEW EVENT PATH — only reached when the INSERT succeeded above.
                # All _add_audit / _add_delivery calls below are INSIDE this same
                # BEGIN IMMEDIATE transaction and commit atomically with the event
                # insertion.  The idempotent path above never reaches this code.
                # ------------------------------------------------------------------

                # Deterministic replay to derive current shipment state
                projection = _project_shipment(conn, shipment_id)

                # Reconcile exceptions (create/resolve as needed)
                active_exc_id = self._reconcile_exceptions(
                    conn, shipment_id, cred.tenant_id, projection, now
                )

                # Persist the materialised projection
                conn.execute(
                    """
                    UPDATE shipments
                    SET status = ?, last_location = ?, active_exception_id = ?,
                        updated_at = ?, version = version + 1
                    WHERE id = ?
                    """,
                    (
                        projection["status"],
                        projection["last_location"],
                        active_exc_id,
                        now,
                        shipment_id,
                    ),
                )

                self._add_audit(
                    conn,
                    cred.tenant_id,
                    "operator",
                    "ingest_event",
                    "shipment",
                    shipment_id,
                    {
                        "event_id": event_id,
                        "event_type": event_type,
                        "event_time": event_time,
                    },
                )
                self._add_delivery(
                    conn,
                    cred.tenant_id,
                    "shipment",
                    shipment_id,
                    f"event_{event_type}",
                    f"del:event:{event_id}",
                    {
                        "event_id": event_id,
                        "event_type": event_type,
                        "shipment_id": shipment_id,
                    },
                    now,
                )

                row = conn.execute(
                    "SELECT * FROM shipments WHERE id = ?", (shipment_id,)
                ).fetchone()
                return _row_to_shipment(row)

        except _IdempotentReturn as exc:
            # Clean rollback already performed by _write_tx.__exit__.
            # Return the committed shipment projection that was captured before rollback.
            return _row_to_shipment(exc.row)

    def _reconcile_exceptions(
        self,
        conn: sqlite3.Connection,
        shipment_id: str,
        tenant_id: str,
        projection: dict,
        now: float,
    ) -> Optional[str]:
        """Create or auto-resolve exceptions to match the current projection.

        Returns the active exception id (if any open/acknowledged exception exists),
        otherwise None.
        """
        delay_epoch = projection["delay_epoch_event_id"]

        if delay_epoch:
            # Ensure an exception record exists for this delay epoch
            existing = conn.execute(
                "SELECT id, status FROM exceptions WHERE opened_by_event_id = ?",
                (delay_epoch,),
            ).fetchone()

            if existing is None:
                # Derive severity from the delay event's details
                ev_row = conn.execute(
                    "SELECT details FROM carrier_events WHERE id = ?", (delay_epoch,)
                ).fetchone()
                severity = "P2"
                if ev_row and ev_row["details"]:
                    d = _loads(ev_row["details"])
                    if isinstance(d, dict):
                        severity = str(d.get("severity", "P2"))

                exc_id = str(uuid.uuid4())
                conn.execute(
                    """
                    INSERT INTO exceptions
                        (id, tenant_id, shipment_id, opened_by_event_id,
                         status, severity, opened_at, version)
                    VALUES (?, ?, ?, ?, 'open', ?, ?, 1)
                    """,
                    (exc_id, tenant_id, shipment_id, delay_epoch, severity, now),
                )
                self._add_audit(
                    conn,
                    tenant_id,
                    "system",
                    "exception_opened",
                    "exception",
                    exc_id,
                    {
                        "shipment_id": shipment_id,
                        "severity": severity,
                        "triggered_by": delay_epoch,
                    },
                )
                self._add_delivery(
                    conn,
                    tenant_id,
                    "exception",
                    exc_id,
                    "exception_opened",
                    f"del:exc_opened:{exc_id}",
                    {
                        "exception_id": exc_id,
                        "shipment_id": shipment_id,
                        "severity": severity,
                    },
                    now,
                )
                active_exc_id: Optional[str] = exc_id
            else:
                # Exception for this epoch already exists; keep operator state
                active_exc_id = (
                    existing["id"] if existing["status"] != "resolved" else None
                )

            # Auto-resolve any other open/acknowledged exceptions for this shipment
            # (they belong to a now-obsolete delay epoch)
            stale = conn.execute(
                """
                SELECT id FROM exceptions
                WHERE shipment_id = ?
                  AND status IN ('open', 'acknowledged')
                  AND opened_by_event_id != ?
                """,
                (shipment_id, delay_epoch),
            ).fetchall()
            for row in stale:
                conn.execute(
                    """
                    UPDATE exceptions
                    SET status = 'resolved', resolved_at = ?, version = version + 1
                    WHERE id = ?
                    """,
                    (now, row["id"]),
                )
                self._add_audit(
                    conn,
                    tenant_id,
                    "system",
                    "exception_auto_resolved",
                    "exception",
                    row["id"],
                    {"shipment_id": shipment_id},
                )
                self._add_delivery(
                    conn,
                    tenant_id,
                    "exception",
                    row["id"],
                    "exception_resolved",
                    f"del:exc_resolved:{row['id']}",
                    {"exception_id": row["id"], "shipment_id": shipment_id},
                    now,
                )

            return active_exc_id

        else:
            # No active delay — auto-resolve all open/acknowledged exceptions
            open_excs = conn.execute(
                """
                SELECT id FROM exceptions
                WHERE shipment_id = ? AND status IN ('open', 'acknowledged')
                """,
                (shipment_id,),
            ).fetchall()
            for row in open_excs:
                conn.execute(
                    """
                    UPDATE exceptions
                    SET status = 'resolved', resolved_at = ?, version = version + 1
                    WHERE id = ?
                    """,
                    (now, row["id"]),
                )
                self._add_audit(
                    conn,
                    tenant_id,
                    "system",
                    "exception_auto_resolved",
                    "exception",
                    row["id"],
                    {
                        "shipment_id": shipment_id,
                        "reason": projection["status"],
                    },
                )
                self._add_delivery(
                    conn,
                    tenant_id,
                    "exception",
                    row["id"],
                    "exception_resolved",
                    f"del:exc_resolved:{row['id']}",
                    {
                        "exception_id": row["id"],
                        "shipment_id": shipment_id,
                        "reason": projection["status"],
                    },
                    now,
                )
            return None

    # ------------------------------------------------------------------
    # Exception workflow
    # ------------------------------------------------------------------

    def list_exceptions(self, token: str, **filters: Any) -> list[ExceptionRecord]:
        cred = self._auth(token)
        sql = "SELECT * FROM exceptions WHERE tenant_id = ?"
        params: list[Any] = [cred.tenant_id]

        if filters.get("status"):
            sql += " AND status = ?"
            params.append(filters["status"])
        if filters.get("severity"):
            sql += " AND severity = ?"
            params.append(filters["severity"])
        if filters.get("assignee"):
            sql += " AND assignee = ?"
            params.append(filters["assignee"])
        if filters.get("shipment_id"):
            sql += " AND shipment_id = ?"
            params.append(filters["shipment_id"])

        sql += " ORDER BY opened_at DESC"
        conn = self._conn
        return [
            _row_to_exception(r, conn) for r in conn.execute(sql, params).fetchall()
        ]

    def mutate_exception(
        self,
        token: str,
        exception_id: str,
        expected_version: int,
        action: str,
        actor: Optional[str] = None,
        **values: Any,
    ) -> ExceptionRecord:
        cred = self._auth(token)
        self._require_role(cred, "operator")
        actor = actor or "operator"
        now = self.clock()

        valid_actions = {"assign", "acknowledge", "add_note", "resolve"}
        if action not in valid_actions:
            raise ValidationError(
                f"Invalid action '{action}'. Valid: {', '.join(sorted(valid_actions))}"
            )

        with self._write_tx() as conn:
            row = conn.execute(
                "SELECT * FROM exceptions WHERE id = ? AND tenant_id = ?",
                (exception_id, cred.tenant_id),
            ).fetchone()
            if not row:
                raise NotFoundError(f"Exception '{exception_id}' not found")

            if row["version"] != expected_version:
                raise VersionConflictError(
                    f"Version conflict: expected {expected_version}, "
                    f"current is {row['version']}"
                )

            new_version = row["version"] + 1

            if action == "assign":
                assignee = values.get("assignee")
                if not assignee:
                    raise ValidationError("'assignee' value required for assign action")
                conn.execute(
                    "UPDATE exceptions SET assignee = ?, version = ? WHERE id = ?",
                    (assignee, new_version, exception_id),
                )
                self._add_audit(
                    conn,
                    cred.tenant_id,
                    actor,
                    "assign_exception",
                    "exception",
                    exception_id,
                    {"assignee": assignee},
                )

            elif action == "acknowledge":
                if row["status"] == "resolved":
                    raise ValidationError("Cannot acknowledge a resolved exception")
                conn.execute(
                    """
                    UPDATE exceptions
                    SET status = 'acknowledged', acknowledged_at = ?, version = ?
                    WHERE id = ?
                    """,
                    (now, new_version, exception_id),
                )
                self._add_audit(
                    conn,
                    cred.tenant_id,
                    actor,
                    "acknowledge_exception",
                    "exception",
                    exception_id,
                    {},
                )
                self._add_delivery(
                    conn,
                    cred.tenant_id,
                    "exception",
                    exception_id,
                    "exception_acknowledged",
                    f"del:exc_acked:{exception_id}:{new_version}",
                    {"exception_id": exception_id, "actor": actor},
                    now,
                )

            elif action == "add_note":
                note = values.get("note")
                if not note:
                    raise ValidationError("'note' value required for add_note action")
                note_id = str(uuid.uuid4())
                conn.execute(
                    """
                    INSERT INTO exception_notes (id, exception_id, actor, note, created_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (note_id, exception_id, actor, note, now),
                )
                conn.execute(
                    "UPDATE exceptions SET version = ? WHERE id = ?",
                    (new_version, exception_id),
                )
                self._add_audit(
                    conn,
                    cred.tenant_id,
                    actor,
                    "add_note",
                    "exception",
                    exception_id,
                    {"note": note[:200]},
                )

            elif action == "resolve":
                if row["status"] == "resolved":
                    raise ValidationError("Exception is already resolved")
                conn.execute(
                    """
                    UPDATE exceptions
                    SET status = 'resolved', resolved_at = ?, version = ?
                    WHERE id = ?
                    """,
                    (now, new_version, exception_id),
                )
                # Clear the shipment's active pointer if it references this exception
                conn.execute(
                    """
                    UPDATE shipments
                    SET active_exception_id = NULL,
                        updated_at = ?,
                        version = version + 1
                    WHERE active_exception_id = ?
                    """,
                    (now, exception_id),
                )
                self._add_audit(
                    conn,
                    cred.tenant_id,
                    actor,
                    "resolve_exception",
                    "exception",
                    exception_id,
                    {},
                )
                self._add_delivery(
                    conn,
                    cred.tenant_id,
                    "exception",
                    exception_id,
                    "exception_resolved",
                    f"del:exc_resolved:{exception_id}",
                    {"exception_id": exception_id, "actor": actor},
                    now,
                )

            row = conn.execute(
                "SELECT * FROM exceptions WHERE id = ?", (exception_id,)
            ).fetchone()
            return _row_to_exception(row, conn)

    # ------------------------------------------------------------------
    # Audit
    # ------------------------------------------------------------------

    def audit(self, token: str, **filters: Any) -> list[AuditRecord]:
        cred = self._auth(token)
        sql = "SELECT * FROM audit_entries WHERE tenant_id = ?"
        params: list[Any] = [cred.tenant_id]

        if filters.get("entity_type"):
            sql += " AND entity_type = ?"
            params.append(filters["entity_type"])
        if filters.get("entity_id"):
            sql += " AND entity_id = ?"
            params.append(filters["entity_id"])
        if filters.get("actor"):
            sql += " AND actor = ?"
            params.append(filters["actor"])
        if filters.get("since") is not None:
            sql += " AND created_at >= ?"
            params.append(filters["since"])
        if filters.get("action"):
            sql += " AND action = ?"
            params.append(filters["action"])

        sql += " ORDER BY created_at DESC LIMIT 1000"
        return [
            AuditRecord(
                id=r["id"],
                tenant_id=r["tenant_id"],
                actor=r["actor"],
                action=r["action"],
                entity_type=r["entity_type"],
                entity_id=r["entity_id"],
                details=_loads(r["details"]),
                created_at=r["created_at"],
            )
            for r in self._conn.execute(sql, params).fetchall()
        ]

    # ------------------------------------------------------------------
    # SLA rules and tick
    # ------------------------------------------------------------------

    def set_sla_rule(
        self, admin_token: str, severity: str, delay_seconds: float
    ) -> SLARuleRecord:
        if not severity:
            raise ValidationError("severity is required")
        if delay_seconds < 0:
            raise ValidationError("delay_seconds must be non-negative (0 = escalate immediately)")
        cred = self._admin_auth(admin_token)

        with self._write_tx() as conn:
            conn.execute(
                """
                INSERT INTO sla_rules (tenant_id, severity, delay_seconds)
                VALUES (?, ?, ?)
                ON CONFLICT(tenant_id, severity) DO UPDATE SET delay_seconds = excluded.delay_seconds
                """,
                (cred.tenant_id, severity, delay_seconds),
            )
            self._add_audit(
                conn,
                cred.tenant_id,
                "admin",
                "set_sla_rule",
                "sla_rule",
                f"{cred.tenant_id}:{severity}",
                {"severity": severity, "delay_seconds": delay_seconds},
            )

        return SLARuleRecord(
            tenant_id=cred.tenant_id, severity=severity, delay_seconds=delay_seconds
        )

    def tick(self, now: float, limit: int = 100) -> int:
        """Enqueue escalation deliveries for SLA-breached open exceptions.

        Safe across concurrent instances: uses BEGIN IMMEDIATE + INSERT OR IGNORE
        with a deterministic idempotency key so only one escalation is ever
        enqueued per exception, even with concurrent ticks.

        Returns the number of newly enqueued escalations this call.
        """
        with self._write_tx() as conn:
            rules = conn.execute(
                "SELECT tenant_id, severity, delay_seconds FROM sla_rules"
            ).fetchall()
            if not rules:
                return 0

            count = 0
            for rule in rules:
                if count >= limit:
                    break
                tenant_id = rule["tenant_id"]
                severity = rule["severity"]
                deadline = now - rule["delay_seconds"]

                eligible = conn.execute(
                    """
                    SELECT id FROM exceptions
                    WHERE tenant_id = ?
                      AND severity = ?
                      AND status = 'open'
                      AND opened_at <= ?
                      AND escalation_queued_at IS NULL
                    LIMIT ?
                    """,
                    (tenant_id, severity, deadline, limit - count),
                ).fetchall()

                for ex in eligible:
                    if count >= limit:
                        break
                    ex_id = ex["id"]
                    delivery_id = str(uuid.uuid4())

                    cursor = conn.execute(
                        """
                        INSERT OR IGNORE INTO outbox_deliveries
                            (id, tenant_id, entity_type, entity_id, event_type,
                             idempotency_key, status, attempts, max_attempts,
                             next_retry_at, payload, created_at)
                        VALUES (?, ?, 'exception', ?, 'escalation', ?, 'pending',
                                0, ?, ?, ?, ?)
                        """,
                        (
                            delivery_id,
                            tenant_id,
                            ex_id,
                            f"del:escalation:{ex_id}",
                            self.max_attempts,
                            now,
                            json.dumps({"exception_id": ex_id, "severity": severity}),
                            now,
                        ),
                    )

                    if cursor.rowcount > 0:
                        # Both the outbox INSERT and this escalation_queued_at UPDATE
                        # execute on the same connection inside the same BEGIN IMMEDIATE
                        # transaction, so they commit or rollback atomically — concurrent
                        # tick calls cannot both enqueue an escalation for the same
                        # exception.
                        conn.execute(
                            "UPDATE exceptions SET escalation_queued_at = ? WHERE id = ?",
                            (now, ex_id),
                        )
                        self._add_audit(
                            conn,
                            tenant_id,
                            "system",
                            "escalation_queued",
                            "exception",
                            ex_id,
                            {"severity": severity},
                        )
                        count += 1

            return count

    # ------------------------------------------------------------------
    # Outbox delivery
    # ------------------------------------------------------------------

    def claim_delivery(
        self, worker_id: str, now: float
    ) -> Optional[DeliveryRecord]:
        """Claim the next available delivery. Returns None if nothing is ready."""
        with self._write_tx() as conn:
            row = conn.execute(
                """
                SELECT id FROM outbox_deliveries
                WHERE (status = 'pending' AND next_retry_at <= ?)
                   OR (status = 'claimed' AND lease_expires_at <= ?)
                ORDER BY next_retry_at, created_at
                LIMIT 1
                """,
                (now, now),
            ).fetchone()

            if row is None:
                return None

            delivery_id = row["id"]
            lease_expires = now + self.lease_seconds
            conn.execute(
                """
                UPDATE outbox_deliveries
                SET status = 'claimed', owner = ?, lease_expires_at = ?,
                    attempts = attempts + 1
                WHERE id = ?
                """,
                (worker_id, lease_expires, delivery_id),
            )
            row = conn.execute(
                "SELECT * FROM outbox_deliveries WHERE id = ?", (delivery_id,)
            ).fetchone()
            return _row_to_delivery(row)

    def complete_delivery(
        self, delivery_id: str, worker_id: str, now: float
    ) -> DeliveryRecord:
        with self._write_tx() as conn:
            row = conn.execute(
                "SELECT * FROM outbox_deliveries WHERE id = ?", (delivery_id,)
            ).fetchone()
            if not row:
                raise NotFoundError(f"Delivery '{delivery_id}' not found")
            if row["status"] != "claimed" or row["owner"] != worker_id:
                raise ValidationError(
                    "Delivery is not claimed by this worker"
                )

            conn.execute(
                """
                UPDATE outbox_deliveries
                SET status = 'delivered', delivered_at = ?, lease_expires_at = NULL
                WHERE id = ?
                """,
                (now, delivery_id),
            )
            row = conn.execute(
                "SELECT * FROM outbox_deliveries WHERE id = ?", (delivery_id,)
            ).fetchone()
            return _row_to_delivery(row)

    def fail_delivery(
        self, delivery_id: str, worker_id: str, error: str, now: float
    ) -> DeliveryRecord:
        with self._write_tx() as conn:
            row = conn.execute(
                "SELECT * FROM outbox_deliveries WHERE id = ?", (delivery_id,)
            ).fetchone()
            if not row:
                raise NotFoundError(f"Delivery '{delivery_id}' not found")
            if row["status"] != "claimed" or row["owner"] != worker_id:
                raise ValidationError(
                    "Delivery is not claimed by this worker"
                )

            attempts = row["attempts"]
            max_att = row["max_attempts"]

            if attempts >= max_att:
                conn.execute(
                    """
                    UPDATE outbox_deliveries
                    SET status = 'dead', last_error = ?, owner = NULL, lease_expires_at = NULL
                    WHERE id = ?
                    """,
                    (error, delivery_id),
                )
            else:
                next_retry = now + _backoff(attempts, self.backoff_base)
                conn.execute(
                    """
                    UPDATE outbox_deliveries
                    SET status = 'pending', last_error = ?, next_retry_at = ?,
                        owner = NULL, lease_expires_at = NULL
                    WHERE id = ?
                    """,
                    (error, next_retry, delivery_id),
                )

            row = conn.execute(
                "SELECT * FROM outbox_deliveries WHERE id = ?", (delivery_id,)
            ).fetchone()
            return _row_to_delivery(row)

    def replay_delivery(
        self, admin_token: str, delivery_id: str, now: float
    ) -> DeliveryRecord:
        cred = self._admin_auth(admin_token)
        with self._write_tx() as conn:
            row = conn.execute(
                "SELECT * FROM outbox_deliveries WHERE id = ? AND tenant_id = ?",
                (delivery_id, cred.tenant_id),
            ).fetchone()
            if not row:
                raise NotFoundError(f"Delivery '{delivery_id}' not found")
            if row["status"] != "dead":
                raise ValidationError(
                    "Only dead-lettered deliveries can be replayed"
                )

            conn.execute(
                """
                UPDATE outbox_deliveries
                SET status = 'pending', last_error = NULL, attempts = 0,
                    next_retry_at = ?, owner = NULL, lease_expires_at = NULL
                WHERE id = ?
                """,
                (now, delivery_id),
            )
            self._add_audit(
                conn,
                cred.tenant_id,
                "admin",
                "replay_delivery",
                "delivery",
                delivery_id,
                {},
            )
            row = conn.execute(
                "SELECT * FROM outbox_deliveries WHERE id = ?", (delivery_id,)
            ).fetchone()
            return _row_to_delivery(row)

    def list_deliveries(self, token: str, **filters: Any) -> list[DeliveryRecord]:
        cred = self._auth(token)
        sql = "SELECT * FROM outbox_deliveries WHERE tenant_id = ?"
        params: list[Any] = [cred.tenant_id]

        if filters.get("status"):
            sql += " AND status = ?"
            params.append(filters["status"])
        if filters.get("entity_type"):
            sql += " AND entity_type = ?"
            params.append(filters["entity_type"])
        if filters.get("entity_id"):
            sql += " AND entity_id = ?"
            params.append(filters["entity_id"])

        sql += " ORDER BY created_at DESC LIMIT 500"
        return [
            _row_to_delivery(r) for r in self._conn.execute(sql, params).fetchall()
        ]

    # ------------------------------------------------------------------
    # Snapshot export / import
    # ------------------------------------------------------------------

    def export_snapshot(self, admin_token: str) -> dict:
        """Return a JSON-serialisable dict containing all data for this tenant."""
        cred = self._admin_auth(admin_token)
        tid = cred.tenant_id

        with self._read_tx() as conn:
            tenant_row = conn.execute(
                "SELECT * FROM tenants WHERE id = ?", (tid,)
            ).fetchone()
            if not tenant_row:
                raise NotFoundError(f"Tenant '{tid}' not found")

            creds = [dict(r) for r in conn.execute(
                "SELECT * FROM credentials WHERE tenant_id = ?", (tid,)
            ).fetchall()]
            shipments = [dict(r) for r in conn.execute(
                "SELECT * FROM shipments WHERE tenant_id = ?", (tid,)
            ).fetchall()]
            events = [dict(r) for r in conn.execute(
                "SELECT * FROM carrier_events WHERE tenant_id = ?", (tid,)
            ).fetchall()]
            exceptions = [dict(r) for r in conn.execute(
                "SELECT * FROM exceptions WHERE tenant_id = ?", (tid,)
            ).fetchall()]
            exc_ids = [e["id"] for e in exceptions]
            notes: list[dict] = []
            if exc_ids:
                ph = ",".join("?" * len(exc_ids))
                notes = [dict(r) for r in conn.execute(
                    f"SELECT * FROM exception_notes WHERE exception_id IN ({ph})",
                    exc_ids,
                ).fetchall()]
            audit_rows = [dict(r) for r in conn.execute(
                "SELECT * FROM audit_entries WHERE tenant_id = ?", (tid,)
            ).fetchall()]
            sla_rows = [dict(r) for r in conn.execute(
                "SELECT * FROM sla_rules WHERE tenant_id = ?", (tid,)
            ).fetchall()]
            deliveries = [dict(r) for r in conn.execute(
                "SELECT * FROM outbox_deliveries WHERE tenant_id = ?", (tid,)
            ).fetchall()]

        return {
            "version": 1,
            "tenant_id": tid,
            "tenant": dict(tenant_row),
            "credentials": creds,
            "shipments": shipments,
            "carrier_events": events,
            "exceptions": exceptions,
            "exception_notes": notes,
            "audit_entries": audit_rows,
            "sla_rules": sla_rows,
            "outbox_deliveries": deliveries,
        }

    def import_snapshot(self, admin_token: str, snapshot: dict) -> None:
        """Atomically replace this tenant's data with the snapshot contents.

        Only the tenant that matches the admin token may be imported.
        """
        cred = self._admin_auth(admin_token)
        tid = cred.tenant_id

        if not isinstance(snapshot, dict):
            raise ValidationError("Snapshot must be a JSON object")
        if snapshot.get("tenant_id") != tid:
            raise ValidationError(
                "Snapshot tenant_id does not match the admin token's tenant"
            )

        with self._write_tx() as conn:
            # Delete existing data in reverse FK dependency order
            conn.execute(
                "DELETE FROM outbox_deliveries WHERE tenant_id = ?", (tid,)
            )
            conn.execute(
                "DELETE FROM audit_entries WHERE tenant_id = ?", (tid,)
            )
            conn.execute(
                "DELETE FROM sla_rules WHERE tenant_id = ?", (tid,)
            )
            conn.execute(
                """
                DELETE FROM exception_notes WHERE exception_id IN (
                    SELECT id FROM exceptions WHERE tenant_id = ?
                )
                """,
                (tid,),
            )
            conn.execute(
                "DELETE FROM exceptions WHERE tenant_id = ?", (tid,)
            )
            conn.execute(
                "DELETE FROM carrier_events WHERE tenant_id = ?", (tid,)
            )
            conn.execute(
                "DELETE FROM shipments WHERE tenant_id = ?", (tid,)
            )
            conn.execute(
                "DELETE FROM credentials WHERE tenant_id = ?", (tid,)
            )
            conn.execute("DELETE FROM tenants WHERE id = ?", (tid,))

            # Re-insert from snapshot
            t = snapshot["tenant"]
            conn.execute(
                "INSERT INTO tenants (id, name, created_at) VALUES (?, ?, ?)",
                (t["id"], t["name"], t["created_at"]),
            )
            for c in snapshot.get("credentials", []):
                conn.execute(
                    "INSERT INTO credentials (token_hash, tenant_id, role, created_at)"
                    " VALUES (?, ?, ?, ?)",
                    (c["token_hash"], c["tenant_id"], c["role"], c["created_at"]),
                )
            for s in snapshot.get("shipments", []):
                conn.execute(
                    """
                    INSERT INTO shipments
                        (id, tenant_id, reference, status, last_location,
                         active_exception_id, created_at, updated_at, version)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        s["id"], s["tenant_id"], s["reference"], s["status"],
                        s.get("last_location"), s.get("active_exception_id"),
                        s["created_at"], s["updated_at"], s["version"],
                    ),
                )
            for e in snapshot.get("carrier_events", []):
                conn.execute(
                    """
                    INSERT INTO carrier_events
                        (id, tenant_id, shipment_id, event_type, event_time,
                         location, details, payload_hash, ingested_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        e["id"], e["tenant_id"], e["shipment_id"], e["event_type"],
                        e["event_time"], e.get("location"), e.get("details"),
                        e["payload_hash"], e["ingested_at"],
                    ),
                )
            for ex in snapshot.get("exceptions", []):
                conn.execute(
                    """
                    INSERT INTO exceptions
                        (id, tenant_id, shipment_id, opened_by_event_id, status,
                         severity, assignee, opened_at, acknowledged_at,
                         resolved_at, escalation_queued_at, version)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        ex["id"], ex["tenant_id"], ex["shipment_id"],
                        ex["opened_by_event_id"], ex["status"], ex["severity"],
                        ex.get("assignee"), ex["opened_at"],
                        ex.get("acknowledged_at"), ex.get("resolved_at"),
                        ex.get("escalation_queued_at"), ex["version"],
                    ),
                )
            for n in snapshot.get("exception_notes", []):
                conn.execute(
                    "INSERT INTO exception_notes"
                    " (id, exception_id, actor, note, created_at) VALUES (?, ?, ?, ?, ?)",
                    (n["id"], n["exception_id"], n["actor"], n["note"], n["created_at"]),
                )
            for a in snapshot.get("audit_entries", []):
                conn.execute(
                    """
                    INSERT INTO audit_entries
                        (id, tenant_id, actor, action, entity_type, entity_id,
                         details, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        a["id"], a["tenant_id"], a["actor"], a["action"],
                        a["entity_type"], a["entity_id"],
                        a.get("details"), a["created_at"],
                    ),
                )
            for r in snapshot.get("sla_rules", []):
                conn.execute(
                    "INSERT INTO sla_rules (tenant_id, severity, delay_seconds)"
                    " VALUES (?, ?, ?)",
                    (r["tenant_id"], r["severity"], r["delay_seconds"]),
                )
            for d in snapshot.get("outbox_deliveries", []):
                conn.execute(
                    """
                    INSERT INTO outbox_deliveries
                        (id, tenant_id, entity_type, entity_id, event_type,
                         idempotency_key, status, owner, lease_expires_at,
                         attempts, max_attempts, last_error, next_retry_at,
                         payload, created_at, delivered_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        d["id"], d["tenant_id"], d["entity_type"], d["entity_id"],
                        d["event_type"], d["idempotency_key"], d["status"],
                        d.get("owner"), d.get("lease_expires_at"),
                        d["attempts"], d["max_attempts"],
                        d.get("last_error"), d["next_retry_at"],
                        d.get("payload"), d["created_at"], d.get("delivered_at"),
                    ),
                )


# ---------------------------------------------------------------------------
# Module-level projection helpers (pure functions, no DB side effects)
# ---------------------------------------------------------------------------

def _project_shipment(conn: sqlite3.Connection, shipment_id: str) -> dict:
    """Deterministic replay of all carrier events to derive current shipment state.

    Events are ordered by (event_time, id) — ascending event_time with the
    event_id as a stable lexicographic tie-break.  A late historical event
    inserted afterward will be replayed in the correct position, so the
    materialised status always reflects the true timeline.
    """
    events = conn.execute(
        """
        SELECT id, event_type, event_time, location
        FROM carrier_events
        WHERE shipment_id = ?
        ORDER BY event_time, id
        """,
        (shipment_id,),
    ).fetchall()

    status = "created"
    last_location: Optional[str] = None
    delay_epoch_event_id: Optional[str] = None

    for ev in events:
        et = ev["event_type"]
        loc = ev["location"]

        if et in ("picked_up", "in_transit"):
            status = "in_transit"
            if loc:
                last_location = loc
            delay_epoch_event_id = None

        elif et == "delayed":
            if status != "delayed":
                # Entering a new delay epoch
                delay_epoch_event_id = ev["id"]
            status = "delayed"
            if loc:
                last_location = loc

        elif et == "delivered":
            status = "delivered"
            if loc:
                last_location = loc
            delay_epoch_event_id = None

        elif et == "cancelled":
            status = "cancelled"
            if loc:
                last_location = loc
            delay_epoch_event_id = None

    return {
        "status": status,
        "last_location": last_location,
        "delay_epoch_event_id": delay_epoch_event_id,
    }


def _row_to_shipment(row: sqlite3.Row) -> ShipmentRecord:
    return ShipmentRecord(
        id=row["id"],
        tenant_id=row["tenant_id"],
        reference=row["reference"],
        status=row["status"],
        last_location=row["last_location"],
        active_exception_id=row["active_exception_id"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        version=row["version"],
    )


def _row_to_exception(row: sqlite3.Row, conn: sqlite3.Connection) -> ExceptionRecord:
    notes_rows = conn.execute(
        "SELECT * FROM exception_notes WHERE exception_id = ? ORDER BY created_at",
        (row["id"],),
    ).fetchall()
    notes = [
        NoteRecord(
            id=n["id"],
            exception_id=n["exception_id"],
            actor=n["actor"],
            note=n["note"],
            created_at=n["created_at"],
        )
        for n in notes_rows
    ]
    return ExceptionRecord(
        id=row["id"],
        tenant_id=row["tenant_id"],
        shipment_id=row["shipment_id"],
        opened_by_event_id=row["opened_by_event_id"],
        status=row["status"],
        severity=row["severity"],
        assignee=row["assignee"],
        opened_at=row["opened_at"],
        acknowledged_at=row["acknowledged_at"],
        resolved_at=row["resolved_at"],
        escalation_queued_at=row["escalation_queued_at"],
        version=row["version"],
        notes=notes,
    )


def _row_to_delivery(row: sqlite3.Row) -> DeliveryRecord:
    return DeliveryRecord(
        id=row["id"],
        tenant_id=row["tenant_id"],
        entity_type=row["entity_type"],
        entity_id=row["entity_id"],
        event_type=row["event_type"],
        idempotency_key=row["idempotency_key"],
        status=row["status"],
        owner=row["owner"],
        lease_expires_at=row["lease_expires_at"],
        attempts=row["attempts"],
        max_attempts=row["max_attempts"],
        last_error=row["last_error"],
        next_retry_at=row["next_retry_at"],
        payload=_loads(row["payload"]),
        created_at=row["created_at"],
        delivered_at=row["delivered_at"],
    )
