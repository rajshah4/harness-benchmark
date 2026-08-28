"""Custom exception classes for the freight control tower."""
from __future__ import annotations


class FreightError(Exception):
    """Base exception for freight tower errors."""


class AuthenticationError(FreightError):
    """Invalid or missing credentials."""


class AuthorizationError(FreightError):
    """Insufficient permissions for the requested operation."""


class NotFoundError(FreightError):
    """Requested resource not found."""


class ValidationError(FreightError):
    """Invalid input data."""


class IdempotencyConflictError(FreightError):
    """Same idempotency key used with a conflicting payload."""


class StaleVersionError(FreightError):
    """Version conflict – expected version does not match current version."""
