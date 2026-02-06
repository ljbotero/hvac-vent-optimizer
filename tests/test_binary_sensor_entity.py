from types import SimpleNamespace

from custom_components.hvac_vent_optimizer.binary_sensor import FlairPuckOccupancyBinarySensor


class _FakeCoordinator:
    def __init__(self, data):
        self.data = data
        self.last_update_success = True

    def get_room_device_info_for_puck(self, puck_id):
        return {"identifiers": {("hvac_vent_optimizer", f"room_{puck_id}")}}


def test_puck_occupancy_from_attributes():
    coordinator = _FakeCoordinator(
        {"pucks": {"p1": {"name": "Puck 1", "attributes": {"occupied": True}}}}
    )
    entity = FlairPuckOccupancyBinarySensor(coordinator, "entry", "p1")
    assert entity.is_on is True
    assert entity.available is True
    assert entity.name == "Puck 1 Occupancy"


def test_puck_occupancy_from_room():
    coordinator = _FakeCoordinator(
        {"pucks": {"p1": {"name": "Puck 1", "room": {"attributes": {"occupied": "true"}}}}}
    )
    entity = FlairPuckOccupancyBinarySensor(coordinator, "entry", "p1")
    assert entity.is_on is True
    assert entity.available is True


def test_puck_occupancy_unavailable():
    coordinator = _FakeCoordinator({"pucks": {"p1": {"name": "Puck 1", "attributes": {}}}})
    entity = FlairPuckOccupancyBinarySensor(coordinator, "entry", "p1")
    assert entity.available is False


def test_puck_occupancy_string_values_and_unavailable_on_failure():
    coordinator = _FakeCoordinator(
        {"pucks": {"p1": {"name": "Puck 1", "attributes": {"room-occupied": "occupied"}}}}
    )
    entity = FlairPuckOccupancyBinarySensor(coordinator, "entry", "p1")
    assert entity.is_on is True

    coordinator.last_update_success = False
    assert entity.available is False
