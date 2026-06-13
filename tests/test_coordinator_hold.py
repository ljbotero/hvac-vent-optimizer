"""Coordinator hold/recompute tests for the ``balance`` strategy (Task 19.1).

These exercise the *real* coordinator deviation/hold check in
``_apply_dab_adjustments_impl`` through the Home Assistant fakes installed by
``conftest.py``. They prove two coupled behaviors required by R5.2/R7.1/R7.2:

* **Airflow-limited rooms are excluded from the "all rooms tracking"
  determination (R5.2).** A pinned-but-hot room (Bedroom 2) physically cannot
  track its predicted slope, so its (expected) deviation must neither force a
  recompute (churn) nor be counted as "tracking" in a way that produces a
  *false hold*.
* **The active-room spread guardrail is the PRIMARY recompute trigger
  (R7.1/R7.2).** When the predicted active-room spread exceeds
  ``spread_guardrail_c`` the system is permitted to recompute even if every
  per-vent deviation is within threshold; while at/below the guardrail it
  prefers to hold.

Legacy strategies (``dab``/``cost``/``stats``/``hybrid``) keep their original
deviation-only hold behavior and are explicitly NOT subject to the spread
guardrail or the airflow-limited exclusion.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from hvac_vent_optimizer import const
from tests._fakes import FakeApi, FakeEntry, FakeHass, FakeState


# ---------------------------------------------------------------------------
# Builders
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


def _build(
    rooms,
    *,
    strategy="balance",
    unit="°C",
    thermostat="climate.t",
    target_temp=24.0,
):
    """Build a coordinator wired for a multi-room cooling scenario.

    ``rooms`` is a list of dicts: {id, name, temp, active, open, eff}.
    """
    from hvac_vent_optimizer.coordinator import FlairCoordinator

    vents = {}
    assignments = {}
    for r in rooms:
        vents[r["id"]] = _vent(
            r["id"], f"room_{r['id']}", r["name"], r["temp"], r["active"], r["open"]
        )
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


def _seed_cycle(coord, thermostat, rooms, *, elapsed_min=10.0, deviations=None):
    """Seed ``_cycle_targets`` so the deviation/hold check runs.

    ``deviations`` maps room id -> deviation magnitude (°C) at ``elapsed_min``.
    Rooms not listed track perfectly (deviation 0). Cooling per-minute rate is
    fixed small so ``expected = initial - rate*elapsed`` lands on the desired
    deviation from the current temperature.
    """
    deviations = deviations or {}
    rate_per_min = 0.05
    cycle_start = datetime.now(UTC) - timedelta(minutes=elapsed_min)
    targets = {}
    initial_temps = {}
    predicted_rates = {}
    for r in rooms:
        cur = r["temp"]
        dev = deviations.get(r["id"], 0.0)
        # cooling: expected = initial - rate*elapsed; want |cur - expected| = dev
        initial_temps[r["id"]] = cur + rate_per_min * elapsed_min + dev
        predicted_rates[r["id"]] = rate_per_min
        targets[r["id"]] = r["open"]
    coord._cycle_targets[thermostat] = {
        "targets": targets,
        "initial_temps": initial_temps,
        "predicted_rates": predicted_rates,
        "cycle_start": cycle_start,
        "recalc_count": 0,
        "last_recalc": None,
        "adjustment_batches": 0,
    }


def _run(coord, thermostat, data):
    vent_ids = list(data["vents"].keys())
    asyncio.run(
        coord._async_apply_dab_adjustments(thermostat, "cooling", vent_ids, data)
    )


# ---------------------------------------------------------------------------
# 1. A pinned-but-hot airflow-limited room must NOT force a false hold.
# ---------------------------------------------------------------------------
def test_balance_airflow_limited_room_does_not_force_false_hold():
    # Bedroom 2: pinned at 100 %, still hot (27.9) and low efficiency -> airflow
    # limited. Bathroom: overcooled (22.0) -> satisfied. Both *track* their
    # predicted slope (deviation 0), so the legacy deviation-only check would
    # HOLD. But the active-room spread is huge, so balance must recompute/act.
    rooms = [
        {"id": "bedroom_2", "name": "Bedroom 2", "temp": 27.9, "active": True, "open": 100, "eff": 0.017},
        {"id": "bath", "name": "Bathroom", "temp": 22.0, "active": True, "open": 0, "eff": 0.438},
    ]
    coord, _api, thermostat, data = _build(rooms)
    _seed_cycle(coord, thermostat, rooms)
    _run(coord, thermostat, data)
    assert coord._hold_status == "recalculating", (
        "balance must not hold when a pinned-hot room keeps the active-room "
        "spread above the guardrail"
    )


# ---------------------------------------------------------------------------
# 2. Spread above the guardrail triggers a recompute (primary trigger).
# ---------------------------------------------------------------------------
def test_balance_spread_above_guardrail_triggers_recompute():
    # Two unsatisfied rooms ~2.5 C apart, both tracking (deviation 0). No room
    # is airflow-limited (open 50 %). Per-vent deviation alone would HOLD; the
    # spread guardrail forces a recompute.
    rooms = [
        {"id": "a", "name": "RoomA", "temp": 27.0, "active": True, "open": 50, "eff": 0.05},
        {"id": "b", "name": "RoomB", "temp": 24.5, "active": True, "open": 50, "eff": 0.05},
    ]
    coord, _api, thermostat, data = _build(rooms)
    _seed_cycle(coord, thermostat, rooms)
    _run(coord, thermostat, data)
    assert coord._hold_status == "recalculating"


# ---------------------------------------------------------------------------
# 3. Spread within the guardrail holds.
# ---------------------------------------------------------------------------
def test_balance_spread_within_guardrail_holds():
    # Two unsatisfied rooms only ~0.4 C apart, both tracking. Spread is within
    # the guardrail -> prefer holding.
    rooms = [
        {"id": "a", "name": "RoomA", "temp": 25.0, "active": True, "open": 50, "eff": 0.05},
        {"id": "b", "name": "RoomB", "temp": 25.4, "active": True, "open": 50, "eff": 0.05},
    ]
    coord, _api, thermostat, data = _build(rooms)
    _seed_cycle(coord, thermostat, rooms)
    _run(coord, thermostat, data)
    assert coord._hold_status == "holding"


# ---------------------------------------------------------------------------
# 4. Within the guardrail, an airflow-limited room's large (expected) deviation
#    must NOT force churn — it is excluded from the tracking determination.
# ---------------------------------------------------------------------------
def test_balance_airflow_limited_excluded_from_deviation_determination():
    # Single active room, pinned at 100 % and hot -> airflow limited. With a
    # single active room the predicted spread is 0 (within guardrail), so the
    # deviation safety check runs. The room has a large deviation (it cannot
    # track), but because it is airflow-limited it is excluded -> HOLD (no
    # churn).
    rooms = [
        {"id": "bedroom_2", "name": "Bedroom 2", "temp": 27.9, "active": True, "open": 100, "eff": 0.017},
    ]
    coord, _api, thermostat, data = _build(rooms)
    _seed_cycle(coord, thermostat, rooms, deviations={"bedroom_2": 2.0})
    _run(coord, thermostat, data)
    assert coord._hold_status == "holding", (
        "an airflow-limited room's expected deviation must not force a recompute"
    )


# ---------------------------------------------------------------------------
# 5. Legacy strategies are unchanged: no spread guardrail, no exclusion.
# ---------------------------------------------------------------------------
def test_legacy_strategy_ignores_spread_guardrail_and_holds_when_tracking():
    # Same far-apart-but-tracking setup as test 2. A legacy strategy has no
    # spread trigger, so it HOLDS (deviation-only behavior preserved).
    rooms = [
        {"id": "a", "name": "RoomA", "temp": 27.0, "active": True, "open": 50, "eff": 0.05},
        {"id": "b", "name": "RoomB", "temp": 24.5, "active": True, "open": 50, "eff": 0.05},
    ]
    coord, _api, thermostat, data = _build(rooms, strategy="dab")
    _seed_cycle(coord, thermostat, rooms)
    _run(coord, thermostat, data)
    assert coord._hold_status == "holding"


def test_legacy_strategy_recomputes_on_large_deviation_without_exclusion():
    # A legacy strategy must still recompute when a (would-be airflow-limited)
    # room deviates beyond threshold — no airflow-limited exclusion applies.
    rooms = [
        {"id": "bedroom_2", "name": "Bedroom 2", "temp": 27.9, "active": True, "open": 100, "eff": 0.017},
    ]
    coord, _api, thermostat, data = _build(rooms, strategy="dab")
    _seed_cycle(coord, thermostat, rooms, deviations={"bedroom_2": 2.0})
    _run(coord, thermostat, data)
    assert coord._hold_status == "recalculating"
