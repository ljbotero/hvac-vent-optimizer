import asyncio
from types import SimpleNamespace

from custom_components.hvac_vent_optimizer.coordinator import FlairCoordinator
from custom_components.hvac_vent_optimizer.const import (
    BRAND_MANUAL,
    CONF_VENT_ASSIGNMENTS,
    CONF_CLOSE_INACTIVE_ROOMS,
    CONF_CONTROL_STRATEGY,
    CONF_CONVENTIONAL_VENTS_BY_THERMOSTAT,
    CONF_DAB_ENABLED,
    CONF_MIN_ADJUSTMENT_INTERVAL,
    CONF_MIN_ADJUSTMENT_PERCENT,
    CONF_TEMP_ERROR_OVERRIDE,
    CONF_VENT_BRAND,
    CONF_VENT_GRANULARITY,
)


def _make_coord():
    coord = FlairCoordinator.__new__(FlairCoordinator)
    coord.hass = SimpleNamespace(states=None, config=SimpleNamespace(units=None))
    coord.entry = SimpleNamespace(
        data={},
        options={
            CONF_VENT_BRAND: BRAND_MANUAL,
            CONF_DAB_ENABLED: True,
            CONF_CLOSE_INACTIVE_ROOMS: False,
            CONF_CONTROL_STRATEGY: "hybrid",
            CONF_VENT_GRANULARITY: 5,
            CONF_MIN_ADJUSTMENT_PERCENT: 0,
            CONF_MIN_ADJUSTMENT_INTERVAL: 0,
            CONF_TEMP_ERROR_OVERRIDE: 0.0,
            CONF_CONVENTIONAL_VENTS_BY_THERMOSTAT: {},
        },
    )
    coord.api = None
    coord._vent_rates = {"v1": {"heating": 0.1}, "v2": {"heating": 0.2}}
    coord._max_running_minutes = {}
    coord._vent_last_commanded = {}
    coord._vent_last_target = {}
    coord._vent_models = {}
    coord._cycle_stats = {}
    coord._last_strategy = None
    coord._initial_efficiency_percent = 50
    return coord


def test_async_apply_dab_adjustments_updates_targets_manual():
    coord = _make_coord()
    data = {
        "vents": {
            "v1": {
                "id": "v1",
                "attributes": {"percent-open": 20},
                "room": {
                    "id": "room1",
                    "attributes": {"current-temperature-c": 20.0, "active": True},
                },
            },
            "v2": {
                "id": "v2",
                "attributes": {"percent-open": 20},
                "room": {
                    "id": "room2",
                    "attributes": {"active": False},
                },
            },
        }
    }

    coord._get_thermostat_setpoint = lambda *_: 22.0

    asyncio.run(coord._async_apply_dab_adjustments("climate.main", "heating", ["v1", "v2"], data))
    assert "v1" in coord._vent_last_target
    assert coord._last_strategy == "hybrid"
    assert coord._cycle_stats["climate.main"]["adjustments"] >= 0


def test_async_apply_dab_adjustments_handles_api_timeout():
    class _FailApi:
        async def async_set_vent_position(self, vent_id, target):
            raise asyncio.TimeoutError()

    coord = _make_coord()
    coord.entry.options[CONF_VENT_BRAND] = "flair"
    coord.entry.options[CONF_VENT_ASSIGNMENTS] = {}
    coord.api = _FailApi()
    data = {
        "vents": {
            "v1": {
                "id": "v1",
                "attributes": {"percent-open": 20},
                "room": {
                    "id": "room1",
                    "attributes": {"current-temperature-c": 20.0, "active": True},
                },
            }
        }
    }
    coord._get_thermostat_setpoint = lambda *_: 22.0

    asyncio.run(coord._async_apply_dab_adjustments("climate.main", "heating", ["v1"], data))
    assert coord._vent_last_commanded == {}
