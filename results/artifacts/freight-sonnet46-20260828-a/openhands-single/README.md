# Freight Control Tower

Durable, multi-tenant freight exception control tower backed by SQLite.
No external database, broker, cloud service, or network call required.

---

## Quick start

```bash
# 1. Initialise the database
python -m freight_tower.cli init --db freight.db

# 2. Bootstrap a tenant and admin credential
python -m freight_tower.cli bootstrap --db freight.db \
    --tenant-id acme --name "ACME Corp" --admin-token my-admin-secret

# 3. Create additional credentials
python -m freight_tower.cli credential --db freight.db \
    --admin-token my-admin-secret --token op-secret --role operator
python -m freight_tower.cli credential --db freight.db \
    --admin-token my-admin-secret --token view-secret --role viewer

# 4. Start the server (serves dashboard + API on http://127.0.0.1:8080)
python -m freight_tower.cli serve --db freight.db --port 8080
```

Open http://127.0.0.1:8080 in a browser, sign in with a bearer token, and
you will see the live operations dashboard.

---

## Authentication

All API calls (except `/api/health`) require an `Authorization: Bearer <token>` header.
Roles:

| Role | Capabilities |
|------|-------------|
| `viewer` | Read shipments, exceptions, audit, deliveries |
| `operator` | + create/ingest shipments, mutate exceptions |
| `admin` | + manage tenants/credentials, SLA rules, tick, replay dead letters, snapshots |

---

## CLI reference

```
freight-tower init --db PATH
freight-tower bootstrap --db PATH --tenant-id ID --name NAME --admin-token TOK
freight-tower credential --db PATH --admin-token TOK --token TOK --role ROLE
freight-tower serve --db PATH [--host HOST] [--port PORT]

freight-tower create-shipment --db PATH --token TOK --reference REF
freight-tower list-shipments --db PATH --token TOK [--status STATUS] [--reference SUBSTR]
freight-tower ingest --db PATH --token TOK --shipment-id ID --event-id EID \
    --event-type TYPE --event-time FLOAT [--location LOC] [--details STR]
freight-tower list-exceptions --db PATH --token TOK [--status S] [--severity S] [--assignee A]
freight-tower mutate-exception --db PATH --token TOK --exception-id ID \
    --version N --action ACTION [--actor NAME] [--assignee A] [--note TEXT]
freight-tower audit --db PATH --token TOK [--resource-type T] [--resource-id ID]

freight-tower set-sla-rule --db PATH --admin-token TOK --severity SEV --delay-seconds N
freight-tower tick --db PATH [--now FLOAT] [--limit N]
freight-tower worker --db PATH [--worker-id ID] [--interval SECS]

freight-tower export-snapshot --db PATH --admin-token TOK [--out FILE]
freight-tower import-snapshot --db PATH --admin-token TOK --file FILE
```

---

## HTTP API

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| GET | `/api/health` | None | Health check |
| POST | `/api/tenants` | Body: `{tenant_id,name,admin_token}` | Bootstrap tenant |
| POST | `/api/credentials` | Admin | Create credential |
| GET | `/api/shipments` | Any | List shipments (`?status=&reference=`) |
| POST | `/api/shipments` | Operator | Create shipment |
| GET | `/api/shipments/{id}` | Any | Get shipment |
| POST | `/api/shipments/{id}/events` | Operator | Ingest carrier event |
| GET | `/api/exceptions` | Any | List exceptions |
| GET | `/api/exceptions/{id}` | Any | Get exception |
| POST | `/api/exceptions/{id}/mutate` | Operator | Mutate exception |
| GET | `/api/audit` | Any | List audit entries |
| GET | `/api/sla-rules` | Any | List SLA rules |
| POST | `/api/sla-rules` | Admin | Set SLA rule |
| POST | `/api/tick` | Any | Trigger escalation check |
| GET | `/api/deliveries` | Any | List outbox deliveries |
| POST | `/api/deliveries/claim` | Worker | Claim delivery |
| POST | `/api/deliveries/{id}/complete` | Worker | Mark complete |
| POST | `/api/deliveries/{id}/fail` | Worker | Mark failed |
| POST | `/api/deliveries/{id}/replay` | Admin | Replay dead letter |
| GET | `/api/snapshot` | Admin | Export tenant snapshot |
| POST | `/api/snapshot` | Admin | Import tenant snapshot |

Mutations accept an `Idempotency-Key` header. Version conflicts return HTTP 409
with `{"type":"version_conflict"}`.

---

## Python API (`SQLiteFreightStore`)

```python
from freight_tower.sqlite_store import SQLiteFreightStore

store = SQLiteFreightStore("freight.db")
store.init_schema()

store.bootstrap_tenant("acme", "ACME Corp", "my-admin-token")
store.create_credential("my-admin-token", "op-token", "operator")

ship = store.create_shipment("op-token", "SHIP-001")
store.ingest_event("op-token", ship.id, "ev-1", "delayed", 1000.0, location="Memphis")

excs = store.list_exceptions("op-token")
store.mutate_exception("op-token", excs[0].id, 1, "acknowledge", actor="ops")

store.set_sla_rule("my-admin-token", "P2", delay_seconds=300)
n = store.tick(now=time.time())          # enqueue escalations

d = store.claim_delivery("worker-1", now=time.time())
store.complete_delivery(d.id, "worker-1", now=time.time())

snap = store.export_snapshot("my-admin-token")
store.import_snapshot("my-admin-token", snap)
```

---

## Running the outbox worker

```bash
# Long-running worker that claims and completes deliveries
python -m freight_tower.cli worker --db freight.db --worker-id w1 --interval 2
```

---

## Backup and restore

```bash
# Backup (SQLite WAL-safe copy)
sqlite3 freight.db ".backup freight-backup.db"

# Or use snapshot export/import for tenant-level backup
python -m freight_tower.cli export-snapshot --db freight.db \
    --admin-token MY_TOKEN --out acme-snapshot.json

python -m freight_tower.cli import-snapshot --db freight.db \
    --admin-token MY_TOKEN --file acme-snapshot.json
```

---

## Running tests

```bash
python -m pytest
```

`create_server` binds but does not start its request loop — existing behaviour preserved.
