"""Durable SQLite-backed freight control tower store."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

from .exceptions import (
    AuthenticationError,
    AuthorizationError,
    IdempotencyConflict,
    NotFoundError,
    ValidationError,
    VersionConflict,
)
from .models import Shipment, ShipmentStatus

SCHEMA_VERSION = 1

SCHEMA_DDL = """
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY,
    applied_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS tenants (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS credentials (
    token_hash TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL REFERENCES tenants(id),
    role TEXT NOT NULL CHECK(role IN ('viewer', 'operator', 'admin')),
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS shipments (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL REFERENCES tenants(id),
    reference TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'created',
    last_location TEXT,
    version INTEGER NOT NULL DEFAULT 1,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    UNIQUE(tenant_id, reference)
);

CREATE TABLE IF NOT EXISTS carrier_events (
    row_id INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id TEXT NOT NULL,
    shipment_id TEXT NOT NULL REFERENCES shipments(id),
    event_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    event_time REAL NOT NULL,
    location TEXT,
    details TEXT,
    created_at REAL NOT NULL,
    UNIQUE(event_id)
);

CREATE TABLE IF NOT EXISTS exceptions (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    shipment_id TEXT NOT NULL REFERENCES shipments(id),
    epoch INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open', 'acknowledged', 'resolved')),
    severity TEXT NOT NULL DEFAULT 'P2',
    assignee TEXT,
    version INTEGER NOT NULL DEFAULT 1,
    opened_at REAL NOT NULL,
    resolved_at REAL,
    escalation_enqueued INTEGER NOT NULL DEFAULT 0,
    UNIQUE(shipment_id, epoch)
);

CREATE TABLE IF NOT EXISTS exception_notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    exception_id TEXT NOT NULL REFERENCES exceptions(id),
    actor TEXT NOT NULL,
    note TEXT NOT NULL,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id TEXT NOT NULL,
    actor TEXT,
    action TEXT NOT NULL,
    resource_type TEXT,
    resource_id TEXT,
    details TEXT,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS sla_rules (
    tenant_id TEXT NOT NULL REFERENCES tenants(id),
    severity TEXT NOT NULL,
    delay_seconds INTEGER NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    PRIMARY KEY(tenant_id, severity)
);

CREATE TABLE IF NOT EXISTS outbox (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    delivery_type TEXT NOT NULL,
    payload TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK(status IN ('pending', 'claimed', 'delivered', 'failed', 'dead_letter')),
    attempts INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 5,
    owner TEXT,
    lease_expires_at REAL,
    next_attempt_at REAL NOT NULL,
    last_error TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    UNIQUE(idempotency_key)
);

CREATE TABLE IF NOT EXISTS outbox_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    outbox_id TEXT NOT NULL REFERENCES outbox(id),
    attempt INTEGER NOT NULL,
    status TEXT NOT NULL,
    error TEXT,
    worker_id TEXT,
    created_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_credentials_tenant ON credentials(tenant_id);
CREATE INDEX IF NOT EXISTS idx_shipments_tenant ON shipments(tenant_id, status);
CREATE INDEX IF NOT EXISTS idx_events_shipment ON carrier_events(shipment_id, event_time, event_id);
CREATE INDEX IF NOT EXISTS idx_exceptions_tenant ON exceptions(tenant_id, status);
CREATE INDEX IF NOT EXISTS idx_exceptions_shipment ON exceptions(shipment_id, epoch);
CREATE INDEX IF NOT EXISTS idx_audit_tenant ON audit(tenant_id, created_at);
CREATE INDEX IF NOT EXISTS idx_outbox_claim ON outbox(status, next_attempt_at);
CREATE INDEX IF NOT EXISTS idx_outbox_tenant ON outbox(tenant_id);
CREATE INDEX IF NOT EXISTS idx_outbox_history ON outbox_history(outbox_id);
"""

VALID_ROLES = frozenset({"viewer", "operator", "admin"})
VALID_EVENT_TYPES = frozenset({"picked_up", "in_transit", "delayed", "delivered", "cancelled"})

# Roles allowed to perform write operations
WRITE_ROLES = frozenset({"operator", "admin"})
ADMIN_ROLES = frozenset({"admin"})


# ---------------------------------------------------------------------------
# Return-value dataclasses
# ---------------------------------------------------------------------------

@dataclass
class FreightException:
    id: str
    tenant_id: str
    shipment_id: str
    epoch: int
    status: str
    severity: str
    assignee: str | None
    version: int
    opened_at: float
    resolved_at: float | None
    escalation_enqueued: bool
    notes: list[ExceptionNote] = field(default_factory=list)


@dataclass
class ExceptionNote:
    id: int
    exception_id: str
    actor: str
    note: str
    created_at: float


@dataclass
class AuditEntry:
    id: int
    tenant_id: str
    actor: str | None
    action: str
    resource_type: str | None
    resource_id: str | None
    details: dict | None
    created_at: float


@dataclass
class SLARule:
    tenant_id: str
    severity: str
    delay_seconds: int
    created_at: float
    updated_at: float


@dataclass
class DeliveryHistoryEntry:
    id: int
    outbox_id: str
    attempt: int
    status: str
    error: str | None
    worker_id: str | None
    created_at: float


@dataclass
class Delivery:
    id: str
    tenant_id: str
    delivery_type: str
    payload: dict
    idempotency_key: str
    status: str
    attempts: int
    max_attempts: int
    owner: str | None
    lease_expires_at: float | None
    next_attempt_at: float
    last_error: str | None
    created_at: float
    updated_at: float
    history: list[DeliveryHistoryEntry] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _extract_severity(details: Any) -> str:
    if isinstance(details, dict):
        return details.get("severity", "P2")
    if isinstance(details, str):
        try:
            d = json.loads(details)
            if isinstance(d, dict):
                return str(d.get("severity", "P2"))
        except (json.JSONDecodeError, TypeError):
            pass
    return "P2"


def _retry_delay(attempt: int) -> float:
    """Exponential back-off capped at 10 minutes."""
    return min(2 ** attempt, 600)


# ---------------------------------------------------------------------------
# Main store
# ---------------------------------------------------------------------------

class SQLiteFreightStore:
    """
    Durable, multi-tenant freight exception control tower backed by SQLite.

    Two instances opened on the same *db_path* observe each other's committed
    work.  No network listeners are started by importing or constructing this
    class — call ``init_schema()`` to prepare the database, then use the
    domain methods.
    """

    def __init__(
        self,
        db_path: str,
        clock: Any = None,
        lease_seconds: int = 30,
        max_attempts: int = 5,
    ) -> None:
        self._db_path = str(db_path)
        self._clock = clock if clock is not None else time.time
        self._lease_seconds = lease_seconds
        self._max_attempts = max_attempts

    # ------------------------------------------------------------------
    # Schema
    # ------------------------------------------------------------------

    def init_schema(self) -> None:
        """Idempotently create or migrate the database schema."""
        con = sqlite3.connect(self._db_path, timeout=30)
        try:
            con.execute("PRAGMA journal_mode=WAL")
            con.execute("PRAGMA foreign_keys=ON")
            con.executescript(SCHEMA_DDL)
            con.execute(
                "INSERT OR IGNORE INTO schema_version (version, applied_at) VALUES (?, ?)",
                (SCHEMA_VERSION, self._clock()),
            )
            con.commit()
        finally:
            con.close()

    # ------------------------------------------------------------------
    # Connection / transaction helpers
    # ------------------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(self._db_path, timeout=30)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA foreign_keys=ON")
        return con

    @contextmanager
    def _txn(self, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        """Context manager yielding a connection inside a transaction."""
        con = self._connect()
        if immediate:
            con.isolation_level = None  # manual transaction control
        try:
            if immediate:
                con.execute("BEGIN IMMEDIATE")
            yield con
            if immediate:
                con.execute("COMMIT")
            else:
                con.commit()
        except Exception:
            try:
                if immediate:
                    con.execute("ROLLBACK")
                else:
                    con.rollback()
            except Exception:
                pass
            raise
        finally:
            con.close()

    # ------------------------------------------------------------------
    # Auth helpers
    # ------------------------------------------------------------------

    def _authenticate(self, token: str, con: sqlite3.Connection) -> tuple[str, str]:
        """Return (tenant_id, role) or raise AuthenticationError."""
        if not token:
            raise AuthenticationError("Missing bearer token")
        row = con.execute(
            "SELECT tenant_id, role FROM credentials WHERE token_hash=?",
            (_hash_token(token),),
        ).fetchone()
        if row is None:
            raise AuthenticationError("Invalid or revoked token")
        return row["tenant_id"], row["role"]

    def _authorize(self, role: str, allowed: frozenset[str], action: str) -> None:
        if role not in allowed:
            raise AuthorizationError(f"Role '{role}' cannot perform '{action}'")

    # ------------------------------------------------------------------
    # Audit / outbox helpers
    # ------------------------------------------------------------------

    def _audit(
        self,
        tenant_id: str,
        actor: str | None,
        action: str,
        resource_type: str | None,
        resource_id: str | None,
        details: dict | None,
        con: sqlite3.Connection,
    ) -> None:
        con.execute(
            "INSERT INTO audit "
            "(tenant_id, actor, action, resource_type, resource_id, details, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                tenant_id,
                actor,
                action,
                resource_type,
                resource_id,
                json.dumps(details) if details is not None else None,
                self._clock(),
            ),
        )

    def _enqueue(
        self,
        tenant_id: str,
        delivery_type: str,
        payload: dict,
        idempotency_key: str,
        con: sqlite3.Connection,
    ) -> None:
        now = self._clock()
        con.execute(
            "INSERT OR IGNORE INTO outbox "
            "(id, tenant_id, delivery_type, payload, idempotency_key, "
            " max_attempts, next_attempt_at, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                str(uuid.uuid4()),
                tenant_id,
                delivery_type,
                json.dumps(payload),
                idempotency_key,
                self._max_attempts,
                now,
                now,
                now,
            ),
        )

    # ------------------------------------------------------------------
    # Shipment helpers
    # ------------------------------------------------------------------

    def _fetch_shipment(
        self, shipment_id: str, con: sqlite3.Connection
    ) -> sqlite3.Row:
        row = con.execute(
            "SELECT s.*, "
            "  (SELECT id FROM exceptions "
            "   WHERE shipment_id=s.id AND status IN ('open','acknowledged') "
            "   LIMIT 1) AS active_exception_id "
            "FROM shipments s WHERE s.id=?",
            (shipment_id,),
        ).fetchone()
        return row

    def _row_to_shipment(self, row: sqlite3.Row) -> Shipment:
        keys = row.keys()
        aei = row["active_exception_id"] if "active_exception_id" in keys else None
        return Shipment(
            id=row["id"],
            tenant_id=row["tenant_id"],
            reference=row["reference"],
            status=ShipmentStatus(row["status"]),
            last_location=row["last_location"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            version=row["version"],
            active_exception_id=aei,
        )

    # ------------------------------------------------------------------
    # Event projection
    # ------------------------------------------------------------------

    def _project_shipment(
        self, shipment_id: str, con: sqlite3.Connection
    ) -> tuple[ShipmentStatus, str | None, list[list], int | None]:
        """
        Replay all events for *shipment_id* and return:
          (status, last_location, delay_epochs, active_epoch)

        delay_epochs is a list of [epoch_num, open_time, close_time_or_None, severity].
        active_epoch is the epoch number if currently in a delay phase, else None.
        """
        events = con.execute(
            "SELECT event_id, event_type, event_time, location, details "
            "FROM carrier_events WHERE shipment_id=? "
            "ORDER BY event_time, event_id",
            (shipment_id,),
        ).fetchall()

        status = ShipmentStatus.CREATED
        last_location: str | None = None
        delay_epochs: list[list] = []
        in_delay = False
        current_epoch = -1

        for ev in events:
            etype = ev["event_type"]
            etime = ev["event_time"]
            loc = ev["location"]

            if etype in ("picked_up", "in_transit"):
                status = ShipmentStatus.IN_TRANSIT
            elif etype == "delayed":
                if not in_delay:
                    current_epoch += 1
                    sev = _extract_severity(ev["details"])
                    delay_epochs.append([current_epoch, etime, None, sev])
                    in_delay = True
                status = ShipmentStatus.DELAYED
            elif etype == "delivered":
                if in_delay and delay_epochs:
                    delay_epochs[-1][2] = etime
                in_delay = False
                status = ShipmentStatus.DELIVERED
            elif etype == "cancelled":
                if in_delay and delay_epochs:
                    delay_epochs[-1][2] = etime
                in_delay = False
                status = ShipmentStatus.CANCELLED

            if loc:
                last_location = loc

        active_epoch = current_epoch if in_delay else None
        return status, last_location, delay_epochs, active_epoch

    def _reconcile_exceptions(
        self,
        shipment_id: str,
        tenant_id: str,
        delay_epochs: list[list],
        active_epoch: int | None,
        con: sqlite3.Connection,
    ) -> None:
        """Create or update exception rows to match the projected delay epochs."""
        for epoch_num, open_time, close_time, severity in delay_epochs:
            existing = con.execute(
                "SELECT id, status FROM exceptions WHERE shipment_id=? AND epoch=?",
                (shipment_id, epoch_num),
            ).fetchone()

            if existing is None:
                exc_id = str(uuid.uuid4())
                exc_status = "resolved" if close_time is not None else "open"
                con.execute(
                    "INSERT INTO exceptions "
                    "(id, tenant_id, shipment_id, epoch, status, severity, "
                    " opened_at, resolved_at, version) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1)",
                    (
                        exc_id,
                        tenant_id,
                        shipment_id,
                        epoch_num,
                        exc_status,
                        severity,
                        open_time,
                        close_time,
                    ),
                )
            else:
                exc_id = existing["id"]
                exc_status = existing["status"]
                if close_time is not None and exc_status != "resolved":
                    con.execute(
                        "UPDATE exceptions "
                        "SET status='resolved', resolved_at=?, version=version+1 "
                        "WHERE id=?",
                        (close_time, exc_id),
                    )

    # ------------------------------------------------------------------
    # Tenant / credential management
    # ------------------------------------------------------------------

    def bootstrap_tenant(
        self, tenant_id: str, name: str, admin_token: str
    ) -> dict:
        """
        Create *tenant_id* if it does not exist and register *admin_token*
        as an admin credential.  Idempotent: safe to call more than once.
        """
        if not tenant_id or not tenant_id.strip():
            raise ValidationError("tenant_id is required")
        if not name or not name.strip():
            raise ValidationError("name is required")
        if not admin_token:
            raise ValidationError("admin_token is required")
        tenant_id = tenant_id.strip()
        name = name.strip()
        now = self._clock()
        with self._txn() as con:
            con.execute(
                "INSERT OR IGNORE INTO tenants (id, name, created_at) VALUES (?, ?, ?)",
                (tenant_id, name, now),
            )
            con.execute(
                "INSERT OR IGNORE INTO credentials "
                "(token_hash, tenant_id, role, created_at) VALUES (?, ?, 'admin', ?)",
                (_hash_token(admin_token), tenant_id, now),
            )
            self._audit(
                tenant_id, None, "tenant.bootstrapped", "tenant", tenant_id,
                {"name": name}, con,
            )
            row = con.execute(
                "SELECT id, name, created_at FROM tenants WHERE id=?", (tenant_id,)
            ).fetchone()
        return {"id": row["id"], "name": row["name"], "created_at": row["created_at"]}

    def create_credential(
        self, admin_token: str, token: str, role: str
    ) -> dict:
        """Register *token* with *role* for the admin's tenant."""
        if role not in VALID_ROLES:
            raise ValidationError(f"role must be one of {sorted(VALID_ROLES)}")
        if not token:
            raise ValidationError("token is required")
        with self._txn() as con:
            tenant_id, caller_role = self._authenticate(admin_token, con)
            self._authorize(caller_role, ADMIN_ROLES, "create_credential")
            now = self._clock()
            th = _hash_token(token)
            existing = con.execute(
                "SELECT role FROM credentials WHERE token_hash=?", (th,)
            ).fetchone()
            if existing is not None and existing["role"] != role:
                raise ValidationError("Token already exists with a different role")
            con.execute(
                "INSERT OR IGNORE INTO credentials "
                "(token_hash, tenant_id, role, created_at) VALUES (?, ?, ?, ?)",
                (th, tenant_id, role, now),
            )
            self._audit(
                tenant_id, admin_token[:8] + "…", "credential.created",
                "credential", th[:8], {"role": role}, con,
            )
        return {"tenant_id": tenant_id, "role": role, "created_at": now}

    # ------------------------------------------------------------------
    # Shipments
    # ------------------------------------------------------------------

    def create_shipment(self, token: str, reference: str) -> Shipment:
        if not reference or not reference.strip():
            raise ValidationError("reference is required")
        reference = reference.strip()
        with self._txn() as con:
            tenant_id, role = self._authenticate(token, con)
            self._authorize(role, WRITE_ROLES, "create_shipment")
            now = self._clock()
            shipment_id = str(uuid.uuid4())
            try:
                con.execute(
                    "INSERT INTO shipments "
                    "(id, tenant_id, reference, status, last_location, version, "
                    " created_at, updated_at) "
                    "VALUES (?, ?, ?, 'created', NULL, 1, ?, ?)",
                    (shipment_id, tenant_id, reference, now, now),
                )
            except sqlite3.IntegrityError:
                raise ValidationError(
                    f"Reference '{reference}' already exists for this tenant"
                )
            self._audit(
                tenant_id, token[:8] + "…", "shipment.created",
                "shipment", shipment_id, {"reference": reference}, con,
            )
            self._enqueue(
                tenant_id,
                "shipment.created",
                {"shipment_id": shipment_id, "reference": reference},
                f"shipment:{shipment_id}:created",
                con,
            )
            row = self._fetch_shipment(shipment_id, con)
        return self._row_to_shipment(row)

    def get_shipment(self, token: str, shipment_id: str) -> Shipment:
        with self._txn() as con:
            tenant_id, _ = self._authenticate(token, con)
            row = self._fetch_shipment(shipment_id, con)
            if row is None or row["tenant_id"] != tenant_id:
                raise NotFoundError(f"Shipment '{shipment_id}' not found")
        return self._row_to_shipment(row)

    def list_shipments(self, token: str, **filters: Any) -> list[Shipment]:
        with self._txn() as con:
            tenant_id, _ = self._authenticate(token, con)
            sql = (
                "SELECT s.*, "
                "  (SELECT id FROM exceptions "
                "   WHERE shipment_id=s.id AND status IN ('open','acknowledged') "
                "   LIMIT 1) AS active_exception_id "
                "FROM shipments s WHERE s.tenant_id=?"
            )
            params: list = [tenant_id]
            if "status" in filters and filters["status"] is not None:
                sql += " AND s.status=?"
                params.append(filters["status"])
            if "reference" in filters and filters["reference"] is not None:
                sql += " AND s.reference LIKE ?"
                params.append(f"%{filters['reference']}%")
            sql += " ORDER BY s.created_at"
            rows = con.execute(sql, params).fetchall()
        return [self._row_to_shipment(r) for r in rows]

    # ------------------------------------------------------------------
    # Event ingestion
    # ------------------------------------------------------------------

    def ingest_event(
        self,
        token: str,
        shipment_id: str,
        event_id: str,
        event_type: str,
        event_time: float,
        location: str | None = None,
        details: Any = None,
    ) -> Shipment:
        if event_type not in VALID_EVENT_TYPES:
            raise ValidationError(
                f"event_type must be one of {sorted(VALID_EVENT_TYPES)}"
            )
        if not event_id or not event_id.strip():
            raise ValidationError("event_id is required")

        # Normalise details to a string for storage and comparison
        if isinstance(details, dict):
            details_str: str | None = json.dumps(details, sort_keys=True)
        elif details is not None:
            details_str = str(details)
        else:
            details_str = None

        with self._txn(immediate=True) as con:
            tenant_id, role = self._authenticate(token, con)
            self._authorize(role, WRITE_ROLES, "ingest_event")

            ship_row = con.execute(
                "SELECT id, tenant_id FROM shipments WHERE id=?", (shipment_id,)
            ).fetchone()
            if ship_row is None or ship_row["tenant_id"] != tenant_id:
                raise NotFoundError(f"Shipment '{shipment_id}' not found")

            # Idempotency check
            existing_ev = con.execute(
                "SELECT event_type, event_time, location, details "
                "FROM carrier_events WHERE event_id=?",
                (event_id,),
            ).fetchone()
            if existing_ev is not None:
                if (
                    existing_ev["event_type"] != event_type
                    or existing_ev["event_time"] != event_time
                    or existing_ev["location"] != location
                    or existing_ev["details"] != details_str
                ):
                    raise IdempotencyConflict(
                        f"event_id '{event_id}' already exists with a different payload"
                    )
                # Truly idempotent – return current projection
                row = self._fetch_shipment(shipment_id, con)
                return self._row_to_shipment(row)

            now = self._clock()
            try:
                con.execute(
                    "INSERT INTO carrier_events "
                    "(tenant_id, shipment_id, event_id, event_type, event_time, "
                    " location, details, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (tenant_id, shipment_id, event_id, event_type, event_time,
                     location, details_str, now),
                )
            except sqlite3.IntegrityError:
                # Concurrent duplicate – re-check idempotency
                existing_ev = con.execute(
                    "SELECT event_type, event_time, location, details "
                    "FROM carrier_events WHERE event_id=?",
                    (event_id,),
                ).fetchone()
                if existing_ev is not None and (
                    existing_ev["event_type"] != event_type
                    or existing_ev["event_time"] != event_time
                    or existing_ev["location"] != location
                    or existing_ev["details"] != details_str
                ):
                    raise IdempotencyConflict(
                        f"event_id '{event_id}' already exists with a different payload"
                    )
                row = self._fetch_shipment(shipment_id, con)
                return self._row_to_shipment(row)

            # Project new state
            status, last_location, delay_epochs, active_epoch = (
                self._project_shipment(shipment_id, con)
            )
            # Reconcile exceptions
            self._reconcile_exceptions(
                shipment_id, tenant_id, delay_epochs, active_epoch, con
            )
            # Update shipment record
            con.execute(
                "UPDATE shipments SET status=?, last_location=?, updated_at=?, "
                "version=version+1 WHERE id=?",
                (status.value, last_location, now, shipment_id),
            )
            self._audit(
                tenant_id, token[:8] + "…", "event.ingested",
                "shipment", shipment_id,
                {"event_id": event_id, "event_type": event_type}, con,
            )
            self._enqueue(
                tenant_id,
                "event.ingested",
                {"shipment_id": shipment_id, "event_id": event_id,
                 "event_type": event_type, "status": status.value},
                f"event:{event_id}:ingested",
                con,
            )
            # Notify exception open/close
            if active_epoch is not None:
                exc_row = con.execute(
                    "SELECT id FROM exceptions WHERE shipment_id=? AND epoch=?",
                    (shipment_id, active_epoch),
                ).fetchone()
                if exc_row:
                    self._enqueue(
                        tenant_id, "exception.opened",
                        {"exception_id": exc_row["id"], "shipment_id": shipment_id},
                        f"exception:{shipment_id}:{active_epoch}:opened",
                        con,
                    )
            row = self._fetch_shipment(shipment_id, con)
        return self._row_to_shipment(row)

    # ------------------------------------------------------------------
    # Exceptions
    # ------------------------------------------------------------------

    def _fetch_exception(
        self, exception_id: str, con: sqlite3.Connection
    ) -> sqlite3.Row | None:
        return con.execute(
            "SELECT * FROM exceptions WHERE id=?", (exception_id,)
        ).fetchone()

    def _row_to_exception(
        self, row: sqlite3.Row, notes: list[ExceptionNote]
    ) -> FreightException:
        return FreightException(
            id=row["id"],
            tenant_id=row["tenant_id"],
            shipment_id=row["shipment_id"],
            epoch=row["epoch"],
            status=row["status"],
            severity=row["severity"],
            assignee=row["assignee"],
            version=row["version"],
            opened_at=row["opened_at"],
            resolved_at=row["resolved_at"],
            escalation_enqueued=bool(row["escalation_enqueued"]),
            notes=notes,
        )

    def _fetch_notes(
        self, exception_id: str, con: sqlite3.Connection
    ) -> list[ExceptionNote]:
        rows = con.execute(
            "SELECT * FROM exception_notes WHERE exception_id=? ORDER BY created_at",
            (exception_id,),
        ).fetchall()
        return [
            ExceptionNote(
                id=r["id"],
                exception_id=r["exception_id"],
                actor=r["actor"],
                note=r["note"],
                created_at=r["created_at"],
            )
            for r in rows
        ]

    def list_exceptions(self, token: str, **filters: Any) -> list[FreightException]:
        with self._txn() as con:
            tenant_id, _ = self._authenticate(token, con)
            sql = "SELECT * FROM exceptions WHERE tenant_id=?"
            params: list = [tenant_id]
            if "status" in filters and filters["status"] is not None:
                sql += " AND status=?"
                params.append(filters["status"])
            if "severity" in filters and filters["severity"] is not None:
                sql += " AND severity=?"
                params.append(filters["severity"])
            if "assignee" in filters and filters["assignee"] is not None:
                sql += " AND assignee=?"
                params.append(filters["assignee"])
            if "shipment_id" in filters and filters["shipment_id"] is not None:
                sql += " AND shipment_id=?"
                params.append(filters["shipment_id"])
            sql += " ORDER BY opened_at DESC"
            rows = con.execute(sql, params).fetchall()
            result = []
            for r in rows:
                notes = self._fetch_notes(r["id"], con)
                result.append(self._row_to_exception(r, notes))
        return result

    def get_exception(self, token: str, exception_id: str) -> FreightException:
        with self._txn() as con:
            tenant_id, _ = self._authenticate(token, con)
            row = self._fetch_exception(exception_id, con)
            if row is None or row["tenant_id"] != tenant_id:
                raise NotFoundError(f"Exception '{exception_id}' not found")
            notes = self._fetch_notes(exception_id, con)
        return self._row_to_exception(row, notes)

    def mutate_exception(
        self,
        token: str,
        exception_id: str,
        expected_version: int,
        action: str,
        actor: str | None = None,
        **values: Any,
    ) -> FreightException:
        """
        Apply *action* to an exception with optimistic-lock version check.

        Actions: ``assign``, ``acknowledge``, ``add_note``, ``resolve``.
        """
        valid_actions = {"assign", "acknowledge", "add_note", "resolve"}
        if action not in valid_actions:
            raise ValidationError(f"action must be one of {sorted(valid_actions)}")

        with self._txn(immediate=True) as con:
            tenant_id, role = self._authenticate(token, con)
            self._authorize(role, WRITE_ROLES, f"exception.{action}")

            row = self._fetch_exception(exception_id, con)
            if row is None or row["tenant_id"] != tenant_id:
                raise NotFoundError(f"Exception '{exception_id}' not found")

            if row["version"] != expected_version:
                raise VersionConflict(
                    f"Expected version {expected_version}, "
                    f"current version is {row['version']}"
                )

            now = self._clock()

            if action == "assign":
                assignee = values.get("assignee")
                if not assignee:
                    raise ValidationError("'assignee' is required for assign action")
                con.execute(
                    "UPDATE exceptions SET assignee=?, version=version+1 WHERE id=?",
                    (assignee, exception_id),
                )
            elif action == "acknowledge":
                if row["status"] == "resolved":
                    raise ValidationError("Cannot acknowledge a resolved exception")
                con.execute(
                    "UPDATE exceptions SET status='acknowledged', version=version+1 "
                    "WHERE id=?",
                    (exception_id,),
                )
            elif action == "add_note":
                note = values.get("note")
                if not note:
                    raise ValidationError("'note' is required for add_note action")
                actor_name = actor or "unknown"
                con.execute(
                    "INSERT INTO exception_notes "
                    "(exception_id, actor, note, created_at) VALUES (?, ?, ?, ?)",
                    (exception_id, actor_name, note, now),
                )
                con.execute(
                    "UPDATE exceptions SET version=version+1 WHERE id=?",
                    (exception_id,),
                )
            elif action == "resolve":
                if row["status"] == "resolved":
                    raise ValidationError("Exception is already resolved")
                con.execute(
                    "UPDATE exceptions "
                    "SET status='resolved', resolved_at=?, version=version+1 "
                    "WHERE id=?",
                    (now, exception_id),
                )
                self._enqueue(
                    tenant_id, "exception.resolved",
                    {"exception_id": exception_id,
                     "shipment_id": row["shipment_id"]},
                    f"exception:{exception_id}:resolved",
                    con,
                )

            audit_details = {"action": action, "actor": actor}
            audit_details.update(values)
            self._audit(
                tenant_id, actor or (token[:8] + "…"),
                f"exception.{action}",
                "exception", exception_id, audit_details, con,
            )
            row = self._fetch_exception(exception_id, con)
            notes = self._fetch_notes(exception_id, con)
        return self._row_to_exception(row, notes)

    # ------------------------------------------------------------------
    # Audit
    # ------------------------------------------------------------------

    def audit(self, token: str, **filters: Any) -> list[AuditEntry]:
        with self._txn() as con:
            tenant_id, _ = self._authenticate(token, con)
            sql = "SELECT * FROM audit WHERE tenant_id=?"
            params: list = [tenant_id]
            for col in ("resource_type", "resource_id", "actor", "action"):
                if col in filters and filters[col] is not None:
                    sql += f" AND {col}=?"
                    params.append(filters[col])
            sql += " ORDER BY created_at DESC"
            rows = con.execute(sql, params).fetchall()
        return [
            AuditEntry(
                id=r["id"],
                tenant_id=r["tenant_id"],
                actor=r["actor"],
                action=r["action"],
                resource_type=r["resource_type"],
                resource_id=r["resource_id"],
                details=json.loads(r["details"]) if r["details"] else None,
                created_at=r["created_at"],
            )
            for r in rows
        ]

    # ------------------------------------------------------------------
    # SLA rules
    # ------------------------------------------------------------------

    def set_sla_rule(
        self, admin_token: str, severity: str, delay_seconds: int
    ) -> SLARule:
        if not severity:
            raise ValidationError("severity is required")
        if delay_seconds <= 0:
            raise ValidationError("delay_seconds must be positive")
        with self._txn() as con:
            tenant_id, role = self._authenticate(admin_token, con)
            self._authorize(role, ADMIN_ROLES, "set_sla_rule")
            now = self._clock()
            existing = con.execute(
                "SELECT created_at FROM sla_rules WHERE tenant_id=? AND severity=?",
                (tenant_id, severity),
            ).fetchone()
            created_at = existing["created_at"] if existing else now
            con.execute(
                "INSERT OR REPLACE INTO sla_rules "
                "(tenant_id, severity, delay_seconds, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (tenant_id, severity, delay_seconds, created_at, now),
            )
            self._audit(
                tenant_id, admin_token[:8] + "…", "sla_rule.set",
                "sla_rule", severity,
                {"delay_seconds": delay_seconds}, con,
            )
        return SLARule(
            tenant_id=tenant_id,
            severity=severity,
            delay_seconds=delay_seconds,
            created_at=created_at,
            updated_at=now,
        )

    def list_sla_rules(self, token: str) -> list[SLARule]:
        with self._txn() as con:
            tenant_id, _ = self._authenticate(token, con)
            rows = con.execute(
                "SELECT * FROM sla_rules WHERE tenant_id=? ORDER BY severity",
                (tenant_id,),
            ).fetchall()
        return [
            SLARule(
                tenant_id=r["tenant_id"],
                severity=r["severity"],
                delay_seconds=r["delay_seconds"],
                created_at=r["created_at"],
                updated_at=r["updated_at"],
            )
            for r in rows
        ]

    # ------------------------------------------------------------------
    # Tick / SLA escalation
    # ------------------------------------------------------------------

    def tick(self, now: float, limit: int = 100) -> int:
        """
        Claim and enqueue escalations for exceptions that have exceeded their
        SLA deadline.  Safe to call concurrently across multiple instances.
        Returns the number of newly enqueued escalations.
        """
        count = 0
        with self._txn(immediate=True) as con:
            eligible = con.execute(
                """
                SELECT e.id, e.tenant_id, e.shipment_id, e.severity
                FROM exceptions e
                JOIN sla_rules sr ON sr.tenant_id=e.tenant_id AND sr.severity=e.severity
                WHERE e.status='open'
                  AND e.escalation_enqueued=0
                  AND e.opened_at + sr.delay_seconds <= ?
                LIMIT ?
                """,
                (now, limit),
            ).fetchall()

            for row in eligible:
                # Atomic claim: only the winner of this UPDATE proceeds
                updated = con.execute(
                    "UPDATE exceptions SET escalation_enqueued=1 "
                    "WHERE id=? AND escalation_enqueued=0",
                    (row["id"],),
                )
                if updated.rowcount == 0:
                    continue  # Lost race with another instance
                self._enqueue(
                    row["tenant_id"],
                    "escalation.triggered",
                    {"exception_id": row["id"],
                     "shipment_id": row["shipment_id"],
                     "severity": row["severity"]},
                    f"escalation:{row['id']}",
                    con,
                )
                count += 1
        return count

    # ------------------------------------------------------------------
    # Outbox / delivery
    # ------------------------------------------------------------------

    def _row_to_delivery(
        self, row: sqlite3.Row, history: list[DeliveryHistoryEntry]
    ) -> Delivery:
        return Delivery(
            id=row["id"],
            tenant_id=row["tenant_id"],
            delivery_type=row["delivery_type"],
            payload=json.loads(row["payload"]),
            idempotency_key=row["idempotency_key"],
            status=row["status"],
            attempts=row["attempts"],
            max_attempts=row["max_attempts"],
            owner=row["owner"],
            lease_expires_at=row["lease_expires_at"],
            next_attempt_at=row["next_attempt_at"],
            last_error=row["last_error"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            history=history,
        )

    def _fetch_history(
        self, outbox_id: str, con: sqlite3.Connection
    ) -> list[DeliveryHistoryEntry]:
        rows = con.execute(
            "SELECT * FROM outbox_history WHERE outbox_id=? ORDER BY id",
            (outbox_id,),
        ).fetchall()
        return [
            DeliveryHistoryEntry(
                id=r["id"],
                outbox_id=r["outbox_id"],
                attempt=r["attempt"],
                status=r["status"],
                error=r["error"],
                worker_id=r["worker_id"],
                created_at=r["created_at"],
            )
            for r in rows
        ]

    def claim_delivery(
        self, worker_id: str, now: float
    ) -> Delivery | None:
        """
        Claim one pending delivery for *worker_id*.  Returns None if nothing
        is available.  Safe to call concurrently; each eligible row is
        claimed by exactly one caller.
        """
        with self._txn(immediate=True) as con:
            row = con.execute(
                """
                SELECT id FROM outbox
                WHERE (status='pending'
                       OR (status='claimed' AND lease_expires_at <= ?))
                  AND next_attempt_at <= ?
                  AND attempts < max_attempts
                ORDER BY next_attempt_at
                LIMIT 1
                """,
                (now, now),
            ).fetchone()
            if row is None:
                return None
            outbox_id = row["id"]
            lease_exp = now + self._lease_seconds
            updated = con.execute(
                "UPDATE outbox SET status='claimed', owner=?, lease_expires_at=?, "
                "updated_at=? WHERE id=? AND (status='pending' OR "
                "(status='claimed' AND lease_expires_at<=?))",
                (worker_id, lease_exp, now, outbox_id, now),
            )
            if updated.rowcount == 0:
                return None  # Lost race
            delivery_row = con.execute(
                "SELECT * FROM outbox WHERE id=?", (outbox_id,)
            ).fetchone()
            history = self._fetch_history(outbox_id, con)
        return self._row_to_delivery(delivery_row, history)

    def complete_delivery(
        self, delivery_id: str, worker_id: str, now: float
    ) -> Delivery:
        with self._txn(immediate=True) as con:
            row = con.execute(
                "SELECT * FROM outbox WHERE id=?", (delivery_id,)
            ).fetchone()
            if row is None:
                raise NotFoundError(f"Delivery '{delivery_id}' not found")
            if row["owner"] != worker_id:
                raise AuthorizationError(
                    "Delivery is not owned by this worker"
                )
            attempt_num = row["attempts"] + 1
            con.execute(
                "UPDATE outbox SET status='delivered', attempts=?, updated_at=? "
                "WHERE id=?",
                (attempt_num, now, delivery_id),
            )
            con.execute(
                "INSERT INTO outbox_history "
                "(outbox_id, attempt, status, worker_id, created_at) "
                "VALUES (?, ?, 'delivered', ?, ?)",
                (delivery_id, attempt_num, worker_id, now),
            )
            delivery_row = con.execute(
                "SELECT * FROM outbox WHERE id=?", (delivery_id,)
            ).fetchone()
            history = self._fetch_history(delivery_id, con)
        return self._row_to_delivery(delivery_row, history)

    def fail_delivery(
        self, delivery_id: str, worker_id: str, error: str, now: float
    ) -> Delivery:
        with self._txn(immediate=True) as con:
            row = con.execute(
                "SELECT * FROM outbox WHERE id=?", (delivery_id,)
            ).fetchone()
            if row is None:
                raise NotFoundError(f"Delivery '{delivery_id}' not found")
            if row["owner"] != worker_id:
                raise AuthorizationError(
                    "Delivery is not owned by this worker"
                )
            attempt_num = row["attempts"] + 1
            if attempt_num >= row["max_attempts"]:
                new_status = "dead_letter"
                next_at = None
            else:
                new_status = "pending"
                next_at = now + _retry_delay(attempt_num)
            con.execute(
                "UPDATE outbox SET status=?, attempts=?, last_error=?, "
                "next_attempt_at=COALESCE(?, next_attempt_at), owner=NULL, "
                "lease_expires_at=NULL, updated_at=? WHERE id=?",
                (new_status, attempt_num, error, next_at, now, delivery_id),
            )
            con.execute(
                "INSERT INTO outbox_history "
                "(outbox_id, attempt, status, error, worker_id, created_at) "
                "VALUES (?, ?, 'failed', ?, ?, ?)",
                (delivery_id, attempt_num, error, worker_id, now),
            )
            delivery_row = con.execute(
                "SELECT * FROM outbox WHERE id=?", (delivery_id,)
            ).fetchone()
            history = self._fetch_history(delivery_id, con)
        return self._row_to_delivery(delivery_row, history)

    def replay_delivery(
        self, admin_token: str, delivery_id: str, now: float
    ) -> Delivery:
        """Reset a dead-lettered delivery so it can be re-attempted."""
        with self._txn(immediate=True) as con:
            tenant_id, role = self._authenticate(admin_token, con)
            self._authorize(role, ADMIN_ROLES, "replay_delivery")
            row = con.execute(
                "SELECT * FROM outbox WHERE id=?", (delivery_id,)
            ).fetchone()
            if row is None or row["tenant_id"] != tenant_id:
                raise NotFoundError(f"Delivery '{delivery_id}' not found")
            if row["status"] != "dead_letter":
                raise ValidationError(
                    "Only dead-lettered deliveries can be replayed"
                )
            con.execute(
                "UPDATE outbox SET status='pending', attempts=0, owner=NULL, "
                "lease_expires_at=NULL, last_error=NULL, next_attempt_at=?, "
                "updated_at=? WHERE id=?",
                (now, now, delivery_id),
            )
            self._audit(
                tenant_id, admin_token[:8] + "…", "delivery.replayed",
                "outbox", delivery_id, None, con,
            )
            delivery_row = con.execute(
                "SELECT * FROM outbox WHERE id=?", (delivery_id,)
            ).fetchone()
            history = self._fetch_history(delivery_id, con)
        return self._row_to_delivery(delivery_row, history)

    def list_deliveries(self, token: str, **filters: Any) -> list[Delivery]:
        with self._txn() as con:
            tenant_id, _ = self._authenticate(token, con)
            sql = "SELECT * FROM outbox WHERE tenant_id=?"
            params: list = [tenant_id]
            if "status" in filters and filters["status"] is not None:
                sql += " AND status=?"
                params.append(filters["status"])
            if "delivery_type" in filters and filters["delivery_type"] is not None:
                sql += " AND delivery_type=?"
                params.append(filters["delivery_type"])
            sql += " ORDER BY created_at DESC"
            rows = con.execute(sql, params).fetchall()
            result = []
            for r in rows:
                history = self._fetch_history(r["id"], con)
                result.append(self._row_to_delivery(r, history))
        return result

    # ------------------------------------------------------------------
    # Snapshot export / import
    # ------------------------------------------------------------------

    def export_snapshot(self, admin_token: str) -> dict:
        """Export all tenant data as a JSON-serializable dict."""
        with self._txn() as con:
            tenant_id, role = self._authenticate(admin_token, con)
            self._authorize(role, ADMIN_ROLES, "export_snapshot")

            tenant = dict(
                con.execute(
                    "SELECT * FROM tenants WHERE id=?", (tenant_id,)
                ).fetchone()
            )
            credentials = [
                dict(r)
                for r in con.execute(
                    "SELECT * FROM credentials WHERE tenant_id=?", (tenant_id,)
                ).fetchall()
            ]
            shipments = [
                dict(r)
                for r in con.execute(
                    "SELECT * FROM shipments WHERE tenant_id=?", (tenant_id,)
                ).fetchall()
            ]
            ship_ids = [s["id"] for s in shipments]
            events: list[dict] = []
            exceptions: list[dict] = []
            notes: list[dict] = []
            if ship_ids:
                placeholders = ",".join("?" * len(ship_ids))
                events = [
                    dict(r)
                    for r in con.execute(
                        f"SELECT * FROM carrier_events WHERE shipment_id IN ({placeholders})",
                        ship_ids,
                    ).fetchall()
                ]
                exc_rows = con.execute(
                    f"SELECT * FROM exceptions WHERE shipment_id IN ({placeholders})",
                    ship_ids,
                ).fetchall()
                exc_ids = [r["id"] for r in exc_rows]
                exceptions = [dict(r) for r in exc_rows]
                if exc_ids:
                    ep = ",".join("?" * len(exc_ids))
                    notes = [
                        dict(r)
                        for r in con.execute(
                            f"SELECT * FROM exception_notes WHERE exception_id IN ({ep})",
                            exc_ids,
                        ).fetchall()
                    ]
            audit_entries = [
                dict(r)
                for r in con.execute(
                    "SELECT * FROM audit WHERE tenant_id=?", (tenant_id,)
                ).fetchall()
            ]
            sla_rules = [
                dict(r)
                for r in con.execute(
                    "SELECT * FROM sla_rules WHERE tenant_id=?", (tenant_id,)
                ).fetchall()
            ]
            outbox_rows = [
                dict(r)
                for r in con.execute(
                    "SELECT * FROM outbox WHERE tenant_id=?", (tenant_id,)
                ).fetchall()
            ]
            ob_ids = [o["id"] for o in outbox_rows]
            outbox_history: list[dict] = []
            if ob_ids:
                hp = ",".join("?" * len(ob_ids))
                outbox_history = [
                    dict(r)
                    for r in con.execute(
                        f"SELECT * FROM outbox_history WHERE outbox_id IN ({hp})",
                        ob_ids,
                    ).fetchall()
                ]

        return {
            "version": SCHEMA_VERSION,
            "tenant": tenant,
            "credentials": credentials,
            "shipments": shipments,
            "carrier_events": events,
            "exceptions": exceptions,
            "exception_notes": notes,
            "audit": audit_entries,
            "sla_rules": sla_rules,
            "outbox": outbox_rows,
            "outbox_history": outbox_history,
        }

    def import_snapshot(self, admin_token: str, snapshot: dict) -> None:
        """
        Atomically restore a tenant snapshot.  Only data belonging to the
        admin's own tenant is replaced; other tenants are untouched.
        """
        with self._txn() as con:
            tenant_id, role = self._authenticate(admin_token, con)
            self._authorize(role, ADMIN_ROLES, "import_snapshot")

            snap_tenant = snapshot.get("tenant", {})
            if snap_tenant.get("id") != tenant_id:
                raise ValidationError(
                    "Snapshot tenant does not match the admin credential's tenant"
                )

            # Delete existing tenant data (order matters for FK constraints)
            ship_ids_row = con.execute(
                "SELECT id FROM shipments WHERE tenant_id=?", (tenant_id,)
            ).fetchall()
            ship_ids = [r["id"] for r in ship_ids_row]
            if ship_ids:
                ph = ",".join("?" * len(ship_ids))
                exc_ids_row = con.execute(
                    f"SELECT id FROM exceptions WHERE shipment_id IN ({ph})",
                    ship_ids,
                ).fetchall()
                exc_ids = [r["id"] for r in exc_ids_row]
                if exc_ids:
                    ep = ",".join("?" * len(exc_ids))
                    con.execute(
                        f"DELETE FROM exception_notes WHERE exception_id IN ({ep})",
                        exc_ids,
                    )
                con.execute(
                    f"DELETE FROM exceptions WHERE shipment_id IN ({ph})", ship_ids
                )
                con.execute(
                    f"DELETE FROM carrier_events WHERE shipment_id IN ({ph})", ship_ids
                )
            # Outbox
            ob_ids_row = con.execute(
                "SELECT id FROM outbox WHERE tenant_id=?", (tenant_id,)
            ).fetchall()
            ob_ids = [r["id"] for r in ob_ids_row]
            if ob_ids:
                hp = ",".join("?" * len(ob_ids))
                con.execute(
                    f"DELETE FROM outbox_history WHERE outbox_id IN ({hp})", ob_ids
                )
            con.execute("DELETE FROM outbox WHERE tenant_id=?", (tenant_id,))
            con.execute("DELETE FROM audit WHERE tenant_id=?", (tenant_id,))
            con.execute("DELETE FROM sla_rules WHERE tenant_id=?", (tenant_id,))
            con.execute("DELETE FROM shipments WHERE tenant_id=?", (tenant_id,))
            con.execute("DELETE FROM credentials WHERE tenant_id=?", (tenant_id,))
            con.execute("DELETE FROM tenants WHERE id=?", (tenant_id,))

            # Re-insert
            t = snapshot["tenant"]
            con.execute(
                "INSERT INTO tenants (id, name, created_at) VALUES (?, ?, ?)",
                (t["id"], t["name"], t["created_at"]),
            )
            for c in snapshot.get("credentials", []):
                con.execute(
                    "INSERT INTO credentials (token_hash, tenant_id, role, created_at) "
                    "VALUES (?, ?, ?, ?)",
                    (c["token_hash"], c["tenant_id"], c["role"], c["created_at"]),
                )
            for s in snapshot.get("shipments", []):
                con.execute(
                    "INSERT INTO shipments "
                    "(id, tenant_id, reference, status, last_location, version, "
                    " created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (s["id"], s["tenant_id"], s["reference"], s["status"],
                     s.get("last_location"), s["version"],
                     s["created_at"], s["updated_at"]),
                )
            for ev in snapshot.get("carrier_events", []):
                con.execute(
                    "INSERT INTO carrier_events "
                    "(row_id, tenant_id, shipment_id, event_id, event_type, "
                    " event_time, location, details, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (ev.get("row_id"), ev["tenant_id"], ev["shipment_id"],
                     ev["event_id"], ev["event_type"], ev["event_time"],
                     ev.get("location"), ev.get("details"), ev["created_at"]),
                )
            for ex in snapshot.get("exceptions", []):
                con.execute(
                    "INSERT INTO exceptions "
                    "(id, tenant_id, shipment_id, epoch, status, severity, assignee, "
                    " version, opened_at, resolved_at, escalation_enqueued) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (ex["id"], ex["tenant_id"], ex["shipment_id"], ex["epoch"],
                     ex["status"], ex["severity"], ex.get("assignee"),
                     ex["version"], ex["opened_at"], ex.get("resolved_at"),
                     ex["escalation_enqueued"]),
                )
            for n in snapshot.get("exception_notes", []):
                con.execute(
                    "INSERT INTO exception_notes "
                    "(id, exception_id, actor, note, created_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (n["id"], n["exception_id"], n["actor"], n["note"], n["created_at"]),
                )
            for a in snapshot.get("audit", []):
                con.execute(
                    "INSERT INTO audit "
                    "(id, tenant_id, actor, action, resource_type, resource_id, "
                    " details, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (a["id"], a["tenant_id"], a.get("actor"), a["action"],
                     a.get("resource_type"), a.get("resource_id"),
                     a.get("details"), a["created_at"]),
                )
            for sr in snapshot.get("sla_rules", []):
                con.execute(
                    "INSERT INTO sla_rules "
                    "(tenant_id, severity, delay_seconds, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (sr["tenant_id"], sr["severity"], sr["delay_seconds"],
                     sr["created_at"], sr["updated_at"]),
                )
            for ob in snapshot.get("outbox", []):
                con.execute(
                    "INSERT INTO outbox "
                    "(id, tenant_id, delivery_type, payload, idempotency_key, status, "
                    " attempts, max_attempts, owner, lease_expires_at, next_attempt_at, "
                    " last_error, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (ob["id"], ob["tenant_id"], ob["delivery_type"], ob["payload"],
                     ob["idempotency_key"], ob["status"], ob["attempts"],
                     ob["max_attempts"], ob.get("owner"), ob.get("lease_expires_at"),
                     ob["next_attempt_at"], ob.get("last_error"),
                     ob["created_at"], ob["updated_at"]),
                )
            for h in snapshot.get("outbox_history", []):
                con.execute(
                    "INSERT INTO outbox_history "
                    "(id, outbox_id, attempt, status, error, worker_id, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (h["id"], h["outbox_id"], h["attempt"], h["status"],
                     h.get("error"), h.get("worker_id"), h["created_at"]),
                )
