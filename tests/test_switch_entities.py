import asyncio
from types import SimpleNamespace

from custom_components.hvac_vent_optimizer.switch import FlairRoomActiveSwitch


class _FakeCoordinator:
    def __init__(self, data):
        self.data = data
        self.last_active = None

    async def async_set_room_active(self, room_id, active):
        self.last_active = (room_id, active)

    def is_manual_brand(self):
        return False

    def get_room_device_info(self, room):
        room_id = room.get("id")
        name = (room.get("attributes") or {}).get("name") or f"Room {room_id}"
        return {"identifiers": {("hvac_vent_optimizer", f"room_{room_id}")}, "name": name}


def test_room_switch_state_and_name():
    coordinator = _FakeCoordinator(
        {
            "vents": {
                "v1": {
                    "room": {"id": "room1", "attributes": {"name": "Office", "active": False}}
                }
            }
        }
    )
    entity = FlairRoomActiveSwitch(coordinator, "entry1", "room1")
    assert entity.name == "Office Active"
    assert entity.is_on is False
    assert entity.device_info["identifiers"] == {("hvac_vent_optimizer", "room_room1")}


def test_room_switch_defaults_to_active_and_handles_strings():
    coordinator = _FakeCoordinator(
        {
            "vents": {
                "v1": {"room": {"id": "room1", "attributes": {"name": "Office", "active": None}}},
                "v2": {"room": {"id": "room2", "attributes": {"name": "Guest", "active": "active"}}},
            }
        }
    )
    entity_default = FlairRoomActiveSwitch(coordinator, "entry1", "room1")
    assert entity_default.is_on is True

    entity_string = FlairRoomActiveSwitch(coordinator, "entry1", "room2")
    assert entity_string.is_on is True

    entity_missing = FlairRoomActiveSwitch(coordinator, "entry1", "room3")
    assert entity_missing.is_on is True


def test_room_switch_turn_on_off():
    coordinator = _FakeCoordinator(
        {
            "pucks": {
                "p1": {
                    "room": {"id": "room2", "attributes": {"name": "Bedroom", "active": True}}
                }
            }
        }
    )
    entity = FlairRoomActiveSwitch(coordinator, "entry1", "room2")
    asyncio.run(entity.async_turn_off())
    assert coordinator.last_active == ("room2", False)
    asyncio.run(entity.async_turn_on())
    assert coordinator.last_active == ("room2", True)


def test_async_setup_entry_adds_entities():
    from custom_components.hvac_vent_optimizer import switch as switch_module

    coordinator = _FakeCoordinator(
        {
            "vents": {"v1": {"room": {"id": "room1", "attributes": {}}}},
            "pucks": {"p1": {"room": {"id": "room2", "attributes": {}}}},
        }
    )
    hass = SimpleNamespace(data={"hvac_vent_optimizer": {"entry1": coordinator}})
    entry = SimpleNamespace(entry_id="entry1")
    added = []

    def add_entities(entities):
        added.extend(entities)

    asyncio.run(switch_module.async_setup_entry(hass, entry, add_entities))
    assert len(added) == 2


def test_async_setup_entry_skips_manual():
    from custom_components.hvac_vent_optimizer import switch as switch_module

    coordinator = _FakeCoordinator({"vents": {}, "pucks": {}})
    coordinator.is_manual_brand = lambda: True
    hass = SimpleNamespace(data={"hvac_vent_optimizer": {"entry1": coordinator}})
    entry = SimpleNamespace(entry_id="entry1")
    added = []

    def add_entities(entities):
        added.extend(entities)

    asyncio.run(switch_module.async_setup_entry(hass, entry, add_entities))
    assert added == []
