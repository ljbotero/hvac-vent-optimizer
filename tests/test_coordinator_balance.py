"""Coordinator-level tests for the ``balance`` control strategy (Task 15, R1/R20.5).

These exercise the *real* coordinator apply path (``_apply_dab_adjustments_impl``)
through the Home Assistant fakes installed by ``conftest.py``. They prove that
when ``control_strategy == "balance"`` the coordinator:

* gathers a ``RoomAllocInput`` per room (room temp in Celsius, active flag,
  current %open, group vent_ids, effective_rate, leak),
* calls ``balance.allocate`` then routes the result through the single
  ``balance.apply_safety_floor`` choke point (no bypass), and
* dispatches vent commands (Flair API rate-limiting preserved),

and the worked-example-ish behavior holds: a hot low-efficiency room gets more
open than a satisfied room, the slowest room saturates at 100 %, inactive rooms
are not repositioned by balancing (held) unless ``close_inactive_rooms`` is on,
missing temp/efficiency rooms are skipped gracefully, and the legacy strategies
are unchanged.
"""

from __future__ import annotations

import asyncio

import pytest

from hvac_vent_optimizer import const
from tests._fakes import FakeApi, FakeEntry, FakeHass, FakeState


# ---------------------------------------------------------------------------
# Builder helpers
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

    ``rooms`` is a list of dicts:
        {id, name, temp, active, open, eff}
    """
    from hvac_vent_optimizer.coordinator import FlairCoordinator

    vents = {}
    assignments = {}
    for r in rooms:
        vents[r["id"]] = _vent(r["id"], f"room_{r['id']}", r["name"], r["temp"], r["active"], r["open"])
        assignments[r["id"]] = {const.CONF_THERMOSTAT_ENTITY: thermostat, const.CONF_TEMP_SENSOR_ENTITY: None}
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
        data={const.CONF_STRUCTURE_ID: "s1", const.CONF_CLIENT_ID: "id", const.CONF_CLIENT_SECRET: "sec"},
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
    return dict(api.set_vent_calls)  # vent_id -> last commanded percent


# ---------------------------------------------------------------------------
# 1. balance gathers rooms, allocates, applies floor, commands vents
# ---------------------------------------------------------------------------
def test_balance_hot_low_efficiency_room_gets_more_open_than_satisfied():
    # Cooling, thermostat target 24.0 (effective setpoint ~23.3 after offset).
    # Bedroom 2-like: hot (27.9) and low efficiency (0.017) -> bottleneck.
    # Bathroom-like: overcooled (22.0) -> satisfied -> 0 %.
    coord, api, thermostat, data = _build(
        [
            {"id": "hot", "name": "Bedroom 2", "temp": 27.9, "active": True, "open": 0, "eff": 0.017},
            {"id": "cold", "name": "Bathroom", "temp": 22.0, "active": True, "open": 0, "eff": 0.438},
        ]
    )
    _run(coord, thermostat, data)

    calls = _calls(api)
    assert api.set_vent_calls, "balance must dispatch at least one vent command"
    # Satisfied room stays closed (never commanded above 0).
    assert calls.get("cold", 0) == 0
    # Hot bottleneck gets meaningfully more open than the satisfied room.
    assert calls.get("hot", 0) > calls.get("cold", 0)


def test_balance_bottleneck_saturates_at_full_open():
    coord, api, thermostat, data = _build(
        [
            {"id": "hot", "name": "Bedroom 2", "temp": 27.9, "active": True, "open": 0, "eff": 0.017},
            {"id": "warm", "name": "Bedroom 3", "temp": 24.2, "active": True, "open": 0, "eff": 0.5},
        ]
    )
    _run(coord, thermostat, data)
    calls = _calls(api)
    assert calls.get("hot") == 100, f"bottleneck must be driven to 100%, got {calls.get('hot')}"


# ---------------------------------------------------------------------------
# 2. The safety floor is on the dispatch path (single choke point, no bypass)
# ---------------------------------------------------------------------------
def test_balance_routes_through_apply_safety_floor(monkeypatch):
    from hvac_vent_optimizer import coordinator as coord_mod

    seen = {}
    real = coord_mod.apply_safety_floor

    def spy(targets, rooms, settings):
        seen["called"] = True
        return real(targets, rooms, settings)

    monkeypatch.setattr(coord_mod, "apply_safety_floor", spy)

    coord, _api, thermostat, data = _build(
        [
            {"id": "hot", "name": "Bedroom 2", "temp": 27.9, "active": True, "open": 0, "eff": 0.017},
            {"id": "cold", "name": "Bathroom", "temp": 22.0, "active": True, "open": 0, "eff": 0.438},
        ]
    )
    _run(coord, thermostat, data)
    assert seen.get("called"), "balance must route through balance.apply_safety_floor (no bypass)"


def test_balance_floor_padding_reaches_dispatch():
    # One hot bottleneck (-> 100) plus four warm rooms whose required flow is
    # below leak (-> 0 from allocation). Combined ~ (100+0+0+0+0)/5 = 20 % < 40 %
    # floor, so apply_safety_floor must pad warm rooms and at least one of those
    # opens must reach the dispatch path.
    rooms = [{"id": "hot", "name": "Bedroom 2", "temp": 27.9, "active": True, "open": 0, "eff": 0.017}]
    warm_ids = []
    for i in range(4):
        wid = f"warm{i}"
        warm_ids.append(wid)
        rooms.append({"id": wid, "name": f"Warm{i}", "temp": 24.0, "active": True, "open": 0, "eff": 0.5})

    coord, api, thermostat, data = _build(rooms)
    _run(coord, thermostat, data)
    calls = _calls(api)
    assert calls.get("hot") == 100
    assert any(
        calls.get(w, 0) > 0 for w in warm_ids
    ), "safety floor padding must reach the dispatch path (a warm room opened to meet the floor)"


# ---------------------------------------------------------------------------
# 3. Inactive rooms are not repositioned by balancing (held)
# ---------------------------------------------------------------------------
def test_balance_inactive_room_held_when_close_inactive_disabled():
    coord, api, thermostat, data = _build(
        [
            {"id": "hot", "name": "Bedroom 2", "temp": 27.9, "active": True, "open": 0, "eff": 0.017},
            {"id": "guest", "name": "Guest", "temp": 26.0, "active": False, "open": 60, "eff": 0.2},
        ],
        close_inactive=False,
    )
    _run(coord, thermostat, data)
    calls = _calls(api)
    # The inactive room must not be repositioned by balancing — held at current.
    assert "guest" not in calls, "inactive room must be held (not repositioned) by balancing"
    # The active bottleneck still gets commanded.
    assert calls.get("hot") == 100


def test_balance_close_inactive_rooms_honored():
    coord, api, thermostat, data = _build(
        [
            {"id": "hot", "name": "Bedroom 2", "temp": 27.9, "active": True, "open": 0, "eff": 0.017},
            {"id": "guest", "name": "Guest", "temp": 26.0, "active": False, "open": 60, "eff": 0.2},
        ],
        close_inactive=True,
    )
    _run(coord, thermostat, data)
    calls = _calls(api)
    assert calls.get("guest") == 0, "close_inactive_rooms must close the inactive room"


# ---------------------------------------------------------------------------
# 4. Missing temp/efficiency -> room skipped, never crash
# ---------------------------------------------------------------------------
def test_balance_missing_room_temp_skipped_gracefully():
    coord, api, thermostat, data = _build(
        [
            {"id": "hot", "name": "Bedroom 2", "temp": 27.9, "active": True, "open": 0, "eff": 0.017},
            # No room temp and no temp sensor -> unresolvable temperature.
            {"id": "ghost", "name": "Ghost", "temp": None, "active": True, "open": 0, "eff": 0.2},
        ]
    )
    # Must not raise.
    _run(coord, thermostat, data)
    calls = _calls(api)
    # The room with no usable temperature is skipped (held, never commanded).
    assert "ghost" not in calls
    # The healthy room still allocates and commands.
    assert calls.get("hot") == 100


# ---------------------------------------------------------------------------
# 5. Legacy strategies unchanged by the R20.5 refactor
# ---------------------------------------------------------------------------
def test_legacy_hybrid_strategy_still_commands():
    coord, api, thermostat, data = _build(
        [
            {"id": "v1", "name": "Room1", "temp": 27.0, "active": True, "open": 0, "eff": 0.5},
        ],
        strategy="hybrid",
    )
    _run(coord, thermostat, data)
    assert api.set_vent_calls, "legacy hybrid strategy must still dispatch commands after the refactor"


@pytest.mark.parametrize("strategy", ["dab", "cost", "stats", "hybrid"])
def test_legacy_strategies_do_not_route_through_balance(monkeypatch, strategy):
    from hvac_vent_optimizer import coordinator as coord_mod

    called = {"allocate": False}
    real = coord_mod.allocate

    def spy(*a, **k):
        called["allocate"] = True
        return real(*a, **k)

    monkeypatch.setattr(coord_mod, "allocate", spy)

    coord, _api, thermostat, data = _build(
        [{"id": "v1", "name": "Room1", "temp": 27.0, "active": True, "open": 0, "eff": 0.5}],
        strategy=strategy,
    )
    _run(coord, thermostat, data)
    assert called["allocate"] is False, f"legacy strategy '{strategy}' must not call balance.allocate"


# ---------------------------------------------------------------------------
# 6. Task 32 — the gather builds and passes a learned VentCurve per room
#    (replacing the scalar leak), and the curve flows through to allocation.
# ---------------------------------------------------------------------------
def _seed_saturating_curve(coord, vent_id, *, leak=0.1, mode="cooling"):
    """Seed a saturating vent_effectiveness curve (50 % knee) for ``vent_id``."""
    coord._vent_effectiveness[vent_id] = {
        mode: {
            "leak": leak,
            "n": 24,
            "curve": {
                "breakpoints": [0, 5, 10, 20, 35, 50, 75, 100],
                "flow": [leak, 0.45, 0.65, 0.82, 0.93, 1.0, 1.0, 1.0],
                "counts": [3, 3, 3, 3, 3, 3, 3, 3],
            },
            "knee_pct": 50,
        }
    }


def test_balance_gather_passes_ventcurve_on_room_alloc_input(monkeypatch):
    # The coordinator must construct a VentCurve per room and pass it on
    # RoomAllocInput.curve (Task 32). Spy on balance.allocate to capture rooms.
    from hvac_vent_optimizer import coordinator as coord_mod
    from hvac_vent_optimizer.learning import VentCurve

    captured = {}
    real = coord_mod.allocate

    def spy(rooms, setpoint, mode, settings, *a, **k):
        captured["rooms"] = rooms
        return real(rooms, setpoint, mode, settings, *a, **k)

    monkeypatch.setattr(coord_mod, "allocate", spy)

    coord, _api, thermostat, data = _build(
        [
            {"id": "hot", "name": "Bedroom 2", "temp": 27.9, "active": True, "open": 0, "eff": 0.017},
            {"id": "warm", "name": "Bedroom 3", "temp": 26.5, "active": True, "open": 0, "eff": 0.05},
        ]
    )
    _run(coord, thermostat, data)

    assert "rooms" in captured, "balance.allocate must be called for the balance strategy"
    assert captured["rooms"], "at least one room must be gathered"
    for room in captured["rooms"]:
        assert room.curve is not None, f"room {room.room_id} must carry a VentCurve"
        assert isinstance(room.curve, VentCurve)


def test_balance_seeded_saturating_curve_drives_bottleneck_to_knee():
    # With a learned saturating curve (knee 50 %) for the bottleneck vent, the
    # coordinator must command it at its knee (~50 %), NOT 100 % — opening past
    # the knee is wasted airflow.
    coord, api, thermostat, data = _build(
        [{"id": "hot", "name": "Bedroom 2", "temp": 27.9, "active": True, "open": 0, "eff": 0.017}]
    )
    _seed_saturating_curve(coord, "hot")
    _run(coord, thermostat, data)
    calls = _calls(api)
    assert calls.get("hot") == 50, f"bottleneck should be commanded at its 50%% knee, got {calls.get('hot')}"


def test_get_vent_curve_falls_back_to_near_linear_when_unseeded():
    # No vent_effectiveness and no regression → a near-linear seed curve whose
    # knee is 100 % (so behavior matches the scalar-leak model it supersedes).
    coord, _api, _thermostat, _data = _build(
        [{"id": "hot", "name": "Bedroom 2", "temp": 27.9, "active": True, "open": 0, "eff": 0.017}]
    )
    from homeassistant.components.climate.const import HVACAction

    curve = coord._get_vent_curve("hot", HVACAction.COOLING)
    assert curve is not None
    assert curve.knee() == 100
