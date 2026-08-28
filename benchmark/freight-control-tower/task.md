# Build a durable freight exception control tower

Evolve the starter into an operations application used by multiple freight customers. Preserve the existing package entry points and public starter tests. The completed app must run locally and must not require an external database, broker, cloud service, or network call.

## Product contract

1. **Durable data and restart.** Store tenants, users or tokens, shipments, carrier events, exceptions, SLA rules, outbox deliveries, and audit entries durably. Two service instances opened on the same database must observe committed work. Provide an explicit schema initialization/migration path. Do not start network listeners merely by importing or constructing the app.

2. **Deterministic carrier-event projection.** Ingest carrier events containing a tenant, shipment reference, globally unique event id, event type, event time, and optional location/details. Support at least `picked_up`, `in_transit`, `delayed`, `delivered`, and `cancelled`. Events can arrive out of timestamp order: the materialized shipment status, last location, and active exception must equal a deterministic replay ordered by event time with a stable tie-break. A late historical event must not incorrectly roll back a later delivery.

3. **Idempotent concurrent ingestion.** Repeating the same event id and semantic payload returns the original result without adding another event, exception, audit entry, or notification. Reusing an event id with a conflicting payload is a conflict. Concurrent duplicate submissions from separate service instances produce one durable effect.

4. **Exception workflow and optimistic concurrency.** A `delayed` projection opens one active exception per shipment; delivery or cancellation resolves it. A later delay after resolution may reopen a new exception. Operators can assign, acknowledge, add a note, and resolve an exception. Mutations require an expected integer version; stale versions fail without partial changes. Record actor, action, time, and relevant ids in an append-only audit trail.

5. **Tenant isolation and roles.** Every operational read and write is scoped to a tenant. Provide durable credentials for `viewer`, `operator`, and `admin`. Viewers cannot mutate, operators can operate shipments and exceptions, and only admins can manage tenants/credentials, SLA rules, replay dead letters, and import snapshots. Return authentication, authorization, not-found, validation, and version-conflict failures distinctly. Never reveal another tenant's resource merely because its id is known.

6. **SLA rules and durable scheduling.** Admins can configure a tenant rule such as “enqueue an escalation if a P1 delay is still unacknowledged after N seconds.” A deterministic `tick(now, limit)` operation claims due work safely across concurrent instances. Acknowledged/resolved exceptions do not escalate; an eligible exception enqueues once even across repeated or concurrent ticks. Restarting must not lose timers.

7. **Transactional outbox delivery.** Domain mutations and their notification records commit atomically. Workers claim deliveries with owner and lease expiry, acknowledge success, or fail with an error and retry schedule. Expired leases are recoverable. Enforce a bounded retry policy, dead-letter exhausted deliveries, and allow an admin to replay them. Concurrent workers must not deliver the same attempt. Expose delivery history and stable idempotency keys suitable for downstream deduplication.

8. **Complete operations surfaces.** Provide documented Python APIs and a JSON HTTP API for shipment/event ingestion, shipment and exception lists/details, exception mutations, audit history, rules, ticking, and outbox work. Use bearer credentials for non-health HTTP routes. Mutations accept an `Idempotency-Key` where applicable and version conflicts use HTTP 409. Provide a CLI with database selection and commands for initialization, credential bootstrap, ingest/list/operate, tick/worker, and tenant snapshot export/import. Snapshot import must be atomic and tenant-safe.

9. **Usable browser dashboard.** Serve a responsive dashboard from the same app. It must authenticate without embedding a credential in source, show shipment and exception states, filter by status/severity/assignee, display audit/delivery context, and let an operator acknowledge and resolve exceptions. Show visible loading, empty, success, validation, authorization, and conflict feedback. Avoid unsafe HTML interpolation of carrier-supplied values.

10. **Quality.** Keep deterministic tests fast, document how to initialize, run, authenticate, use the CLI/API, start a worker, and back up/restore. Preserve existing behavior unless the contract explicitly strengthens it. Do not weaken or delete tests.

You choose the schema, architecture, module layout, and UI design. Favor coherent product behavior over mocks or verifier-specific branches.

## Minimum Python compatibility API

To make independent behavioral scoring possible while leaving architecture open, provide `freight_tower.sqlite_store.SQLiteFreightStore`. Its constructor accepts a database path plus optional `clock`, `lease_seconds`, and `max_attempts`. Provide these methods (additional methods are welcome):

- `bootstrap_tenant(tenant_id, name, admin_token)`; `create_credential(admin_token, token, role)`
- `create_shipment(token, reference)`; `get_shipment(token, shipment_id)`; `list_shipments(token, **filters)`
- `ingest_event(token, shipment_id, event_id, event_type, event_time, location=None, details=None)` returning the current shipment projection
- `list_exceptions(token, **filters)`; `mutate_exception(token, exception_id, expected_version, action, actor=None, **values)`
- `audit(token, **filters)`
- `set_sla_rule(admin_token, severity, delay_seconds)`; `tick(now, limit=100)` returning the number of newly enqueued escalations
- `claim_delivery(worker_id, now)`; `complete_delivery(delivery_id, worker_id, now)`; `fail_delivery(delivery_id, worker_id, error, now)`; `replay_delivery(admin_token, delivery_id, now)`; `list_deliveries(token, **filters)`
- `export_snapshot(admin_token)` returning a JSON-serializable value and `import_snapshot(admin_token, snapshot)` atomically restoring only that credential's tenant

Returned domain values may be dataclasses, mappings, or objects, but expose named fields used by the contract (`id`, `status`, `version`, and so on). Raise distinct exception classes or clearly distinct exception messages for authentication, authorization, not-found, validation, idempotency conflict, and stale version failures.
