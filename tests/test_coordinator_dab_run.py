import asyncio
from types import SimpleNamespace

from custom_components.hvac_vent_optimizer.coordinator import FlairCoordinator
from custom_components.hvac_vent_optimizer.const import (
    CONF_DAB_ENABLED,
    CONF_THERMOSTAT_ENTITY,
    CONF_VENT_ASSIGNMENTS,
)


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
    coord.entry = SimpleNamespace(
        data={},
        options={CONF_DAB_ENABLED: True, CONF_VENT_ASSIGNMENTS: {}},
    )
    coord.hass = SimpleNamespace(states=_States({}))
    coord.data = {"vents": {"v1": {}}}
    coord._get_vent_assignments = lambda: coord.entry.options[CONF_VENT_ASSIGNMENTS]
    coord._resolve_hvac_action = lambda state: state.attributes.get("hvac_action")
    coord._async_apply_dab_adjustments = lambda *args, **kwargs: None

    async def _noop():
        return None

    coord.async_request_refresh = _noop
    return coord


def test_async_run_dab_skips_when_disabled():
    coord = _make_coord()
    coord.entry.options[CONF_DAB_ENABLED] = False
    asyncio.run(coord.async_run_dab())


def test_async_run_dab_skips_when_no_assignments():
    coord = _make_coord()
    coord.entry.options[CONF_VENT_ASSIGNMENTS] = {}
    asyncio.run(coord.async_run_dab())


def test_async_run_dab_skips_when_not_heating_or_cooling():
    coord = _make_coord()
    coord.entry.options[CONF_VENT_ASSIGNMENTS] = {
        "v1": {CONF_THERMOSTAT_ENTITY: "climate.main"}
    }
    coord.hass.states = _States({"climate.main": _State("heat", {"hvac_action": None})})
    asyncio.run(coord.async_run_dab())
