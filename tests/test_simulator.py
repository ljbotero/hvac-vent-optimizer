"""Tests for the offline closed-loop thermal simulator (Task 25.1, R15).

``simulator.py`` is a **pure** module (no Home Assistant imports) so — like the
sibling ``test_balance_*.py`` / ``test_learning_*.py`` files — it is loaded
standalone by absolute path under a private name. The simulator itself loads its
pure dependencies (``balance``/``learning``/``context``/``dab``) the same way, so
nothing here touches the ``hvac_vent_optimizer`` package ``__init__`` (which
pulls in Home Assistant, not installed in the test environment).

    python3 -m pytest tests/test_simulator.py -q --import-mode=importlib

Covers (R15.1/15.2/15.4/15.5/15.7, R25.12):

* the per-step stepper advances ``T_i += sign*e_i(ctx)*flow_i(a_i)*dt -
  idle_drift_i*dt`` (R15.1);
* the run ends when the **average of active-room temps** reaches setpoint, else
  at the horizon (R15.1);
* every step routes through ``apply_safety_floor`` so the combined open % never
  drops below the configured floor (R15.2);
* both ``balance`` and ``dab`` strategies are runnable against the same scenario
  (R15.2);
* runs are deterministic for a fixed seed/scenario (R15.5);
* the learned **non-linear saturating curve** ``flow_i`` is monotonic with
  ``flow(0)=leak``, ``flow(100%)=1`` and a knee below 100 % (R25.12);
* an optional outdoor/weather drift profile feeds the context regime (R15.7);
* inactive rooms are excluded from termination and the spread metric.
"""

from __future__ import annotations

import importlib.util
import pathlib
import sys

import pytest

# --- Load the pure modules standalone (no HA) ------------------------------
_ROOT = pathlib.Path(__file__).resolve().parent.parent / "custom_components" / "hvac_vent_optimizer"


def _load(name: str):
    path = _ROOT / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"hvo_{name}", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


balance = _load("balance")
learning = _load("learning")
context = _load("context")
simulator = _load("simulator")


# ---------------------------------------------------------------------------
# Scenario helpers
# ---------------------------------------------------------------------------
def _cooling_scenario(**overrides):
    """A small multi-room cooling scenario (Bedroom 2-pinned style)."""
    rooms = [
        simulator.RoomScenario(
            room_id="bedroom_2",
            temp_c=27.5,
            efficiency=0.017,
            leak=0.1,
            idle_drift=0.0,
        ),
        simulator.RoomScenario(
            room_id="bedroom_3",
            temp_c=27.0,
            efficiency=0.020,
            leak=0.1,
            idle_drift=0.0,
        ),
        simulator.RoomScenario(
            room_id="bathroom",
            temp_c=25.7,
            efficiency=0.438,
            leak=0.1,
            idle_drift=0.0,
        ),
    ]
    kwargs = {
        "rooms": rooms,
        "setpoint_c": 26.1,
        "mode": "cooling",
        "dt_min": 1.0,
        "horizon_min": 600.0,
        "seed": 7,
    }
    kwargs.update(overrides)
    return simulator.Scenario(**kwargs)


# ---------------------------------------------------------------------------
# Flow curve (R25.12)
# ---------------------------------------------------------------------------
def test_linear_seed_curve_endpoints_monotonic_and_full_knee():
    curve = learning.seed_linear_curve(0.1)
    assert simulator.flow_from_curve(curve, 0.0) == pytest.approx(0.1)
    assert simulator.flow_from_curve(curve, 100.0) == pytest.approx(1.0)
    # Non-decreasing across the sweep.
    prev = -1.0
    for a in range(0, 101, 5):
        f = simulator.flow_from_curve(curve, float(a))
        assert f >= prev - 1e-9
        prev = f
    # A near-linear seed has no plateau, so the knee sits at full open.
    assert learning.curve_knee_pct(curve) == 100


def test_representative_saturating_curve_has_knee_below_100():
    curve = simulator.representative_saturating_curve(0.1)
    assert simulator.flow_from_curve(curve, 0.0) == pytest.approx(0.1)
    assert simulator.flow_from_curve(curve, 100.0) == pytest.approx(1.0)
    # Monotonic non-decreasing.
    prev = -1.0
    for a in range(101):
        f = simulator.flow_from_curve(curve, float(a))
        assert f >= prev - 1e-9
        prev = f
    # Saturating: the knee is reached before 100 % (R25.12).
    knee = learning.curve_knee_pct(curve)
    assert 0 < knee < 100


def test_flow_from_curve_interpolates_between_breakpoints():
    # Straight line leak=0 → flow(a)=a/100, so the midpoint of a segment
    # interpolates linearly.
    curve = learning.seed_linear_curve(0.0)
    assert simulator.flow_from_curve(curve, 50.0) == pytest.approx(0.5, abs=1e-6)
    # Clamps outside [0, 100].
    assert simulator.flow_from_curve(curve, -10.0) == pytest.approx(0.0)
    assert simulator.flow_from_curve(curve, 250.0) == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# Stepper math (R15.1)
# ---------------------------------------------------------------------------
def test_single_step_advances_per_formula():
    # One isolated room, vent forced fully open, linear curve, known drift.
    room = simulator.RoomScenario(
        room_id="r",
        temp_c=28.0,
        efficiency=0.5,
        leak=0.1,
        idle_drift=0.05,  # +°C/min passive cooling drift
        curve=learning.seed_linear_curve(0.1),
    )
    ctx = context.build(hour=14)  # mild, day → neutral multipliers
    a_pct = 100.0
    flow = simulator.flow_from_curve(room.curve, a_pct)  # == 1.0
    e = context.apply_context_multipliers(room.efficiency, ctx, "cooling")
    dt = 1.0
    expected = 28.0 + (-1.0) * e * flow * dt - room.idle_drift * dt
    got = simulator.advance_temp(room.temp_c, a_pct, room, ctx, "cooling", dt)
    assert got == pytest.approx(expected)
    # Cooling drives the temperature down.
    assert got < 28.0


# ---------------------------------------------------------------------------
# Termination governance (R15.1)
# ---------------------------------------------------------------------------
def test_run_ends_on_active_average_reaching_setpoint_cooling():
    scenario = _cooling_scenario()
    result = simulator.run(scenario, strategy="balance")
    assert result.ended_reason == "setpoint"
    # Average of active-room temps has reached (≤) the setpoint at the end.
    active_final = [t for rid, t in result.final_temps.items() if rid != "__none__"]
    assert sum(active_final) / len(active_final) <= scenario.setpoint_c + 1e-6
    assert result.minutes <= scenario.horizon_min


def test_run_ends_at_horizon_when_setpoint_unreachable():
    # Tiny efficiency + strong opposing drift → never reaches setpoint.
    rooms = [
        simulator.RoomScenario(
            room_id="r",
            temp_c=30.0,
            efficiency=0.001,
            leak=0.0,
            idle_drift=-0.05,  # heat ingress dominates the feeble cooling
        ),
    ]
    scenario = simulator.Scenario(
        rooms=rooms,
        setpoint_c=24.0,
        mode="cooling",
        dt_min=1.0,
        horizon_min=30.0,
    )
    result = simulator.run(scenario, strategy="balance")
    assert result.ended_reason == "horizon"
    assert result.minutes == pytest.approx(30.0)


# ---------------------------------------------------------------------------
# Safety floor honored in the sim (R15.2)
# ---------------------------------------------------------------------------
def test_safety_floor_honored_every_step():
    scenario = _cooling_scenario(settings=balance.AllocSettings(safety_floor_pct=40.0, conventional_vents=0))
    result = simulator.run(scenario, strategy="balance")
    floor = 40.0
    assert result.min_combined_open_pct >= floor - 1e-6
    # Recorded for every step.
    assert len(result.combined_open_history) == result.steps


def test_dab_strategy_runs_and_honors_floor():
    scenario = _cooling_scenario()
    result = simulator.run(scenario, strategy="dab")
    assert result.steps > 0
    assert result.min_combined_open_pct >= 40.0 - 1e-6


# ---------------------------------------------------------------------------
# Determinism (R15.5)
# ---------------------------------------------------------------------------
def test_run_is_deterministic_for_fixed_seed():
    a = simulator.run(_cooling_scenario(), strategy="balance")
    b = simulator.run(_cooling_scenario(), strategy="balance")
    assert a.ended_reason == b.ended_reason
    assert a.steps == b.steps
    assert a.minutes == pytest.approx(b.minutes)
    assert a.avg_spread == pytest.approx(b.avg_spread)
    assert a.max_spread == pytest.approx(b.max_spread)
    assert a.total_moves == b.total_moves
    assert a.final_temps.keys() == b.final_temps.keys()
    for rid in a.final_temps:
        assert a.final_temps[rid] == pytest.approx(b.final_temps[rid])


def test_run_deterministic_with_sensor_noise_same_seed():
    s1 = _cooling_scenario(sensor_noise_c=0.05, seed=42)
    s2 = _cooling_scenario(sensor_noise_c=0.05, seed=42)
    r1 = simulator.run(s1, strategy="balance")
    r2 = simulator.run(s2, strategy="balance")
    assert r1.steps == r2.steps
    for rid in r1.final_temps:
        assert r1.final_temps[rid] == pytest.approx(r2.final_temps[rid])


# ---------------------------------------------------------------------------
# Outdoor profile feeds context regime (R15.7)
# ---------------------------------------------------------------------------
def test_outdoor_profile_changes_regime():
    # A profile that is mild early and hot later should change the regime index
    # the simulator derives at those minutes.
    def profile(minute: float) -> float:
        return 8.0 if minute < 60 else 30.0

    scenario = _cooling_scenario(outdoor_profile=profile, start_hour=14)
    ctx_early = simulator.context_at(scenario, 0.0)
    ctx_late = simulator.context_at(scenario, 120.0)
    assert context.regime_index(ctx_early) != context.regime_index(ctx_late)
    # Run still completes deterministically with a profile attached.
    result = simulator.run(scenario, strategy="balance")
    assert result.steps > 0


# ---------------------------------------------------------------------------
# Inactive rooms excluded (R15.1 governance / spread)
# ---------------------------------------------------------------------------
def test_inactive_room_excluded_from_termination_and_spread():
    rooms = [
        simulator.RoomScenario(room_id="active", temp_c=27.0, efficiency=0.2, leak=0.1, idle_drift=0.0),
        # Inactive room sits far off-setpoint; it must NOT keep the run going
        # nor inflate the spread metric.
        simulator.RoomScenario(
            room_id="inactive",
            temp_c=35.0,
            efficiency=0.2,
            leak=0.1,
            idle_drift=0.0,
            active=False,
        ),
    ]
    scenario = simulator.Scenario(
        rooms=rooms,
        setpoint_c=26.0,
        mode="cooling",
        dt_min=1.0,
        horizon_min=600.0,
    )
    result = simulator.run(scenario, strategy="balance")
    assert result.ended_reason == "setpoint"
    # Spread is measured over active rooms only; with a single active room the
    # active spread is 0 throughout.
    assert result.max_spread == pytest.approx(0.0, abs=1e-9)


def test_balance_reduces_active_spread_over_the_run():
    # All rooms start ABOVE setpoint with comparable efficiencies (no satisfied
    # outlier whose residual leak would overcool it), so synchronized
    # convergence tightens the active-room spread (R1/R2).
    rooms = [
        simulator.RoomScenario("a", 27.5, 0.05, 0.1, idle_drift=0.0),
        simulator.RoomScenario("b", 27.0, 0.06, 0.1, idle_drift=0.0),
        simulator.RoomScenario("c", 26.6, 0.07, 0.1, idle_drift=0.0),
    ]
    scenario = simulator.Scenario(
        rooms=rooms, setpoint_c=26.1, mode="cooling", dt_min=1.0, horizon_min=600.0, seed=3
    )
    result = simulator.run(scenario, strategy="balance")
    initial_spread = 27.5 - 26.6
    final_active = [result.final_temps[r] for r in ("a", "b", "c")]
    final_spread = max(final_active) - min(final_active)
    assert final_spread < initial_spread


def test_total_moves_counted_and_per_room_reported():
    scenario = _cooling_scenario()
    result = simulator.run(scenario, strategy="balance")
    assert result.total_moves > 0
    assert set(result.moves_per_room) == {"bedroom_2", "bedroom_3", "bathroom"}
    assert sum(result.moves_per_room.values()) == result.total_moves


# ---------------------------------------------------------------------------
# Side-by-side comparison table (Task 25.2, R15.3)
# ---------------------------------------------------------------------------
def test_compare_runs_each_strategy_and_tabulates():
    scenario = _cooling_scenario()
    cmp = simulator.compare(scenario, ["dab", "balance"], to_stdout=False)
    # Each requested strategy was run via run() and keyed by name.
    assert set(cmp.results) == {"dab", "balance"}
    assert cmp.results["balance"].strategy == "balance"
    assert cmp.results["dab"].strategy == "dab"
    # The rendered table names both strategies as columns.
    assert "balance" in cmp.table
    assert "dab" in cmp.table
    # Every R15.3 metric appears as a row label.
    for label in (
        "avg_spread",
        "max_spread",
        "time_above_guardrail",
        "total_moves",
        "avg_active_error",
        "max_active_error",
    ):
        assert label in cmp.table
    # moves/room is broken out per active room.
    for rid in ("bedroom_2", "bedroom_3", "bathroom"):
        assert rid in cmp.table


def test_compare_metrics_match_individual_runs():
    scenario = _cooling_scenario()
    cmp = simulator.compare(scenario, ["dab", "balance"], to_stdout=False)
    for strat in ("dab", "balance"):
        direct = simulator.run(_cooling_scenario(), strategy=strat)
        assert cmp.results[strat].avg_spread == pytest.approx(direct.avg_spread)
        assert cmp.results[strat].max_spread == pytest.approx(direct.max_spread)
        assert cmp.results[strat].total_moves == direct.total_moves
        assert cmp.results[strat].max_active_error == pytest.approx(direct.max_active_error)


def test_compare_is_deterministic():
    table_a = simulator.compare(_cooling_scenario(), ["dab", "balance"], to_stdout=False).table
    table_b = simulator.compare(_cooling_scenario(), ["dab", "balance"], to_stdout=False).table
    assert table_a == table_b


def test_compare_prints_to_stdout_by_default(capsys):
    simulator.compare(_cooling_scenario(), ["dab", "balance"])
    out = capsys.readouterr().out
    assert "balance" in out
    assert "dab" in out
    assert "avg_spread" in out


def test_compare_rejects_empty_strategy_list():
    with pytest.raises(ValueError):
        simulator.compare(_cooling_scenario(), [], to_stdout=False)


def test_compare_rejects_unknown_strategy():
    with pytest.raises(ValueError):
        simulator.compare(_cooling_scenario(), ["does_not_exist"], to_stdout=False)


# ---------------------------------------------------------------------------
# Per-room door-leakage learning (Task 13.1, R26.1/R26.3/R27.4)
# ---------------------------------------------------------------------------
# The simulator's ``doors_open`` scenario input feeds the *global* context door
# multiplier, but the door-leakage *learning* (learning.update_door_factor /
# resolve_door_factor) is per room/per mode. These tests drive the extended
# scenario in which each room injects its own door-open leakage ratio and the
# simulator folds enough door-open samples through the pure learner to converge
# the cell — proving the per-room differentiation the flat 0.9 cannot express.
def _door_scenario(**overrides):
    """Two rooms with materially different injected door-open leakage + a
    third room kept below the confidence gate, all sharing one cooling setpoint.

    ``door_open_factor`` is the injected *true* ratio ``rate_open / rate_closed``
    for the room (a leaky room degrades a lot → low ratio; a tight interior door
    barely degrades → ratio near 1.0). ``door_open_samples`` is how many
    door-open observations the simulator folds into that room's door-factor cell.
    """
    rooms = [
        simulator.RoomScenario(
            room_id="leaky",
            temp_c=27.0,
            efficiency=0.050,
            leak=0.1,
            idle_drift=0.0,
            door_open_factor=0.5,
            door_open_samples=DOOR_LEARN_SAMPLES,
        ),
        simulator.RoomScenario(
            room_id="tight",
            temp_c=27.0,
            efficiency=0.050,
            leak=0.1,
            idle_drift=0.0,
            door_open_factor=0.97,
            door_open_samples=DOOR_LEARN_SAMPLES,
        ),
        simulator.RoomScenario(
            room_id="cold_start",
            temp_c=27.0,
            efficiency=0.050,
            leak=0.1,
            idle_drift=0.0,
            door_open_factor=0.5,
            # Deliberately below the confidence gate so it must resolve to 0.9.
            door_open_samples=learning.DOOR_MIN_N - 1,
        ),
    ]
    kwargs = {
        "rooms": rooms,
        "setpoint_c": 26.0,
        "mode": "cooling",
        "doors_open": True,
        "dt_min": 1.0,
        "horizon_min": 600.0,
        "seed": 7,
    }
    kwargs.update(overrides)
    return simulator.Scenario(**kwargs)


# A comfortable margin above ``DOOR_MIN_N`` so the EMA settles near the injected
# ratio for the "sufficient samples" rooms.
DOOR_LEARN_SAMPLES = 30


def test_learn_door_factors_differentiates_leaky_from_tight():
    resolved = simulator.learn_door_factors(_door_scenario())
    # A leaky room converges toward the lower clamp...
    assert resolved["leaky"] == pytest.approx(0.5, abs=0.05)
    # ...a tight interior door stays near 1.0...
    assert resolved["tight"] >= 0.95
    # ...and the two are materially different (the whole point of learning it
    # per room instead of one flat 0.9).
    assert resolved["tight"] - resolved["leaky"] > 0.4


def test_learn_door_factors_sub_gate_room_resolves_to_default():
    resolved = simulator.learn_door_factors(_door_scenario())
    # Fewer than DOOR_MIN_N door-open samples → not trusted → legacy 0.9 exactly.
    assert resolved["cold_start"] == pytest.approx(learning.DOOR_FACTOR_DEFAULT)
    assert resolved["cold_start"] == pytest.approx(0.9)


def test_learn_door_factors_only_reports_rooms_with_injected_leakage():
    # A room with no injected door-open leakage is not a door-learning subject
    # and must not appear in the resolved map (R30.2 — no misleading factor).
    scenario = _door_scenario(
        rooms=[
            simulator.RoomScenario(
                "leaky", 27.0, 0.05, 0.1, door_open_factor=0.5, door_open_samples=DOOR_LEARN_SAMPLES
            ),
            simulator.RoomScenario("no_door", 27.0, 0.05, 0.1),
        ]
    )
    resolved = simulator.learn_door_factors(scenario)
    assert set(resolved) == {"leaky"}


def test_learn_door_factors_resolved_values_are_bounded():
    # Every resolved factor stays within [DOOR_FACTOR_MIN, 1.0] (Property 14),
    # even when the injected leakage is below the clamp.
    scenario = _door_scenario(
        rooms=[
            simulator.RoomScenario(
                "over_leaky", 27.0, 0.05, 0.1, door_open_factor=0.2, door_open_samples=DOOR_LEARN_SAMPLES
            ),
            simulator.RoomScenario(
                "tight", 27.0, 0.05, 0.1, door_open_factor=1.2, door_open_samples=DOOR_LEARN_SAMPLES
            ),
        ]
    )
    resolved = simulator.learn_door_factors(scenario)
    for value in resolved.values():
        assert learning.DOOR_FACTOR_MIN <= value <= learning.DOOR_FACTOR_MAX
    # Below-clamp injection resolves to the lower clamp; above-clamp to 1.0.
    assert resolved["over_leaky"] == pytest.approx(learning.DOOR_FACTOR_MIN)
    assert resolved["tight"] == pytest.approx(learning.DOOR_FACTOR_MAX)


def test_learn_door_factors_is_deterministic():
    a = simulator.learn_door_factors(_door_scenario())
    b = simulator.learn_door_factors(_door_scenario())
    assert a == b
