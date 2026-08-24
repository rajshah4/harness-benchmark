# Durable Background Jobs

The existing `jobboard` package can run small jobs in memory. Add a durable SQLite-backed execution path suitable for work that may outlive a process.

Preserve the existing in-memory API and its tests. Do not add third-party runtime dependencies.

## Required behavior

Implement persistent jobs with these states:

- `queued`
- `running`
- `succeeded`
- `failed`
- `cancelled`

A job records its identifier, kind, JSON-compatible payload, state, attempt count, maximum attempts, optional result, optional error, creation and update timestamps, and the next time it is eligible to run.

Add `jobboard.sqlite_store.SQLiteJobStore`. Creating a store must initialize its database automatically. Separate store instances opened on the same file must observe the same data.

The store must support:

- enqueueing a job, with an optional caller-supplied job ID
- retrieving a job by ID
- listing jobs, optionally filtered by state
- atomically claiming the oldest eligible queued job
- completing a running job
- recording an execution failure and either scheduling a retry or marking the job permanently failed
- cancelling a queued or running job
- recovering jobs left in `running` after an interrupted process

Claiming increments the attempt count. Concurrent workers using separate connections must never claim the same job. Completion or failure must not overwrite a job that was cancelled after it was claimed.

Add `jobboard.durable_runner.DurableJobRunner`. It accepts a store, a mapping of job kinds to handler callables, and injectable `clock` and `sleep` callables. A handler receives the job payload and returns a JSON-compatible result.

The runner must:

- recover interrupted running jobs when it starts
- run the next eligible job
- save successful results
- retry exceptions with exponential backoff using `backoff_base * 2 ** (attempts - 1)`
- stop retrying when `max_attempts` is reached
- expose `run_until_idle(max_jobs=None)` without sleeping until future retries become eligible
- treat an unknown job kind as a normal execution failure

## Public interface

Expose these import paths:

```python
from jobboard.models import Job, JobState
from jobboard.sqlite_store import SQLiteJobStore
from jobboard.durable_runner import DurableJobRunner
```

`JobState` must be a string enum. `SQLiteJobStore` must provide:

```python
enqueue(kind, payload, max_attempts=3, job_id=None) -> Job
get(job_id) -> Job | None
list(state=None) -> list[Job]
claim_next(now=None) -> Job | None
complete(job_id, result) -> Job
fail(job_id, error, retry_at=None) -> Job
cancel(job_id) -> bool
recover_running() -> int
```

`DurableJobRunner` must provide:

```python
run_next() -> Job | None
run_until_idle(max_jobs=None) -> list[Job]
```

Use numeric Unix timestamps. If `claim_next` receives no `now`, use the current time. Results, payloads, and errors must survive closing and reopening the database.

## Command line interface

Extend `python -m jobboard.cli` with these commands:

```text
--db PATH enqueue KIND JSON_PAYLOAD [--max-attempts N]
--db PATH work [--max-jobs N]
--db PATH status [--state STATE]
--db PATH cancel JOB_ID
```

Write one JSON value per output line. Include built-in `echo` and `sum` handlers so the CLI can demonstrate a complete durable workflow. The `sum` handler accepts `{"numbers": [...]}` and returns their sum.

Invalid JSON, invalid states, and missing job IDs must produce a useful error and a nonzero exit status.

## Completion expectations

Keep persistence concerns out of the existing in-memory store. Add focused tests for the new behavior without modifying existing tests. Run the full test suite and exercise the CLI across separate processes. Inspect the final Git diff and report any remaining concurrency or durability uncertainty.
