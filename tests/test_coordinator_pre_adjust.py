import asyncio
from types import SimpleNamespace

import pytest

from custom_components.hvac_vent_optimizer.coordinator import FlairCoordinator
from custom_components.hvac_vent_optimizer.const import CONF_DAB_ENABLED


class _State:
    def __init__(self, entity_id, state, attributes):
        self.entity_id = entity_id
        self.state = state
        self.attributes = attributes


def _make_coord():
    coord = FlairCoordinator.__new__(FlairCoordinator)
    coord.entry = SimpleNamespace(options={CONF_DAB_ENABLED: True})
    coord._pre_adjust_flags = {}
    coord._get_thermostat_setpoint = lambda *_: 22.0
    coord.hass = SimpleNamespace(config=SimpleNamespace(units=SimpleNamespace(temperature_unit="C")))
    return coord


def test_async_handle_pre_adjust_disabled():
    coord = _make_coord()
    coord.entry.options[CONF_DAB_ENABLED] = False
    event = SimpleNamespace(data={"new_state": _State("climate.main", "heat", {"current_temperature": 21})})
    asyncio.run(coord._async_handle_pre_adjust(event))


def test_async_handle_pre_adjust_missing_targets_in_auto():
    coord = _make_coord()
    called = {"count": 0}

    async def fake_pre_adjust(*args, **kwargs):
        called["count"] += 1

    coord._async_pre_adjust = fake_pre_adjust
    event = SimpleNamespace(
        data={
            "new_state": _State(
                "climate.main",
                "heat_cool",
                {"current_temperature": 21, "target_temp_low": None},
            )
        }
    )
    asyncio.run(coord._async_handle_pre_adjust(event))
    assert called["count"] == 0


def test_async_handle_pre_adjust_triggers(monkeypatch):
    coord = _make_coord()
    called = {"args": None}

    async def fake_pre_adjust(entity_id, action):
        called["args"] = (entity_id, action)

    coord._async_pre_adjust = fake_pre_adjust
    monkeypatch.setattr(
        "custom_components.hvac_vent_optimizer.coordinator.should_pre_adjust",
        lambda *args, **kwargs: True,
    )

    event = SimpleNamespace(
        data={
            "new_state": _State(
                "climate.main",
                "heat",
                {"current_temperature": 20, "temperature_unit": "C"},
            )
        }
    )
    asyncio.run(coord._async_handle_pre_adjust(event))
    assert called["args"] == ("climate.main", "heating")
