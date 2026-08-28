"""Core values used by the starter application."""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Any


class Severity(StrEnum):
    P1 = "P1"
    P2 = "P2"
    P3 = "P3"
    P4 = "P4"


class IncidentStatus(StrEnum):
    OPEN = "open"
    ACKNOWLEDGED = "acknowledged"
    RESOLVED = "resolved"


# SLA deadline offset in seconds per severity level
SLA_SECONDS: dict[str, int] = {
    "P1": 60,
    "P2": 300,
    "P3": 900,
    "P4": 3600,
}

# Valid one-step status transitions
VALID_TRANSITIONS: set[tuple[IncidentStatus, IncidentStatus]] = {
    (IncidentStatus.OPEN, IncidentStatus.ACKNOWLEDGED),
    (IncidentStatus.OPEN, IncidentStatus.RESOLVED),
    (IncidentStatus.ACKNOWLEDGED, IncidentStatus.RESOLVED),
}


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class IncidentNotFound(KeyError):
    """Raised when an incident does not exist."""


class InvalidTransition(ValueError):
    """Raised when a status transition is not allowed."""


class VersionConflict(Exception):
    """Raised when expected_version does not match the current version."""


class ClaimError(Exception):
    """Raised when escalation claim verification fails."""


# ---------------------------------------------------------------------------
# Value objects (immutable)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Incident:
    id: str
    title: str
    severity: Severity
    status: IncidentStatus
    owner: str | None
    created_at: float
    updated_at: float
    # Durable fields added for SQLite store (with defaults for backward compat)
    fingerprint: str = ""
    alert_count: int = 1
    version: int = 1
    escalation_level: int = 0
    sla_deadline: float = 0.0

    def changed(self, **values: object) -> "Incident":
        return replace(self, **values)


@dataclass(frozen=True, slots=True)
class AuditEvent:
    id: str
    incident_id: str
    type: str
    timestamp: float
    details: dict[str, Any]
