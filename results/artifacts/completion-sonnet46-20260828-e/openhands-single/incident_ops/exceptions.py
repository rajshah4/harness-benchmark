"""Exceptions raised by the incident store."""

from __future__ import annotations


class IncidentNotFound(KeyError):
    """Raised when an incident ID does not exist in the store."""


class InvalidTransition(ValueError):
    """Raised when a status transition is not permitted."""


class VersionConflict(Exception):
    """Raised when expected_version does not match the current version."""


class ClaimOwnershipError(ValueError):
    """Raised when a worker tries to complete an escalation it does not own."""
