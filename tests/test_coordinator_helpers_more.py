from types import SimpleNamespace

import pytest

from custom_components.hvac_vent_optimizer.coordinator import FlairCoordinator
from custom_components.hvac_vent_optimizer.dab import DEFAULT_SETTINGS
from custom_components.hvac_vent_optimizer.const import (
    BRAND_MANUAL,
    CONF_MANUAL_VENTS,
    CONF_TEMP_SENSOR_ENTITY,
    CONF_THERMOSTAT_ENTITY,
    CONF_VENT_BRAND,
)


class _DummyState:
    def __init__(self, state, attributes):
        self.state = state
        self.attributes = attributes


class _DummyStates:
    def __init__(self, mapping):
        self._mapping = mapping

    def get(self, entity_id):
        return self._mapping.get(entity_id)


class _DummyUnits:
    temperature_unit = "C"


class _DummyConfig:
    units = _DummyUnits()


class _DummyHass:
    def __init__(self, states):
        self.states = states
        self.config = _DummyConfig()


def _make_coord():
    coord = FlairCoordinator.__new__(FlairCoordinator)
    coord.hass = _DummyHass(_DummyStates({}))
    coord.entry = SimpleNamespace(
        data={},
        options={
            CONF_VENT_BRAND: BRAND_MANUAL,
            CONF_MANUAL_VENTS: [
                {
                    "id": "vent1",
                    "name": "Vent 1",
                    CONF_THERMOSTAT_ENTITY: "climate.test",
                    CONF_TEMP_SENSOR_ENTITY: "sensor.temp",
                }
            ],
        },
    )
    coord._vent_models = {}
    coord._efficiency_models = {}
    coord._vent_rates = {}
    coord._max_rates = {"cooling": 0.0, "heating": 0.0}
    coord._initial_efficiency_percent = 50
    coord._vent_last_target = {"vent1": 22}
    coord.data = {"vents": {"vent1": {"id": "vent1", "name": "Room 1", "room": {"id": "r1"}}}}
    return coord


def test_get_vent_assignments_manual():
    coord = _make_coord()
    assignments = coord._get_vent_assignments()
    assert assignments["vent1"][CONF_THERMOSTAT_ENTITY] == "climate.test"
    assert assignments["vent1"][CONF_TEMP_SENSOR_ENTITY] == "sensor.temp"


def test_room_helpers_and_temperature_resolution():
    state_map = {
        "sensor.temp": _DummyState("72", {"unit_of_measurement": "F"}),
        "sensor.other": _DummyState("21.0", {"unit_of_measurement": "C"}),
    }
    coord = _make_coord()
    coord.hass = _DummyHass(_DummyStates(state_map))
    coord.data = {
        "vents": {
            "vent1": {
                "id": "vent1",
                "room": {"id": "room1", "attributes": {"name": "Office", "active": "true"}},
            }
        }
    }
    assert coord._get_room_name("vent1", coord.data) == "Office"
    assert coord._get_room_active("vent1", coord.data) is True
    assert coord._get_room_temp("vent1", coord.data) == pytest.approx(22.222, rel=1e-3)

    # fallback to room temp
    coord.entry.options[CONF_MANUAL_VENTS][0][CONF_TEMP_SENSOR_ENTITY] = None
    coord.data["vents"]["vent1"]["room"]["attributes"]["current-temperature-c"] = 20.5
    assert coord._get_room_temp("vent1", coord.data) == pytest.approx(20.5)


def test_get_room_temperature_prefers_assigned_sensor():
    state_map = {
        "sensor.temp": _DummyState("68", {"unit_of_measurement": "F"}),
    }
    coord = _make_coord()
    coord.hass = _DummyHass(_DummyStates(state_map))
    coord.data = {
        "vents": {
            "vent1": {
                "id": "vent1",
                "room": {"id": "room1", "attributes": {"current-temperature-c": 25.0}},
            }
        }
    }
    assert coord.get_room_temperature("room1") == pytest.approx(20.0)


def test_room_lookup_helpers():
    coord = _make_coord()
    coord.data = {
        "vents": {
            "vent1": {"id": "vent1", "room": {"id": "room1", "attributes": {"name": "Office"}}}
        },
        "pucks": {
            "puck1": {"id": "puck1", "room": {"id": "room2", "attributes": {"name": "Bedroom"}}}
        },
    }
    assert coord.get_room_for_vent("vent1")["id"] == "room1"
    assert coord.get_room_for_puck("puck1")["id"] == "room2"
    assert coord.get_room_by_id("room2")["id"] == "room2"
    assert coord.get_room_by_id("missing") == {}


def test_room_active_string_false_defaults():
    coord = _make_coord()
    coord.data = {
        "vents": {
            "vent1": {"room": {"id": "room1", "attributes": {"active": "false"}}}
        }
    }
    assert coord._get_room_active("vent1", coord.data) is False
    coord.data["vents"]["vent1"]["room"]["attributes"].pop("active")
    assert coord._get_room_active("vent1", coord.data) is True


def test_get_thermostat_entities_from_assignments():
    coord = _make_coord()
    coord.entry.options[CONF_MANUAL_VENTS] = [
        {"id": "vent1", CONF_THERMOSTAT_ENTITY: "climate.b"},
        {"id": "vent2", CONF_THERMOSTAT_ENTITY: "climate.a"},
    ]
    assert coord._get_thermostat_entities() == ["climate.a", "climate.b"]


def test_get_room_thermostat_uses_assignments():
    coord = _make_coord()
    coord.data = {
        "vents": {
            "vent1": {"id": "vent1", "room": {"id": "room1"}},
            "vent2": {"id": "vent2", "room": {"id": "room1"}},
        }
    }
    coord.entry.options[CONF_MANUAL_VENTS] = [
        {"id": "vent1", CONF_THERMOSTAT_ENTITY: "climate.b"},
        {"id": "vent2", CONF_THERMOSTAT_ENTITY: "climate.a"},
    ]
    assert coord.get_room_thermostat("room1") == "climate.a"


def test_resolve_hvac_action_uses_explicit_action():
    coord = _make_coord()
    state = _DummyState("heat", {"hvac_action": "heating"})
    assert coord._resolve_hvac_action(state) == "heating"


def test_resolve_hvac_action_heat_mode():
    coord = _make_coord()
    state = _DummyState(
        "heat",
        {"current_temperature": 19.0, "temperature": 20.0, "temperature_unit": "C"},
    )
    assert coord._resolve_hvac_action(state) == "heating"


def test_resolve_hvac_action_auto_in_band():
    coord = _make_coord()
    state = _DummyState(
        "heat_cool",
        {
            "current_temperature": 22.0,
            "target_temp_low": 20.0,
            "target_temp_high": 24.0,
            "temperature_unit": "C",
        },
    )
    assert coord._resolve_hvac_action(state) is None


def test_resolve_hvac_action_cool_mode():
    coord = _make_coord()
    state = _DummyState(
        "cool",
        {"current_temperature": 25.0, "temperature": 23.0, "temperature_unit": "C"},
    )
    assert coord._resolve_hvac_action(state) == "cooling"


def test_get_thermostat_setpoint_with_fahrenheit():
    coord = _make_coord()
    state = _DummyState(
        "heat",
        {"temperature": 70.0, "temperature_unit": "F"},
    )
    coord.hass.states = _DummyStates({"climate.test": state})
    setpoint = coord._get_thermostat_setpoint("climate.test", "heating")
    assert setpoint == pytest.approx((70 - 32) * 5 / 9 + DEFAULT_SETTINGS.setpoint_offset)


def test_get_thermostat_setpoint_cooling_and_missing():
    coord = _make_coord()
    state = _DummyState(
        "cool",
        {"target_temp_high": 24.0, "temperature_unit": "C"},
    )
    coord.hass.states = _DummyStates({"climate.cool": state})
    setpoint = coord._get_thermostat_setpoint("climate.cool", "cooling")
    assert setpoint == pytest.approx(24.0 - DEFAULT_SETTINGS.setpoint_offset)

    state_missing = _DummyState("cool", {"temperature_unit": "C"})
    coord.hass.states = _DummyStates({"climate.missing": state_missing})
    assert coord._get_thermostat_setpoint("climate.missing", "cooling") is None


def test_get_thermostat_setpoint_heating_uses_target_low():
    coord = _make_coord()
    state = _DummyState(
        "heat_cool",
        {"target_temp_low": 19.0, "temperature_unit": "C"},
    )
    coord.hass.states = _DummyStates({"climate.heat": state})
    setpoint = coord._get_thermostat_setpoint("climate.heat", "heating")
    assert setpoint == pytest.approx(19.0 + DEFAULT_SETTINGS.setpoint_offset)


def test_get_thermostat_target_raw_cooling_fahrenheit():
    coord = _make_coord()
    state = _DummyState(
        "cool",
        {"target_temp_high": 75.0, "temperature_unit": "F"},
    )
    coord.hass.states = _DummyStates({"climate.cool": state})
    target = coord._get_thermostat_target_raw("climate.cool", "cooling")
    assert target == pytest.approx((75 - 32) * 5 / 9)


def test_update_strategy_metrics_tracks_active_rooms():
    coord = _make_coord()
    coord._strategy_metrics = {}
    coord._update_strategy_metrics("hybrid", 1.0, 2, 5.0, active_temp_error=0.5, active_rooms=3)
    metrics = coord._strategy_metrics["hybrid"]
    assert metrics["cycles"] == 1
    assert metrics["active_cycles"] == 1
    assert metrics["last_active_temp_error"] == pytest.approx(0.5)
    assert metrics["last_active_rooms"] == 3


def test_coerce_temperature_fahrenheit():
    coord = _make_coord()
    assert coord._coerce_temperature(68.0, "F") == pytest.approx(20.0)
    assert coord._coerce_temperature("bad", "C") is None


def test_resolve_temperature_unit_defaults_to_hass():
    coord = _make_coord()
    assert coord._resolve_temperature_unit(None) == "C"


def test_calculate_helpers_and_models():
    coord = _make_coord()
    assert coord._calculate_temp_error("heating", 21.0, 19.0) == pytest.approx(2.0)
    assert coord._calculate_temp_error("cooling", 21.0, 23.0) == pytest.approx(2.0)
    assert coord._calculate_temp_error("idle", 21.0, 23.0) is None

    assert coord._calculate_linear_target_percent(21.0, 23.0, 0.0, 30.0) == 100.0
    assert coord._calculate_linear_target_percent(21.0, 23.0, 0.2, 30.0) > 0
    assert coord._calculate_linear_target_percent(23.0, 23.0, 0.2, 30.0) == 0.0

    cost = coord._cost_for_target(21.0, 23.0, 0.2, 30.0, 40.0, 35.0)
    assert cost > 0
    assert coord._cost_for_target(21.0, 23.0, 0.0, 30.0, 0.0, 35.0) >= 0

    coord._vent_models = {
        "vent1": {
            "heating": {"n": 2, "sum_x": 3.0, "sum_y": 5.0, "sum_xx": 5.0, "sum_xy": 9.0}
        }
    }
    params = coord._get_model_params("vent1", "heating")
    assert params is not None
    slope, intercept = params
    assert slope == pytest.approx(3.0)
    assert intercept == pytest.approx(-2.0)

    coord._vent_models = {
        "vent1": {"heating": {"n": 1, "sum_x": 1.0, "sum_y": 1.0, "sum_xx": 1.0, "sum_xy": 1.0}}
    }
    assert coord._get_model_params("vent1", "heating") is None

    coord._vent_models = {
        "vent1": {"heating": {"n": 2, "sum_x": 1.0, "sum_y": 2.0, "sum_xx": 0.5, "sum_xy": 1.0}}
    }
    assert coord._get_model_params("vent1", "heating") is None


def test_get_vent_efficiency_percent_uses_default():
    coord = _make_coord()
    coord._vent_rates = {"vent1": {"heating": 0.0}}
    assert coord.get_vent_efficiency_percent("vent1", "heating") == 50.0


def test_get_room_device_info_manual_brand():
    coord = _make_coord()
    info = coord.get_room_device_info({"id": "room1", "attributes": {"name": "Office"}})
    assert info["manufacturer"] == "Manual"


def test_get_room_device_info_flair_brand():
    coord = _make_coord()
    coord.entry.options[CONF_VENT_BRAND] = "flair"
    info = coord.get_room_device_info({"id": "room1", "attributes": {"name": "Office"}})
    assert info["manufacturer"] == "Flair"


def test_get_vent_target():
    coord = _make_coord()
    assert coord.get_vent_target("vent1") == 22
