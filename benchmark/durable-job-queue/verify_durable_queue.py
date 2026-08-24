#!/usr/bin/env python3
"""Instructor-owned verifier for the durable job queue experiment."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path


class Checks:
    def __init__(self) -> None:
        self.passed = 0
        self.failures: list[str] = []

    def check(self, name: str, function) -> None:
        try:
            function()
        except Exception as exc:
            self.failures.append(f"{name}: {type(exc).__name__}: {exc}")
        else:
            self.passed += 1
            print(f"PASS {name}")


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def main() -> int:
    if len(sys.argv) != 2:
        raise SystemExit("usage: verify_durable_queue.py WORKSPACE")
    workspace = Path(sys.argv[1]).resolve()
    sys.path.insert(0, str(workspace / "src"))

    from jobboard.durable_runner import DurableJobRunner
    from jobboard.models import JobState
    from jobboard.sqlite_store import SQLiteJobStore

    checks = Checks()

    def regressions() -> None:
        result = subprocess.run(
            [sys.executable, "-m", "pytest", "-q"],
            cwd=workspace,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        require(result.returncode == 0, result.stdout[-2000:])

    checks.check("existing regression tests", regressions)

    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)

        def persistence() -> None:
            path = root / "persistence.sqlite"
            first = SQLiteJobStore(path)
            job = first.enqueue("echo", {"nested": [1, {"ok": True}]}, max_attempts=4, job_id="known-id")
            require(job.id == "known-id", "caller-supplied ID was not preserved")
            second = SQLiteJobStore(path)
            loaded = second.get(job.id)
            require(loaded is not None, "job disappeared after reopening database")
            require(loaded.payload == job.payload, "payload did not round trip")
            require(loaded.max_attempts == 4, "max_attempts did not round trip")
            require(loaded.state == JobState.QUEUED, "new job is not queued")
            require(second.list() and second.list()[0].id == job.id, "list did not return persisted job")
            require(second.list("queued")[0].id == job.id, "string state filter failed")

        checks.check("persistence and listing", persistence)

        def claiming() -> None:
            path = root / "claiming.sqlite"
            store = SQLiteJobStore(path)
            first = store.enqueue("echo", {"order": 1})
            store.enqueue("echo", {"order": 2})
            claimed = store.claim_next(now=10**12)
            require(claimed is not None and claimed.id == first.id, "oldest eligible job was not claimed")
            require(claimed.state == JobState.RUNNING, "claimed job is not running")
            require(claimed.attempts == 1, "claim did not increment attempts")

        checks.check("FIFO claim and attempt accounting", claiming)

        def concurrent_claims() -> None:
            path = root / "concurrent.sqlite"
            seed = SQLiteJobStore(path)
            expected = {seed.enqueue("echo", {"i": index}).id for index in range(20)}

            def worker() -> list[str]:
                store = SQLiteJobStore(path)
                claimed: list[str] = []
                while True:
                    job = store.claim_next(now=10**12)
                    if job is None:
                        return claimed
                    claimed.append(job.id)

            with ThreadPoolExecutor(max_workers=5) as pool:
                groups = list(pool.map(lambda _index: worker(), range(5)))
            observed = [job_id for group in groups for job_id in group]
            require(len(observed) == 20, f"expected 20 claims, got {len(observed)}")
            require(len(set(observed)) == 20, "a job was claimed more than once")
            require(set(observed) == expected, "claimed job set was incomplete")

        checks.check("atomic concurrent claims", concurrent_claims)

        def retry_and_backoff() -> None:
            path = root / "retry.sqlite"
            store = SQLiteJobStore(path)
            store.enqueue("flaky", {"value": 7}, max_attempts=3)
            now = [10**12]
            calls = [0]

            def flaky(payload):
                calls[0] += 1
                if calls[0] < 3:
                    raise RuntimeError(f"attempt {calls[0]}")
                return payload["value"] * 2

            runner = DurableJobRunner(
                store,
                {"flaky": flaky},
                clock=lambda: now[0],
                sleep=lambda _seconds: None,
                backoff_base=2,
            )
            first = runner.run_next()
            require(first.state == JobState.QUEUED, "first failure was not queued for retry")
            require(first.available_at == 10**12 + 2, f"first retry time was {first.available_at}")
            require(runner.run_next() is None, "runner ignored future availability")
            now[0] = 10**12 + 2
            second = runner.run_next()
            require(second.state == JobState.QUEUED, "second failure was not queued for retry")
            require(second.available_at == 10**12 + 6, f"second retry time was {second.available_at}")
            now[0] = 10**12 + 6
            third = runner.run_next()
            require(third.state == JobState.SUCCEEDED, "third attempt did not succeed")
            require(third.attempts == 3 and third.result == 14, "successful result was not persisted")
            require(SQLiteJobStore(path).get(third.id).result == 14, "result did not survive reopen")

        checks.check("retry scheduling and exponential backoff", retry_and_backoff)

        def terminal_failure() -> None:
            path = root / "failure.sqlite"
            store = SQLiteJobStore(path)
            store.enqueue("missing", {}, max_attempts=2)
            now = [10**12]
            runner = DurableJobRunner(store, {}, clock=lambda: now[0], backoff_base=1)
            first = runner.run_next()
            require(first.state == JobState.QUEUED, "unknown kind should retry before max attempts")
            now[0] = first.available_at
            final = runner.run_next()
            require(final.state == JobState.FAILED, "job did not become permanently failed")
            require(final.attempts == 2, "terminal attempt count is wrong")
            require(bool(final.error), "terminal failure did not preserve an error")

        checks.check("unknown handlers and terminal failure", terminal_failure)

        def cancellation() -> None:
            path = root / "cancel.sqlite"
            store = SQLiteJobStore(path)
            queued = store.enqueue("echo", {})
            require(store.cancel(queued.id), "queued cancellation returned false")
            require(store.get(queued.id).state == JobState.CANCELLED, "queued job was not cancelled")
            running = store.enqueue("echo", {})
            store.claim_next(now=10**12)
            require(store.cancel(running.id), "running cancellation returned false")
            after_complete = store.complete(running.id, {"should": "not win"})
            require(after_complete.state == JobState.CANCELLED, "completion overwrote cancellation")
            require(not store.cancel("does-not-exist"), "missing cancellation should return false")

        checks.check("cancellation wins over stale workers", cancellation)

        def recovery() -> None:
            path = root / "recovery.sqlite"
            store = SQLiteJobStore(path)
            original = store.enqueue("echo", {"message": "resumed"}, max_attempts=3)
            claimed = store.claim_next(now=10**12)
            require(claimed.id == original.id, "setup claim failed")
            runner = DurableJobRunner(SQLiteJobStore(path), {"echo": lambda payload: payload})
            recovered = runner.run_next()
            require(recovered.state == JobState.SUCCEEDED, "interrupted job did not recover")
            require(recovered.attempts == 2, "recovered execution did not record a new attempt")
            require(recovered.result == {"message": "resumed"}, "recovered result is wrong")

        checks.check("interrupted-process recovery", recovery)

        def run_until_idle() -> None:
            path = root / "idle.sqlite"
            store = SQLiteJobStore(path)
            for value in range(3):
                store.enqueue("double", {"value": value})
            runner = DurableJobRunner(store, {"double": lambda payload: payload["value"] * 2})
            results = runner.run_until_idle(max_jobs=2)
            require(len(results) == 2, "max_jobs was not respected")
            require(len(store.list(JobState.QUEUED)) == 1, "unexpected queued count after bounded run")
            require(len(runner.run_until_idle()) == 1, "remaining job did not run")

        checks.check("bounded run until idle", run_until_idle)

        def cli_workflow() -> None:
            path = root / "cli.sqlite"
            env = os.environ.copy()
            env["PYTHONPATH"] = str(workspace / "src")

            def command(*arguments: str, ok: bool = True):
                result = subprocess.run(
                    [sys.executable, "-m", "jobboard.cli", "--db", str(path), *arguments],
                    cwd=workspace,
                    env=env,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
                require((result.returncode == 0) == ok, f"CLI exit mismatch: {result.stdout} {result.stderr}")
                return result

            enqueued = command("enqueue", "sum", '{"numbers": [2, 3, 5]}')
            job = json.loads(enqueued.stdout.strip().splitlines()[-1])
            require(job["state"] == "queued", "CLI enqueue did not return queued job")
            command("work")
            status = command("status", "--state", "succeeded")
            rows = [json.loads(line) for line in status.stdout.splitlines() if line.strip()]
            require(len(rows) == 1, "CLI status did not return one succeeded job")
            require(rows[0]["result"] == 10, "CLI sum result is wrong")
            command("enqueue", "echo", "not-json", ok=False)

        checks.check("cross-process CLI workflow", cli_workflow)

    total = checks.passed + len(checks.failures)
    print(f"\n{checks.passed}/{total} checks passed")
    if checks.failures:
        print("FAIL")
        for failure in checks.failures:
            print(f"- {failure}")
        return 1
    print("PASS: durable queue contract satisfied")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
