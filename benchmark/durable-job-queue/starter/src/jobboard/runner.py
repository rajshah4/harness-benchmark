"""The original synchronous in-memory runner."""

from __future__ import annotations

from collections.abc import Callable, Mapping

from .memory_store import MemoryJobStore
from .models import Job, JobState


class JobRunner:
    def __init__(self, store: MemoryJobStore, handlers: Mapping[str, Callable]) -> None:
        self.store = store
        self.handlers = handlers

    def run_next(self) -> Job | None:
        job = self.store.next_queued()
        if job is None:
            return None
        job.state = JobState.RUNNING
        job.attempts += 1
        try:
            job.result = self.handlers[job.kind](job.payload)
        except Exception as exc:
            job.state = JobState.FAILED
            job.error = str(exc)
        else:
            job.state = JobState.SUCCEEDED
        return job
