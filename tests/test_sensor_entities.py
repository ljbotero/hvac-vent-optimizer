from types import SimpleNamespace

from custom_components.hvac_vent_optimizer.sensor import (
    FlairPuckSensor,
    FlairSystemSensor,
    FlairVentSensor,
    PUCK_SENSOR_DESCRIPTIONS,
    STRATEGY_METRIC_DESCRIPTIONS,
    VENT_METRIC_SENSOR_DESCRIPTIONS,
    VENT_SENSOR_DESCRIPTIONS,
)


class _FakeCoordinator:
    def __init__(self, data):
        self.data = data

    def get_vent_efficiency_percent(self, vent_id, mode):
        return 42.0

    def get_room_device_info_for_puck(self, puck_id):
        puck = self.data.get("pucks", {}).get(puck_id, {})
        room = puck.get("room") or {}
        room_id = room.get("id")
        if not room_id:
            return None
        name = (room.get("attributes") or {}).get("name") or f"Room {room_id}"
        return {"identifiers": {("hvac_vent_optimizer", f"room_{room_id}")}, "name": name}

    def get_room_device_info_for_vent(self, vent_id):
        vent = self.data.get("vents", {}).get(vent_id, {})
        room = vent.get("room") or {}
        room_id = room.get("id")
        if not room_id:
            return None
        name = (room.get("attributes") or {}).get("name") or f"Room {room_id}"
        return {"identifiers": {("hvac_vent_optimizer", f"room_{room_id}")}, "name": name}

    def get_vent_last_reading(self, vent_id):
        return None

    def get_strategy_metrics(self):
        return {"last_strategy": "hybrid", "strategies": {}}

    def is_manual_brand(self):
        return False

    def get_manual_vents(self):
        return []


def test_puck_sensor_values_and_battery():
    coordinator = _FakeCoordinator(
        {
            "pucks": {
                "p1": {
                    "id": "p1",
                    "name": "Bedroom Puck",
                    "attributes": {
                        "current-temperature-c": 21.5,
                        "current-humidity": 40,
                        "system-voltage": 2.8,
                        "room-pressure": 101.0,
                        "rssi": -40,
                    },
                    "room": {"id": "room1", "attributes": {"name": "Bedroom"}},
                }
            }
        }
    )

    temp_desc = PUCK_SENSOR_DESCRIPTIONS[0]
    temp_sensor = FlairPuckSensor(coordinator, "entry", "p1", temp_desc)
    assert temp_sensor.native_value == 21.5
    assert temp_sensor.device_info["identifiers"] == {("hvac_vent_optimizer", "room_room1")}

    humidity_desc = PUCK_SENSOR_DESCRIPTIONS[1]
    humidity_sensor = FlairPuckSensor(coordinator, "entry", "p1", humidity_desc)
    assert humidity_sensor.native_value == 40

    battery_desc = next(desc for desc in PUCK_SENSOR_DESCRIPTIONS if desc.key == "battery")
    battery_sensor = FlairPuckSensor(coordinator, "entry", "p1", battery_desc)
    assert battery_sensor.native_value == 50

    no_voltage_coordinator = _FakeCoordinator(
        {"pucks": {"p1": {"id": "p1", "name": "No Voltage", "attributes": {}}}}
    )
    battery_sensor = FlairPuckSensor(no_voltage_coordinator, "entry", "p1", battery_desc)
    assert battery_sensor.native_value is None

    pressure_desc = next(desc for desc in PUCK_SENSOR_DESCRIPTIONS if desc.key == "pressure")
    pressure_sensor = FlairPuckSensor(coordinator, "entry", "p1", pressure_desc)
    assert pressure_sensor.native_value == 101.0


def test_puck_sensor_availability_checks():
    class _Coordinator(_FakeCoordinator):
        def __init__(self, data):
            super().__init__(data)
            self.last_update_success = True

    coordinator = _Coordinator({"pucks": {"p1": {"id": "p1", "attributes": {}}}})
    temp_desc = PUCK_SENSOR_DESCRIPTIONS[0]
    sensor = FlairPuckSensor(coordinator, "entry", "p1", temp_desc)
    assert sensor.available is False

    coordinator.last_update_success = False
    assert sensor.available is False


def test_vent_sensor_values():
    coordinator = _FakeCoordinator(
        {
            "vents": {
                "v1": {
                    "id": "v1",
                    "name": "Office Vent",
                    "attributes": {
                        "percent-open": 45,
                        "duct-temperature-c": 19.2,
                        "system-voltage": 2.9,
                        "rssi": -50,
                    },
                    "room": {"id": "room2", "attributes": {"name": "Office"}},
                }
            }
        }
    )

    for desc in VENT_SENSOR_DESCRIPTIONS:
        sensor = FlairVentSensor(coordinator, "entry", "v1", desc)
        if desc.key != "last_reading":
            assert sensor.native_value is not None
        assert sensor.device_info["identifiers"] == {("hvac_vent_optimizer", "room_room2")}


def test_vent_sensor_last_reading_timezone():
    from datetime import datetime

    class _Coordinator(_FakeCoordinator):
        def get_vent_last_reading(self, vent_id):
            return datetime(2026, 1, 1, 12, 0, 0)

    coordinator = _Coordinator({"vents": {"v1": {"id": "v1", "attributes": {}}}})
    desc = next(d for d in VENT_SENSOR_DESCRIPTIONS if d.key == "last_reading")
    sensor = FlairVentSensor(coordinator, "entry", "v1", desc)
    value = sensor.native_value
    assert value.tzinfo is not None


def test_vent_sensor_available_for_efficiency_and_last_reading():
    class _Coordinator(_FakeCoordinator):
        def __init__(self, data):
            super().__init__(data)
            self.last_update_success = True

        def get_vent_last_reading(self, vent_id):
            return None

    coordinator = _Coordinator({"vents": {"v1": {"id": "v1", "attributes": {}}}})
    efficiency_desc = next(d for d in VENT_SENSOR_DESCRIPTIONS if d.key == "cooling_efficiency")
    sensor = FlairVentSensor(coordinator, "entry", "v1", efficiency_desc)
    assert sensor.available is True

    last_reading_desc = next(d for d in VENT_SENSOR_DESCRIPTIONS if d.key == "last_reading")
    sensor = FlairVentSensor(coordinator, "entry", "v1", last_reading_desc)
    assert sensor.available is False


def test_vent_sensor_unavailable_when_update_failed():
    class _Coordinator(_FakeCoordinator):
        def __init__(self, data):
            super().__init__(data)
            self.last_update_success = False

    coordinator = _Coordinator({"vents": {"v1": {"id": "v1", "attributes": {"percent-open": 10}}}})
    desc = next(d for d in VENT_SENSOR_DESCRIPTIONS if d.key == "aperture")
    sensor = FlairVentSensor(coordinator, "entry", "v1", desc)
    assert sensor.available is False


def test_async_setup_entry_adds_entities():
    from custom_components.hvac_vent_optimizer import sensor as sensor_module

    coordinator = _FakeCoordinator(
        {
            "pucks": {"p1": {"id": "p1", "attributes": {}}},
            "vents": {"v1": {"id": "v1", "attributes": {}}},
        }
    )
    hass = SimpleNamespace(data={"hvac_vent_optimizer": {"entry1": coordinator}})
    entry = SimpleNamespace(entry_id="entry1")
    added = []

    def add_entities(entities):
        added.extend(entities)

    import asyncio

    asyncio.run(sensor_module.async_setup_entry(hass, entry, add_entities))
    assert len(added) == (
        len(PUCK_SENSOR_DESCRIPTIONS)
        + len(VENT_SENSOR_DESCRIPTIONS)
        + len(VENT_METRIC_SENSOR_DESCRIPTIONS)
        + 1
        + len(STRATEGY_METRIC_DESCRIPTIONS)
    )


def test_system_sensor_exposes_metrics():
    coordinator = _FakeCoordinator({"pucks": {}, "vents": {}})
    sensor = FlairSystemSensor(coordinator, "entry")
    assert sensor.native_value == "hybrid"
    assert sensor.extra_state_attributes["last_strategy"] == "hybrid"


def test_system_sensor_defaults_to_unknown():
    coordinator = _FakeCoordinator({"pucks": {}, "vents": {}})
    coordinator.get_strategy_metrics = lambda: {}
    sensor = FlairSystemSensor(coordinator, "entry")
    assert sensor.native_value == "unknown"


def test_async_setup_entry_adds_manual_suggested_entities():
    from custom_components.hvac_vent_optimizer import sensor as sensor_module

    class _ManualCoordinator:
        def __init__(self):
            self.data = {
                "vents": {
                    "manual_1": {
                        "id": "manual_1",
                        "name": "Office",
                        "room": {"id": "room1"},
                    }
                }
            }

        def is_manual_brand(self):
            return True

        def get_manual_vents(self):
            return [{"id": "manual_1", "name": "Office"}]

        def get_room_device_info_for_vent(self, vent_id):
            return {"identifiers": {("hvac_vent_optimizer", f"room_{vent_id}")}}

        def get_vent_target(self, vent_id):
            return 40

    coordinator = _ManualCoordinator()
    hass = SimpleNamespace(data={"hvac_vent_optimizer": {"entry1": coordinator}})
    entry = SimpleNamespace(entry_id="entry1")
    added = []

    def add_entities(entities):
        added.extend(entities)

    import asyncio

    asyncio.run(sensor_module.async_setup_entry(hass, entry, add_entities))
    expected_count = (
        len(sensor_module.VENT_SENSOR_DESCRIPTIONS)
        + len(sensor_module.VENT_METRIC_SENSOR_DESCRIPTIONS)
        + len(sensor_module.ROOM_SENSOR_DESCRIPTIONS)
        + 1  # Manual suggested
        + 1  # System sensor
        + len(sensor_module.STRATEGY_METRIC_DESCRIPTIONS)
    )
    assert len(added) == expected_count
    assert any(isinstance(entity, sensor_module.ManualSuggestedApertureSensor) for entity in added)
