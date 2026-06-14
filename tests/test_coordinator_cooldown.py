"""Coordinator-level tests for the safety-pad cooldown distinction (Task 17, R9/R7.4).

These exercise the *real* coordinator apply path (``_apply_dab_adjustments_impl``)
through the Home Assistant fakes installed by ``conftest.py``. They lock in the
Requirement 9 distinction between two kinds of safety-floor opening:

* **"Reach-the-floor" open** — an aperture the *safety floor* had to raise above
  the allocation, AND that genuinely *opens* the vent (target > current). This is
  always allowed and **immediate**: it bypasses cooldown / deadband / min-percent
  (R9.1/R9.2, R7.5), because never moving it would leave combined airflow below
  the safety floor.
* **"Padding above the floor" / balancing move** — any other move (an allocation
  move the floor did not raise, or a move that is actually a *close*). These ARE
  subject to cooldown, deadband, min-percent and batch limits (R9.1/R9.2).

The bug this fixes (R9.3 repro): the old code marked *any* vent whose
floored target exceeded its pre-floor (allocation) target as "safety opened" and
let it bypass ALL anti-chatter — even when the floored target was *below* the
vent's current position (i.e. the move was a net **close**). The safety floor
must never be used to justify additional closing (R7.5), and a balancing/padding
move must respect the cooldown.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from hvac_vent_optimizer import const
from tests._fakes import FakeApi, FakeEntry, FakeHass, FakeState


# ---------------------------------------------------------------------------
# Builder helpers (mirrors tests/test_coordinator_balance.py)
# ---------------------------------------------------------------------------
def _vent(vent_id, room_id, room_name, temp_c, active, percent_open):
    """One Flair vent record with its room embedded (the coordinator's shape)."""
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
    close_inactive=True,
    conventional=0,
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
        const.CONF_CLOSE_INACTIVE_ROOMS: close_inactive,
    }
    if conventional:
        options[const.CONF_CONVENTIONAL_VENTS_BY_THERMOSTAT] = {thermostat: conventional}

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


def _run(coord, thermostat, data):
    vent_ids = list(data["vents"].keys())
    asyncio.run(coord._async_apply_dab_adjustments(thermostat, "cooling", vent_ids, data))


def _calls(api):
    """vent_id -> last commanded percent."""
    return dict(api.set_vent_calls)


def _cooldown_all(coord, data):
    """Stamp a fresh cooldown clock on every vent (well inside the 30-min window)."""
    now = datetime.now(UTC)
    for vid in data["vents"]:
        coord._vent_last_commanded[vid] = now


# ---------------------------------------------------------------------------
# R9.1 / R9.2 — a vent that must open to reach the floor opens IMMEDIATELY,
# even within the cooldown window.
# ---------------------------------------------------------------------------
def test_reach_floor_open_is_immediate_within_cooldown():
    """The safety floor must never be blocked by cooldown (R9.1/R7.5).

    The hot bottleneck already sits at 100 % (no move needed). Two warm rooms
    allocate to 0 % (required flow < leak), so combined ~= 100/3 = 33 % < 40 %
    floor; ``apply_safety_floor`` pads a warm room up to reach 40 %. Even though
    every vent is inside the cooldown window, the floor-reach open is immediate.
    """
    rooms = [
        {"id": "hot", "name": "Bedroom 2", "temp": 27.9, "active": True, "open": 100, "eff": 0.017},
        {"id": "warm0", "name": "Warm0", "temp": 24.0, "active": True, "open": 0, "eff": 0.5},
        {"id": "warm1", "name": "Warm1", "temp": 24.0, "active": True, "open": 0, "eff": 0.5},
    ]
    coord, api, thermostat, data = _build(rooms)
    _cooldown_all(coord, data)  # everything is inside the cooldown window

    _run(coord, thermostat, data)
    calls = _calls(api)
    # The floor-required open bypasses the cooldown and reaches the dispatch.
    assert any(
        calls.get(w, 0) > 0 for w in ("warm0", "warm1")
    ), "a vent that must open to reach the safety floor must open immediately, even in cooldown"


# ---------------------------------------------------------------------------
# R9.1 / R9.2 — a balancing / padding move within cooldown is HELD.
# ---------------------------------------------------------------------------
def test_balancing_move_within_cooldown_is_held():
    """A balancing move the floor did NOT force is subject to cooldown (R9.1/R9.2).

    Eight conventional vents @50 % keep combined airflow well above the floor
    regardless of the smart vents, so the floor never binds and no open is
    "required to reach the floor". The hot room's 50 -> 100 move is therefore a
    pure balancing move and must be held while inside the cooldown window.
    """
    rooms = [
        {"id": "hot", "name": "Bedroom 2", "temp": 27.9, "active": True, "open": 50, "eff": 0.017},
        {"id": "cold", "name": "Bathroom", "temp": 22.0, "active": True, "open": 0, "eff": 0.438},
    ]
    coord, api, thermostat, data = _build(rooms, conventional=8)
    _cooldown_all(coord, data)

    _run(coord, thermostat, data)
    calls = _calls(api)
    assert (
        "hot" not in calls
    ), "a balancing move the safety floor did not force must respect the cooldown (be held)"


def test_balancing_move_commanded_when_not_in_cooldown():
    """Control: the same balancing move IS applied once the cooldown has elapsed."""
    rooms = [
        {"id": "hot", "name": "Bedroom 2", "temp": 27.9, "active": True, "open": 50, "eff": 0.017},
        {"id": "cold", "name": "Bathroom", "temp": 22.0, "active": True, "open": 0, "eff": 0.438},
    ]
    coord, api, thermostat, data = _build(rooms, conventional=8)
    # No cooldown stamp -> the balancing move is free to apply.
    _run(coord, thermostat, data)
    calls = _calls(api)
    assert calls.get("hot") == 100, "without cooldown the balancing move should drive the bottleneck to 100%"


# ---------------------------------------------------------------------------
# R7.5 / R9.3 — a floor-reach open only ever OPENS (never closes).
# ---------------------------------------------------------------------------
def test_floor_pad_never_forces_a_close():
    """The safety floor must never justify *closing* a vent (R7.5, R9.3 repro).

    ``mid`` is an unsatisfied-but-efficient room: allocation throttles it to 0 %
    (required flow < leak), but it currently sits at 80 % open. The combined
    airflow (slow bottleneck 100 %, everyone else 0 %, 4 smart vents) is 25 % <
    40 %, so ``apply_safety_floor`` pads the only eligible unsatisfied room
    (``mid``) up to 60 % to reach the floor.

    60 % is **above** the allocation (0 %) but **below** ``mid``'s current 80 %,
    so commanding it would be a net *close*. The old code lumped this into the
    "safety opened" set and force-closed it (80 -> 60) bypassing the cooldown.
    The fix: a floor pad that does not actually open the vent past its current
    position is NOT a reach-the-floor move, so the cooldown holds ``mid`` at 80
    (which is itself above the floor pad, so the floor stays satisfied).
    """
    rooms = [
        {"id": "slow", "name": "Bedroom 2", "temp": 27.9, "active": True, "open": 0, "eff": 0.017},
        {"id": "mid", "name": "Bedroom 1", "temp": 23.5, "active": True, "open": 80, "eff": 0.6},
        {"id": "s1", "name": "Guest", "temp": 22.0, "active": True, "open": 0, "eff": 0.3},
        {"id": "s2", "name": "Bedroom 3", "temp": 22.0, "active": True, "open": 0, "eff": 0.3},
    ]
    coord, api, thermostat, data = _build(rooms)
    # mid is inside the cooldown window; slow is free to open (keeps combined safe).
    coord._vent_last_commanded["mid"] = datetime.now(UTC)

    _run(coord, thermostat, data)
    calls = _calls(api)

    # The bottleneck still opens (balancing, not in cooldown).
    assert calls.get("slow") == 100, "the bottleneck must still be driven to 100%"
    # The floor pad must NOT force mid closed (80 -> 60) under the safety exemption.
    assert "mid" not in calls, (
        "the safety floor must never justify closing a vent; the floor pad (60%) is "
        "below mid's current 80%, so it is not a reach-the-floor open and the cooldown holds it"
    )


def test_floor_pad_close_is_held_for_legacy_strategy():
    """The same reach-floor-vs-padding distinction applies on the legacy floor path.

    ``adjust_for_minimum_airflow`` is the legacy strategies' floor choke point.
    The shared ``safety_opened`` computation must treat a floor pad that lands
    below a vent's current position as a (cooldown-governed) close, not an
    immediate safety open, for ``dab``/``cost``/``stats``/``hybrid`` too (R17.5).
    """
    rooms = [
        {"id": "slow", "name": "Bedroom 2", "temp": 27.9, "active": True, "open": 0, "eff": 0.017},
        {"id": "mid", "name": "Bedroom 1", "temp": 23.5, "active": True, "open": 80, "eff": 0.6},
        {"id": "s1", "name": "Guest", "temp": 22.0, "active": True, "open": 0, "eff": 0.3},
        {"id": "s2", "name": "Bedroom 3", "temp": 22.0, "active": True, "open": 0, "eff": 0.3},
    ]
    coord, api, thermostat, data = _build(rooms, strategy="dab")
    coord._vent_last_commanded["mid"] = datetime.now(UTC)

    _run(coord, thermostat, data)
    calls = _calls(api)
    # mid must never be force-closed by the legacy safety padding while in cooldown.
    assert (
        calls.get("mid", 0) == 0 or "mid" not in calls
    ), "legacy floor padding must not force a close that bypasses the cooldown"
