# Freight Control Tower

A durable, multi-tenant freight exception operations platform.  
All state is stored in a local SQLite database – no external services required.

---

## Quick start

```bash
# 1. Initialise the database
python -m freight_tower.cli --db freight.db init

# 2. Bootstrap your first tenant (creates an admin credential)
python -m freight_tower.cli --db freight.db bootstrap \
    --tenant acme --name "ACME Corp" --token my-admin-token

# 3. Create operator and viewer credentials
python -m freight_tower.cli --db freight.db create-credential \
    --admin-token my-admin-token --token op-token --role operator
python -m freight_tower.cli --db freight.db create-credential \
    --admin-token my-admin-token --token view-token --role viewer

# 4. Start the HTTP server (dashboard + REST API)
python -m freight_tower.cli --db freight.db serve --port 8080
# Open http://localhost:8080 in your browser and sign in with my-admin-token
```

---

## Architecture

```
freight_tower/
├── __init__.py          – public surface (FreightService, SQLiteFreightStore, exceptions)
├── models.py            – Shipment, ShipmentStatus dataclasses (in-memory starter)
├── service.py           – in-memory FreightService (legacy starter – preserved)
├── exceptions.py        – domain exception hierarchy
├── sqlite_store.py      – SQLiteFreightStore (durable, multi-tenant)
├── web.py               – ThreadingHTTPServer (JSON REST + static dashboard)
├── cli.py               – argparse CLI
└── static/
    ├── index.html       – SPA dashboard
    ├── app.js           – vanilla JS, no framework, no XSS
    └── styles.css       – responsive CSS
tests/
├── test_memory_service.py  – original starter tests (preserved)
└── test_sqlite_store.py    – SQLiteFreightStore behavioural tests
```

---

## Database schema

Tables: `tenants`, `credentials`, `shipments`, `carrier_events`, `exceptions`,
`sla_rules`, `audit_log`, `deliveries`.

WAL mode and foreign keys are enabled.  Two instances on the same file observe
each other's committed writes.

---

## Python API – `SQLiteFreightStore`

```python
from freight_tower.sqlite_store import SQLiteFreightStore

store = SQLiteFreightStore("freight.db")   # no listener started
store.init_schema()                        # idempotent DDL

# Tenant / credentials
store.bootstrap_tenant("acme", "ACME Corp", "admin-token")
store.create_credential("admin-token", "op-token", "operator")

# Shipments
ship = store.create_shipment("op-token", "ACME-001")
ship = store.get_shipment("op-token", ship.id)
ships = store.list_shipments("op-token", status="delayed")

# Carrier events (idempotent, deterministic projection)
projection = store.ingest_event(
    "op-token", ship.id,
    event_id="ev-001", event_type="delayed",
    event_time=1700000000.0,
    location="Memphis", details="weather"
)

# Exceptions
excs = store.list_exceptions("op-token", status="open", severity="P1")
exc  = store.mutate_exception("op-token", excs[0].id,
                              expected_version=1,
                              action="acknowledge")

# Audit
entries = store.audit("op-token", resource_type="exception")

# SLA rules and ticking
store.set_sla_rule("admin-token", "P1", delay_seconds=300)
n = store.tick(now=time.time(), limit=100)   # returns count of escalations

# Outbox workers
delivery = store.claim_delivery("worker-1", now=time.time())
store.complete_delivery(delivery.id, "worker-1", now=time.time())
store.fail_delivery(delivery.id, "worker-1", "timeout", now=time.time())

# Replay dead-lettered deliveries (admin only)
store.replay_delivery("admin-token", delivery.id, now=time.time())
deliveries = store.list_deliveries("admin-token", status="dead")

# Snapshot export / import (atomic, tenant-scoped)
snap = store.export_snapshot("admin-token")           # JSON-serialisable dict
store.import_snapshot("admin-token", snap)            # atomically restores
```

---

## HTTP REST API

All routes except `GET /api/health` and static files require:

```
Authorization: Bearer <token>
```

### Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `GET`  | `/api/health` | Health check (no auth) |
| `POST` | `/api/tenants/bootstrap` | Bootstrap tenant (body: `tenant_id`, `name`, `admin_token`) |
| `POST` | `/api/credentials` | Create credential (body: `token`, `role`) |
| `GET`  | `/api/shipments[?status=]` | List shipments |
| `POST` | `/api/shipments` | Create shipment (body: `reference`) |
| `GET`  | `/api/shipments/{id}` | Get shipment |
| `POST` | `/api/shipments/{id}/events` | Ingest carrier event |
| `GET`  | `/api/exceptions[?status=&severity=&assigned_to=]` | List exceptions |
| `PATCH`| `/api/exceptions/{id}` | Mutate exception (body: `action`, `expected_version`, …) |
| `POST` | `/api/exceptions/{id}/mutate` | Same as PATCH |
| `GET`  | `/api/audit[?resource_type=&resource_id=&action=]` | Audit log |
| `GET`  | `/api/deliveries[?status=]` | List outbox deliveries |
| `POST` | `/api/sla-rules` | Upsert SLA rule (admin) |
| `POST` | `/api/tick` | Tick SLA escalations (body: `now`, `limit`) |
| `POST` | `/api/worker/claim` | Claim a delivery (body: `worker_id`) |
| `POST` | `/api/worker/complete` | Complete delivery (body: `delivery_id`, `worker_id`) |
| `POST` | `/api/worker/fail` | Fail delivery (body: `delivery_id`, `worker_id`, `error`) |
| `POST` | `/api/deliveries/replay` | Replay dead delivery (admin; body: `delivery_id`) |
| `POST` | `/api/snapshot/export` | Export tenant snapshot (admin) |
| `POST` | `/api/snapshot/import` | Import tenant snapshot (admin; body: `snapshot`) |

### HTTP status codes

| Code | Meaning |
|------|---------|
| 401  | Missing or invalid token |
| 403  | Insufficient role |
| 404  | Resource not found |
| 409  | Version conflict or idempotency conflict |
| 400  | Validation error |

---

## CLI reference

```bash
freight-tower --db freight.db <command> [options]

Commands:
  init                  Initialise schema
  bootstrap             Create tenant + admin credential
  create-credential     Create operator/viewer credential
  create-shipment       Create a shipment
  get-shipment          Fetch a shipment by id
  list-shipments        List shipments (--status filter)
  ingest-event          Ingest a carrier event
  list-exceptions       List exceptions (--status, --severity)
  mutate-exception      Acknowledge/assign/note/resolve an exception
  audit                 Show audit log
  set-sla-rule          Configure SLA escalation rule
  tick                  Run SLA tick (enqueue due escalations)
  worker                Simple outbox delivery worker (prints & acks)
  list-deliveries       List outbox deliveries
  replay-delivery       Replay a dead-lettered delivery
  export-snapshot       Export tenant snapshot to file or stdout
  import-snapshot       Import tenant snapshot from file
  serve                 Start HTTP server
```

---

## Running the worker

```bash
# Continuously claim and acknowledge deliveries (prints payloads)
python -m freight_tower.cli --db freight.db worker --worker-id my-worker --interval 2
```

---

## Backup and restore

```bash
# Export
python -m freight_tower.cli --db freight.db export-snapshot \
    --admin-token my-admin-token --output backup.json

# Restore to a new database
python -m freight_tower.cli --db restored.db init
python -m freight_tower.cli --db restored.db bootstrap \
    --tenant acme --name "ACME Corp" --token my-admin-token
python -m freight_tower.cli --db restored.db import-snapshot \
    --admin-token my-admin-token --input backup.json

# Or use SQLite's built-in .backup for a full-database copy:
sqlite3 freight.db ".backup freight-backup.db"
```

---

## Running tests

```bash
python -m pytest tests/ -v
```

---

## Roles

| Role | Can do |
|------|--------|
| `viewer` | Read shipments, exceptions, audit, deliveries |
| `operator` | All viewer actions + create shipments, ingest events, mutate exceptions, worker ops |
| `admin` | All operator actions + manage tenants/credentials, SLA rules, tick, replay dead letters, snapshots |
