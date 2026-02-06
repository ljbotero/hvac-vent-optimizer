from custom_components.hvac_vent_optimizer.binary_sensor import FlairPuckOccupancyBinarySensor
from types import SimpleNamespace


class _FakeCoordinator:
    def __init__(self, data):
        self.data = data

    def get_room_device_info_for_puck(self, puck_id):
        puck = self.data.get("pucks", {}).get(puck_id, {})
        room = puck.get("room") or {}
        room_id = room.get("id")
        if not room_id:
            return None
        name = (room.get("attributes") or {}).get("name") or f"Room {room_id}"
        return {"identifiers": {("hvac_vent_optimizer", f"room_{room_id}")}, "name": name}


def test_occupancy_from_puck_attributes():
    coordinator = _FakeCoordinator(
        {
            "pucks": {
                "p1": {
                    "name": "P1",
                    "attributes": {"room-occupied": "true"},
                    "room": {"id": "room1", "attributes": {"name": "Room One"}},
                }
            }
        }
    )
    sensor = FlairPuckOccupancyBinarySensor(coordinator, "entry", "p1")
    assert sensor.is_on is True
    assert sensor.device_info["identifiers"] == {("hvac_vent_optimizer", "room_room1")}


def test_occupancy_from_room_attributes():
    coordinator = _FakeCoordinator(
        {
            "pucks": {
                "p1": {
                    "name": "P1",
                    "attributes": {},
                    "room": {"attributes": {"occupied": True}},
                }
            }
        }
    )
    sensor = FlairPuckOccupancyBinarySensor(coordinator, "entry", "p1")
    assert sensor.is_on is True


def test_async_setup_entry_adds_entities():
    from custom_components.hvac_vent_optimizer import binary_sensor as binary_module

    coordinator = _FakeCoordinator({"pucks": {"p1": {"name": "P1", "attributes": {}}}})
    hass = SimpleNamespace(data={"hvac_vent_optimizer": {"entry1": coordinator}})
    entry = SimpleNamespace(entry_id="entry1")
    added = []

    def add_entities(entities):
        added.extend(entities)

    import asyncio

    asyncio.run(binary_module.async_setup_entry(hass, entry, add_entities))
    assert len(added) == 1
