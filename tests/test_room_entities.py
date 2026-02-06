import asyncio
from types import SimpleNamespace

from custom_components.hvac_vent_optimizer.climate import FlairRoomClimate
from custom_components.hvac_vent_optimizer.sensor import FlairRoomSensor, ROOM_SENSOR_DESCRIPTIONS


class _FakeApi:
    def __init__(self):
        self.calls = []

    async def async_set_room_setpoint(self, room_id, set_point_c, hold_until=None):
        self.calls.append((room_id, set_point_c, hold_until))


class _FakeCoordinator:
    def __init__(self, data, assignments=None):
        self.data = data
        self.entry = SimpleNamespace(options=assignments or {})
        self.api = _FakeApi()
        self.last_update_success = True

    def get_room_by_id(self, room_id):
        for vent in self.data.get("vents", {}).values():
            room = vent.get("room") or {}
            if room.get("id") == room_id:
                return room
        return {}

    def get_room_device_info(self, room):
        room_id = room.get("id")
        name = (room.get("attributes") or {}).get("name") or f"Room {room_id}"
        return {"identifiers": {("hvac_vent_optimizer", f"room_{room_id}")}, "name": name}

    def get_room_temperature(self, room_id):
        room = self.get_room_by_id(room_id)
        temp = (room.get("attributes") or {}).get("current-temperature-c")
        return float(temp) if temp is not None else None

    def get_room_thermostat(self, room_id):
        return "climate.main"

    async def async_request_refresh(self):
        return None

    def is_manual_brand(self):
        return False


def test_room_sensors_values():
    coordinator = _FakeCoordinator(
        {
            "vents": {
                "v1": {
                    "room": {
                        "id": "room1",
                        "attributes": {"name": "Office", "current-temperature-c": 22.5},
                    }
                }
            }
        }
    )
    temp_desc = next(d for d in ROOM_SENSOR_DESCRIPTIONS if d.key == "room_temperature")
    thermo_desc = next(d for d in ROOM_SENSOR_DESCRIPTIONS if d.key == "room_thermostat")

    temp_sensor = FlairRoomSensor(coordinator, "entry1", "room1", temp_desc)
    thermostat_sensor = FlairRoomSensor(coordinator, "entry1", "room1", thermo_desc)

    assert temp_sensor.native_value == 22.5
    assert thermostat_sensor.native_value == "climate.main"
    assert temp_sensor.device_info["identifiers"] == {("hvac_vent_optimizer", "room_room1")}


def test_room_sensor_availability_checks():
    coordinator = _FakeCoordinator(
        {"vents": {"v1": {"room": {"id": "room1", "attributes": {"name": "Office"}}}}}
    )
    temp_desc = next(d for d in ROOM_SENSOR_DESCRIPTIONS if d.key == "room_temperature")
    temp_sensor = FlairRoomSensor(coordinator, "entry1", "room1", temp_desc)

    coordinator.last_update_success = False
    assert temp_sensor.available is False

    coordinator.last_update_success = True
    coordinator.data["vents"]["v1"]["room"] = {}
    assert temp_sensor.available is False

    coordinator.data["vents"]["v1"]["room"] = {"id": "room1", "attributes": {}}
    coordinator.get_room_temperature = lambda room_id: None
    assert temp_sensor.available is False


def test_room_climate_setpoint():
    coordinator = _FakeCoordinator(
        {
            "vents": {
                "v1": {
                    "room": {
                        "id": "room1",
                        "attributes": {"name": "Office", "current-temperature-c": 22.5, "set-point-c": 21},
                    }
                }
            }
        }
    )
    coordinator.hass = SimpleNamespace(config=SimpleNamespace(units=SimpleNamespace(temperature_unit="C")))

    entity = FlairRoomClimate(coordinator, "entry1", "room1")
    entity.hass = coordinator.hass
    assert entity.current_temperature == 22.5
    assert entity.target_temperature == 21.0

    asyncio.run(entity.async_set_temperature(temperature=23))
    assert coordinator.api.calls[0][:2] == ("room1", 23.0)
