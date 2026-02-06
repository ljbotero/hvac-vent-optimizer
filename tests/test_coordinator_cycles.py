import asyncio
from types import SimpleNamespace

from custom_components.hvac_vent_optimizer.coordinator import FlairCoordinator
from custom_components.hvac_vent_optimizer.const import CONF_CONTROL_STRATEGY


class _State:
    def __init__(self, state, attributes):
        self.state = state
        self.attributes = attributes


class _States:
    def __init__(self, mapping):
        self._mapping = mapping

    def get(self, entity_id):
        return self._mapping.get(entity_id)


def _make_coord():
    coord = FlairCoordinator.__new__(FlairCoordinator)
    coord.hass = SimpleNamespace(
        states=_States({}),
        async_create_task=lambda coro: coro,
        config=SimpleNamespace(units=SimpleNamespace(temperature_unit="C")),
    )
    coord.entry = SimpleNamespace(options={CONF_CONTROL_STRATEGY: "hybrid"}, data={})
    coord._last_hvac_action = {}
    coord._dab_state = {}
    coord._cycle_stats = {}
    coord._vent_starting_temps = {}
    coord._vent_starting_open = {}
    coord._pending_finalize = {}
    return coord


def test_process_thermostat_group_starts_cycle_and_applies():
    coord = _make_coord()
    climate = _State("heat", {"hvac_action": "heating"})
    coord.hass.states = _States({"climate.main": climate})
    called = {}

    async def fake_apply(thermo, action, vent_ids, data):
        called["apply"] = (thermo, action, tuple(vent_ids))

    coord._async_apply_dab_adjustments = fake_apply
    data = {
        "vents": {
            "v1": {
                "attributes": {"percent-open": 20},
                "room": {"attributes": {"current-temperature-c": 20.0}},
            }
        }
    }
    asyncio.run(coord._async_process_thermostat_group("climate.main", ["v1"], data))
    assert "climate.main" in coord._dab_state
    assert called["apply"][0] == "climate.main"


def test_process_thermostat_group_schedules_finalize():
    coord = _make_coord()
    coord._last_hvac_action["climate.main"] = "heating"
    coord.hass.states = _States({"climate.main": _State("heat", {"hvac_action": None})})
    called = {}

    async def fake_schedule(thermo, action, vent_ids):
        called["finalize"] = (thermo, action, tuple(vent_ids))

    coord._schedule_finalize = fake_schedule
    asyncio.run(coord._async_process_thermostat_group("climate.main", ["v1"], {"vents": {}}))
    assert called["finalize"][0] == "climate.main"
