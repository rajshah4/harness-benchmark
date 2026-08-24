"""Data models shared by job stores and runners."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any
from uuid import uuid4


class JobState(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class Job:
    kind: str
    payload: dict[str, Any]
    id: str = field(default_factory=lambda: str(uuid4()))
    state: JobState = JobState.QUEUED
    attempts: int = 0
    max_attempts: int = 1
    result: Any = None
    error: str | None = None
    created_at: float = 0.0
    updated_at: float = 0.0
    available_at: float = 0.0
