"""Freight control tower – durable multi-tenant exception management."""

from .exceptions import (
    AuthError,
    AuthzError,
    ConflictError,
    FreightError,
    NotFoundError,
    ValidationError,
    VersionError,
)
from .models import Shipment, ShipmentStatus
from .service import FreightService
from .sqlite_store import SQLiteFreightStore

__all__ = [
    # Starter exports (preserved)
    "FreightService",
    "Shipment",
    "ShipmentStatus",
    # New durable store
    "SQLiteFreightStore",
    # Exception hierarchy
    "FreightError",
    "AuthError",
    "AuthzError",
    "ConflictError",
    "NotFoundError",
    "ValidationError",
    "VersionError",
]
