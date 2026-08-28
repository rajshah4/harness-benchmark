"""Core values used by the starter application."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import StrEnum


class Severity(StrEnum):
    P1 = "P1"
    P2 = "P2"
    P3 = "P3"
    P4 = "P4"


class IncidentStatus(StrEnum):
    OPEN = "open"
    ACKNOWLEDGED = "acknowledged"
    RESOLVED = "resolved"


@dataclass(frozen=True, slots=True)
class Incident:
    id: str
    title: str
    severity: Severity
    status: IncidentStatus
    owner: str | None
    created_at: float
    updated_at: float
    # Durable fields added for the SQLite store (backward-compat defaults allow
    # MemoryIncidentStore to keep creating incidents without specifying them).
    fingerprint: str = ""
    alert_count: int = 1
    version: int = 1
    escalation_level: int = 0
    sla_deadline: float = 0.0

    def changed(self, **values: object) -> "Incident":
        return replace(self, **values)


@dataclass(slots=True)
class AuditEvent:
    """Append-only record of every change made to an incident."""

    id: str
    incident_id: str
    type: str
    timestamp: float
    details: dict = field(default_factory=dict)

