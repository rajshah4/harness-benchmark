"""Freight control tower — durable multi-tenant exception management."""

from .exceptions import (
    AuthError,
    AuthzError,
    ConflictError,
    FreightError,
    NotFoundError,
    ValidationError,
    VersionConflictError,
)
from .models import Shipment, ShipmentStatus
from .service import FreightService
from .sqlite_store import SQLiteFreightStore

__all__ = [
    # Starter / in-memory API (preserved for backward compat)
    "FreightService",
    "Shipment",
    "ShipmentStatus",
    # Durable store
    "SQLiteFreightStore",
    # Exception hierarchy
    "FreightError",
    "AuthError",
    "AuthzError",
    "ConflictError",
    "NotFoundError",
    "ValidationError",
    "VersionConflictError",
]
