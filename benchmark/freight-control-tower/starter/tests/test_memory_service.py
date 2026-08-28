from freight_tower import FreightService, ShipmentStatus


def test_create_list_and_update() -> None:
    ticks = iter([10.0, 11.0])
    service = FreightService(lambda: next(ticks))
    shipment = service.create_shipment("acme", "ACME-1")
    assert shipment.status is ShipmentStatus.CREATED
    updated = service.update_status(shipment.id, "delayed", "Memphis")
    assert updated.version == 2
    assert service.list_shipments("acme") == [updated]


def test_tenant_lists_are_separate() -> None:
    service = FreightService(lambda: 1.0)
    service.create_shipment("one", "ONE-1")
    service.create_shipment("two", "TWO-1")
    assert [item.reference for item in service.list_shipments("one")] == ["ONE-1"]
