"""Overshoot fix for the legacy strategies (Task 14, R8.1/8.2/8.3/17.5).

Before this fix, `_calculate_linear_target_percent` (the `cost` core) and the
`stats` target path used ``abs(setpoint - temp)``, so an overcooled room (temp
already past the setpoint) produced a positive target and the vent was
reopened. `hybrid` then preferred that positive target over the (correct)
``dab`` 0% and reopened satisfied rooms like the Main Bathroom.

These tests pin the corrected directional behavior: all four legacy strategies
(`dab`, `cost`, `stats`, `hybrid`) close a satisfied room.

Coordinator tests use the Home Assistant fakes installed by ``conftest.py``.
"""

from __future__ import annotations

import asyncio

from tests._fakes import FakeState


# ---------------------------------------------------------------------------
# _calculate_linear_target_percent  (the `cost` strategy core)
# ---------------------------------------------------------------------------
def test_linear_target_overcooled_cooling_room_returns_zero(make_coordinator):
    coord, *_ = make_coordinator()
    # Cooling: room at 23.0 is already BELOW the 26.0 setpoint (overcooled).
    # The old abs() logic returned a positive target (the bug); the directional
    # guard must return 0.0.
    result = coord._calculate_linear_target_percent(23.0, 26.0, 0.5, 30.0)
    assert result == 0.0


def test_linear_target_overheated_heating_room_returns_zero(make_coordinator):
    coord, *_ = make_coordinator()
    # Heating: room at 23.0 is already ABOVE the 21.0 setpoint (satisfied).
    result = coord._calculate_linear_target_percent(23.0, 21.0, 0.5, 30.0, "heating")
    assert result == 0.0


def test_linear_target_room_still_needing_cooling_opens(make_coordinator):
    coord, *_ = make_coordinator()
    # Cooling: room at 28.0 is above the 26.0 setpoint -> still needs airflow.
    result = coord._calculate_linear_target_percent(28.0, 26.0, 0.5, 30.0)
    assert result > 0.0


def test_linear_target_room_still_needing_heating_opens(make_coordinator):
    coord, *_ = make_coordinator()
    # Heating: room at 18.0 is below the 21.0 setpoint -> still needs airflow.
    result = coord._calculate_linear_target_percent(18.0, 21.0, 0.5, 30.0, "heating")
    assert result > 0.0


# ---------------------------------------------------------------------------
# hybrid end-to-end: a satisfied (overcooled) room is not reopened
# ---------------------------------------------------------------------------
def _build_two_room_cooling_coordinator(make_coordinator):
    """A cooling thermostat with a hot room and an overcooled (satisfied) room.

    Conventional vents are configured so the safety floor is satisfied by the
    hot room + conventional vents alone; the floor therefore never needs to
    reopen the satisfied room, isolating the strategy behavior under test.
    """
    from hvac_vent_optimizer import const

    thermostat = "climate.t"
    hot_id = "v_hot"
    bath_id = "v_bath"
    data = {
        "vents": {
            hot_id: {
                "id": hot_id,
                "name": "Game Room",
                "attributes": {"percent-open": 0},
                "room": {
                    "id": "room_hot",
                    "attributes": {
                        "name": "Game Room",
                        "active": True,
                        "current-temperature-c": 28.0,
                    },
                },
            },
            bath_id: {
                "id": bath_id,
                "name": "Main Bathroom",
                # Currently closed; a correct strategy leaves it closed. The bug
                # reopens it (0% -> a large positive target), which IS commanded.
                "attributes": {"percent-open": 0},
                "room": {
                    "id": "room_bath",
                    "attributes": {
                        "name": "Main Bathroom",
                        "active": True,
                        # Overcooled: below the cooling setpoint (24.0 - 0.7 = 23.3).
                        "current-temperature-c": 21.0,
                    },
                },
            },
        },
        "pucks": {},
    }
    options = {
        const.CONF_VENT_ASSIGNMENTS: {
            hot_id: {const.CONF_THERMOSTAT_ENTITY: thermostat, const.CONF_TEMP_SENSOR_ENTITY: None},
            bath_id: {const.CONF_THERMOSTAT_ENTITY: thermostat, const.CONF_TEMP_SENSOR_ENTITY: None},
        },
        const.CONF_CONTROL_STRATEGY: "hybrid",
        # Enough conventional vents that the floor is met without the smart vents.
        const.CONF_CONVENTIONAL_VENTS_BY_THERMOSTAT: {thermostat: 4},
    }
    coord, hass, api, _entry = make_coordinator(options=options, data=data)
    hass.states.set(
        thermostat,
        FakeState(
            "cool",
            {
                "hvac_action": "cooling",
                "current_temperature": 24.0,
                "temperature": 24.0,
                "temperature_unit": "°C",
            },
        ),
    )
    coord._vent_rates[hot_id] = {"cooling": 0.5, "heating": 0.5}
    coord._vent_rates[bath_id] = {"cooling": 0.5, "heating": 0.5}
    return coord, hass, api, thermostat, hot_id, bath_id, data


def test_hybrid_does_not_reopen_satisfied_bathroom(make_coordinator):
    coord, _hass, api, thermostat, hot_id, bath_id, data = _build_two_room_cooling_coordinator(
        make_coordinator
    )

    asyncio.run(coord._async_apply_dab_adjustments(thermostat, "cooling", [hot_id, bath_id], data))

    # The hot room must still be conditioned (sanity: the apply path ran).
    hot_cmds = [pct for (vid, pct) in api.set_vent_calls if vid == hot_id]
    assert hot_cmds and hot_cmds[-1] > 0, "hot room should be opened"

    # The overcooled bathroom must never be commanded open.
    bath_cmds = [pct for (vid, pct) in api.set_vent_calls if vid == bath_id]
    assert all(
        pct == 0 for pct in bath_cmds
    ), f"satisfied bathroom must not be reopened; got commands {bath_cmds}"
