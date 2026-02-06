import asyncio
from types import SimpleNamespace

from custom_components.hvac_vent_optimizer.climate import FlairRoomClimate


class _FakeApi:
    def __init__(self):
        self.calls = []

    async def async_set_room_setpoint(self, room_id, temp_c):
        self.calls.append((room_id, temp_c))


class _FakeCoordinator:
    def __init__(self):
        self.api = _FakeApi()
        self.data = {
            "vents": {
                "v1": {
                    "room": {"id": "room1", "attributes": {"name": "Office", "set-point-c": 22.0}}
                }
            }
        }
        self.last_update_success = True
        self.hass = SimpleNamespace(config=SimpleNamespace(units=SimpleNamespace(temperature_unit="C")))

    def get_room_by_id(self, room_id):
        return self.data["vents"]["v1"]["room"] if room_id == "room1" else {}

    def get_room_device_info(self, room):
        return {"identifiers": {("hvac_vent_optimizer", f"room_{room.get('id')}")}}

    def get_room_temperature(self, room_id):
        return 21.5

    async def async_request_refresh(self):
        return None


def test_room_climate_properties_and_setpoint():
    coordinator = _FakeCoordinator()
    entity = FlairRoomClimate(coordinator, "entry1", "room1")
    entity.hass = coordinator.hass
    assert entity.name == "Office Climate"
    assert entity.current_temperature == 21.5
    assert entity.target_temperature == 22.0

    asyncio.run(entity.async_set_temperature(temperature=23.0))
    assert coordinator.api.calls == [("room1", 23.0)]


def test_room_climate_setpoint_fahrenheit():
    coordinator = _FakeCoordinator()
    coordinator.hass.config.units.temperature_unit = "F"
    entity = FlairRoomClimate(coordinator, "entry1", "room1")
    entity.hass = coordinator.hass
    asyncio.run(entity.async_set_temperature(temperature=68.0))
    room_id, temp_c = coordinator.api.calls[-1]
    assert room_id == "room1"
    assert round(temp_c, 2) == 20.0


def test_room_climate_availability_checks():
    coordinator = _FakeCoordinator()
    entity = FlairRoomClimate(coordinator, "entry1", "room1")
    entity.hass = coordinator.hass

    coordinator.last_update_success = False
    assert entity.available is False

    coordinator.last_update_success = True
    coordinator.data["vents"]["v1"]["room"] = {}
    assert entity.available is False

    coordinator.data["vents"]["v1"]["room"] = {"id": "room1", "attributes": {}}
    coordinator.get_room_temperature = lambda room_id: None
    assert entity.available is False
