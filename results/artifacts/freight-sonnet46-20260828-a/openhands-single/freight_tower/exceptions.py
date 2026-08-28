"""Domain exceptions for the freight control tower."""
from __future__ import annotations


class FreightError(Exception):
    """Base class for all domain errors."""


class AuthenticationError(FreightError):
    """Token is missing, invalid, or revoked."""


class AuthorizationError(FreightError):
    """Authenticated principal lacks the required role."""


class NotFoundError(FreightError):
    """Resource does not exist within the tenant's scope."""


class ValidationError(FreightError):
    """Input fails structural or business-rule validation."""


class IdempotencyConflict(FreightError):
    """Same idempotency key reused with a conflicting payload."""


class VersionConflict(FreightError):
    """Optimistic-lock version is stale."""
