import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

from custom_components.hvac_vent_optimizer.coordinator import FlairCoordinator
from custom_components.hvac_vent_optimizer.const import (
    CONF_CLOSE_INACTIVE_ROOMS,
    CONF_CONTROL_STRATEGY,
    CONF_INITIAL_EFFICIENCY_PERCENT,
    CONF_MIN_ADJUSTMENT_INTERVAL,
    CONF_MIN_ADJUSTMENT_PERCENT,
    CONF_TEMP_ERROR_OVERRIDE,
    CONF_VENT_ASSIGNMENTS,
    CONF_VENT_GRANULARITY,
    CONF_THERMOSTAT_ENTITY,
)


class _FakeState:
    def __init__(self, state, attributes=None, entity_id=None):
        self.state = state
        self.attributes = attributes or {}
        self.entity_id = entity_id


class _FakeStates:
    def __init__(self, mapping):
        self._mapping = mapping

    def get(self, entity_id):
        return self._mapping.get(entity_id)


class _FakeHass:
    def __init__(self, states):
        self.states = states
        self.config = SimpleNamespace(units=SimpleNamespace(temperature_unit="F"))

    def async_create_task(self, coro):
        return asyncio.create_task(coro)


class _FakeEntry:
    def __init__(self, data=None, options=None, entry_id="entry1", title="test"):
        self.data = data or {}
        self.options = options or {}
        self.entry_id = entry_id
        self.title = title


class _FakeApi:
    def __init__(self):
        self.mode_calls = []
        self.remote_calls = []
        self.vent_calls = []

    async def async_set_vent_position(self, vent_id, position):
        self.vent_calls.append((vent_id, position))
        return None

    async def async_set_structure_mode(self, structure_id, mode):
        self.mode_calls.append((structure_id, mode))

    async def async_get_remote_sensor_reading(self, remote_id):
        self.remote_calls.append(remote_id)
        return {"occupied": True}


def _make_coordinator(data=None, options=None, states=None, api=None):
    hass = _FakeHass(_FakeStates(states or {}))
    entry = _FakeEntry(
        data={"structure_id": "struct1"},
        options=options or {},
        entry_id="entry1",
        title="test",
    )
    coord = FlairCoordinator(hass, api or _FakeApi(), entry)
    coord.data = data or {}
    return coord


def test_get_room_helpers():
    coord = _make_coordinator(
        data={"vents": {"v1": {"attributes": {"percent-open": 50}, "room": {"id": "r1"}}}}
    )
    assert coord._get_vent_attribute("v1", coord.data, "percent-open") == 50
    assert coord._get_room_data("v1", coord.data)["id"] == "r1"
    assert coord.resolve_room_id_from_vent("v1") == "r1"


def test_get_room_active_parsing():
    coord = _make_coordinator(
        data={
            "vents": {
                "v1": {"room": {"attributes": {"active": "false"}}},
                "v2": {"room": {"attributes": {"active": True}}},
            }
        }
    )
    assert coord._get_room_active("v1", coord.data) is False
    assert coord._get_room_active("v2", coord.data) is True


def test_get_room_temp_from_sensor_fahrenheit():
    states = {
        "sensor.temp": _FakeState(
            "77", {"unit_of_measurement": "F"}, entity_id="sensor.temp"
        )
    }
    options = {"vent_assignments": {"v1": {"temp_sensor_entity": "sensor.temp"}}}
    coord = _make_coordinator(states=states, options=options)
    temp = coord._get_room_temp("v1", {"vents": {"v1": {}}})
    assert round(temp, 2) == 25.0


def test_get_thermostat_setpoint_cooling_fahrenheit():
    state = _FakeState(
        "cool",
        {
            "temperature_unit": "F",
            "target_temp_high": 75,
            "temperature": 72,
        },
        entity_id="climate.test",
    )
    coord = _make_coordinator(states={"climate.test": state})
    setpoint = coord._get_thermostat_setpoint("climate.test", "cooling")
    assert round(setpoint, 2) == round(((75 - 32) * 5 / 9) - 0.7, 2)


def test_resolve_hvac_action_prefers_hvac_action():
    state = _FakeState(
        "heat_cool",
        {"hvac_action": "heating", "current_temperature": 70},
        entity_id="climate.test",
    )
    coord = _make_coordinator(states={"climate.test": state})
    assert coord._resolve_hvac_action(state) == "heating"


def test_resolve_hvac_action_uses_targets_when_missing_action():
    state = _FakeState(
        "heat_cool",
        {
            "current_temperature": 68,
            "target_temp_low": 70,
            "target_temp_high": 74,
        },
        entity_id="climate.test",
    )
    coord = _make_coordinator(states={"climate.test": state})
    assert coord._resolve_hvac_action(state) == "heating"


def test_calculate_linear_target_percent():
    coord = _make_coordinator()
    percent = coord._calculate_linear_target_percent(20, 22, 0.5, 10)
    assert round(percent, 2) == 40.0


def test_get_model_params_linear_fit():
    coord = _make_coordinator()
    coord._vent_models = {
        "vent1": {
            "heating": {
                "n": 3,
                "sum_x": 150.0,
                "sum_y": 1.5,
                "sum_xx": 7700.0,
                "sum_xy": 85.0,
            }
        }
    }
    params = coord._get_model_params("vent1", "heating")
    assert params is not None
    slope, intercept = params
    assert slope > 0


def test_recompute_polling_interval():
    state = _FakeState(
        "cool",
        {"hvac_action": "cooling"},
        entity_id="climate.test",
    )
    options = {"vent_assignments": {"v1": {"thermostat_entity": "climate.test"}}}
    coord = _make_coordinator(states={"climate.test": state}, options=options)
    asyncio.run(coord._recompute_polling_interval())
    assert coord.update_interval == coord._poll_interval_active


def test_async_initialize_loads_store():
    coord = _make_coordinator()
    coord._store.data = {
        "vent_rates": {"v1": {"cooling": 0.1}},
        "max_rates": {"cooling": 1.2, "heating": 0.8},
        "max_running_minutes": {"climate.test": 15},
    }
    asyncio.run(coord.async_initialize())
    assert coord._vent_rates["v1"]["cooling"] == 0.1
    assert coord._max_rates["cooling"] == 1.2
    assert coord._max_running_minutes["climate.test"] == 15


def test_async_ensure_structure_mode_calls_api():
    api = _FakeApi()
    options = {"dab_enabled": True, "dab_force_manual": True}
    coord = _make_coordinator(options=options, api=api)
    asyncio.run(coord.async_ensure_structure_mode())
    assert api.mode_calls == [("struct1", "manual")]


def test_async_enrich_room_remote_sensor():
    api = _FakeApi()
    coord = _make_coordinator(api=api)
    room = {"relationships": {"remote-sensors": {"data": [{"id": "remote-1"}]}}}
    result = asyncio.run(coord._async_enrich_room(room, {}))
    assert result["attributes"]["occupied"] is True
    assert result["remote_sensor_id"] == "remote-1"


def test_async_setup_thermostat_listeners_registers():
    states = {"climate.test": _FakeState("cool", {"hvac_action": "cooling"})}
    options = {"vent_assignments": {"v1": {"thermostat_entity": "climate.test"}}}
    coord = _make_coordinator(states=states, options=options)
    asyncio.run(coord.async_setup_thermostat_listeners())
    assert len(coord._unsub_thermostat_listeners) == 1


def test_efficiency_percent_uses_initial_value():
    options = {CONF_INITIAL_EFFICIENCY_PERCENT: 40}
    coord = _make_coordinator(options=options)
    assert coord.get_vent_efficiency_percent("vent1", "cooling") == 40.0


def test_build_efficiency_export_includes_room_data():
    coord = _make_coordinator(
        data={
            "vents": {
                "v1": {"room": {"id": "r1", "attributes": {"name": "Office"}}}
            }
        }
    )
    coord._vent_rates = {"v1": {"cooling": 0.25, "heating": 0.1}}
    coord._max_rates = {"cooling": 0.5, "heating": 0.2}

    export = coord.build_efficiency_export()
    room_eff = export["efficiencyData"]["roomEfficiencies"][0]
    assert room_eff["roomId"] == "r1"
    assert room_eff["roomName"] == "Office"
    assert room_eff["ventId"] == "v1"
    assert room_eff["coolingRate"] == 0.25
    assert export["efficiencyData"]["globalRates"]["maxCoolingRate"] == 0.5


def test_async_import_efficiency_matches_vent_id():
    coord = _make_coordinator(
        data={
            "vents": {
                "v1": {"room": {"id": "r1", "attributes": {"name": "Office"}}}
            }
        }
    )
    payload = {
        "efficiencyData": {
            "globalRates": {"maxCoolingRate": 0.8, "maxHeatingRate": 0.6},
            "roomEfficiencies": [
                {
                    "roomId": "r1",
                    "roomName": "Office",
                    "ventId": "v1",
                    "coolingRate": 0.4,
                    "heatingRate": 0.2,
                }
            ],
        }
    }
    result = asyncio.run(coord.async_import_efficiency(payload))
    assert result["applied"] == 1
    assert coord._vent_rates["v1"]["cooling"] == 0.4
    assert coord._max_rates["cooling"] == 0.8


def test_async_import_efficiency_fallback_room_name():
    coord = _make_coordinator(
        data={
            "vents": {
                "v9": {"room": {"id": "room-9", "attributes": {"name": "Guest"}}}
            }
        }
    )
    payload = {
        "efficiencyData": {
            "roomEfficiencies": [
                {"roomName": "Guest", "coolingRate": 0.33, "heatingRate": 0.0}
            ]
        }
    }
    result = asyncio.run(coord.async_import_efficiency(payload))
    assert result["applied"] == 1
    assert coord._vent_rates["v9"]["cooling"] == 0.33


def test_min_adjustment_percent_blocks_small_changes():
    api = _FakeApi()
    state = _FakeState(
        "heat",
        {"target_temp_low": 72},
        entity_id="climate.test",
    )
    options = {
        CONF_VENT_ASSIGNMENTS: {"vent1": {CONF_THERMOSTAT_ENTITY: "climate.test"}},
        CONF_CONTROL_STRATEGY: "cost",
        CONF_MIN_ADJUSTMENT_PERCENT: 10,
        CONF_MIN_ADJUSTMENT_INTERVAL: 30,
        CONF_TEMP_ERROR_OVERRIDE: 0.6,
        CONF_VENT_GRANULARITY: 5,
        CONF_CLOSE_INACTIVE_ROOMS: True,
    }
    coord = _make_coordinator(
        data={
            "vents": {
                "vent1": {
                    "attributes": {"percent-open": 95},
                    "room": {"attributes": {"current-temperature-c": 22.6, "active": True}},
                }
            }
        },
        options=options,
        states={"climate.test": state},
        api=api,
    )
    coord._vent_rates = {"vent1": {"heating": 0.5}}
    asyncio.run(
        coord._async_apply_dab_adjustments("climate.test", "heating", ["vent1"], coord.data)
    )
    assert api.vent_calls == []


def test_min_adjustment_interval_blocks_changes():
    api = _FakeApi()
    state = _FakeState(
        "heat",
        {"target_temp_low": 72},
        entity_id="climate.test",
    )
    options = {
        CONF_VENT_ASSIGNMENTS: {"vent1": {CONF_THERMOSTAT_ENTITY: "climate.test"}},
        CONF_CONTROL_STRATEGY: "cost",
        CONF_MIN_ADJUSTMENT_PERCENT: 0,
        CONF_MIN_ADJUSTMENT_INTERVAL: 30,
        CONF_TEMP_ERROR_OVERRIDE: 0.6,
        CONF_VENT_GRANULARITY: 5,
        CONF_CLOSE_INACTIVE_ROOMS: True,
    }
    coord = _make_coordinator(
        data={
            "vents": {
                "vent1": {
                    "attributes": {"percent-open": 50},
                    "room": {"attributes": {"current-temperature-c": 22.6, "active": True}},
                }
            }
        },
        options=options,
        states={"climate.test": state},
        api=api,
    )
    coord._vent_rates = {"vent1": {"heating": 0.5}}
    coord._vent_last_commanded["vent1"] = datetime.now(timezone.utc)
    asyncio.run(
        coord._async_apply_dab_adjustments("climate.test", "heating", ["vent1"], coord.data)
    )
    assert api.vent_calls == []


def test_inactive_room_can_reopen_for_airflow_safety():
    api = _FakeApi()
    state = _FakeState(
        "heat",
        {"target_temp_low": 72},
        entity_id="climate.test",
    )
    options = {
        CONF_VENT_ASSIGNMENTS: {"vent1": {CONF_THERMOSTAT_ENTITY: "climate.test"}},
        CONF_CONTROL_STRATEGY: "cost",
        CONF_MIN_ADJUSTMENT_PERCENT: 0,
        CONF_MIN_ADJUSTMENT_INTERVAL: 0,
        CONF_TEMP_ERROR_OVERRIDE: 0.6,
        CONF_VENT_GRANULARITY: 5,
        CONF_CLOSE_INACTIVE_ROOMS: True,
    }
    coord = _make_coordinator(
        data={
            "vents": {
                "vent1": {
                    "attributes": {"percent-open": 0},
                    "room": {"attributes": {"current-temperature-c": 22.0, "active": False}},
                }
            }
        },
        options=options,
        states={"climate.test": state},
        api=api,
    )
    coord._vent_rates = {"vent1": {"heating": 0.3}}
    asyncio.run(
        coord._async_apply_dab_adjustments("climate.test", "heating", ["vent1"], coord.data)
    )
    assert api.vent_calls


def test_inactive_room_not_adjusted_when_close_inactive_false():
    api = _FakeApi()
    state = _FakeState(
        "heat",
        {"target_temp_low": 72},
        entity_id="climate.test",
    )
    options = {
        CONF_VENT_ASSIGNMENTS: {"vent1": {CONF_THERMOSTAT_ENTITY: "climate.test"}},
        CONF_CONTROL_STRATEGY: "cost",
        CONF_MIN_ADJUSTMENT_PERCENT: 0,
        CONF_MIN_ADJUSTMENT_INTERVAL: 0,
        CONF_TEMP_ERROR_OVERRIDE: 0.6,
        CONF_VENT_GRANULARITY: 5,
        CONF_CLOSE_INACTIVE_ROOMS: False,
    }
    coord = _make_coordinator(
        data={
            "vents": {
                "vent1": {
                    "attributes": {"percent-open": 50},
                    "room": {"attributes": {"current-temperature-c": 22.0, "active": False}},
                }
            }
        },
        options=options,
        states={"climate.test": state},
        api=api,
    )
    coord._vent_rates = {"vent1": {"heating": 0.5}}
    asyncio.run(
        coord._async_apply_dab_adjustments("climate.test", "heating", ["vent1"], coord.data)
    )
    assert api.vent_calls == []


def test_inactive_room_can_open_for_safety_when_close_inactive_false():
    api = _FakeApi()
    state = _FakeState(
        "heat",
        {"target_temp_low": 72},
        entity_id="climate.test",
    )
    options = {
        CONF_VENT_ASSIGNMENTS: {"vent1": {CONF_THERMOSTAT_ENTITY: "climate.test"}},
        CONF_CONTROL_STRATEGY: "cost",
        CONF_MIN_ADJUSTMENT_PERCENT: 0,
        CONF_MIN_ADJUSTMENT_INTERVAL: 0,
        CONF_TEMP_ERROR_OVERRIDE: 0.6,
        CONF_VENT_GRANULARITY: 5,
        CONF_CLOSE_INACTIVE_ROOMS: False,
    }
    coord = _make_coordinator(
        data={
            "vents": {
                "vent1": {
                    "attributes": {"percent-open": 0},
                    "room": {"attributes": {"current-temperature-c": 22.0, "active": False}},
                }
            }
        },
        options=options,
        states={"climate.test": state},
        api=api,
    )
    coord._vent_rates = {"vent1": {"heating": 0.3}}
    asyncio.run(
        coord._async_apply_dab_adjustments("climate.test", "heating", ["vent1"], coord.data)
    )
    assert api.vent_calls
