"""SQLite-backed durable store for the freight control tower.

All state is stored in a single SQLite database.  Two instances opened on
the same database file see each other's committed writes.  WAL mode is used
so concurrent readers do not block writers.

The public API surface matches the contract specified in the README:

    store = SQLiteFreightStore("freight.db")
    store.bootstrap_tenant("acme", "ACME Corp", "secret-admin-token")
    store.create_credential("secret-admin-token", "viewer-token", "viewer")
    ship = store.create_shipment("secret-admin-token", "ACME-001")
    ...

Callers should call ``SQLiteFreightStore.init_schema()`` (or the class method
``SQLiteFreightStore.open(path)`` which calls it automatically) before using
any instance.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .exceptions import (
    AuthenticationError,
    AuthorizationError,
    IdempotencyConflictError,
    NotFoundError,
    StaleVersionError,
    ValidationError,
)

# ---------------------------------------------------------------------------
# Domain dataclasses
# ---------------------------------------------------------------------------

VALID_ROLES = {"viewer", "operator", "admin"}
VALID_EVENT_TYPES = {"picked_up", "in_transit", "delayed", "delivered", "cancelled"}
VALID_EXCEPTION_ACTIONS = {"assign", "acknowledge", "note", "resolve"}
VALID_SEVERITIES = {"P1", "P2", "P3"}

# Mapping from event_type to shipment status
EVENT_TO_STATUS = {
    "picked_up": "picked_up",
    "in_transit": "in_transit",
    "delayed": "delayed",
    "delivered": "delivered",
    "cancelled": "cancelled",
}

# Statuses that close an exception
CLOSING_STATUSES = {"delivered", "cancelled"}


@dataclass
class Tenant:
    id: str
    name: str
    created_at: float


@dataclass
class Credential:
    id: str
    tenant_id: str
    role: str
    created_at: float


@dataclass
class Shipment:
    id: str
    tenant_id: str
    reference: str
    status: str
    last_location: str | None
    created_at: float
    updated_at: float
    version: int
    active_exception_id: str | None = None


@dataclass
class CarrierEvent:
    id: str
    shipment_id: str
    tenant_id: str
    event_id: str
    event_type: str
    event_time: float
    location: str | None
    details: str | None
    received_at: float


@dataclass
class ShipmentException:
    id: str
    shipment_id: str
    tenant_id: str
    status: str           # open | acknowledged | resolved
    severity: str
    opened_at: float
    resolved_at: float | None
    assigned_to: str | None
    notes: str
    version: int
    escalated: bool = False


@dataclass
class AuditEntry:
    id: str
    tenant_id: str
    actor: str
    action: str
    resource_type: str
    resource_id: str
    meta: dict
    created_at: float


@dataclass
class Delivery:
    id: str
    tenant_id: str
    idempotency_key: str
    payload: dict
    status: str           # pending | claimed | delivered | failed | dead
    owner: str | None
    lease_expires_at: float | None
    attempts: int
    last_error: str | None
    next_attempt_at: float
    created_at: float
    updated_at: float


# ---------------------------------------------------------------------------
# Schema DDL
# ---------------------------------------------------------------------------

_SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS tenants (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    created_at  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS credentials (
    id          TEXT PRIMARY KEY,
    tenant_id   TEXT NOT NULL REFERENCES tenants(id),
    token_hash  TEXT NOT NULL UNIQUE,
    role        TEXT NOT NULL,
    created_at  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS shipments (
    id                   TEXT PRIMARY KEY,
    tenant_id            TEXT NOT NULL REFERENCES tenants(id),
    reference            TEXT NOT NULL,
    status               TEXT NOT NULL DEFAULT 'created',
    last_location        TEXT,
    created_at           REAL NOT NULL,
    updated_at           REAL NOT NULL,
    version              INTEGER NOT NULL DEFAULT 1,
    active_exception_id  TEXT,
    UNIQUE(tenant_id, reference)
);

CREATE TABLE IF NOT EXISTS carrier_events (
    id           TEXT PRIMARY KEY,
    shipment_id  TEXT NOT NULL REFERENCES shipments(id),
    tenant_id    TEXT NOT NULL,
    event_id     TEXT NOT NULL UNIQUE,
    event_type   TEXT NOT NULL,
    event_time   REAL NOT NULL,
    location     TEXT,
    details      TEXT,
    received_at  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS exceptions (
    id           TEXT PRIMARY KEY,
    shipment_id  TEXT NOT NULL REFERENCES shipments(id),
    tenant_id    TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'open',
    severity     TEXT NOT NULL DEFAULT 'P2',
    opened_at    REAL NOT NULL,
    resolved_at  REAL,
    assigned_to  TEXT,
    notes        TEXT NOT NULL DEFAULT '',
    version      INTEGER NOT NULL DEFAULT 1,
    escalated    INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS sla_rules (
    id             TEXT PRIMARY KEY,
    tenant_id      TEXT NOT NULL REFERENCES tenants(id),
    severity       TEXT NOT NULL,
    delay_seconds  REAL NOT NULL,
    created_at     REAL NOT NULL,
    UNIQUE(tenant_id, severity)
);

CREATE TABLE IF NOT EXISTS audit_log (
    id            TEXT PRIMARY KEY,
    tenant_id     TEXT NOT NULL,
    actor         TEXT NOT NULL,
    action        TEXT NOT NULL,
    resource_type TEXT NOT NULL,
    resource_id   TEXT NOT NULL,
    meta          TEXT NOT NULL DEFAULT '{}',
    created_at    REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS deliveries (
    id                TEXT PRIMARY KEY,
    tenant_id         TEXT NOT NULL,
    idempotency_key   TEXT NOT NULL UNIQUE,
    payload           TEXT NOT NULL,
    status            TEXT NOT NULL DEFAULT 'pending',
    owner             TEXT,
    lease_expires_at  REAL,
    attempts          INTEGER NOT NULL DEFAULT 0,
    last_error        TEXT,
    next_attempt_at   REAL NOT NULL,
    created_at        REAL NOT NULL,
    updated_at        REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_shipments_tenant    ON shipments(tenant_id);
CREATE INDEX IF NOT EXISTS idx_exceptions_tenant   ON exceptions(tenant_id);
CREATE INDEX IF NOT EXISTS idx_exceptions_shipment ON exceptions(shipment_id);
CREATE INDEX IF NOT EXISTS idx_audit_tenant        ON audit_log(tenant_id);
CREATE INDEX IF NOT EXISTS idx_deliveries_tenant   ON deliveries(tenant_id);
CREATE INDEX IF NOT EXISTS idx_deliveries_status   ON deliveries(status, next_attempt_at);
"""


# ---------------------------------------------------------------------------
# Helper utilities
# ---------------------------------------------------------------------------

def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _new_id() -> str:
    return str(uuid.uuid4())


def _row_to_shipment(row: dict) -> Shipment:
    return Shipment(
        id=row["id"],
        tenant_id=row["tenant_id"],
        reference=row["reference"],
        status=row["status"],
        last_location=row["last_location"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        version=row["version"],
        active_exception_id=row["active_exception_id"],
    )


def _row_to_exception(row: dict) -> ShipmentException:
    return ShipmentException(
        id=row["id"],
        shipment_id=row["shipment_id"],
        tenant_id=row["tenant_id"],
        status=row["status"],
        severity=row["severity"],
        opened_at=row["opened_at"],
        resolved_at=row["resolved_at"],
        assigned_to=row["assigned_to"],
        notes=row["notes"],
        version=row["version"],
        escalated=bool(row["escalated"]),
    )


def _row_to_delivery(row: dict) -> Delivery:
    return Delivery(
        id=row["id"],
        tenant_id=row["tenant_id"],
        idempotency_key=row["idempotency_key"],
        payload=json.loads(row["payload"]),
        status=row["status"],
        owner=row["owner"],
        lease_expires_at=row["lease_expires_at"],
        attempts=row["attempts"],
        last_error=row["last_error"],
        next_attempt_at=row["next_attempt_at"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _row_to_audit(row: dict) -> AuditEntry:
    return AuditEntry(
        id=row["id"],
        tenant_id=row["tenant_id"],
        actor=row["actor"],
        action=row["action"],
        resource_type=row["resource_type"],
        resource_id=row["resource_id"],
        meta=json.loads(row["meta"]),
        created_at=row["created_at"],
    )


# ---------------------------------------------------------------------------
# Main store
# ---------------------------------------------------------------------------

class SQLiteFreightStore:
    """Durable, multi-tenant freight operations store backed by SQLite."""

    def __init__(
        self,
        db_path: str,
        clock: Callable[[], float] | None = None,
        lease_seconds: float = 60.0,
        max_attempts: int = 5,
    ) -> None:
        self._db_path = db_path
        self._clock = clock or time.time
        self._lease_seconds = lease_seconds
        self._max_attempts = max_attempts
        self._conn: sqlite3.Connection | None = None

    # ------------------------------------------------------------------
    # Connection / schema
    # ------------------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        if self._conn is None:
            conn = sqlite3.connect(self._db_path, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            self._conn = conn
        return self._conn

    def init_schema(self) -> None:
        """Create tables and indexes if they do not already exist."""
        conn = self._connect()
        conn.executescript(_SCHEMA)
        conn.commit()

    def close(self) -> None:
        if self._conn:
            self._conn.close()
            self._conn = None

    # ------------------------------------------------------------------
    # Auth helpers
    # ------------------------------------------------------------------

    def _resolve_credential(self, token: str) -> dict:
        """Return the credential row or raise AuthenticationError."""
        conn = self._connect()
        h = _hash_token(token)
        row = conn.execute(
            "SELECT c.id, c.tenant_id, c.role, t.name as tenant_name "
            "FROM credentials c JOIN tenants t ON t.id = c.tenant_id "
            "WHERE c.token_hash = ?",
            (h,),
        ).fetchone()
        if not row:
            raise AuthenticationError("Invalid token")
        return dict(row)

    def _require_role(self, token: str, *roles: str) -> dict:
        cred = self._resolve_credential(token)
        if cred["role"] not in roles:
            raise AuthorizationError(
                f"Role '{cred['role']}' cannot perform this operation (requires one of {roles})"
            )
        return cred

    def _require_admin(self, token: str) -> dict:
        return self._require_role(token, "admin")

    def _require_operator_or_admin(self, token: str) -> dict:
        return self._require_role(token, "operator", "admin")

    # ------------------------------------------------------------------
    # Tenant / credential management
    # ------------------------------------------------------------------

    def bootstrap_tenant(self, tenant_id: str, name: str, admin_token: str) -> Tenant:
        """Create a new tenant with an initial admin credential.

        Idempotent: if tenant_id already exists with matching name, returns the
        existing tenant without error.
        """
        if not tenant_id.strip():
            raise ValidationError("tenant_id is required")
        if not name.strip():
            raise ValidationError("name is required")
        if not admin_token.strip():
            raise ValidationError("admin_token is required")

        conn = self._connect()
        now = self._clock()
        tid = tenant_id.strip()
        h = _hash_token(admin_token)

        with conn:
            # Check existing tenant
            existing = conn.execute(
                "SELECT * FROM tenants WHERE id = ?", (tid,)
            ).fetchone()
            if existing:
                # idempotent: must not conflict
                existing_cred = conn.execute(
                    "SELECT token_hash FROM credentials WHERE tenant_id = ? AND role = 'admin'",
                    (tid,),
                ).fetchone()
                if existing_cred and existing_cred["token_hash"] != h:
                    raise ValidationError(f"Tenant '{tid}' already exists with a different admin token")
                return Tenant(id=existing["id"], name=existing["name"], created_at=existing["created_at"])

            # Check token uniqueness across tenants
            conflict = conn.execute(
                "SELECT id FROM credentials WHERE token_hash = ?", (h,)
            ).fetchone()
            if conflict:
                raise ValidationError("Token is already in use")

            conn.execute(
                "INSERT INTO tenants(id, name, created_at) VALUES (?, ?, ?)",
                (tid, name.strip(), now),
            )
            cred_id = _new_id()
            conn.execute(
                "INSERT INTO credentials(id, tenant_id, token_hash, role, created_at) VALUES (?, ?, ?, 'admin', ?)",
                (cred_id, tid, h, now),
            )

        self._audit(
            conn, tid, f"system/bootstrap", "bootstrap_tenant", "tenant", tid, {}
        )
        return Tenant(id=tid, name=name.strip(), created_at=now)

    def create_credential(self, admin_token: str, token: str, role: str) -> Credential:
        """Create a new credential in the admin's tenant."""
        cred = self._require_admin(admin_token)
        if not token.strip():
            raise ValidationError("token is required")
        if role not in VALID_ROLES:
            raise ValidationError(f"role must be one of {VALID_ROLES}")

        conn = self._connect()
        h = _hash_token(token)
        now = self._clock()
        tid = cred["tenant_id"]

        with conn:
            conflict = conn.execute(
                "SELECT id FROM credentials WHERE token_hash = ?", (h,)
            ).fetchone()
            if conflict:
                raise ValidationError("Token is already in use")
            cid = _new_id()
            conn.execute(
                "INSERT INTO credentials(id, tenant_id, token_hash, role, created_at) VALUES (?, ?, ?, ?, ?)",
                (cid, tid, h, role, now),
            )

        self._audit(conn, tid, cred["id"], "create_credential", "credential", cid, {"role": role})
        return Credential(id=cid, tenant_id=tid, role=role, created_at=now)

    # ------------------------------------------------------------------
    # Shipments
    # ------------------------------------------------------------------

    def create_shipment(self, token: str, reference: str) -> Shipment:
        """Create a new shipment in the caller's tenant."""
        cred = self._require_operator_or_admin(token)
        if not reference.strip():
            raise ValidationError("reference is required")

        conn = self._connect()
        tid = cred["tenant_id"]
        now = self._clock()
        sid = _new_id()

        with conn:
            existing = conn.execute(
                "SELECT * FROM shipments WHERE tenant_id = ? AND reference = ?",
                (tid, reference.strip()),
            ).fetchone()
            if existing:
                return _row_to_shipment(dict(existing))
            conn.execute(
                "INSERT INTO shipments(id, tenant_id, reference, status, last_location, created_at, updated_at, version) "
                "VALUES (?, ?, ?, 'created', NULL, ?, ?, 1)",
                (sid, tid, reference.strip(), now, now),
            )
            self._enqueue_delivery(
                conn,
                tid,
                f"shipment.created:{sid}",
                {"event": "shipment.created", "shipment_id": sid, "tenant_id": tid, "reference": reference.strip()},
                now,
            )

        self._audit(conn, tid, cred["id"], "create_shipment", "shipment", sid, {"reference": reference.strip()})
        return Shipment(
            id=sid,
            tenant_id=tid,
            reference=reference.strip(),
            status="created",
            last_location=None,
            created_at=now,
            updated_at=now,
            version=1,
        )

    def get_shipment(self, token: str, shipment_id: str) -> Shipment:
        cred = self._resolve_credential(token)
        conn = self._connect()
        row = conn.execute(
            "SELECT * FROM shipments WHERE id = ? AND tenant_id = ?",
            (shipment_id, cred["tenant_id"]),
        ).fetchone()
        if not row:
            raise NotFoundError(f"Shipment '{shipment_id}' not found")
        return _row_to_shipment(dict(row))

    def list_shipments(self, token: str, **filters: Any) -> list[Shipment]:
        cred = self._resolve_credential(token)
        conn = self._connect()
        tid = cred["tenant_id"]
        sql = "SELECT * FROM shipments WHERE tenant_id = ?"
        params: list[Any] = [tid]
        if "status" in filters and filters["status"]:
            sql += " AND status = ?"
            params.append(filters["status"])
        sql += " ORDER BY created_at ASC"
        rows = conn.execute(sql, params).fetchall()
        return [_row_to_shipment(dict(r)) for r in rows]

    # ------------------------------------------------------------------
    # Carrier event ingestion and projection
    # ------------------------------------------------------------------

    def ingest_event(
        self,
        token: str,
        shipment_id: str,
        event_id: str,
        event_type: str,
        event_time: float,
        location: str | None = None,
        details: str | None = None,
    ) -> Shipment:
        """Ingest a carrier event and return the updated shipment projection."""
        cred = self._require_operator_or_admin(token)
        tid = cred["tenant_id"]

        if not event_id.strip():
            raise ValidationError("event_id is required")
        if event_type not in VALID_EVENT_TYPES:
            raise ValidationError(f"event_type must be one of {VALID_EVENT_TYPES}")

        conn = self._connect()

        # Verify shipment belongs to this tenant
        ship_row = conn.execute(
            "SELECT * FROM shipments WHERE id = ? AND tenant_id = ?",
            (shipment_id, tid),
        ).fetchone()
        if not ship_row:
            raise NotFoundError(f"Shipment '{shipment_id}' not found")

        now = self._clock()

        # Idempotency check
        existing_event = conn.execute(
            "SELECT * FROM carrier_events WHERE event_id = ?", (event_id.strip(),)
        ).fetchone()
        if existing_event:
            if (
                existing_event["event_type"] != event_type
                or existing_event["shipment_id"] != shipment_id
                or (location is not None and existing_event["location"] != location)
                or (details is not None and existing_event["details"] != details)
            ):
                raise IdempotencyConflictError(
                    f"Event id '{event_id}' already exists with a conflicting payload"
                )
            # Return current projection unchanged
            return _row_to_shipment(dict(conn.execute(
                "SELECT * FROM shipments WHERE id = ?", (shipment_id,)
            ).fetchone()))

        # Insert the event
        eid = _new_id()
        with conn:
            try:
                conn.execute(
                    "INSERT INTO carrier_events(id, shipment_id, tenant_id, event_id, event_type, event_time, location, details, received_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (eid, shipment_id, tid, event_id.strip(), event_type, event_time, location, details, now),
                )
            except sqlite3.IntegrityError:
                # Race: another instance inserted the same event_id
                existing_event = conn.execute(
                    "SELECT * FROM carrier_events WHERE event_id = ?", (event_id.strip(),)
                ).fetchone()
                if existing_event and (
                    existing_event["event_type"] != event_type
                    or existing_event["shipment_id"] != shipment_id
                ):
                    raise IdempotencyConflictError(
                        f"Event id '{event_id}' already exists with a conflicting payload"
                    )
                return _row_to_shipment(dict(conn.execute(
                    "SELECT * FROM shipments WHERE id = ?", (shipment_id,)
                ).fetchone()))

            # Reproject the shipment from all events ordered by (event_time, event_id)
            events = conn.execute(
                "SELECT * FROM carrier_events WHERE shipment_id = ? ORDER BY event_time ASC, event_id ASC",
                (shipment_id,),
            ).fetchall()

            new_status, new_location = _project_shipment(events)

            # Determine exception changes
            current = dict(ship_row)
            old_status = current["status"]
            old_exc_id = current["active_exception_id"]

            new_exc_id = old_exc_id
            version_bump = current["version"] + 1

            if new_status == "delayed" and old_exc_id is None:
                # Open a new exception
                exc_id = _new_id()
                # Determine severity from SLA rules
                severity = self._get_severity_for_tenant(conn, tid)
                conn.execute(
                    "INSERT INTO exceptions(id, shipment_id, tenant_id, status, severity, opened_at, version) "
                    "VALUES (?, ?, ?, 'open', ?, ?, 1)",
                    (exc_id, shipment_id, tid, severity, now),
                )
                new_exc_id = exc_id
                self._enqueue_delivery(
                    conn,
                    tid,
                    f"exception.opened:{exc_id}",
                    {"event": "exception.opened", "exception_id": exc_id, "shipment_id": shipment_id, "tenant_id": tid},
                    now,
                )
                self._audit_in_txn(conn, tid, cred["id"], "exception_opened", "exception", exc_id,
                                   {"shipment_id": shipment_id, "event_type": event_type})

            elif new_status in CLOSING_STATUSES and old_exc_id:
                # Resolve existing exception
                conn.execute(
                    "UPDATE exceptions SET status = 'resolved', resolved_at = ?, version = version + 1 "
                    "WHERE id = ?",
                    (now, old_exc_id),
                )
                new_exc_id = None
                self._enqueue_delivery(
                    conn,
                    tid,
                    f"exception.resolved:{old_exc_id}:{event_type}",
                    {"event": "exception.resolved", "exception_id": old_exc_id, "shipment_id": shipment_id, "tenant_id": tid, "reason": event_type},
                    now,
                )
                self._audit_in_txn(conn, tid, cred["id"], "exception_resolved", "exception", old_exc_id,
                                   {"shipment_id": shipment_id, "event_type": event_type})

            # Handle reopen: if we had previously resolved and now get another delay
            elif new_status == "delayed" and old_exc_id is None and old_status in CLOSING_STATUSES:
                exc_id = _new_id()
                severity = self._get_severity_for_tenant(conn, tid)
                conn.execute(
                    "INSERT INTO exceptions(id, shipment_id, tenant_id, status, severity, opened_at, version) "
                    "VALUES (?, ?, ?, 'open', ?, ?, 1)",
                    (exc_id, shipment_id, tid, severity, now),
                )
                new_exc_id = exc_id

            conn.execute(
                "UPDATE shipments SET status = ?, last_location = ?, updated_at = ?, version = ?, active_exception_id = ? "
                "WHERE id = ?",
                (new_status, new_location, now, version_bump, new_exc_id, shipment_id),
            )

            self._enqueue_delivery(
                conn,
                tid,
                f"carrier_event:{event_id}",
                {"event": "carrier_event.ingested", "event_id": event_id, "event_type": event_type, "shipment_id": shipment_id, "tenant_id": tid},
                now,
            )

        self._audit(conn, tid, cred["id"], "ingest_event", "carrier_event", eid,
                    {"event_id": event_id, "event_type": event_type, "shipment_id": shipment_id})

        row = conn.execute("SELECT * FROM shipments WHERE id = ?", (shipment_id,)).fetchone()
        return _row_to_shipment(dict(row))

    def _get_severity_for_tenant(self, conn: sqlite3.Connection, tenant_id: str) -> str:
        """Return the lowest-priority SLA severity or P2 as default."""
        rule = conn.execute(
            "SELECT severity FROM sla_rules WHERE tenant_id = ? ORDER BY delay_seconds ASC LIMIT 1",
            (tenant_id,),
        ).fetchone()
        return rule["severity"] if rule else "P2"

    # ------------------------------------------------------------------
    # Exceptions
    # ------------------------------------------------------------------

    def list_exceptions(self, token: str, **filters: Any) -> list[ShipmentException]:
        cred = self._resolve_credential(token)
        conn = self._connect()
        tid = cred["tenant_id"]
        sql = "SELECT * FROM exceptions WHERE tenant_id = ?"
        params: list[Any] = [tid]
        if "status" in filters and filters["status"]:
            sql += " AND status = ?"
            params.append(filters["status"])
        if "severity" in filters and filters["severity"]:
            sql += " AND severity = ?"
            params.append(filters["severity"])
        if "assigned_to" in filters and filters["assigned_to"]:
            sql += " AND assigned_to = ?"
            params.append(filters["assigned_to"])
        if "shipment_id" in filters and filters["shipment_id"]:
            sql += " AND shipment_id = ?"
            params.append(filters["shipment_id"])
        sql += " ORDER BY opened_at ASC"
        rows = conn.execute(sql, params).fetchall()
        return [_row_to_exception(dict(r)) for r in rows]

    def mutate_exception(
        self,
        token: str,
        exception_id: str,
        expected_version: int,
        action: str,
        actor: str | None = None,
        **values: Any,
    ) -> ShipmentException:
        """Apply an action to an exception with optimistic concurrency."""
        cred = self._require_operator_or_admin(token)
        tid = cred["tenant_id"]

        if action not in VALID_EXCEPTION_ACTIONS:
            raise ValidationError(f"action must be one of {VALID_EXCEPTION_ACTIONS}")

        conn = self._connect()

        with conn:
            row = conn.execute(
                "SELECT * FROM exceptions WHERE id = ? AND tenant_id = ?",
                (exception_id, tid),
            ).fetchone()
            if not row:
                raise NotFoundError(f"Exception '{exception_id}' not found")

            exc = _row_to_exception(dict(row))
            if exc.version != expected_version:
                raise StaleVersionError(
                    f"Version conflict: expected {expected_version}, current {exc.version}"
                )

            now = self._clock()
            actor_id = actor or cred["id"]

            if action == "assign":
                assignee = values.get("assignee")
                if not assignee:
                    raise ValidationError("assignee is required for assign action")
                conn.execute(
                    "UPDATE exceptions SET assigned_to = ?, version = version + 1 WHERE id = ?",
                    (assignee, exception_id),
                )

            elif action == "acknowledge":
                if exc.status == "resolved":
                    raise ValidationError("Cannot acknowledge a resolved exception")
                conn.execute(
                    "UPDATE exceptions SET status = 'acknowledged', version = version + 1 WHERE id = ?",
                    (exception_id,),
                )

            elif action == "note":
                note_text = values.get("note", "")
                if not note_text:
                    raise ValidationError("note text is required")
                conn.execute(
                    "UPDATE exceptions SET notes = notes || ?, version = version + 1 WHERE id = ?",
                    (f"\n[{actor_id}] {note_text}", exception_id),
                )

            elif action == "resolve":
                if exc.status == "resolved":
                    raise ValidationError("Exception is already resolved")
                conn.execute(
                    "UPDATE exceptions SET status = 'resolved', resolved_at = ?, version = version + 1 WHERE id = ?",
                    (now, exception_id),
                )
                # Clear from shipment
                conn.execute(
                    "UPDATE shipments SET active_exception_id = NULL WHERE active_exception_id = ?",
                    (exception_id,),
                )
                self._enqueue_delivery(
                    conn,
                    tid,
                    f"exception.resolved:operator:{exception_id}:{now}",
                    {"event": "exception.resolved", "exception_id": exception_id, "tenant_id": tid, "actor": actor_id},
                    now,
                )

            self._audit_in_txn(
                conn, tid, actor_id, f"exception_{action}", "exception", exception_id,
                {"action": action, "expected_version": expected_version, **{k: str(v) for k, v in values.items()}}
            )

        row = conn.execute("SELECT * FROM exceptions WHERE id = ?", (exception_id,)).fetchone()
        return _row_to_exception(dict(row))

    # ------------------------------------------------------------------
    # Audit
    # ------------------------------------------------------------------

    def audit(self, token: str, **filters: Any) -> list[AuditEntry]:
        cred = self._resolve_credential(token)
        conn = self._connect()
        tid = cred["tenant_id"]
        sql = "SELECT * FROM audit_log WHERE tenant_id = ?"
        params: list[Any] = [tid]
        if "resource_type" in filters and filters["resource_type"]:
            sql += " AND resource_type = ?"
            params.append(filters["resource_type"])
        if "resource_id" in filters and filters["resource_id"]:
            sql += " AND resource_id = ?"
            params.append(filters["resource_id"])
        if "action" in filters and filters["action"]:
            sql += " AND action = ?"
            params.append(filters["action"])
        sql += " ORDER BY created_at ASC"
        rows = conn.execute(sql, params).fetchall()
        return [_row_to_audit(dict(r)) for r in rows]

    def _audit(
        self,
        conn: sqlite3.Connection,
        tenant_id: str,
        actor: str,
        action: str,
        resource_type: str,
        resource_id: str,
        meta: dict,
    ) -> None:
        """Write an audit entry in its own transaction."""
        with conn:
            self._audit_in_txn(conn, tenant_id, actor, action, resource_type, resource_id, meta)

    def _audit_in_txn(
        self,
        conn: sqlite3.Connection,
        tenant_id: str,
        actor: str,
        action: str,
        resource_type: str,
        resource_id: str,
        meta: dict,
    ) -> None:
        """Write an audit entry within the current transaction."""
        aid = _new_id()
        now = self._clock()
        conn.execute(
            "INSERT INTO audit_log(id, tenant_id, actor, action, resource_type, resource_id, meta, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (aid, tenant_id, actor, action, resource_type, resource_id, json.dumps(meta), now),
        )

    # ------------------------------------------------------------------
    # SLA rules and ticking
    # ------------------------------------------------------------------

    def set_sla_rule(self, admin_token: str, severity: str, delay_seconds: float) -> None:
        """Upsert an SLA escalation rule for the admin's tenant."""
        cred = self._require_admin(admin_token)
        if severity not in VALID_SEVERITIES:
            raise ValidationError(f"severity must be one of {VALID_SEVERITIES}")
        if delay_seconds <= 0:
            raise ValidationError("delay_seconds must be positive")

        conn = self._connect()
        tid = cred["tenant_id"]
        now = self._clock()
        with conn:
            conn.execute(
                "INSERT INTO sla_rules(id, tenant_id, severity, delay_seconds, created_at) VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(tenant_id, severity) DO UPDATE SET delay_seconds = excluded.delay_seconds",
                (_new_id(), tid, severity, delay_seconds, now),
            )
        self._audit(conn, tid, cred["id"], "set_sla_rule", "sla_rule", f"{tid}:{severity}",
                    {"severity": severity, "delay_seconds": delay_seconds})

    def tick(self, now: float, limit: int = 100) -> int:
        """Claim due escalations across all tenants.

        Returns the number of newly enqueued escalation deliveries.
        """
        conn = self._connect()
        count = 0

        # Find all open/unacknowledged exceptions that have SLA rules and haven't been escalated
        rows = conn.execute(
            """
            SELECT e.id, e.tenant_id, e.severity, e.opened_at, e.shipment_id
            FROM exceptions e
            JOIN sla_rules r ON r.tenant_id = e.tenant_id AND r.severity = e.severity
            WHERE e.status = 'open'
              AND e.escalated = 0
              AND (e.opened_at + r.delay_seconds) <= ?
            LIMIT ?
            """,
            (now, limit),
        ).fetchall()

        for row in rows:
            with conn:
                # Re-check under lock
                exc = conn.execute(
                    "SELECT status, escalated FROM exceptions WHERE id = ?", (row["id"],)
                ).fetchone()
                if not exc or exc["status"] != "open" or exc["escalated"]:
                    continue

                conn.execute(
                    "UPDATE exceptions SET escalated = 1 WHERE id = ?", (row["id"],)
                )
                idem_key = f"escalation:{row['id']}"
                self._enqueue_delivery(
                    conn,
                    row["tenant_id"],
                    idem_key,
                    {
                        "event": "exception.escalated",
                        "exception_id": row["id"],
                        "shipment_id": row["shipment_id"],
                        "tenant_id": row["tenant_id"],
                        "severity": row["severity"],
                    },
                    now,
                )
                count += 1

        return count

    # ------------------------------------------------------------------
    # Outbox / deliveries
    # ------------------------------------------------------------------

    def _enqueue_delivery(
        self,
        conn: sqlite3.Connection,
        tenant_id: str,
        idempotency_key: str,
        payload: dict,
        now: float,
    ) -> None:
        """Enqueue a delivery within the current transaction. Ignores duplicates."""
        try:
            conn.execute(
                "INSERT INTO deliveries(id, tenant_id, idempotency_key, payload, status, attempts, next_attempt_at, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, 'pending', 0, ?, ?, ?)",
                (_new_id(), tenant_id, idempotency_key, json.dumps(payload), now, now, now),
            )
        except sqlite3.IntegrityError:
            pass  # already enqueued

    def claim_delivery(self, worker_id: str, now: float) -> Delivery | None:
        """Claim the oldest due delivery for a worker.

        Returns the claimed delivery or None if nothing is available.
        Expired leases are reclaimed.
        """
        conn = self._connect()
        with conn:
            row = conn.execute(
                """
                SELECT * FROM deliveries
                WHERE status IN ('pending', 'claimed')
                  AND next_attempt_at <= ?
                  AND (status = 'pending' OR lease_expires_at <= ?)
                ORDER BY next_attempt_at ASC
                LIMIT 1
                """,
                (now, now),
            ).fetchone()
            if not row:
                return None
            lease_exp = now + self._lease_seconds
            conn.execute(
                "UPDATE deliveries SET status = 'claimed', owner = ?, lease_expires_at = ?, updated_at = ? WHERE id = ?",
                (worker_id, lease_exp, now, row["id"]),
            )
        final = conn.execute("SELECT * FROM deliveries WHERE id = ?", (row["id"],)).fetchone()
        if final is None:
            return None
        return _row_to_delivery(dict(final))

    def complete_delivery(self, delivery_id: str, worker_id: str, now: float) -> Delivery:
        """Mark a delivery as successfully delivered."""
        conn = self._connect()
        with conn:
            row = conn.execute(
                "SELECT * FROM deliveries WHERE id = ?", (delivery_id,)
            ).fetchone()
            if not row:
                raise NotFoundError(f"Delivery '{delivery_id}' not found")
            if row["owner"] != worker_id:
                raise AuthorizationError("Delivery not owned by this worker")
            conn.execute(
                "UPDATE deliveries SET status = 'delivered', updated_at = ?, lease_expires_at = NULL WHERE id = ?",
                (now, delivery_id),
            )
        row = conn.execute("SELECT * FROM deliveries WHERE id = ?", (delivery_id,)).fetchone()
        return _row_to_delivery(dict(row))

    def fail_delivery(
        self, delivery_id: str, worker_id: str, error: str, now: float
    ) -> Delivery:
        """Record a delivery failure. Applies retry schedule or dead-letters."""
        conn = self._connect()
        with conn:
            row = conn.execute(
                "SELECT * FROM deliveries WHERE id = ?", (delivery_id,)
            ).fetchone()
            if not row:
                raise NotFoundError(f"Delivery '{delivery_id}' not found")
            if row["owner"] != worker_id:
                raise AuthorizationError("Delivery not owned by this worker")

            attempts = row["attempts"] + 1
            if attempts >= self._max_attempts:
                conn.execute(
                    "UPDATE deliveries SET status = 'dead', attempts = ?, last_error = ?, updated_at = ?, owner = NULL, lease_expires_at = NULL WHERE id = ?",
                    (attempts, error, now, delivery_id),
                )
            else:
                # Exponential backoff: 2^attempt seconds
                backoff = min(2 ** attempts, 3600)
                next_attempt = now + backoff
                conn.execute(
                    "UPDATE deliveries SET status = 'pending', attempts = ?, last_error = ?, next_attempt_at = ?, updated_at = ?, owner = NULL, lease_expires_at = NULL WHERE id = ?",
                    (attempts, error, next_attempt, now, delivery_id),
                )
        row = conn.execute("SELECT * FROM deliveries WHERE id = ?", (delivery_id,)).fetchone()
        return _row_to_delivery(dict(row))

    def replay_delivery(self, admin_token: str, delivery_id: str, now: float) -> Delivery:
        """Re-enqueue a dead-lettered delivery. Admin only."""
        cred = self._require_admin(admin_token)
        conn = self._connect()
        with conn:
            row = conn.execute(
                "SELECT * FROM deliveries WHERE id = ? AND tenant_id = ?",
                (delivery_id, cred["tenant_id"]),
            ).fetchone()
            if not row:
                raise NotFoundError(f"Delivery '{delivery_id}' not found")
            if row["status"] != "dead":
                raise ValidationError("Only dead-lettered deliveries can be replayed")
            conn.execute(
                "UPDATE deliveries SET status = 'pending', attempts = 0, last_error = NULL, next_attempt_at = ?, updated_at = ? WHERE id = ?",
                (now, now, delivery_id),
            )
        self._audit(conn, cred["tenant_id"], cred["id"], "replay_delivery", "delivery", delivery_id, {})
        row = conn.execute("SELECT * FROM deliveries WHERE id = ?", (delivery_id,)).fetchone()
        return _row_to_delivery(dict(row))

    def list_deliveries(self, token: str, **filters: Any) -> list[Delivery]:
        cred = self._resolve_credential(token)
        conn = self._connect()
        tid = cred["tenant_id"]
        sql = "SELECT * FROM deliveries WHERE tenant_id = ?"
        params: list[Any] = [tid]
        if "status" in filters and filters["status"]:
            sql += " AND status = ?"
            params.append(filters["status"])
        sql += " ORDER BY created_at ASC"
        rows = conn.execute(sql, params).fetchall()
        return [_row_to_delivery(dict(r)) for r in rows]

    # ------------------------------------------------------------------
    # Snapshot export / import
    # ------------------------------------------------------------------

    def export_snapshot(self, admin_token: str) -> dict:
        """Export all data for this admin's tenant as a JSON-serializable dict."""
        cred = self._require_admin(admin_token)
        conn = self._connect()
        tid = cred["tenant_id"]

        def rows(table: str, where: str = "tenant_id = ?") -> list[dict]:
            return [dict(r) for r in conn.execute(f"SELECT * FROM {table} WHERE {where}", (tid,)).fetchall()]

        tenant_row = conn.execute("SELECT * FROM tenants WHERE id = ?", (tid,)).fetchone()
        return {
            "version": 1,
            "tenant": dict(tenant_row),
            "credentials": rows("credentials"),
            "shipments": rows("shipments"),
            "carrier_events": rows("carrier_events"),
            "exceptions": rows("exceptions"),
            "sla_rules": rows("sla_rules"),
            "audit_log": rows("audit_log"),
            "deliveries": rows("deliveries"),
        }

    def import_snapshot(self, admin_token: str, snapshot: dict) -> None:
        """Atomically restore a tenant from a snapshot.

        Only the credential's own tenant is affected.
        """
        cred = self._require_admin(admin_token)
        conn = self._connect()
        tid = cred["tenant_id"]

        if snapshot.get("version") != 1:
            raise ValidationError("Unsupported snapshot version")

        snap_tenant = snapshot.get("tenant", {})
        if snap_tenant.get("id") != tid:
            raise AuthorizationError("Snapshot tenant does not match credential's tenant")

        with conn:
            # Delete existing data for this tenant
            conn.execute("DELETE FROM deliveries WHERE tenant_id = ?", (tid,))
            conn.execute("DELETE FROM audit_log WHERE tenant_id = ?", (tid,))
            conn.execute("DELETE FROM sla_rules WHERE tenant_id = ?", (tid,))
            conn.execute("DELETE FROM exceptions WHERE tenant_id = ?", (tid,))
            conn.execute("DELETE FROM carrier_events WHERE tenant_id = ?", (tid,))
            conn.execute("DELETE FROM shipments WHERE tenant_id = ?", (tid,))
            conn.execute("DELETE FROM credentials WHERE tenant_id = ?", (tid,))
            conn.execute("DELETE FROM tenants WHERE id = ?", (tid,))

            # Re-insert
            t = snapshot["tenant"]
            conn.execute(
                "INSERT INTO tenants(id, name, created_at) VALUES (?, ?, ?)",
                (t["id"], t["name"], t["created_at"]),
            )
            for c in snapshot.get("credentials", []):
                conn.execute(
                    "INSERT INTO credentials(id, tenant_id, token_hash, role, created_at) VALUES (?, ?, ?, ?, ?)",
                    (c["id"], c["tenant_id"], c["token_hash"], c["role"], c["created_at"]),
                )
            for s in snapshot.get("shipments", []):
                conn.execute(
                    "INSERT INTO shipments(id, tenant_id, reference, status, last_location, created_at, updated_at, version, active_exception_id) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (s["id"], s["tenant_id"], s["reference"], s["status"], s["last_location"],
                     s["created_at"], s["updated_at"], s["version"], s["active_exception_id"]),
                )
            for e in snapshot.get("carrier_events", []):
                conn.execute(
                    "INSERT INTO carrier_events(id, shipment_id, tenant_id, event_id, event_type, event_time, location, details, received_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (e["id"], e["shipment_id"], e["tenant_id"], e["event_id"], e["event_type"],
                     e["event_time"], e["location"], e["details"], e["received_at"]),
                )
            for exc in snapshot.get("exceptions", []):
                conn.execute(
                    "INSERT INTO exceptions(id, shipment_id, tenant_id, status, severity, opened_at, resolved_at, assigned_to, notes, version, escalated) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (exc["id"], exc["shipment_id"], exc["tenant_id"], exc["status"], exc["severity"],
                     exc["opened_at"], exc["resolved_at"], exc["assigned_to"], exc["notes"],
                     exc["version"], exc["escalated"]),
                )
            for r in snapshot.get("sla_rules", []):
                conn.execute(
                    "INSERT INTO sla_rules(id, tenant_id, severity, delay_seconds, created_at) VALUES (?, ?, ?, ?, ?)",
                    (r["id"], r["tenant_id"], r["severity"], r["delay_seconds"], r["created_at"]),
                )
            for a in snapshot.get("audit_log", []):
                conn.execute(
                    "INSERT INTO audit_log(id, tenant_id, actor, action, resource_type, resource_id, meta, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (a["id"], a["tenant_id"], a["actor"], a["action"], a["resource_type"],
                     a["resource_id"], a["meta"], a["created_at"]),
                )
            for d in snapshot.get("deliveries", []):
                conn.execute(
                    "INSERT INTO deliveries(id, tenant_id, idempotency_key, payload, status, owner, lease_expires_at, attempts, last_error, next_attempt_at, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (d["id"], d["tenant_id"], d["idempotency_key"], d["payload"], d["status"],
                     d["owner"], d["lease_expires_at"], d["attempts"], d["last_error"],
                     d["next_attempt_at"], d["created_at"], d["updated_at"]),
                )


# ---------------------------------------------------------------------------
# Projection logic (deterministic)
# ---------------------------------------------------------------------------

# Priority of statuses for "latest wins" projection
_STATUS_PRIORITY = {
    "created": 0,
    "picked_up": 1,
    "in_transit": 2,
    "delayed": 3,
    "delivered": 4,
    "cancelled": 4,
}


def _project_shipment(events: list) -> tuple[str, str | None]:
    """Return (status, last_location) from a time-ordered list of events.

    Rules:
    - Status is the "dominant" status from the event stream ordered by
      (event_time ASC, event_id ASC).
    - delivered and cancelled are terminal – a later delayed event does
      not override them.
    - last_location is from the most recent event (by event_time, event_id)
      that has a location.
    """
    if not events:
        return "created", None

    status = "created"
    last_location: str | None = None

    for ev in events:
        etype = ev["event_type"]
        new_status = EVENT_TO_STATUS[etype]

        # Terminals are sticky – a delayed event cannot roll back a delivery
        if status in ("delivered", "cancelled"):
            pass  # terminal: ignore further status changes
        else:
            status = new_status

        if ev["location"]:
            last_location = ev["location"]

    return status, last_location
