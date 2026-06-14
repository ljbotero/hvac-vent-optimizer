"""Coordinator-level tests for multi-vent room grouping (Task 16, R23/R7.4).

These exercise the *real* coordinator apply path
(``_apply_dab_adjustments_impl``) through the Home Assistant fakes installed by
``conftest.py``. They lock in the Requirement 23 behavior for a room served by
two smart vents (the Master Bedroom ``vent_a`` + ``vent_b`` pair from the recorder
analysis, which drifted to 53-vs-51 moves over 7 days):

* R23.1/R23.3/R23.5 — both vents in a room always receive the *identical*
  applied target; they never diverge through independent rounding or
  independent anti-chatter (deadband / min-percent) evaluation.
* R23.2 / R7.4 — anti-chatter is evaluated at the room-group level: one shared
  cooldown clock governs the whole group. Commanding the group (even when only
  one physical vent actually had to move) resets the cooldown for *every* vent
  in the group, and a second poll inside the minimum-adjustment-interval holds
  the whole group.
* R23.4 — each physical vent is still counted individually in the combined-flow
  safety computation (grouping affects targeting, not the safety-floor device
  count): the coordinator passes both vent ids to the allocation input.
"""

from __future__ import annotations

import asyncio

from hvac_vent_optimizer import const
from tests._fakes import FakeApi, FakeEntry, FakeHass, FakeState


# ---------------------------------------------------------------------------
# Builder helpers (multi-vent-per-room aware)
# ---------------------------------------------------------------------------
def _vent(vent_id, room_id, room_name, temp_c, active, percent_open):
    """One Flair vent record with its room embedded (the coordinator's shape).

    Rooms are grouped by their Flair room *name* (see
    ``FlairCoordinator._build_room_vent_groups``), so two vent records sharing
    ``room_name`` form a single logical room group.
    """
    attrs = {"name": room_name, "active": active}
    if temp_c is not None:
        attrs["current-temperature-c"] = temp_c
    return {
        "id": vent_id,
        "name": f"{room_name} Vent {vent_id}",
        "attributes": {"percent-open": percent_open},
        "room": {"id": room_id, "attributes": attrs},
    }


def _build(
    vents_spec,
    *,
    strategy="balance",
    close_inactive=True,
    conventional=0,
    unit="°C",
    thermostat="climate.t",
    target_temp=24.0,
):
    """Build a coordinator wired for a multi-vent cooling scenario.

    ``vents_spec`` is a list of dicts:
        {id, room, temp, active, open, eff}
    where ``room`` is the room *name* (vents sharing a name are one group).
    """
    from hvac_vent_optimizer.coordinator import FlairCoordinator

    vents = {}
    assignments = {}
    # One stable room id per room name so vents in the same room share a room.
    room_ids: dict[str, str] = {}
    for v in vents_spec:
        room_name = v["room"]
        room_id = room_ids.setdefault(room_name, f"room_{room_name}")
        vents[v["id"]] = _vent(v["id"], room_id, room_name, v["temp"], v["active"], v["open"])
        assignments[v["id"]] = {
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
    for v in vents_spec:
        coord._vent_rates[v["id"]] = {"cooling": v["eff"], "heating": v["eff"]}

    return coord, api, thermostat, data


def _run(coord, thermostat, data):
    vent_ids = list(data["vents"].keys())
    asyncio.run(coord._async_apply_dab_adjustments(thermostat, "cooling", vent_ids, data))


def _calls(api):
    """vent_id -> last commanded percent."""
    return dict(api.set_vent_calls)


def _count_calls(api, vent_id):
    return sum(1 for vid, _pct in api.set_vent_calls if vid == vent_id)


# Master Bedroom's two physical vents from the recorder analysis.
M1 = "vent_a"
M2 = "vent_b"


# ---------------------------------------------------------------------------
# R23.1 / R23.3 / R23.5 — identical applied target, no divergence
# ---------------------------------------------------------------------------
def test_master_room_two_vents_get_identical_applied_target():
    """Both Master vents are commanded to the same value (bottleneck -> 100)."""
    coord, api, thermostat, data = _build(
        [
            {"id": M1, "room": "Master", "temp": 27.9, "active": True, "open": 30, "eff": 0.05},
            {"id": M2, "room": "Master", "temp": 27.9, "active": True, "open": 34, "eff": 0.05},
            {"id": "bath", "room": "Bathroom", "temp": 22.0, "active": True, "open": 0, "eff": 0.438},
        ]
    )
    _run(coord, thermostat, data)
    calls = _calls(api)
    assert M1 in calls and M2 in calls, "both Master vents must be commanded"
    assert (
        calls[M1] == calls[M2]
    ), f"Master vents must get identical applied target, got {calls[M1]} vs {calls[M2]}"
    assert calls[M1] == 100, "the hot bottleneck room must be driven to 100%"


def test_master_room_vents_do_not_diverge_via_deadband():
    """The 53-vs-51 bug: independent deadband/min-percent must not split a group.

    Master is overcooled (satisfied) so its group target is 0 %. One vent sits
    inside the deadband of 0 (held if evaluated independently) and the other
    sits well outside it (moved). Grouping forces ONE decision for the whole
    room, so both vents land on the identical applied target (0) instead of
    drifting apart (one held at its old position, the other driven to 0).
    """
    coord, api, thermostat, data = _build(
        [
            # Overcooled Master pair: 0 % group target. open=10 is within the
            # 15 % deadband of 0; open=40 is outside it.
            {"id": M1, "room": "Master", "temp": 22.0, "active": True, "open": 10, "eff": 0.4},
            {"id": M2, "room": "Master", "temp": 22.0, "active": True, "open": 40, "eff": 0.4},
            # Hot bottleneck absorbs the airflow floor so Master stays at 0.
            {"id": "hot", "room": "Bedroom 2", "temp": 27.9, "active": True, "open": 0, "eff": 0.017},
        ]
    )
    _run(coord, thermostat, data)
    calls = _calls(api)
    assert (
        M1 in calls and M2 in calls
    ), "both Master vents must move together, not just the one outside the deadband"
    assert (
        calls[M1] == calls[M2] == 0
    ), f"grouped vents must converge to the identical target 0, got {calls[M1]} vs {calls[M2]}"


# ---------------------------------------------------------------------------
# R23.2 / R7.4 — one shared cooldown clock governs the group
# ---------------------------------------------------------------------------
def test_group_shares_one_cooldown_clock_even_when_one_vent_already_correct():
    """Commanding the group resets cooldown for every vent in it (R23.2).

    ``vent_a`` already sits at the group target (100) so it is not physically
    re-commanded, but ``vent_b`` moves. The group's command must stamp the shared
    cooldown clock onto BOTH vents so the next poll holds the whole room.
    """
    coord, api, thermostat, data = _build(
        [
            {"id": M1, "room": "Master", "temp": 27.9, "active": True, "open": 100, "eff": 0.05},
            {"id": M2, "room": "Master", "temp": 27.9, "active": True, "open": 30, "eff": 0.05},
            {"id": "bath", "room": "Bathroom", "temp": 22.0, "active": True, "open": 0, "eff": 0.438},
        ]
    )
    _run(coord, thermostat, data)

    # Only vent_b physically moved; vent_a was already at 100.
    calls = _calls(api)
    assert calls.get(M2) == 100
    assert M1 not in calls, "the vent already at target should not be re-commanded"

    # ...but BOTH vents share the cooldown clock (R23.2).
    assert (
        M1 in coord._vent_last_commanded
    ), "the group's command must stamp the shared cooldown clock onto every vent"
    assert M2 in coord._vent_last_commanded
    assert (
        coord._vent_last_commanded[M1] == coord._vent_last_commanded[M2]
    ), "grouped vents must share one cooldown timestamp"


def test_group_held_on_second_poll_within_min_interval():
    """A second poll inside the min-adjustment-interval holds the whole group."""
    coord, api, thermostat, data = _build(
        [
            {"id": M1, "room": "Master", "temp": 27.9, "active": True, "open": 30, "eff": 0.05},
            {"id": M2, "room": "Master", "temp": 27.9, "active": True, "open": 34, "eff": 0.05},
            {"id": "bath", "room": "Bathroom", "temp": 22.0, "active": True, "open": 0, "eff": 0.438},
        ]
    )
    _run(coord, thermostat, data)
    first = _calls(api)
    assert first.get(M1) == 100 and first.get(M2) == 100

    calls_before = len(api.set_vent_calls)
    # Second poll immediately after (well inside the 30-min cooldown).
    _run(coord, thermostat, data)
    new_calls = api.set_vent_calls[calls_before:]
    assert all(
        vid not in (M1, M2) for vid, _pct in new_calls
    ), "the shared cooldown must hold both grouped vents on the second poll"


# ---------------------------------------------------------------------------
# R23.4 — each physical vent counted individually in the safety floor
# ---------------------------------------------------------------------------
def test_group_passes_each_physical_vent_to_safety_floor(monkeypatch):
    """The coordinator hands both vent ids to the allocation/floor input (R23.4)."""
    from hvac_vent_optimizer import coordinator as coord_mod

    seen = {}
    real = coord_mod.apply_safety_floor

    def spy(targets, rooms, settings):
        for room in rooms:
            if room.room_id == "Master":
                seen["master_vent_ids"] = tuple(room.vent_ids)
        return real(targets, rooms, settings)

    monkeypatch.setattr(coord_mod, "apply_safety_floor", spy)

    coord, _api, thermostat, data = _build(
        [
            {"id": M1, "room": "Master", "temp": 27.9, "active": True, "open": 30, "eff": 0.05},
            {"id": M2, "room": "Master", "temp": 27.9, "active": True, "open": 34, "eff": 0.05},
            {"id": "bath", "room": "Bathroom", "temp": 22.0, "active": True, "open": 0, "eff": 0.438},
        ]
    )
    _run(coord, thermostat, data)
    assert seen.get("master_vent_ids") is not None, "balance must route through the floor"
    assert set(seen["master_vent_ids"]) == {
        M1,
        M2,
    }, "both physical vents must be counted individually in the floor input (R23.4)"
