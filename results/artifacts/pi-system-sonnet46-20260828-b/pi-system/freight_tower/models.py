from __future__ import annotations

from dataclasses import dataclass, replace
from enum import StrEnum


class ShipmentStatus(StrEnum):
    CREATED = "created"
    IN_TRANSIT = "in_transit"
    DELAYED = "delayed"
    DELIVERED = "delivered"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class Shipment:
    id: str
    tenant_id: str
    reference: str
    status: ShipmentStatus
    last_location: str | None
    created_at: float
    updated_at: float
    version: int = 1

    def changed(self, **values: object) -> "Shipment":
        return replace(self, **values)
