"""Coordinator wiring of the pure ``context`` + ``learning`` modules (Task 20).

These exercise the *real* coordinator through the Home Assistant fakes installed
by ``conftest.py``. They prove that for the ``balance`` strategy the coordinator:

* builds a :class:`context.Context` each evaluation from a configured
  ``outdoor_temp_entity``, the room's occupancy, the per-vent
  ``door_sensor_entity`` and ``sun.sun`` (R12.1/12.5), resolving them to the
  primitive values ``context.build`` expects and computing ``regime_index``;
* sources the allocation ``effective_rate`` from the per-room
  :class:`learning.RoomEfficiencyModel` so a learned hot-regime rate is actually
  used once enough samples exist (R11.1/R25.1), with bounded occupancy/door
  context multipliers applied (R12.x); and
* feeds observed cycle samples into ``learning.update_room_efficiency`` on
  finalize so the model improves online (R25.4/R25.5).

Graceful degradation (R22.3): a missing outdoor source degrades to the neutral
"mild" band, missing occupancy/door leave the multiplier at 1.0, and a missing
room model falls back to the existing effective-rate source — never crashing.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from hvac_vent_optimizer import const, context, learning
from tests._fakes import FakeApi, FakeEntry, FakeHass, FakeState


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------
def _vent(vent_id, room_id, room_name, temp_c, active, percent_open, *, occupied=None):
    attrs = {"name": room_name, "active": active}
    if temp_c is not None:
        attrs["current-temperature-c"] = temp_c
    if occupied is not None:
        attrs["occupied"] = occupied
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
    outdoor_entity=None,
    door_assignments=None,
):
    """Build a coordinator wired for a cooling scenario.

    ``rooms`` is a list of dicts: {id, name, temp, active, open, eff, occupied?}.
    ``door_assignments`` maps vent id -> door sensor entity id.
    """
    from hvac_vent_optimizer.coordinator import FlairCoordinator

    door_assignments = door_assignments or {}
    vents = {}
    assignments = {}
    for r in rooms:
        vents[r["id"]] = _vent(
            r["id"],
            f"room_{r['id']}",
            r["name"],
            r["temp"],
            r["active"],
            r["open"],
            occupied=r.get("occupied"),
        )
        assignment = {
            const.CONF_THERMOSTAT_ENTITY: thermostat,
            const.CONF_TEMP_SENSOR_ENTITY: None,
        }
        if r["id"] in door_assignments:
            assignment[const.CONF_DOOR_SENSOR_ENTITY] = door_assignments[r["id"]]
        assignments[r["id"]] = assignment
    data = {"vents": vents, "pucks": {}}

    options = {
        const.CONF_VENT_BRAND: const.BRAND_FLAIR,
        const.CONF_DAB_ENABLED: True,
        const.CONF_VENT_ASSIGNMENTS: assignments,
        const.CONF_CONTROL_STRATEGY: strategy,
        const.CONF_CLOSE_INACTIVE_ROOMS: True,
    }
    if outdoor_entity is not None:
        options[const.CONF_OUTDOOR_TEMP_ENTITY] = outdoor_entity

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

    return coord, hass, api, thermostat, data


# ---------------------------------------------------------------------------
# 1. Context is built from HA states (outdoor / occupancy / door / sun).
# ---------------------------------------------------------------------------
def test_build_context_resolves_outdoor_occupancy_door_and_sun():
    coord, hass, _api, _therm, data = _build(
        [
            {
                "id": "v1",
                "name": "Room1",
                "temp": 27.0,
                "active": True,
                "open": 0,
                "eff": 0.05,
                "occupied": True,
            }
        ],
        outdoor_entity="sensor.outdoor",
        door_assignments={"v1": "binary_sensor.door"},
    )
    hass.states.set("sensor.outdoor", FakeState("30.0", {"unit_of_measurement": "°C"}))
    hass.states.set("binary_sensor.door", FakeState("on"))
    hass.states.set("sun.sun", FakeState("above_horizon"))

    ctx = coord._build_context("v1", data)
    assert ctx.outdoor_band == 2  # hot (>HOT_C)
    assert ctx.occupied is True
    assert ctx.doors_open is True
    assert ctx.is_daytime is True
    assert context.regime_index(ctx) == 1  # day-hot


def test_build_context_outdoor_band_cold_and_night():
    coord, hass, _api, _therm, data = _build(
        [{"id": "v1", "name": "Room1", "temp": 21.0, "active": True, "open": 0, "eff": 0.05}],
        outdoor_entity="sensor.outdoor",
    )
    hass.states.set("sensor.outdoor", FakeState("3.0", {"unit_of_measurement": "°C"}))
    hass.states.set("sun.sun", FakeState("below_horizon"))

    ctx = coord._build_context("v1", data)
    assert ctx.outdoor_band == 0  # cold (<COLD_C)
    assert ctx.is_daytime is False
    # cold collapses into mild, night -> regime 2 (night-mild)
    assert context.regime_index(ctx) == 2


def test_build_context_outdoor_temp_resolves_fahrenheit():
    coord, hass, _api, _therm, data = _build(
        [{"id": "v1", "name": "Room1", "temp": 27.0, "active": True, "open": 0, "eff": 0.05}],
        outdoor_entity="sensor.outdoor",
    )
    # 90 F ~= 32.2 C -> hot band.
    hass.states.set("sensor.outdoor", FakeState("90", {"unit_of_measurement": "°F"}))
    ctx = coord._build_context("v1", data)
    assert ctx.outdoor_band == 2


def test_build_context_outdoor_temp_from_weather_entity():
    coord, hass, _api, _therm, data = _build(
        [{"id": "v1", "name": "Room1", "temp": 27.0, "active": True, "open": 0, "eff": 0.05}],
        outdoor_entity="weather.home",
    )
    # weather.* exposes temperature as an attribute, not the state.
    hass.states.set(
        "weather.home",
        FakeState("sunny", {"temperature": 30.0, "temperature_unit": "°C"}),
    )
    ctx = coord._build_context("v1", data)
    assert ctx.outdoor_band == 2


# ---------------------------------------------------------------------------
# 2. Graceful degradation when sources are missing/unavailable.
# ---------------------------------------------------------------------------
def test_build_context_graceful_when_sources_missing():
    coord, _hass, _api, _therm, data = _build(
        [{"id": "v1", "name": "Room1", "temp": 27.0, "active": True, "open": 0, "eff": 0.05}],
    )
    # No outdoor entity, no occupancy attribute, no door sensor, no sun.
    ctx = coord._build_context("v1", data)
    assert ctx.outdoor_band == 1  # mild (neutral default)
    assert ctx.occupied is None
    assert ctx.doors_open is None
    # Multipliers are neutral when occupancy/door are unknown.
    assert context.apply_context_multipliers(0.5, ctx, "cooling") == 0.5


def test_build_context_graceful_when_outdoor_unavailable():
    coord, hass, _api, _therm, data = _build(
        [{"id": "v1", "name": "Room1", "temp": 27.0, "active": True, "open": 0, "eff": 0.05}],
        outdoor_entity="sensor.outdoor",
        door_assignments={"v1": "binary_sensor.door"},
    )
    hass.states.set("sensor.outdoor", FakeState("unavailable"))
    hass.states.set("binary_sensor.door", FakeState("unknown"))
    ctx = coord._build_context("v1", data)
    assert ctx.outdoor_band == 1  # unavailable -> mild
    assert ctx.doors_open is None  # unknown door -> tri-state None


# ---------------------------------------------------------------------------
# 3. effective_rate is sourced from the learned per-room model + regime.
# ---------------------------------------------------------------------------
def _seed_room_model(coord, room_name, *, baseline, hot_rate):
    """Seed a room model: a learned baseline + a trusted day-hot (regime 1) cell."""
    model = learning.new_room_model()
    cooling = model.cooling
    cooling.baseline = baseline
    cooling.n = 50
    cell = cooling.regimes[1]  # day-hot
    cell.rate = hot_rate
    cell.n = learning.REGIME_MIN_N
    coord._room_efficiency_models[room_name] = model
    return model


def test_effective_rate_uses_learned_hot_regime_when_hot():
    coord, hass, _api, _therm, data = _build(
        [{"id": "v1", "name": "Room1", "temp": 27.0, "active": True, "open": 0, "eff": 0.5}],
        outdoor_entity="sensor.outdoor",
    )
    _seed_room_model(coord, "Room1", baseline=0.02, hot_rate=0.08)
    hass.states.set("sensor.outdoor", FakeState("32.0", {"unit_of_measurement": "°C"}))
    hass.states.set("sun.sun", FakeState("above_horizon"))

    rate = coord._get_room_effective_rate("v1", "cooling", data)
    # day-hot regime (index 1) is trusted -> its learned rate is used, not baseline.
    assert abs(rate - 0.08) < 1e-9


def test_effective_rate_uses_baseline_when_regime_untrusted():
    coord, hass, _api, _therm, data = _build(
        [{"id": "v1", "name": "Room1", "temp": 27.0, "active": True, "open": 0, "eff": 0.5}],
        outdoor_entity="sensor.outdoor",
    )
    _seed_room_model(coord, "Room1", baseline=0.02, hot_rate=0.08)
    # Mild + day -> regime 0 (day-mild) which has no samples -> baseline used.
    hass.states.set("sensor.outdoor", FakeState("18.0", {"unit_of_measurement": "°C"}))
    hass.states.set("sun.sun", FakeState("above_horizon"))

    rate = coord._get_room_effective_rate("v1", "cooling", data)
    assert abs(rate - 0.02) < 1e-9


def test_effective_rate_applies_occupancy_multiplier():
    coord, hass, _api, _therm, data = _build(
        [
            {
                "id": "v1",
                "name": "Room1",
                "temp": 27.0,
                "active": True,
                "open": 0,
                "eff": 0.5,
                "occupied": True,
            }
        ],
        outdoor_entity="sensor.outdoor",
    )
    _seed_room_model(coord, "Room1", baseline=0.02, hot_rate=0.10)
    hass.states.set("sensor.outdoor", FakeState("32.0", {"unit_of_measurement": "°C"}))
    hass.states.set("sun.sun", FakeState("above_horizon"))

    rate = coord._get_room_effective_rate("v1", "cooling", data)
    # Occupied -> learned hot rate 0.10 scaled by OCC_FACTOR (0.9).
    assert abs(rate - 0.10 * context.OCC_FACTOR) < 1e-9


def test_effective_rate_falls_back_to_legacy_when_no_model():
    coord, _hass, _api, _therm, data = _build(
        [{"id": "v1", "name": "Room1", "temp": 27.0, "active": True, "open": 0, "eff": 0.5}],
    )
    # No room model seeded -> must fall back to the legacy effective-rate source.
    rate = coord._get_room_effective_rate("v1", "cooling", data)
    assert rate > 0  # legacy _vent_rates / initial-rate path
    assert abs(rate - 0.5) < 1e-6


def test_effective_rate_does_not_crash_without_room_name():
    coord, _hass, _api, _therm, data = _build(
        [{"id": "v1", "name": "Room1", "temp": 27.0, "active": True, "open": 0, "eff": 0.5}],
    )
    # Strip room name -> still resolves a rate (fallback), never raises.
    data["vents"]["v1"]["room"]["attributes"].pop("name", None)
    rate = coord._get_room_effective_rate("v1", "cooling", data)
    assert rate >= 0


# ---------------------------------------------------------------------------
# 4. A finalize feeds a sample into the room model; effective_rate reflects it.
# ---------------------------------------------------------------------------
def _seed_cycle_with_samples(coord, thermostat, vent_id):
    """Seed a running cycle whose samples yield a valid cooling efficiency sample."""
    now = datetime.now(UTC)
    started_running = now - timedelta(minutes=12)
    # Two samples inside [warmup, max] window, > MIN_WINDOW apart, cooling
    # (temp decreasing), steady aperture 50% -> a clean negative slope.
    samples = [
        {"t": started_running + timedelta(minutes=3), "temp": 26.0, "aperture": 50.0, "duct": None},
        {"t": started_running + timedelta(minutes=9), "temp": 25.0, "aperture": 50.0, "duct": None},
    ]
    coord._dab_state[thermostat] = {
        "mode": "cooling",
        "started_cycle": started_running,
        "started_running": started_running,
        "samples": {vent_id: samples},
    }
    coord._cycle_stats[thermostat] = {
        "adjustments": 0,
        "movement": 0.0,
        "strategy": "balance",
        "vent_movement": {},
    }


def test_finalize_feeds_sample_into_room_model():
    coord, _hass, _api, thermostat, data = _build(
        [{"id": "v1", "name": "Room1", "temp": 25.0, "active": True, "open": 50, "eff": 0.5}],
        target_temp=23.0,
    )
    _seed_cycle_with_samples(coord, thermostat, "v1")

    assert "Room1" not in coord._room_efficiency_models

    asyncio.run(coord._async_finalize_cycle(thermostat, "cooling", ["v1"], None))

    # A room model now exists and folded the observed sample into the cooling
    # sub-model baseline (R25.4/R25.5).
    model = coord._room_efficiency_models.get("Room1")
    assert model is not None
    assert model.cooling.baseline is not None
    assert model.cooling.baseline > 0
    # The learned model now drives the balance effective-rate source.
    rate = coord._get_room_effective_rate("v1", "cooling", data)
    assert rate > 0


def test_finalize_no_sample_leaves_room_model_untouched():
    coord, _hass, _api, thermostat, _data = _build(
        [{"id": "v1", "name": "Room1", "temp": 25.0, "active": True, "open": 50, "eff": 0.5}],
        target_temp=23.0,
    )
    # A single sample cannot form a slope -> no efficiency sample -> no update.
    now = datetime.now(UTC)
    started_running = now - timedelta(minutes=12)
    coord._dab_state[thermostat] = {
        "mode": "cooling",
        "started_cycle": started_running,
        "started_running": started_running,
        "samples": {
            "v1": [
                {"t": started_running + timedelta(minutes=3), "temp": 26.0, "aperture": 50.0, "duct": None}
            ]
        },
    }
    coord._cycle_stats[thermostat] = {
        "adjustments": 0,
        "movement": 0.0,
        "strategy": "balance",
        "vent_movement": {},
    }

    asyncio.run(coord._async_finalize_cycle(thermostat, "cooling", ["v1"], None))
    assert "Room1" not in coord._room_efficiency_models


# ---------------------------------------------------------------------------
# 5. The balance apply path sources its rate from the learned model.
# ---------------------------------------------------------------------------
def test_balance_apply_path_uses_learned_regime_rate():
    # Two rooms equally hot; only RoomA has a strong learned day-hot rate, so it
    # finishes faster and should be throttled below the slower RoomB bottleneck.
    coord, hass, api, thermostat, data = _build(
        [
            {"id": "a", "name": "RoomA", "temp": 27.0, "active": True, "open": 0, "eff": 0.02},
            {"id": "b", "name": "RoomB", "temp": 27.0, "active": True, "open": 0, "eff": 0.02},
        ],
        outdoor_entity="sensor.outdoor",
    )
    hass.states.set("sensor.outdoor", FakeState("32.0", {"unit_of_measurement": "°C"}))
    hass.states.set("sun.sun", FakeState("above_horizon"))
    # RoomA learns a much higher day-hot rate -> it is the faster room.
    _seed_room_model(coord, "RoomA", baseline=0.02, hot_rate=0.20)
    _seed_room_model(coord, "RoomB", baseline=0.02, hot_rate=0.02)

    asyncio.run(coord._async_apply_dab_adjustments(thermostat, "cooling", ["a", "b"], data))
    calls = dict(api.set_vent_calls)
    # The slow room (RoomB) is the bottleneck -> 100%; the fast room (RoomA) is
    # throttled below it (here all the way to leakage-only 0%) because its
    # learned hot-regime rate is used instead of the cold baseline.
    assert calls.get("b") == 100
    assert calls.get("a", 0) < 100
