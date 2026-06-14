"""Observability tests for Task 24 (R13/R14/R5.4/R25.11).

These exercise the *real* coordinator through the Home Assistant fakes
installed by ``conftest.py``. Task 24 adds, computed every poll while the HVAC
is active:

* ``get_active_room_spread()`` — the current active-room temperature spread
  (max - min) in °C (R13.1/R13.2). Inactive rooms are excluded (R2.5).
* ``get_max_active_error()`` — the maximum *absolute* active-room error vs the
  shared setpoint (R14.1).
* ``get_room_signed_error(room_id)`` — per-room **signed** error where a
  negative value means overcooled (cooling) / overheated (heating) (R13.3).
* ``is_room_airflow_limited(room_id)`` — per-room airflow-limited indicator
  (R5.4).
* ``get_recalculations_24h()`` / ``get_holds_24h()`` — rolling 24 h counters
  (R14.1).
* per-strategy spread metrics (``avg_spread``/``max_spread``/
  ``time_above_guardrail_min``) accumulated each poll (R13.4).

All temperatures are Celsius internally (R18.4) and no diagnostic exposes
credentials (R14.4/R22.4).
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta

from hvac_vent_optimizer import const
from tests._fakes import FakeApi, FakeEntry, FakeHass, FakeState


# ---------------------------------------------------------------------------
# Builders (mirrors tests/test_coordinator_hold.py)
# ---------------------------------------------------------------------------
def _vent(vent_id, room_id, room_name, temp_c, active, percent_open):
    attrs = {"name": room_name, "active": active}
    if temp_c is not None:
        attrs["current-temperature-c"] = temp_c
    return {
        "id": vent_id,
        "name": f"{room_name} Vent",
        "attributes": {"percent-open": percent_open},
        "room": {"id": room_id, "attributes": attrs},
    }


def _build(rooms, *, strategy="balance", unit="°C", thermostat="climate.t", target_temp=24.0):
    from hvac_vent_optimizer.coordinator import FlairCoordinator

    vents = {}
    assignments = {}
    for r in rooms:
        vents[r["id"]] = _vent(r["id"], f"room_{r['id']}", r["name"], r["temp"], r["active"], r["open"])
        assignments[r["id"]] = {
            const.CONF_THERMOSTAT_ENTITY: thermostat,
            const.CONF_TEMP_SENSOR_ENTITY: None,
        }
    data = {"vents": vents, "pucks": {}}
    options = {
        const.CONF_VENT_BRAND: const.BRAND_FLAIR,
        const.CONF_DAB_ENABLED: True,
        const.CONF_VENT_ASSIGNMENTS: assignments,
        const.CONF_CONTROL_STRATEGY: strategy,
        const.CONF_CLOSE_INACTIVE_ROOMS: True,
    }
    entry = FakeEntry(
        data={
            const.CONF_STRUCTURE_ID: "s1",
            const.CONF_CLIENT_ID: "id",
            const.CONF_CLIENT_SECRET: "sec",
        },
        options=options,
    )
    hass = FakeHass(unit=unit)
    api = FakeApi()
    coord = FlairCoordinator(hass, api, entry)
    coord.data = data
    hass.states.set(
        thermostat,
        FakeState(
            "cool",
            {
                "hvac_action": "cooling",
                "current_temperature": target_temp,
                "temperature": target_temp,
                "temperature_unit": unit,
            },
        ),
    )
    for r in rooms:
        coord._vent_rates[r["id"]] = {"cooling": r["eff"], "heating": r["eff"]}
    return coord, api, thermostat, data


def _run(coord, thermostat, data, action="cooling"):
    vent_ids = list(data["vents"].keys())
    asyncio.run(coord._async_apply_dab_adjustments(thermostat, action, vent_ids, data))


# Bedroom 2 pinned-hot, Bathroom overcooled, Guest mid; one inactive far-out room.
_ROOMS = [
    {"id": "bedroom_2", "name": "Bedroom 2", "temp": 27.9, "active": True, "open": 100, "eff": 0.017},
    {"id": "bath", "name": "Bathroom", "temp": 22.0, "active": True, "open": 0, "eff": 0.438},
    {"id": "guest", "name": "Guest", "temp": 25.0, "active": True, "open": 50, "eff": 0.05},
    {"id": "attic", "name": "Attic", "temp": 35.0, "active": False, "open": 0, "eff": 0.05},
]


# ---------------------------------------------------------------------------
# Spread + max error (R13.1/R13.2/R14.1)
# ---------------------------------------------------------------------------
def test_active_room_spread_excludes_inactive_rooms():
    coord, _api, thermostat, data = _build(_ROOMS)
    _run(coord, thermostat, data)
    # Active temps: 27.9, 22.0, 25.0 -> spread 5.9. The inactive 35.0 attic must
    # NOT widen the spread (R2.5).
    assert coord.get_active_room_spread() == 5.9


def test_max_active_error_is_absolute_against_setpoint():
    coord, _api, thermostat, data = _build(_ROOMS, target_temp=24.0)
    _run(coord, thermostat, data)
    # The coordinator applies its configured setpoint offset; derive the
    # expectation from the resolved cooling setpoint so the test tracks the
    # real control law rather than a hard-coded value.
    setpoint = coord._get_thermostat_setpoint(thermostat, "cooling")
    expected = round(abs(27.9 - setpoint), 2)  # Bedroom 2 is the hottest active room
    assert coord.get_max_active_error() == expected


def test_single_active_room_spread_is_zero_and_no_crash():
    rooms = [{"id": "a", "name": "A", "temp": 27.0, "active": True, "open": 50, "eff": 0.05}]
    coord, _api, thermostat, data = _build(rooms)
    _run(coord, thermostat, data)
    assert coord.get_active_room_spread() == 0.0


# ---------------------------------------------------------------------------
# Per-room signed error (R13.3): negative == overcooled while cooling.
# ---------------------------------------------------------------------------
def test_signed_error_negative_for_overcooled_room_cooling():
    coord, _api, thermostat, data = _build(_ROOMS, target_temp=24.0)
    _run(coord, thermostat, data)
    assert coord.get_room_signed_error("room_bath") < 0  # 22.0 - 24.0 = -2.0
    assert coord.get_room_signed_error("room_bedroom_2") > 0  # 27.9 - 24.0 = +3.9


def test_signed_error_negative_for_overheated_room_heating():
    rooms = [
        {"id": "hot", "name": "Hot", "temp": 23.0, "active": True, "open": 0, "eff": 0.05},
        {"id": "cold", "name": "Cold", "temp": 18.0, "active": True, "open": 100, "eff": 0.02},
    ]
    coord, _api, thermostat, data = _build(rooms, target_temp=21.0)
    # Heating setpoint comes from the thermostat 'temperature' attribute.
    coord.data["vents"]
    _run(coord, thermostat, data, action="heating")
    # Heating: signed = setpoint - temp -> overheated (23 > 21) is negative.
    assert coord.get_room_signed_error("room_hot") < 0
    assert coord.get_room_signed_error("room_cold") > 0


# ---------------------------------------------------------------------------
# Airflow-limited indicator (R5.4)
# ---------------------------------------------------------------------------
def test_airflow_limited_flag_set_for_pinned_hot_room():
    coord, _api, thermostat, data = _build(_ROOMS)
    _run(coord, thermostat, data)
    assert coord.is_room_airflow_limited("room_bedroom_2") is True
    assert coord.is_room_airflow_limited("room_bath") is False
    assert coord.is_room_airflow_limited("room_guest") is False


# ---------------------------------------------------------------------------
# Per-strategy spread metrics (R13.4)
# ---------------------------------------------------------------------------
def test_per_strategy_spread_metrics_recorded():
    coord, _api, thermostat, data = _build(_ROOMS)
    _run(coord, thermostat, data)
    metrics = coord._strategy_metrics.get("balance", {})
    assert metrics.get("max_spread", 0.0) >= 5.9
    assert metrics.get("avg_spread", 0.0) > 0.0
    # Spread (5.9) exceeds the default 1.0 guardrail -> time accrues.
    assert metrics.get("time_above_guardrail_min", 0.0) > 0.0


# ---------------------------------------------------------------------------
# 24h rolling counters (R14.1)
# ---------------------------------------------------------------------------
def test_recalc_and_hold_24h_counters_prune_old_events():
    coord, _api, _thermostat, _data = _build(_ROOMS)
    now = datetime.now(UTC)
    coord._recalc_events = [now - timedelta(hours=30), now - timedelta(hours=1)]
    coord._hold_events = [now - timedelta(hours=48), now - timedelta(minutes=5), now]
    assert coord.get_recalculations_24h() == 1
    assert coord.get_holds_24h() == 2


# ---------------------------------------------------------------------------
# No credentials exposed (R14.4 / R22.4)
# ---------------------------------------------------------------------------
def test_strategy_metrics_never_expose_credentials():
    coord, _api, thermostat, data = _build(_ROOMS)
    _run(coord, thermostat, data)
    blob = json.dumps(coord.get_strategy_metrics(), default=str).lower()
    assert "client_id" not in blob
    assert "client_secret" not in blob
    assert "sec" not in blob  # the fake secret value
