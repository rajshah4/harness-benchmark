# Freight Control Tower

A durable, multi-tenant freight exception control tower backed by SQLite.
No external database, broker, or network service required.

---

## Quick start

```bash
# Install (editable)
pip install -e .

# 1. Initialise the database
freight-tower --db freight.db init

# 2. Create a tenant and its first admin credential
freight-tower --db freight.db bootstrap \
    --tenant-id acme --name "ACME Corp" --admin-token my-admin-secret

# 3. Add an operator credential
freight-tower --db freight.db credential \
    --admin-token my-admin-secret --token op-secret --role operator

# 4. Create a shipment
freight-tower --db freight.db shipment create \
    --token op-secret --reference ACME-001

# 5. Ingest a carrier event (the shipment id is from step 4)
freight-tower --db freight.db ingest \
    --token op-secret --shipment-id <ID> \
    --event-type delayed --location "Memphis Hub"

# 6. List open exceptions
freight-tower --db freight.db exceptions --token op-secret --status open

# 7. Serve the dashboard + HTTP API
freight-tower --db freight.db serve --host 127.0.0.1 --port 8080
# Open http://127.0.0.1:8080 and sign in with op-secret
```

---

## Running tests

```bash
python -m pytest          # all tests, including the original starter tests
python -m pytest -v       # verbose
```

---

## CLI reference

| Command | Description |
|---|---|
| `init` | Create/migrate DB schema |
| `bootstrap` | Create tenant + admin credential |
| `credential` | Add viewer/operator/admin credential |
| `shipment create/list/get` | Shipment CRUD |
| `ingest` | Ingest a carrier event |
| `exceptions` | List exceptions |
| `mutate` | Acknowledge / assign / note / resolve an exception |
| `audit` | View audit trail |
| `sla-rule` | Set SLA escalation rule |
| `tick` | Enqueue SLA escalations |
| `worker` | Process outbox deliveries |
| `export` | Export tenant snapshot to JSON |
| `import` | Import tenant snapshot from JSON |
| `serve` | Start HTTP + dashboard server |

All commands accept `--db PATH` (default `freight.db`).

---

## HTTP API (bearer token auth)

All `/api/*` routes require `Authorization: Bearer <token>`.

| Method | Path | Description |
|---|---|---|
| GET | `/health` | Health check (no auth) |
| POST | `/api/tenants/bootstrap` | Bootstrap tenant |
| POST | `/api/credentials` | Create credential |
| POST | `/api/shipments` | Create shipment |
| GET | `/api/shipments` | List shipments (`?status=`) |
| GET | `/api/shipments/:id` | Get shipment |
| POST | `/api/shipments/:id/events` | Ingest event (supports `Idempotency-Key` header) |
| GET | `/api/exceptions` | List exceptions (`?status=&severity=&assignee=`) |
| GET | `/api/exceptions/:id` | Get exception |
| POST | `/api/exceptions/:id/mutate` | Mutate exception |
| GET | `/api/audit` | Audit trail |
| POST | `/api/sla-rules` | Set SLA rule |
| POST | `/api/tick` | Trigger SLA tick |
| GET | `/api/deliveries` | List outbox deliveries |
| POST | `/api/deliveries/claim` | Claim delivery |
| POST | `/api/deliveries/:id/complete` | Complete delivery |
| POST | `/api/deliveries/:id/fail` | Fail delivery |
| POST | `/api/deliveries/:id/replay` | Replay dead-lettered delivery |
| POST | `/api/snapshot/export` | Export snapshot |
| POST | `/api/snapshot/import` | Import snapshot |

---

## Worker (outbox delivery processor)

```bash
# Process deliveries continuously (2-second poll)
freight-tower --db freight.db worker

# Process one delivery and exit
freight-tower --db freight.db worker --once
```

---

## Backup and restore

```bash
# Export
freight-tower --db freight.db export --admin-token my-admin-secret --output backup.json

# Restore (atomically replaces the tenant's data)
freight-tower --db freight.db import --admin-token my-admin-secret --input backup.json

# SQLite-level backup (consistent WAL snapshot)
sqlite3 freight.db ".backup freight-backup.db"
```

---

## Python API

```python
from freight_tower.sqlite_store import SQLiteFreightStore

store = SQLiteFreightStore("freight.db")
store.bootstrap_tenant("acme", "ACME Corp", "admin-tok")
store.create_credential("admin-tok", "op-tok", "operator")

ship = store.create_shipment("op-tok", "ACME-001")
store.ingest_event("op-tok", ship.id, "ev-001", "delayed", 1700000000.0,
                   location="Memphis", details={"severity": "P1"})

exceptions = store.list_exceptions("op-tok", status="open")
exc = exceptions[0]
store.mutate_exception("op-tok", exc.id, exc.version, "acknowledge", actor="alice")

store.set_sla_rule("admin-tok", "P1", delay_seconds=300)
count = store.tick(now=1700000400.0)  # enqueues escalations

snapshot = store.export_snapshot("admin-tok")
store.import_snapshot("admin-tok", snapshot)
```

---

## Architecture notes

- **SQLite WAL mode** — concurrent reads never block writes; two instances share the same file safely.
- **BEGIN IMMEDIATE** — all writes serialise at the SQLite level; no application-level locking needed.
- **Thread-local connections** — the HTTP server's thread pool gets a connection per thread.
- **Deterministic event replay** — shipment status is derived by replaying events ordered `(event_time, event_id)`; a late event re-projects correctly without side effects.
- **Transactional outbox** — domain writes and their notification records commit atomically.
- **Idempotency keys** — `INSERT OR IGNORE` on `idempotency_key UNIQUE` prevents duplicate deliveries even under concurrent workers or replayed ticks.

`create_server` binds but does not start its request loop — call `server.serve_forever()` explicitly. This preserves the original starter contract.
