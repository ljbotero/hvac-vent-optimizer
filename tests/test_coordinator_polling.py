import asyncio
from types import SimpleNamespace

from custom_components.hvac_vent_optimizer.coordinator import FlairCoordinator


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
    coord._poll_interval_active = 1
    coord._poll_interval_idle = 2
    coord.update_interval = None
    coord._get_thermostat_entities = lambda: ["climate.main"]
    coord.hass = SimpleNamespace(states=_States({}), async_create_task=lambda coro: coro)
    return coord


def test_recompute_polling_interval_active():
    coord = _make_coord()
    coord._resolve_hvac_action = lambda state: "heating"
    coord.hass.states = _States({"climate.main": _State("heat", {})})
    asyncio.run(coord._recompute_polling_interval())
    assert coord.update_interval == 1


def test_recompute_polling_interval_idle():
    coord = _make_coord()
    coord._resolve_hvac_action = lambda state: None
    coord.hass.states = _States({"climate.main": _State("heat", {})})
    asyncio.run(coord._recompute_polling_interval())
    assert coord.update_interval == 2


def test_handle_thermostat_event_creates_tasks():
    coord = _make_coord()
    called = []
    coord.hass.async_create_task = lambda coro: called.append(coro)
    event = SimpleNamespace()
    coord._handle_thermostat_event(event)
    assert len(called) == 2
