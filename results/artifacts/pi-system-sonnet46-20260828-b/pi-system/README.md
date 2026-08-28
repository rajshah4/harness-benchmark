# Freight Control Tower

A durable, multi-tenant freight exception control tower backed by SQLite.

## Requirements

- Python ≥ 3.11 (no external dependencies)
- SQLite ≥ 3.35 (ships with Python 3.11+)

## Quick start

### 1. Initialize the database and bootstrap a tenant

```bash
# Create the database and an admin credential
python3 -m freight_tower.cli --db tower.db init acme "Acme Corp" --admin-token "my-secret-admin-token"
```

### 2. Create additional credentials

```bash
python3 -m freight_tower.cli --db tower.db credentials create op-token operator --admin-token my-secret-admin-token
python3 -m freight_tower.cli --db tower.db credentials create viewer-token viewer --admin-token my-secret-admin-token
```

### 3. Run the server

```bash
python3 -m freight_tower.cli --db tower.db serve --host 0.0.0.0 --port 8080
```

Then open **http://localhost:8080** in your browser.  Sign in with any token you created.

### 4. Create a shipment and ingest events

```bash
python3 -m freight_tower.cli --db tower.db shipments create SHIP-2026-001 --token op-token
# → prints JSON including the shipment id

SHIP_ID="<id from above>"

python3 -m freight_tower.cli --db tower.db events ingest \
  "$SHIP_ID" ev-001 picked_up 1700000000 --location "New York, NY" --token op-token

python3 -m freight_tower.cli --db tower.db events ingest \
  "$SHIP_ID" ev-002 delayed 1700086400 --location "Chicago, IL" --token op-token
```

### 5. Work exceptions

```bash
# List open exceptions
python3 -m freight_tower.cli --db tower.db exceptions list --status open --token op-token

EXC_ID="<id from above>"
# Acknowledge (expected_version = current version, usually 1)
python3 -m freight_tower.cli --db tower.db exceptions mutate \
  "$EXC_ID" acknowledge 1 --token op-token

# Resolve
python3 -m freight_tower.cli --db tower.db exceptions mutate \
  "$EXC_ID" resolve 2 --token op-token
```

### 6. Configure SLA rules and run the scheduler

```bash
# Escalate open P1 exceptions after 300 seconds (5 minutes)
python3 -m freight_tower.cli --db tower.db rules set P1 300 --admin-token my-secret-admin-token

# Manually tick (safe to run from cron)
python3 -m freight_tower.cli --db tower.db tick now
```

### 7. Run the outbox worker

```bash
# Continuously process outbound deliveries
python3 -m freight_tower.cli --db tower.db worker run --worker-id my-worker

# Or just process one batch and exit
python3 -m freight_tower.cli --db tower.db worker run --worker-id my-worker --once
```

### 8. Snapshot backup/restore

```bash
# Export
python3 -m freight_tower.cli --db tower.db snapshot export \
  --admin-token my-secret-admin-token --output acme-snapshot.json

# Restore (atomic – replaces all tenant data)
python3 -m freight_tower.cli --db tower.db snapshot import \
  acme-snapshot.json --admin-token my-secret-admin-token
```

---

## HTTP API

All endpoints (except `/health`) require `Authorization: Bearer <token>`.

| Method | Path | Role | Description |
|--------|------|------|-------------|
| GET | `/health` | — | Liveness check |
| POST | `/api/v1/tenants` | any | Bootstrap tenant |
| POST | `/api/v1/credentials` | admin | Create credential |
| GET | `/api/v1/shipments` | any | List shipments (filters: `status`, `reference`) |
| POST | `/api/v1/shipments` | operator/admin | Create shipment |
| GET | `/api/v1/shipments/{id}` | any | Get shipment |
| POST | `/api/v1/shipments/{id}/events` | operator/admin | Ingest carrier event |
| GET | `/api/v1/exceptions` | any | List exceptions (filters: `status`, `severity`, `assignee`, `shipment_id`) |
| PATCH | `/api/v1/exceptions/{id}` | operator/admin | Mutate exception (body: `expected_version`, `action`, ...) |
| GET | `/api/v1/audit` | any | Audit log (filters: `entity_type`, `entity_id`, `action`) |
| GET | `/api/v1/deliveries` | any | List deliveries (filters: `status`, `event_type`) |
| POST | `/api/v1/deliveries/{id}/replay` | admin | Replay dead-lettered delivery |
| POST | `/api/v1/sla-rules` | admin | Set SLA rule |
| POST | `/api/v1/tick` | — | Claim due escalations |
| POST | `/api/v1/worker/claim` | — | Claim next delivery |
| POST | `/api/v1/worker/complete` | — | Acknowledge delivery |
| POST | `/api/v1/worker/fail` | — | Fail delivery |
| GET | `/api/v1/snapshot/export` | admin | Export snapshot |
| POST | `/api/v1/snapshot/import` | admin | Import snapshot |

### Error codes

| Error class | HTTP status |
|-------------|-------------|
| AuthError | 401 |
| AuthzError | 403 |
| NotFoundError | 404 |
| ValidationError | 400 |
| ConflictError | 409 |
| VersionError | 409 |

---

## Python API

```python
from freight_tower.sqlite_store import SQLiteFreightStore
from freight_tower.exceptions import AuthError, AuthzError, ConflictError, NotFoundError, ValidationError, VersionError

store = SQLiteFreightStore("tower.db")  # or ":memory:"

# Tenant management
store.bootstrap_tenant("acme", "Acme Corp", "admin-token")
store.create_credential("admin-token", "op-token", "operator")

# Shipments
shipment = store.create_shipment("op-token", "SHIP-001")
store.get_shipment("op-token", shipment.id)
store.list_shipments("op-token", status="delayed")

# Events (idempotent by event_id)
shipment = store.ingest_event(
    "op-token", shipment.id,
    event_id="ev-001",
    event_type="delayed",     # picked_up | in_transit | delayed | delivered | cancelled
    event_time=1700000000.0,
    location="Memphis, TN",
    details="Weather delay",
)

# Exceptions
excs = store.list_exceptions("op-token", status="open")
exc = store.mutate_exception(
    "op-token", excs[0].id,
    expected_version=1,
    action="acknowledge",    # assign | acknowledge | note | resolve
)

# Audit
entries = store.audit("op-token", entity_type="exception")

# SLA rules
store.set_sla_rule("admin-token", "P1", delay_seconds=300)
count = store.tick(now=time.time())  # returns number of escalations enqueued

# Outbox deliveries
delivery = store.claim_delivery("worker-1", now=time.time())
store.complete_delivery(delivery.id, "worker-1", now=time.time())
# or
store.fail_delivery(delivery.id, "worker-1", "connection refused", now=time.time())

# Admin: replay dead-lettered delivery
store.replay_delivery("admin-token", delivery_id, now=time.time())

# Snapshot
snapshot = store.export_snapshot("admin-token")
store.import_snapshot("admin-token", snapshot)
```

---

## Architecture

```
freight_tower/
  __init__.py          # Public exports
  models.py            # In-memory Shipment dataclass + ShipmentStatus (starter)
  service.py           # In-memory FreightService (starter, preserved)
  exceptions.py        # Domain exception hierarchy
  sqlite_store.py      # SQLiteFreightStore – durable, multi-tenant implementation
  web.py               # HTTP API (ThreadingHTTPServer)
  cli.py               # CLI entry point
  static/
    index.html         # Dashboard shell
    app.js             # Dashboard JavaScript (no unsafe innerHTML with user data)
    styles.css         # Styles
tests/
  test_memory_service.py   # Original starter tests (preserved, unmodified)
  test_sqlite_store.py     # Behavioral tests for SQLiteFreightStore
```

### Key design decisions

- **SQLite WAL mode** with `BEGIN IMMEDIATE` serializes concurrent writers for file-based databases.
- **`:memory:` concurrency** is handled by sharing one connection + a Python `threading.Lock()`.
- **Deterministic projection**: events are ordered by `(event_time ASC, event_id ASC)`.  Terminal states (`delivered`, `cancelled`) can never be reverted by a late historical event.
- **Idempotency**: carrier events are deduplicated on `(event_id, tenant_id)`. A conflicting payload raises `ConflictError`. `INSERT OR IGNORE` protects delivery idempotency keys.
- **Optimistic concurrency**: exception mutations require `expected_version`; stale versions raise `VersionError` (HTTP 409).
- **Transactional outbox**: shipment/exception mutations and their delivery records commit atomically inside the same SQLite transaction.
- **Tenant isolation**: every read and write filters by `tenant_id` derived from the authenticated token.

## Backup and restore

SQLite WAL databases can be safely backed up with:

```bash
# Online backup (safe while server is running)
sqlite3 tower.db ".backup tower-backup.db"

# Or stop the server and copy the file:
cp tower.db tower-$(date +%Y%m%d).db
```

Use the snapshot export/import feature for logical (JSON) backups of a single tenant.
