from __future__ import annotations

import time
import uuid
from collections.abc import Callable

from .models import Shipment, ShipmentStatus


class FreightService:
    def __init__(self, clock: Callable[[], float] = time.time) -> None:
        self.clock = clock
        self._shipments: dict[str, Shipment] = {}

    def create_shipment(self, tenant_id: str, reference: str) -> Shipment:
        if not tenant_id.strip() or not reference.strip():
            raise ValueError("tenant_id and reference are required")
        now = self.clock()
        shipment = Shipment(str(uuid.uuid4()), tenant_id.strip(), reference.strip(), ShipmentStatus.CREATED, None, now, now)
        self._shipments[shipment.id] = shipment
        return shipment

    def list_shipments(self, tenant_id: str) -> list[Shipment]:
        return sorted((item for item in self._shipments.values() if item.tenant_id == tenant_id), key=lambda item: item.created_at)

    def update_status(self, shipment_id: str, status: ShipmentStatus | str, location: str | None = None) -> Shipment:
        shipment = self._shipments[shipment_id]
        updated = shipment.changed(status=ShipmentStatus(status), last_location=location, updated_at=self.clock(), version=shipment.version + 1)
        self._shipments[shipment_id] = updated
        return updated
