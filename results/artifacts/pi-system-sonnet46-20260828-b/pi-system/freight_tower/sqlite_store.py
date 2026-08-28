"""
SQLiteFreightStore – durable, multi-tenant freight exception control tower backed by SQLite.

All public methods are documented in the product contract.  The class uses
WAL-mode SQLite so that two concurrent connections to the same file observe
each other's committed work without exclusive locking.
"""
from __future__ import annotations

import contextlib
import json
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Generator, Optional

from .exceptions import (
    AuthError,
    AuthzError,
    ConflictError,
    NotFoundError,
    ValidationError,
    VersionError,
)
from .models import ShipmentStatus

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

VALID_ROLES = {"viewer", "operator", "admin"}
VALID_EVENT_TYPES = {"picked_up", "in_transit", "delayed", "delivered", "cancelled"}
VALID_ACTIONS = {"assign", "acknowledge", "note", "resolve"}
VALID_SEVERITIES = {"P1", "P2", "P3"}

EVENT_STATUS_MAP = {
    "picked_up": ShipmentStatus.IN_TRANSIT,
    "in_transit": ShipmentStatus.IN_TRANSIT,
    "delayed": ShipmentStatus.DELAYED,
    "delivered": ShipmentStatus.DELIVERED,
    "cancelled": ShipmentStatus.CANCELLED,
}

# ---------------------------------------------------------------------------
# Domain dataclasses
# ---------------------------------------------------------------------------


@dataclass
class Shipment:
    id: str
    tenant_id: str
    reference: str
    status: str
    last_location: Optional[str]
    created_at: float
    updated_at: float
    version: int
    active_exception_id: Optional[str] = None


@dataclass
class CarrierEvent:
    id: str
    tenant_id: str
    shipment_id: str
    event_id: str
    event_type: str
    event_time: float
    location: Optional[str]
    details: Optional[str]
    received_at: float


@dataclass
class Exception_:
    id: str
    tenant_id: str
    shipment_id: str
    status: str            # open | acknowledged | resolved
    severity: str
    assignee: Optional[str]
    opened_at: float
    updated_at: float
    version: int
    resolved_at: Optional[float] = None
    notes: list[str] = field(default_factory=list)


@dataclass
class AuditEntry:
    id: str
    tenant_id: str
    actor: Optional[str]
    action: str
    entity_type: str
    entity_id: str
    payload: dict
    created_at: float


@dataclass
class SlaRule:
    id: str
    tenant_id: str
    severity: str
    delay_seconds: int
    created_at: float
    updated_at: float


@dataclass
class Delivery:
    id: str
    tenant_id: str
    idempotency_key: str
    event_type: str
    payload: dict
    status: str            # pending | delivered | failed | dead
    attempts: int
    next_attempt_at: float
    owner: Optional[str]
    lease_expires_at: Optional[float]
    last_error: Optional[str]
    created_at: float
    updated_at: float


# ---------------------------------------------------------------------------
# Schema DDL
# ---------------------------------------------------------------------------

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS tenants (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    created_at  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS credentials (
    token       TEXT NOT NULL,
    tenant_id   TEXT NOT NULL REFERENCES tenants(id),
    role        TEXT NOT NULL CHECK(role IN ('viewer','operator','admin')),
    created_at  REAL NOT NULL,
    PRIMARY KEY (tenant_id, token)
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
    tenant_id    TEXT NOT NULL REFERENCES tenants(id),
    shipment_id  TEXT NOT NULL REFERENCES shipments(id),
    event_id     TEXT NOT NULL,
    event_type   TEXT NOT NULL,
    event_time   REAL NOT NULL,
    location     TEXT,
    details      TEXT,
    received_at  REAL NOT NULL,
    UNIQUE(event_id, tenant_id)
);

CREATE TABLE IF NOT EXISTS exceptions (
    id           TEXT PRIMARY KEY,
    tenant_id    TEXT NOT NULL REFERENCES tenants(id),
    shipment_id  TEXT NOT NULL REFERENCES shipments(id),
    status       TEXT NOT NULL DEFAULT 'open',
    severity     TEXT NOT NULL DEFAULT 'P2',
    assignee     TEXT,
    opened_at    REAL NOT NULL,
    updated_at   REAL NOT NULL,
    resolved_at  REAL,
    version      INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS exception_notes (
    id           TEXT PRIMARY KEY,
    exception_id TEXT NOT NULL REFERENCES exceptions(id),
    tenant_id    TEXT NOT NULL,
    note         TEXT NOT NULL,
    actor        TEXT,
    created_at   REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS audit (
    id           TEXT PRIMARY KEY,
    tenant_id    TEXT NOT NULL,
    actor        TEXT,
    action       TEXT NOT NULL,
    entity_type  TEXT NOT NULL,
    entity_id    TEXT NOT NULL,
    payload      TEXT NOT NULL,
    created_at   REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS sla_rules (
    id             TEXT PRIMARY KEY,
    tenant_id      TEXT NOT NULL REFERENCES tenants(id),
    severity       TEXT NOT NULL,
    delay_seconds  INTEGER NOT NULL,
    created_at     REAL NOT NULL,
    updated_at     REAL NOT NULL,
    UNIQUE(tenant_id, severity)
);

CREATE TABLE IF NOT EXISTS deliveries (
    id               TEXT PRIMARY KEY,
    tenant_id        TEXT NOT NULL,
    idempotency_key  TEXT NOT NULL UNIQUE,
    event_type       TEXT NOT NULL,
    payload          TEXT NOT NULL,
    status           TEXT NOT NULL DEFAULT 'pending',
    attempts         INTEGER NOT NULL DEFAULT 0,
    next_attempt_at  REAL NOT NULL,
    owner            TEXT,
    lease_expires_at REAL,
    last_error       TEXT,
    created_at       REAL NOT NULL,
    updated_at       REAL NOT NULL
);
"""


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


class SQLiteFreightStore:
    """
    Durable multi-tenant freight control tower backed by SQLite.

    Parameters
    ----------
    db_path : str | Path
        Path to the SQLite database file.  Use ``:memory:`` for an in-process
        ephemeral store (all threads share a single connection + Python lock).
    clock : callable returning float
        Clock function used for timestamps.  Defaults to ``time.time``.
    lease_seconds : int
        How long a delivery claim lease lasts before expiry.  Default: 30.
    max_attempts : int
        Max delivery attempts before dead-lettering.  Default: 5.
    """

    def __init__(
        self,
        db_path: str | Path = ":memory:",
        clock: Callable[[], float] = time.time,
        lease_seconds: int = 30,
        max_attempts: int = 5,
    ) -> None:
        self._db_path = str(db_path)
        self._clock = clock
        self._lease_seconds = lease_seconds
        self._max_attempts = max_attempts
        # _lock serializes writes; also guards the shared conn for :memory:
        self._lock = threading.Lock()
        self._local = threading.local()  # per-thread connections for file DBs

        # :memory: databases are independent per connection – share one
        if self._db_path == ":memory:":
            self._shared_conn: Optional[sqlite3.Connection] = self._open_conn()
        else:
            self._shared_conn = None

        self._init_schema()

    # ------------------------------------------------------------------
    # Connection management
    # ------------------------------------------------------------------

    def _open_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            self._db_path,
            check_same_thread=False,
            isolation_level=None,   # manual transaction control
            timeout=30,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=15000")
        return conn

    def _conn(self) -> sqlite3.Connection:
        """Return the connection for this context."""
        if self._shared_conn is not None:
            return self._shared_conn
        if not hasattr(self._local, "conn") or self._local.conn is None:
            self._local.conn = self._open_conn()
        return self._local.conn

    def _init_schema(self) -> None:
        with self._lock:
            self._conn().executescript(SCHEMA)

    @contextlib.contextmanager
    def _tx(self) -> Generator[sqlite3.Connection, None, None]:
        """
        Write transaction context manager.

        For :memory: databases acquires the Python lock so that the single
        shared connection is not used concurrently.  For file-based databases
        issues ``BEGIN IMMEDIATE`` to serialize writers at the SQLite level.
        """
        if self._shared_conn is not None:
            # :memory: path: lock + shared connection
            self._lock.acquire()
            conn = self._shared_conn
            conn.execute("BEGIN")
            try:
                yield conn
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            finally:
                self._lock.release()
        else:
            # File DB path: per-thread connection, BEGIN IMMEDIATE for writer
            conn = self._conn()
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _now(self) -> float:
        return self._clock()

    def _uid(self) -> str:
        return str(uuid.uuid4())

    def _authenticate(self, token: str) -> sqlite3.Row:
        """Return credential row or raise AuthError.

        Tokens are unique within a tenant; the same token string may exist in
        multiple tenants.  We select by token value alone and return the first
        match – callers must not assume cross-tenant uniqueness.
        """
        row = self._conn().execute(
            "SELECT * FROM credentials WHERE token=? LIMIT 1", (token,)
        ).fetchone()
        if row is None:
            raise AuthError("Invalid or missing token")
        return row

    def _require_role(self, token: str, *roles: str) -> sqlite3.Row:
        """Authenticate and verify role membership."""
        cred = self._authenticate(token)
        if cred["role"] not in roles:
            raise AuthzError(
                f"Role '{cred['role']}' cannot perform this action; required: {set(roles)}"
            )
        return cred

    def _audit(
        self,
        conn: sqlite3.Connection,
        tenant_id: str,
        actor: Optional[str],
        action: str,
        entity_type: str,
        entity_id: str,
        payload: dict,
    ) -> None:
        conn.execute(
            """INSERT INTO audit(id,tenant_id,actor,action,entity_type,entity_id,payload,created_at)
               VALUES(?,?,?,?,?,?,?,?)""",
            (self._uid(), tenant_id, actor, action, entity_type, entity_id,
             json.dumps(payload), self._now()),
        )

    def _enqueue_delivery(
        self,
        conn: sqlite3.Connection,
        tenant_id: str,
        event_type: str,
        payload: dict,
        idempotency_key: Optional[str] = None,
    ) -> str:
        now = self._now()
        did = self._uid()
        ikey = idempotency_key or self._uid()
        conn.execute(
            """INSERT OR IGNORE INTO deliveries
               (id,tenant_id,idempotency_key,event_type,payload,status,attempts,
                next_attempt_at,owner,lease_expires_at,last_error,created_at,updated_at)
               VALUES(?,?,?,?,?,?,0,?,NULL,NULL,NULL,?,?)""",
            (did, tenant_id, ikey, event_type, json.dumps(payload), "pending",
             now, now, now),
        )
        return did

    # ------------------------------------------------------------------
    # Row converters
    # ------------------------------------------------------------------

    @staticmethod
    def _row_to_shipment(row: sqlite3.Row) -> Shipment:
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

    @staticmethod
    def _row_to_delivery(row: sqlite3.Row) -> Delivery:
        return Delivery(
            id=row["id"],
            tenant_id=row["tenant_id"],
            idempotency_key=row["idempotency_key"],
            event_type=row["event_type"],
            payload=json.loads(row["payload"]),
            status=row["status"],
            attempts=row["attempts"],
            next_attempt_at=row["next_attempt_at"],
            owner=row["owner"],
            lease_expires_at=row["lease_expires_at"],
            last_error=row["last_error"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def _row_to_exception(self, conn: sqlite3.Connection, row: sqlite3.Row) -> Exception_:
        notes_rows = conn.execute(
            "SELECT note FROM exception_notes WHERE exception_id=? ORDER BY created_at",
            (row["id"],),
        ).fetchall()
        return Exception_(
            id=row["id"],
            tenant_id=row["tenant_id"],
            shipment_id=row["shipment_id"],
            status=row["status"],
            severity=row["severity"],
            assignee=row["assignee"],
            opened_at=row["opened_at"],
            updated_at=row["updated_at"],
            version=row["version"],
            resolved_at=row["resolved_at"],
            notes=[r["note"] for r in notes_rows],
        )

    # ------------------------------------------------------------------
    # Tenant / credential management
    # ------------------------------------------------------------------

    def bootstrap_tenant(self, tenant_id: str, name: str, admin_token: str) -> None:
        """Create a tenant and an admin credential for it.

        Idempotent: if the tenant already exists and admin_token matches, succeeds.
        """
        if not tenant_id or not name or not admin_token:
            raise ValidationError("tenant_id, name, and admin_token are required")
        now = self._now()
        with self._tx() as conn:
            existing = conn.execute(
                "SELECT id FROM tenants WHERE id=?", (tenant_id,)
            ).fetchone()
            if existing:
                cred = conn.execute(
                    "SELECT token FROM credentials WHERE tenant_id=? AND role='admin'",
                    (tenant_id,),
                ).fetchone()
                if cred and cred["token"] != admin_token:
                    raise ConflictError("Tenant already bootstrapped with a different admin token")
                return
            conn.execute(
                "INSERT INTO tenants(id,name,created_at) VALUES(?,?,?)",
                (tenant_id, name, now),
            )
            try:
                conn.execute(
                    "INSERT INTO credentials(token,tenant_id,role,created_at) VALUES(?,?,?,?)",
                    (admin_token, tenant_id, "admin", now),
                )
            except sqlite3.IntegrityError:
                # (tenant_id, token) primary key conflict – re-bootstrap of same tenant
                # is idempotent; a conflict here always means same tenant because the
                # tenant row was just inserted above (new tenant path).
                pass  # idempotent

    def create_credential(self, admin_token: str, token: str, role: str) -> None:
        """Create a new credential under the admin's tenant."""
        cred = self._require_role(admin_token, "admin")
        if role not in VALID_ROLES:
            raise ValidationError(f"role must be one of {VALID_ROLES}")
        if not token:
            raise ValidationError("token is required")
        tenant_id = cred["tenant_id"]
        now = self._now()
        with self._tx() as conn:
            existing = conn.execute(
                "SELECT role FROM credentials WHERE tenant_id=? AND token=?",
                (tenant_id, token),
            ).fetchone()
            if existing:
                if existing["role"] == role:
                    return  # idempotent
                raise ConflictError("Token already in use with a different role in this tenant")
            conn.execute(
                "INSERT INTO credentials(token,tenant_id,role,created_at) VALUES(?,?,?,?)",
                (token, tenant_id, role, now),
            )

    # ------------------------------------------------------------------
    # Shipments
    # ------------------------------------------------------------------

    def create_shipment(self, token: str, reference: str) -> Shipment:
        """Create a new shipment in the token's tenant."""
        cred = self._require_role(token, "operator", "admin")
        if not reference or not reference.strip():
            raise ValidationError("reference is required")
        reference = reference.strip()
        tenant_id = cred["tenant_id"]
        now = self._now()
        sid = self._uid()
        with self._tx() as conn:
            existing = conn.execute(
                "SELECT * FROM shipments WHERE tenant_id=? AND reference=?",
                (tenant_id, reference),
            ).fetchone()
            if existing:
                return self._row_to_shipment(existing)
            conn.execute(
                """INSERT INTO shipments(id,tenant_id,reference,status,last_location,
                   created_at,updated_at,version,active_exception_id)
                   VALUES(?,?,?,?,NULL,?,?,1,NULL)""",
                (sid, tenant_id, reference, "created", now, now),
            )
            self._audit(conn, tenant_id, token, "create_shipment", "shipment", sid,
                        {"reference": reference})
            self._enqueue_delivery(
                conn, tenant_id, "shipment_created",
                {"shipment_id": sid, "reference": reference},
            )
            return self._row_to_shipment(
                conn.execute("SELECT * FROM shipments WHERE id=?", (sid,)).fetchone()
            )

    def get_shipment(self, token: str, shipment_id: str) -> Shipment:
        """Fetch a single shipment by id."""
        cred = self._authenticate(token)
        tenant_id = cred["tenant_id"]
        row = self._conn().execute(
            "SELECT * FROM shipments WHERE id=? AND tenant_id=?",
            (shipment_id, tenant_id),
        ).fetchone()
        if row is None:
            raise NotFoundError(f"Shipment '{shipment_id}' not found")
        return self._row_to_shipment(row)

    def list_shipments(self, token: str, **filters: Any) -> list[Shipment]:
        """List shipments in the token's tenant, optionally filtered."""
        cred = self._authenticate(token)
        tenant_id = cred["tenant_id"]
        query = "SELECT * FROM shipments WHERE tenant_id=?"
        params: list[Any] = [tenant_id]
        if status := filters.get("status"):
            query += " AND status=?"
            params.append(status)
        if reference := filters.get("reference"):
            query += " AND reference=?"
            params.append(reference)
        query += " ORDER BY created_at, id"
        return [self._row_to_shipment(r) for r in self._conn().execute(query, params)]

    # ------------------------------------------------------------------
    # Carrier event ingestion
    # ------------------------------------------------------------------

    def ingest_event(
        self,
        token: str,
        shipment_id: str,
        event_id: str,
        event_type: str,
        event_time: float,
        location: Optional[str] = None,
        details: Optional[str] = None,
    ) -> Shipment:
        """
        Ingest a carrier event and re-project the shipment state.

        Idempotent on (event_id, tenant_id).  Returns the current projection.
        Raises ConflictError if event_id is reused with a different payload.
        """
        cred = self._require_role(token, "operator", "admin")
        tenant_id = cred["tenant_id"]

        if not event_id:
            raise ValidationError("event_id is required")
        if event_type not in VALID_EVENT_TYPES:
            raise ValidationError(f"event_type must be one of {VALID_EVENT_TYPES}")
        if event_time is None:
            raise ValidationError("event_time is required")

        # Coerce non-string location/details to JSON strings before storage
        if location is not None and not isinstance(location, str):
            location = json.dumps(location)
        if details is not None and not isinstance(details, str):
            details = json.dumps(details)

        with self._tx() as conn:
            ship_row = conn.execute(
                "SELECT * FROM shipments WHERE id=? AND tenant_id=?",
                (shipment_id, tenant_id),
            ).fetchone()
            if ship_row is None:
                raise NotFoundError(f"Shipment '{shipment_id}' not found")

            # Idempotency check
            existing_event = conn.execute(
                "SELECT * FROM carrier_events WHERE event_id=? AND tenant_id=?",
                (event_id, tenant_id),
            ).fetchone()
            if existing_event:
                if (
                    existing_event["shipment_id"] != shipment_id
                    or existing_event["event_type"] != event_type
                    or abs(existing_event["event_time"] - event_time) > 0.001
                    or existing_event["location"] != location
                    or existing_event["details"] != details
                ):
                    raise ConflictError(
                        f"event_id '{event_id}' already exists with a different payload"
                    )
                return self._row_to_shipment(
                    conn.execute("SELECT * FROM shipments WHERE id=?", (shipment_id,)).fetchone()
                )

            now = self._now()
            conn.execute(
                """INSERT INTO carrier_events
                   (id,tenant_id,shipment_id,event_id,event_type,event_time,
                    location,details,received_at)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                (self._uid(), tenant_id, shipment_id, event_id, event_type,
                 event_time, location, details, now),
            )

            # Re-project shipment from all events ordered by (event_time, event_id)
            events = conn.execute(
                """SELECT * FROM carrier_events WHERE shipment_id=? AND tenant_id=?
                   ORDER BY event_time, event_id""",
                (shipment_id, tenant_id),
            ).fetchall()
            projected_status, projected_location = self._project_events(events)
            new_version = ship_row["version"] + 1

            # Exception management
            active_exc_id = ship_row["active_exception_id"]
            is_terminal = projected_status in ("delivered", "cancelled")

            if projected_status == "delayed" and not active_exc_id:
                exc_id = self._uid()
                rule = conn.execute(
                    "SELECT severity FROM sla_rules WHERE tenant_id=? ORDER BY delay_seconds LIMIT 1",
                    (tenant_id,),
                ).fetchone()
                severity = rule["severity"] if rule else "P2"
                conn.execute(
                    """INSERT INTO exceptions(id,tenant_id,shipment_id,status,severity,
                       assignee,opened_at,updated_at,resolved_at,version)
                       VALUES(?,?,?,?,?,NULL,?,?,NULL,1)""",
                    (exc_id, tenant_id, shipment_id, "open", severity, now, now),
                )
                active_exc_id = exc_id
                self._audit(conn, tenant_id, token, "exception_opened", "exception", exc_id,
                            {"shipment_id": shipment_id, "severity": severity})
                self._enqueue_delivery(
                    conn, tenant_id, "exception_opened",
                    {"exception_id": exc_id, "shipment_id": shipment_id, "severity": severity},
                    idempotency_key=f"exc_open_{exc_id}",
                )
            elif is_terminal and active_exc_id:
                conn.execute(
                    """UPDATE exceptions SET status='resolved', resolved_at=?, updated_at=?,
                       version=version+1 WHERE id=? AND status!='resolved'""",
                    (now, now, active_exc_id),
                )
                self._audit(conn, tenant_id, token, "exception_resolved", "exception", active_exc_id,
                            {"reason": projected_status, "shipment_id": shipment_id})
                active_exc_id = None

            conn.execute(
                """UPDATE shipments SET status=?, last_location=?, updated_at=?,
                   version=?, active_exception_id=? WHERE id=?""",
                (projected_status, projected_location, now, new_version, active_exc_id, shipment_id),
            )
            self._audit(conn, tenant_id, token, "ingest_event", "shipment", shipment_id,
                        {"event_id": event_id, "event_type": event_type, "status": projected_status})
            self._enqueue_delivery(
                conn, tenant_id, "shipment_status_updated",
                {"shipment_id": shipment_id, "status": projected_status, "event_type": event_type},
                idempotency_key=f"evt_{event_id}_ship_{shipment_id}",
            )
            return self._row_to_shipment(
                conn.execute("SELECT * FROM shipments WHERE id=?", (shipment_id,)).fetchone()
            )

    @staticmethod
    def _project_events(events: list[sqlite3.Row]) -> tuple[str, Optional[str]]:
        """
        Deterministic projection over ordered carrier events.

        Ordering: (event_time ASC, event_id ASC) – stable tie-break.
        Terminal states (delivered, cancelled) are never rolled back.
        """
        status = "created"
        location: Optional[str] = None
        TERMINAL = {"delivered", "cancelled"}
        for ev in events:
            new_status = EVENT_STATUS_MAP.get(ev["event_type"], ShipmentStatus.IN_TRANSIT).value
            if status not in TERMINAL:
                status = new_status
            if ev["location"]:
                location = ev["location"]
        return status, location

    # ------------------------------------------------------------------
    # Exceptions
    # ------------------------------------------------------------------

    def list_exceptions(self, token: str, **filters: Any) -> list[Exception_]:
        """List exceptions in the token's tenant."""
        cred = self._authenticate(token)
        tenant_id = cred["tenant_id"]
        query = "SELECT * FROM exceptions WHERE tenant_id=?"
        params: list[Any] = [tenant_id]
        for key, col in [("status", "status"), ("severity", "severity"),
                         ("assignee", "assignee"), ("shipment_id", "shipment_id")]:
            if val := filters.get(key):
                query += f" AND {col}=?"
                params.append(val)
        query += " ORDER BY opened_at, id"
        conn = self._conn()
        return [self._row_to_exception(conn, r) for r in conn.execute(query, params)]

    def mutate_exception(
        self,
        token: str,
        exception_id: str,
        expected_version: int,
        action: str,
        actor: Optional[str] = None,
        **values: Any,
    ) -> Exception_:
        """
        Apply an action to an exception with optimistic concurrency.

        Actions: assign, acknowledge, note, resolve.
        Raises VersionError on stale expected_version.
        """
        cred = self._require_role(token, "operator", "admin")
        tenant_id = cred["tenant_id"]
        if action not in VALID_ACTIONS:
            raise ValidationError(f"action must be one of {VALID_ACTIONS}")
        actor = actor or token

        with self._tx() as conn:
            row = conn.execute(
                "SELECT * FROM exceptions WHERE id=? AND tenant_id=?",
                (exception_id, tenant_id),
            ).fetchone()
            if row is None:
                raise NotFoundError(f"Exception '{exception_id}' not found")
            if row["version"] != expected_version:
                raise VersionError(
                    f"Version conflict: expected {expected_version}, got {row['version']}"
                )

            now = self._now()
            audit_payload: dict = {"action": action, "actor": actor}

            if action == "assign":
                assignee = values.get("assignee")
                if not assignee:
                    raise ValidationError("assignee is required for assign action")
                conn.execute(
                    "UPDATE exceptions SET assignee=?, updated_at=?, version=version+1 WHERE id=?",
                    (assignee, now, exception_id),
                )
                audit_payload["assignee"] = assignee

            elif action == "acknowledge":
                if row["status"] == "resolved":
                    raise ValidationError("Cannot acknowledge a resolved exception")
                conn.execute(
                    "UPDATE exceptions SET status='acknowledged', updated_at=?, version=version+1 WHERE id=?",
                    (now, exception_id),
                )

            elif action == "note":
                note_text = values.get("note", "")
                if not note_text:
                    raise ValidationError("note is required for note action")
                conn.execute(
                    """INSERT INTO exception_notes(id,exception_id,tenant_id,note,actor,created_at)
                       VALUES(?,?,?,?,?,?)""",
                    (self._uid(), exception_id, tenant_id, note_text, actor, now),
                )
                conn.execute(
                    "UPDATE exceptions SET updated_at=?, version=version+1 WHERE id=?",
                    (now, exception_id),
                )
                audit_payload["note"] = note_text

            elif action == "resolve":
                conn.execute(
                    """UPDATE exceptions SET status='resolved', resolved_at=?, updated_at=?,
                       version=version+1 WHERE id=?""",
                    (now, now, exception_id),
                )
                conn.execute(
                    """UPDATE shipments SET active_exception_id=NULL
                       WHERE active_exception_id=? AND tenant_id=?""",
                    (exception_id, tenant_id),
                )
                self._enqueue_delivery(
                    conn, tenant_id, "exception_resolved",
                    {"exception_id": exception_id, "actor": actor},
                    idempotency_key=f"exc_resolve_{exception_id}",
                )

            self._audit(conn, tenant_id, actor, f"exception_{action}", "exception",
                        exception_id, audit_payload)
            updated = conn.execute(
                "SELECT * FROM exceptions WHERE id=?", (exception_id,)
            ).fetchone()
            return self._row_to_exception(conn, updated)

    # ------------------------------------------------------------------
    # Audit
    # ------------------------------------------------------------------

    def audit(self, token: str, **filters: Any) -> list[AuditEntry]:
        """Return audit entries for the token's tenant."""
        cred = self._authenticate(token)
        tenant_id = cred["tenant_id"]
        query = "SELECT * FROM audit WHERE tenant_id=?"
        params: list[Any] = [tenant_id]
        if entity_type := filters.get("entity_type"):
            query += " AND entity_type=?"
            params.append(entity_type)
        if entity_id := filters.get("entity_id"):
            query += " AND entity_id=?"
            params.append(entity_id)
        if action := filters.get("action"):
            query += " AND action=?"
            params.append(action)
        query += " ORDER BY created_at, id"
        return [
            AuditEntry(
                id=r["id"], tenant_id=r["tenant_id"], actor=r["actor"],
                action=r["action"], entity_type=r["entity_type"], entity_id=r["entity_id"],
                payload=json.loads(r["payload"]), created_at=r["created_at"],
            )
            for r in self._conn().execute(query, params)
        ]

    # ------------------------------------------------------------------
    # SLA rules
    # ------------------------------------------------------------------

    def set_sla_rule(self, admin_token: str, severity: str, delay_seconds: int) -> SlaRule:
        """Upsert a SLA rule for a severity level."""
        cred = self._require_role(admin_token, "admin")
        tenant_id = cred["tenant_id"]
        if severity not in VALID_SEVERITIES:
            raise ValidationError(f"severity must be one of {VALID_SEVERITIES}")
        if not isinstance(delay_seconds, int) or delay_seconds <= 0:
            raise ValidationError("delay_seconds must be a positive integer")
        now = self._now()
        rule_id = self._uid()
        with self._tx() as conn:
            existing = conn.execute(
                "SELECT id, created_at FROM sla_rules WHERE tenant_id=? AND severity=?",
                (tenant_id, severity),
            ).fetchone()
            if existing:
                conn.execute(
                    "UPDATE sla_rules SET delay_seconds=?, updated_at=? WHERE id=?",
                    (delay_seconds, now, existing["id"]),
                )
                return SlaRule(existing["id"], tenant_id, severity, delay_seconds,
                               existing["created_at"], now)
            conn.execute(
                """INSERT INTO sla_rules(id,tenant_id,severity,delay_seconds,created_at,updated_at)
                   VALUES(?,?,?,?,?,?)""",
                (rule_id, tenant_id, severity, delay_seconds, now, now),
            )
            return SlaRule(rule_id, tenant_id, severity, delay_seconds, now, now)

    def tick(self, now: float, limit: int = 100) -> int:
        """
        Claim due SLA escalations across all tenants.

        For each open exception whose SLA deadline has passed and no escalation
        delivery has yet been enqueued, enqueue one.  Idempotent.

        Returns the count of newly enqueued escalations.
        """
        with self._tx() as conn:
            # Severity-agnostic: find any rule in the exception's tenant whose
            # delay_seconds threshold has elapsed since the exception was opened.
            # We pick the rule with the smallest delay (strictest threshold) so
            # that late-added rules are immediately applied to existing exceptions.
            # The matched rule's own severity label is used in the escalation
            # payload, not the exception's stored severity field.
            rows = conn.execute(
                """
                SELECT e.id, e.tenant_id, e.shipment_id,
                       (
                           SELECT r2.severity
                           FROM sla_rules r2
                           WHERE r2.tenant_id = e.tenant_id
                             AND e.opened_at + r2.delay_seconds <= ?
                           ORDER BY r2.delay_seconds
                           LIMIT 1
                       ) AS matched_severity
                FROM exceptions e
                WHERE e.status = 'open'
                  AND EXISTS (
                      SELECT 1 FROM sla_rules r
                      WHERE r.tenant_id = e.tenant_id
                        AND e.opened_at + r.delay_seconds <= ?
                  )
                  AND NOT EXISTS (
                      SELECT 1 FROM deliveries d
                      WHERE d.idempotency_key = 'sla_esc_' || e.id
                  )
                ORDER BY e.opened_at
                LIMIT ?
                """,
                (now, now, limit),
            ).fetchall()

            count = 0
            for row in rows:
                self._enqueue_delivery(
                    conn, row["tenant_id"], "sla_escalation",
                    {"exception_id": row["id"], "shipment_id": row["shipment_id"],
                     "severity": row["matched_severity"]},
                    idempotency_key=f"sla_esc_{row['id']}",
                )
                count += 1
            return count

    # ------------------------------------------------------------------
    # Outbox delivery
    # ------------------------------------------------------------------

    def claim_delivery(self, worker_id: str, now: float) -> Optional[Delivery]:
        """
        Claim the next available delivery for the given worker.

        Claimable = status in (pending, failed) AND next_attempt_at <= now
                    AND (owner IS NULL OR lease_expires_at <= now).
        Returns None if nothing is available.
        """
        with self._tx() as conn:
            row = conn.execute(
                """SELECT * FROM deliveries
                   WHERE status IN ('pending','failed')
                     AND next_attempt_at <= ?
                     AND (owner IS NULL OR lease_expires_at <= ?)
                   ORDER BY next_attempt_at, id
                   LIMIT 1""",
                (now, now),
            ).fetchone()
            if row is None:
                return None
            lease_exp = now + self._lease_seconds
            conn.execute(
                """UPDATE deliveries SET owner=?, lease_expires_at=?, status='pending',
                   updated_at=? WHERE id=?""",
                (worker_id, lease_exp, now, row["id"]),
            )
            return self._row_to_delivery(
                conn.execute("SELECT * FROM deliveries WHERE id=?", (row["id"],)).fetchone()
            )

    def complete_delivery(self, delivery_id: str, worker_id: str, now: float) -> None:
        """Mark a delivery as delivered."""
        with self._tx() as conn:
            row = conn.execute(
                "SELECT owner FROM deliveries WHERE id=?", (delivery_id,)
            ).fetchone()
            if row is None:
                raise NotFoundError(f"Delivery '{delivery_id}' not found")
            if row["owner"] != worker_id:
                raise AuthzError("Delivery owned by a different worker")
            conn.execute(
                "UPDATE deliveries SET status='delivered', owner=NULL, "
                "lease_expires_at=NULL, updated_at=? WHERE id=?",
                (now, delivery_id),
            )

    def fail_delivery(self, delivery_id: str, worker_id: str, error: str, now: float) -> None:
        """Record a failure; schedule retry or dead-letter if exhausted."""
        with self._tx() as conn:
            row = conn.execute(
                "SELECT owner, attempts FROM deliveries WHERE id=?", (delivery_id,)
            ).fetchone()
            if row is None:
                raise NotFoundError(f"Delivery '{delivery_id}' not found")
            if row["owner"] != worker_id:
                raise AuthzError("Delivery owned by a different worker")
            # Increment attempts only here, on explicit failure acknowledgement.
            # Lease expiry re-claims do not count toward the dead-letter boundary.
            new_attempts = row["attempts"] + 1
            if new_attempts >= self._max_attempts:
                conn.execute(
                    """UPDATE deliveries SET status='dead', attempts=?, last_error=?, owner=NULL,
                       lease_expires_at=NULL, updated_at=? WHERE id=?""",
                    (new_attempts, error, now, delivery_id),
                )
            else:
                retry_delay = 2 ** new_attempts
                conn.execute(
                    """UPDATE deliveries SET status='failed', attempts=?, last_error=?, owner=NULL,
                       lease_expires_at=NULL, next_attempt_at=?, updated_at=? WHERE id=?""",
                    (new_attempts, error, now + retry_delay, now, delivery_id),
                )

    def replay_delivery(self, admin_token: str, delivery_id: str, now: float) -> Delivery:
        """Re-queue a dead-lettered delivery (admin only)."""
        cred = self._require_role(admin_token, "admin")
        tenant_id = cred["tenant_id"]
        with self._tx() as conn:
            row = conn.execute(
                "SELECT * FROM deliveries WHERE id=? AND tenant_id=?",
                (delivery_id, tenant_id),
            ).fetchone()
            if row is None:
                raise NotFoundError(f"Delivery '{delivery_id}' not found")
            if row["status"] != "dead":
                raise ValidationError("Only dead-lettered deliveries can be replayed")
            conn.execute(
                """UPDATE deliveries SET status='pending', attempts=0, owner=NULL,
                   lease_expires_at=NULL, last_error=NULL, next_attempt_at=?, updated_at=? WHERE id=?""",
                (now, now, delivery_id),
            )
            return self._row_to_delivery(
                conn.execute("SELECT * FROM deliveries WHERE id=?", (delivery_id,)).fetchone()
            )

    def list_deliveries(self, token: str, **filters: Any) -> list[Delivery]:
        """List outbox deliveries for the token's tenant."""
        cred = self._authenticate(token)
        tenant_id = cred["tenant_id"]
        query = "SELECT * FROM deliveries WHERE tenant_id=?"
        params: list[Any] = [tenant_id]
        if status := filters.get("status"):
            query += " AND status=?"
            params.append(status)
        if event_type := filters.get("event_type"):
            query += " AND event_type=?"
            params.append(event_type)
        query += " ORDER BY created_at, id"
        return [self._row_to_delivery(r) for r in self._conn().execute(query, params)]

    # ------------------------------------------------------------------
    # Snapshot export / import
    # ------------------------------------------------------------------

    def export_snapshot(self, admin_token: str) -> dict:
        """Export all data for the admin's tenant as a JSON-serializable dict."""
        cred = self._require_role(admin_token, "admin")
        tenant_id = cred["tenant_id"]
        conn = self._conn()

        def rows(table: str, col: str = "tenant_id") -> list[dict]:
            return [dict(r) for r in conn.execute(
                f"SELECT * FROM {table} WHERE {col}=?", (tenant_id,)
            )]

        tenant_row = conn.execute("SELECT * FROM tenants WHERE id=?", (tenant_id,)).fetchone()
        return {
            "schema_version": 1,
            "tenant": dict(tenant_row),
            "credentials": rows("credentials"),
            "shipments": rows("shipments"),
            "carrier_events": rows("carrier_events"),
            "exceptions": rows("exceptions"),
            "exception_notes": rows("exception_notes"),
            "audit": rows("audit"),
            "sla_rules": rows("sla_rules"),
            "deliveries": rows("deliveries"),
        }

    def import_snapshot(self, admin_token: str, snapshot: dict) -> None:
        """
        Atomically replace the admin's tenant data with the snapshot.

        Only the credential's tenant is affected.
        """
        cred = self._require_role(admin_token, "admin")
        caller_tenant = cred["tenant_id"]

        snap_tenant = snapshot.get("tenant", {})
        snap_tenant_id = snap_tenant.get("id")
        if not snap_tenant_id:
            raise ValidationError("snapshot.tenant.id is missing")
        if snap_tenant_id != caller_tenant:
            raise AuthzError("Snapshot tenant does not match credential's tenant")

        with self._tx() as conn:
            # Wipe in FK-safe order
            for tbl in ("deliveries", "sla_rules", "audit", "exception_notes",
                        "exceptions", "carrier_events", "shipments", "credentials"):
                conn.execute(f"DELETE FROM {tbl} WHERE tenant_id=?", (caller_tenant,))
            conn.execute("DELETE FROM tenants WHERE id=?", (caller_tenant,))

            # Re-insert
            t = snap_tenant
            conn.execute("INSERT INTO tenants(id,name,created_at) VALUES(?,?,?)",
                         (t["id"], t["name"], t["created_at"]))
            for c in snapshot.get("credentials", []):
                conn.execute(
                    "INSERT OR IGNORE INTO credentials(token,tenant_id,role,created_at) VALUES(?,?,?,?)",
                    (c["token"], c["tenant_id"], c["role"], c["created_at"]),
                )
            for s in snapshot.get("shipments", []):
                conn.execute(
                    """INSERT INTO shipments(id,tenant_id,reference,status,last_location,
                       created_at,updated_at,version,active_exception_id) VALUES(?,?,?,?,?,?,?,?,?)""",
                    (s["id"], s["tenant_id"], s["reference"], s["status"], s.get("last_location"),
                     s["created_at"], s["updated_at"], s["version"], s.get("active_exception_id")),
                )
            for ev in snapshot.get("carrier_events", []):
                conn.execute(
                    """INSERT INTO carrier_events
                       (id,tenant_id,shipment_id,event_id,event_type,event_time,
                        location,details,received_at) VALUES(?,?,?,?,?,?,?,?,?)""",
                    (ev["id"], ev["tenant_id"], ev["shipment_id"], ev["event_id"],
                     ev["event_type"], ev["event_time"], ev.get("location"),
                     ev.get("details"), ev["received_at"]),
                )
            for e in snapshot.get("exceptions", []):
                conn.execute(
                    """INSERT INTO exceptions(id,tenant_id,shipment_id,status,severity,
                       assignee,opened_at,updated_at,resolved_at,version) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                    (e["id"], e["tenant_id"], e["shipment_id"], e["status"], e["severity"],
                     e.get("assignee"), e["opened_at"], e["updated_at"],
                     e.get("resolved_at"), e["version"]),
                )
            for n in snapshot.get("exception_notes", []):
                conn.execute(
                    """INSERT INTO exception_notes(id,exception_id,tenant_id,note,actor,created_at)
                       VALUES(?,?,?,?,?,?)""",
                    (n["id"], n["exception_id"], n["tenant_id"], n["note"],
                     n.get("actor"), n["created_at"]),
                )
            for a in snapshot.get("audit", []):
                conn.execute(
                    """INSERT INTO audit(id,tenant_id,actor,action,entity_type,entity_id,payload,created_at)
                       VALUES(?,?,?,?,?,?,?,?)""",
                    (a["id"], a["tenant_id"], a.get("actor"), a["action"],
                     a["entity_type"], a["entity_id"], a["payload"], a["created_at"]),
                )
            for r in snapshot.get("sla_rules", []):
                conn.execute(
                    """INSERT INTO sla_rules(id,tenant_id,severity,delay_seconds,created_at,updated_at)
                       VALUES(?,?,?,?,?,?)""",
                    (r["id"], r["tenant_id"], r["severity"], r["delay_seconds"],
                     r["created_at"], r["updated_at"]),
                )
            for d in snapshot.get("deliveries", []):
                conn.execute(
                    """INSERT INTO deliveries
                       (id,tenant_id,idempotency_key,event_type,payload,status,attempts,
                        next_attempt_at,owner,lease_expires_at,last_error,created_at,updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (d["id"], d["tenant_id"], d["idempotency_key"], d["event_type"],
                     d["payload"], d["status"], d["attempts"], d["next_attempt_at"],
                     d.get("owner"), d.get("lease_expires_at"), d.get("last_error"),
                     d["created_at"], d["updated_at"]),
                )
