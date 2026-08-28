"""Custom exception classes for freight tower domain errors."""
from __future__ import annotations


class FreightError(Exception):
    """Base class for all freight tower errors."""


class AuthError(FreightError):
    """Authentication failure – invalid or missing token (HTTP 401)."""


class AuthzError(FreightError):
    """Authorization failure – insufficient role (HTTP 403)."""


class NotFoundError(FreightError):
    """Requested resource does not exist (HTTP 404)."""


class ValidationError(FreightError):
    """Bad request / invalid input (HTTP 400)."""


class ConflictError(FreightError):
    """Idempotency conflict – same key, different payload (HTTP 409)."""


class VersionError(FreightError):
    """Optimistic-locking conflict – stale expected version (HTTP 409)."""
