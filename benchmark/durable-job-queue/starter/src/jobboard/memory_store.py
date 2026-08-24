"""The original in-memory job store."""

from __future__ import annotations

from .models import Job, JobState


class MemoryJobStore:
    def __init__(self) -> None:
        self.jobs: list[Job] = []

    def enqueue(self, kind: str, payload: dict) -> Job:
        job = Job(kind=kind, payload=payload)
        self.jobs.append(job)
        return job

    def next_queued(self) -> Job | None:
        return next((job for job in self.jobs if job.state == JobState.QUEUED), None)

    def get(self, job_id: str) -> Job | None:
        return next((job for job in self.jobs if job.id == job_id), None)
