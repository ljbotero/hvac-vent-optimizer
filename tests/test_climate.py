"""Fix #3: FlairRoomClimate must not double-convert the setpoint.

HA core converts the service value into the entity's native temperature_unit
(Celsius here) before calling async_set_temperature. The integration must pass
that value straight through to the Flair API.
"""

from __future__ import annotations

import types

import pytest

from tests._fakes import FakeApi, FakeHass


def _make_climate(unit: str):
    from hvac_vent_optimizer.climate import FlairRoomClimate

    api = FakeApi()

    async def _refresh():
        return None

    coord = types.SimpleNamespace(api=api, async_request_refresh=_refresh)
    entity = FlairRoomClimate(coord, "e1", "room1")
    entity.hass = FakeHass(unit=unit)
    return entity, api


@pytest.mark.asyncio
async def test_setpoint_passthrough_celsius_system():
    from hvac_vent_optimizer import const

    entity, api = _make_climate(const.__dict__.get("X", "°C"))
    await entity.async_set_temperature(temperature=22.0)
    assert api.set_setpoint_calls == [("room1", 22.0, None)]


@pytest.mark.asyncio
async def test_setpoint_no_double_conversion_fahrenheit_system():
    from homeassistant.const import UnitOfTemperature

    entity, api = _make_climate(UnitOfTemperature.FAHRENHEIT)
    # HA already converted to the entity's Celsius unit; value is 22.0 C.
    await entity.async_set_temperature(temperature=22.0)
    assert api.set_setpoint_calls == [("room1", 22.0, None)]


@pytest.mark.asyncio
async def test_setpoint_missing_temperature_is_noop():
    entity, api = _make_climate("°C")
    await entity.async_set_temperature()
    assert api.set_setpoint_calls == []
