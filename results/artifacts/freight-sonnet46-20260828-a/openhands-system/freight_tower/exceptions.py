"""Distinct exception types for authentication, authorization, and domain errors."""
from __future__ import annotations


class FreightError(Exception):
    """Base class for all freight domain errors."""


class AuthError(FreightError):
    """Authentication failure — invalid or missing token."""


class AuthzError(FreightError):
    """Authorization failure — insufficient role for the requested operation."""


class NotFoundError(FreightError):
    """The requested resource does not exist (or is not visible to this tenant)."""


class ValidationError(FreightError):
    """Input validation failure — missing or malformed field."""


class ConflictError(FreightError):
    """Idempotency conflict — same event id reused with a different payload."""


class VersionConflictError(FreightError):
    """Optimistic concurrency failure — expected version does not match current version."""
