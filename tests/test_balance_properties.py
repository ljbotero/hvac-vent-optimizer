"""Property-based tests (Hypothesis) for the ``balance`` allocation core.

Task 13 (R20.2). These encode the design's "Correctness Properties" 1-6, 9 and
10 as universal invariants of the pure ``balance`` module and exercise them over
randomized rooms / temperatures / efficiencies / leaks / active-sets / settings.

``balance.py`` imports no Home Assistant, so — like the sibling
``test_balance_*.py`` files — we load it standalone by absolute path under the
``hvo_balance`` name. This never touches the ``hvac_vent_optimizer`` package
(whose ``__init__`` pulls in Home Assistant, which is not installed in the test
environment).

    python3 -m pytest tests/test_balance_properties.py -q --import-mode=importlib
    python3 -m pytest -k property -q

Properties encoded (see design.md "Correctness Properties"):

* P1  Safety floor is inviolable for ALL inputs, including degenerate ones:
      ``apply_safety_floor`` only ever raises apertures, never exceeds 100 %,
      and — when capacity exists — drives the per-vent-expanded combined open %
      to at least the clamped floor. The combined metric uses commanded
      aperture only, so leakage can never relax the floor (R3.x / R25.9).
* P2  No overcool/overheat bias: a satisfied room is allocated 0 % before the
      floor, and no commanded (aperture > 0) room is throttled to finish
      earlier than the bottleneck horizon ``tau*`` (R2.3 / R4.2 / R8).
* P3  Bottleneck saturation: the slowest unsatisfied room is allocated 100 %,
      and no unsatisfied room is more open than it (R4.1 / R4.3 / R5.1).
* P4  Spread monotonicity: a strictly spread-improving move never increases
      predicted spread; reducing a satisfied room's aperture never increases it
      (R2.1 / R6.1).
* P5  Allocation monotonic in need: all else equal, a room with the larger
      error (or lower efficiency) receives an aperture >= a better-off room
      (R4.1 / R4.3).
* P6  Determinism: ``allocate`` is pure — identical inputs -> identical outputs
      (R4.5 / R1.5).
* P9  Grouping consistency (allocation level): rooms with identical inputs
      receive identical commanded targets (R23.x).
* P10 Movement-gating soundness: outside a floor-driven open, ``should_apply``
      returns ``True`` iff predicted spread > guardrail AND improvement >=
      deadband (R7.1 / R7.2 / R7.3 / R7.5).

Task 32.1 (R20.2) re-runs Properties 1-6 and 10 against the learned non-linear
airflow model: in addition to the existing scalar-leak / linear path, each
property is exercised a second time with a random valid monotonic saturating
:class:`learning.VentCurve` attached to every room (``RoomAllocInput.curve``).
The curve-aware ``allocate`` uses ``curve.flow``/``curve.knee``/``curve.inverse``
instead of the linear ``leak + (1-leak)*a`` model, so these variants confirm the
floor stays inviolable and the bottleneck / monotonicity / determinism / gating
invariants still hold when the bottleneck saturates at a knee that may be well
below 100 % open. The shared ``_tau_star`` / projection helpers are written
curve-aware (they reduce to the exact linear behavior when ``curve is None``), so
the linear and curve properties share one source of truth.
"""

from __future__ import annotations

import importlib.util
import pathlib
import sys
from itertools import accumulate

from hypothesis import assume, given, settings as hyp_settings, strategies as st

# --- Load balance.py standalone (pure module, no HA) -----------------------
_BALANCE_PATH = pathlib.Path(__file__).resolve().parent.parent / "custom_components" / "hvac_vent_optimizer" / "balance.py"
_spec = importlib.util.spec_from_file_location("hvo_balance", _BALANCE_PATH)
balance = importlib.util.module_from_spec(_spec)
# Register before exec so dataclass introspection (with `from __future__ import
# annotations`) can resolve the module by name.
sys.modules[_spec.name] = balance
_spec.loader.exec_module(balance)

# --- Load learning.py standalone (pure module, no HA) ----------------------
# Task 32.1 exercises the SAME allocation properties under the learned
# non-linear ``VentCurve`` model. ``learning.py`` is HA-free, so — like
# ``balance.py`` above and the sibling ``test_learning_properties.py`` — we load
# it standalone by absolute path to build random valid curves for the rooms.
_LEARNING_PATH = pathlib.Path(__file__).resolve().parent.parent / "custom_components" / "hvac_vent_optimizer" / "learning.py"
_learning_spec = importlib.util.spec_from_file_location("hvo_learning", _LEARNING_PATH)
learning = importlib.util.module_from_spec(_learning_spec)
sys.modules[_learning_spec.name] = learning
_learning_spec.loader.exec_module(learning)


# ---------------------------------------------------------------------------
# Shared constants / helpers
# ---------------------------------------------------------------------------
# Physically meaningful leakage band: the fraction of full flow that still
# reaches a room with its vent commanded to 0 %. Kept well below 1 so the
# convergence math (which divides by ``1 - leak``) stays well-conditioned.
LEAK_MAX = 0.5

# Efficiency floor kept comfortably above balance._EPS (1e-9) so the bottleneck
# horizon is always finite (no division-by-zero degenerate branch), which lets
# the tests reason about tau* directly.
EFF_MIN = 5e-3
EFF_MAX = 1.0

# Granularities that evenly divide 100 so a precise aperture of 1.0 rounds to
# exactly 100 % (keeps the P3 / P5 bounds tolerance-free where it matters).
DIVISOR_GRANULARITIES = [1, 2, 5, 10, 20, 25, 50]

# Float comparison tolerance (percentage points / °C as appropriate).
TOL = 1e-6

MODES = [balance.MODE_COOLING, balance.MODE_HEATING]


def _signed_error(mode: str, setpoint_c: float, temp_c: float) -> float:
    """Mirror ``balance._signed_error`` (>0 ⇒ still needs conditioning)."""
    if mode == balance.MODE_COOLING:
        return temp_c - setpoint_c
    return setpoint_c - temp_c


def _temp_for_error(mode: str, setpoint_c: float, error_c: float) -> float:
    """Build a temperature with a given signed error toward setpoint."""
    if mode == balance.MODE_COOLING:
        return setpoint_c + error_c
    return setpoint_c - error_c


# ---------------------------------------------------------------------------
# Hypothesis strategies
# ---------------------------------------------------------------------------
_finite = {"allow_nan": False, "allow_infinity": False}


def _temps() -> st.SearchStrategy[float]:
    return st.floats(min_value=5.0, max_value=45.0, **_finite)


def _efficiencies() -> st.SearchStrategy[float]:
    return st.floats(min_value=EFF_MIN, max_value=EFF_MAX, **_finite)


def _leaks() -> st.SearchStrategy[float]:
    return st.floats(min_value=0.0, max_value=LEAK_MAX, **_finite)


def _opens() -> st.SearchStrategy[float]:
    return st.floats(min_value=0.0, max_value=100.0, **_finite)


# --- Learned non-linear curve generation (Task 32.1) -----------------------
# A random valid monotonic saturating ``VentCurve``, mirroring the
# ``monotonic_curves`` composite in ``test_learning_properties.py``. Builds
# ``flows`` by cumulating non-negative increments then normalizing so
# ``flows[-1] == 1`` and clamping the leak into ``[0, LEAK_MAX]`` — exactly the
# valid persisted-curve space. Loaded with trusted counts (``MODEL_MIN_N``) so
# the learned (not seed) shape drives ``flow``/``inverse``/``knee``.
_N_BP = len(learning.CURVE_BREAKPOINTS)


@st.composite
def monotonic_curves(draw, *, strictly: bool = False):
    """A persisted, normalized :class:`VentCurve` with random monotonic shape."""
    lo = 0.02 if strictly else 0.0
    incs = draw(
        st.lists(
            st.floats(min_value=lo, max_value=1.0, **_finite),
            min_size=_N_BP,
            max_size=_N_BP,
        )
    )
    # Force the top breakpoint strictly above the rest so the total is positive
    # and full-open is the unique maximum (a well-formed normalized curve).
    incs[-1] = incs[-1] + 1.0
    raw = list(accumulate(incs))
    total = raw[-1]
    flows = [r / total for r in raw]
    flows[-1] = 1.0
    flows[0] = min(max(flows[0], 0.0), learning.LEAK_MAX)
    return learning.VentCurve.from_dict(
        {
            "breakpoints": list(learning.CURVE_BREAKPOINTS),
            "flow": flows,
            "counts": [learning.MODEL_MIN_N] * _N_BP,
        }
    )


@st.composite
def _room(draw, room_id: str):
    """A single :class:`RoomAllocInput` with a self-consistent signed error.

    ``signed_error_c`` is stored consistently with ``temp_c`` / setpoint / mode
    (set by the caller) so the safety-floor bias-to-need path is exercised with
    realistic values. ``allocate`` ignores the field and re-derives its own
    error, so it is harmless for the allocate-based properties.
    """
    return {
        "room_id": room_id,
        "temp_c": draw(_temps()),
        "active": draw(st.booleans()),
        "efficiency": draw(_efficiencies()),
        "leak": draw(_leaks()),
        "current_open": draw(_opens()),
    }


@st.composite
def scenario(draw, min_rooms: int = 1, max_rooms: int = 6, with_curve: bool = False):
    """Randomized (mode, setpoint, rooms) tuple for allocate/floor properties.

    When ``with_curve`` is set, every room carries a random valid monotonic
    saturating :class:`VentCurve` (Task 32.1) and its scalar ``leak`` is set to
    the curve's own closed-vent flow (``curve.flow(0)``) so the linear-fallback
    fields stay coherent — though the curve-aware allocator reads the curve, not
    the scalar ``leak``, when a curve is present.
    """
    mode = draw(st.sampled_from(MODES))
    setpoint_c = draw(st.floats(min_value=16.0, max_value=30.0, **_finite))
    n = draw(st.integers(min_value=min_rooms, max_value=max_rooms))
    rooms = []
    for i in range(n):
        raw = draw(_room(f"r{i}"))
        err = _signed_error(mode, setpoint_c, raw["temp_c"])
        curve = draw(monotonic_curves()) if with_curve else None
        leak = float(curve.flow(0.0)) if curve is not None else raw["leak"]
        rooms.append(
            balance.RoomAllocInput(
                room_id=raw["room_id"],
                temp_c=raw["temp_c"],
                active=raw["active"],
                efficiency=raw["efficiency"],
                leak=leak,
                current_open=raw["current_open"],
                vent_ids=(f"v{i}",),
                signed_error_c=err,
                curve=curve,
            )
        )
    return mode, setpoint_c, rooms


@st.composite
def alloc_settings(draw, *, with_other_devices: bool = True, granularities=DIVISOR_GRANULARITIES):
    """Randomized :class:`AllocSettings`.

    ``with_other_devices=False`` forces conventional/inactive counts to zero so
    the airflow-safety floor is reachable from the smart vents alone (used by
    the P1 "floor is met" property). ``granularities`` restricts the rounding
    grid — the curve bottleneck property pins this to divisors of the curve
    breakpoints (``{1, 5}``) so a knee at a breakpoint rounds to itself.
    """
    if with_other_devices:
        conventional_vents = draw(st.integers(min_value=0, max_value=5))
        inactive_count = draw(st.integers(min_value=0, max_value=4))
        inactive_open_sum = draw(
            st.floats(min_value=0.0, max_value=float(max(inactive_count, 1) * 100), **_finite)
        )
    else:
        conventional_vents = 0
        inactive_count = 0
        inactive_open_sum = 0.0
    return balance.AllocSettings(
        safety_floor_pct=draw(st.floats(min_value=20.0, max_value=90.0, **_finite)),
        conventional_vents=conventional_vents,
        conventional_open_pct=draw(st.floats(min_value=0.0, max_value=100.0, **_finite)),
        inactive_open_pct_sum=inactive_open_sum,
        inactive_count=inactive_count,
        granularity=draw(st.sampled_from(granularities)),
        crosscoupling=draw(st.booleans()),
        hysteresis_c=draw(st.floats(min_value=0.0, max_value=1.0, **_finite)),
        airflow_limited_margin_pct=draw(st.floats(min_value=0.0, max_value=20.0, **_finite)),
        airflow_limited_error_c=draw(st.floats(min_value=0.1, max_value=3.0, **_finite)),
        horizon_min=draw(st.floats(min_value=1.0, max_value=120.0, **_finite)),
        spread_guardrail_c=draw(st.floats(min_value=0.2, max_value=5.0, **_finite)),
        spread_improvement_deadband_c=draw(st.floats(min_value=0.0, max_value=2.0, **_finite)),
    )


def _tau_star(mode: str, setpoint_c: float, rooms, hysteresis_c: float) -> float:
    """Bottleneck horizon over UNSATISFIED active rooms (mirrors allocate A1.2).

    Curve-aware: a room's fastest achievable rate is at its **knee**, not
    necessarily 100 % open (R25.12), so ``tau_i = err_i / rate_i(knee_i)`` and
    ``tau* = max_i tau_i``. For the linear model (``curve is None``) the knee is
    full open and ``flow(leak, 1.0) = 1``, so this reduces exactly to the old
    ``tau_i = err_i / e_i``. Returns ``0.0`` when no active room is unsatisfied.
    """
    taus = []
    for r in rooms:
        if not r.active or balance.is_satisfied(mode, setpoint_c, r.temp_c, hysteresis_c):
            continue
        rate_knee = balance._room_rate(r, balance._room_knee_frac(r))
        err = _signed_error(mode, setpoint_c, r.temp_c)
        taus.append(err / rate_knee if rate_knee > balance._EPS else float("inf"))
    return max(taus) if taus else 0.0


# ===========================================================================
# Property 1 — Safety floor is inviolable.
# Validates: Requirements 3.1, 3.2, 3.5, 4.4, 25.9
# ===========================================================================
@given(data=scenario(min_rooms=0, max_rooms=6), settings=alloc_settings())
@hyp_settings(deadline=None)
def test_property1_floor_only_raises_and_bounds(data, settings):
    """For ALL inputs (incl. degenerate: empty / all-satisfied / all-inactive)
    the floor choke point only ever raises and never exceeds 100 %."""
    mode, setpoint_c, rooms = data
    result = balance.allocate(rooms, setpoint_c, mode, settings)
    new, binding = balance.apply_safety_floor(result.targets, rooms, settings)

    # Only ever raises the original active-room targets; never exceeds 100 %.
    for room_id, before in result.targets.items():
        assert new[room_id] >= before - TOL, "floor must never lower a target"
        assert new[room_id] <= 100.0 + TOL

    # Any keys the last-resort branch adds (reopened inactive vents) are bounded.
    for value in new.values():
        assert -TOL <= value <= 100.0 + TOL

    # ``binding`` is only reported when something was actually raised.
    if binding:
        raised = any(
            new.get(room_id, 0.0) > before + TOL for room_id, before in result.targets.items()
        ) or any(room_id not in result.targets for room_id in new)
        assert raised


@given(data=scenario(min_rooms=1, max_rooms=6), settings=alloc_settings(with_other_devices=False))
@hyp_settings(deadline=None)
def test_property1_floor_is_met_when_reachable(data, settings):
    """With smart vents the only devices and at least one active room still
    needing conditioning, the combined open % reaches the clamped floor — and
    the combined metric ignores leakage entirely (commanded aperture only)."""
    mode, setpoint_c, rooms = data
    room_by_id = {r.room_id: r for r in rooms}
    floor = balance._clamp_safety_floor(settings.safety_floor_pct)

    result = balance.allocate(rooms, setpoint_c, mode, settings)

    def _expand(commanded: dict[str, float]) -> dict[str, float]:
        out: dict[str, float] = {}
        for room_id, pct in commanded.items():
            room = room_by_id.get(room_id)
            n_vents = len(room.vent_ids) if room and room.vent_ids else 1
            for i in range(n_vents):
                out[f"{room_id}#{i}"] = pct
        return out

    # Reachability ceiling: the floor padder may only raise *not-yet-satisfied*
    # active rooms (``signed_error_c > 0``), exactly mirroring ``_eligible`` in
    # the choke point. Raising a satisfied room would re-overcool it (Property
    # 2). So the highest combined phase 1 can reach is every eligible room at
    # 100 % with the rest left at their allocated target. Only assert the floor
    # is met when it is actually reachable that way.
    ceiling = {
        room_id: (100.0 if (room_by_id[room_id].signed_error_c > 0.0) else pct)
        for room_id, pct in result.targets.items()
    }
    combined_ceiling = balance.combined_open_pct(_expand(ceiling), settings)
    assume(floor <= combined_ceiling + 1e-9)

    new, _binding = balance.apply_safety_floor(result.targets, rooms, settings)
    expanded = _expand(new)

    combined = balance.combined_open_pct(expanded, settings)
    assert combined >= floor - 1e-6, f"combined {combined} below floor {floor}"

    # Never relaxed by leakage: the metric is purely commanded aperture / count.
    manual = sum(expanded.values()) / len(expanded)
    assert combined == manual  # no leak term anywhere in the numerator


# ===========================================================================
# Property 2 — No overcool / overheat bias.
# Validates: Requirements 2.3, 2.4, 4.2, 8.1
# ===========================================================================
@given(data=scenario(min_rooms=1, max_rooms=6), settings=alloc_settings())
@hyp_settings(deadline=None)
def test_property2_no_overcool_bias(data, settings):
    """Satisfied rooms close to 0 % pre-floor; no commanded room finishes before
    the bottleneck horizon tau*."""
    mode, setpoint_c, rooms = data
    result = balance.allocate(rooms, setpoint_c, mode, settings)

    tau_star = _tau_star(mode, setpoint_c, rooms, settings.hysteresis_c)

    for room in rooms:
        if not room.active:
            assert room.room_id not in result.targets  # inactive never allocated
            continue
        satisfied = balance.is_satisfied(mode, setpoint_c, room.temp_c, settings.hysteresis_c)
        if satisfied:
            # Overshoot close (R8 / R4.2): satisfied -> 0 % before the floor.
            assert result.targets[room.room_id] == 0.0
            continue
        # Unsatisfied + actually commanded (aperture > 0): it is throttled to
        # converge at tau*, never earlier (no overcooling, R2.3). Leak-pinned
        # rooms (target 0) cannot be throttled lower, so they are excluded.
        if result.targets[room.room_id] > 0.0:
            finish = result.predicted_finish_min[room.room_id]
            assert finish >= tau_star - 1e-6 * max(1.0, tau_star)


# ===========================================================================
# Property 3 — Bottleneck saturation.
# Validates: Requirements 4.1, 4.3, 5.1
# ===========================================================================
@given(data=scenario(min_rooms=1, max_rooms=6), settings=alloc_settings())
@hyp_settings(deadline=None)
def test_property3_bottleneck_saturation(data, settings):
    """The slowest unsatisfied room is allocated 100 %; nothing is more open."""
    mode, setpoint_c, rooms = data
    unsatisfied = [
        r
        for r in rooms
        if r.active and not balance.is_satisfied(mode, setpoint_c, r.temp_c, settings.hysteresis_c)
    ]
    assume(unsatisfied)

    result = balance.allocate(rooms, setpoint_c, mode, settings)

    tau_star = _tau_star(mode, setpoint_c, rooms, settings.hysteresis_c)
    # Every room achieving tau* (the bottleneck, allowing ties) runs at 100 %.
    # Divisor granularities make the precise aperture 1.0 round to exactly 100.
    bottlenecks = [
        r
        for r in unsatisfied
        if _signed_error(mode, setpoint_c, r.temp_c) / r.efficiency >= tau_star - 1e-9
    ]
    assert bottlenecks
    for room in bottlenecks:
        assert result.targets[room.room_id] >= 100.0 - 1e-6

    # No unsatisfied room is more open than the (fully-open) bottleneck.
    for room in unsatisfied:
        assert result.targets[room.room_id] <= 100.0 + TOL


# ===========================================================================
# Property 4 — Spread monotonicity.
# Validates: Requirements 2.1, 6.1
# ===========================================================================
@given(data=scenario(min_rooms=2, max_rooms=6), settings=alloc_settings())
@hyp_settings(deadline=None)
def test_property4_reducing_satisfied_aperture_never_increases_spread(data, settings):
    """Reducing a satisfied room's aperture never increases predicted spread."""
    mode, setpoint_c, rooms = data
    active = [r for r in rooms if r.active]
    assume(len(active) >= 2)

    # Start from a uniform non-trivial baseline so a reduction is observable.
    baseline = {r.room_id: 80.0 for r in active}
    satisfied = [
        r for r in active if balance.is_satisfied(mode, setpoint_c, r.temp_c, 0.0)
    ]
    assume(satisfied)

    spread_before = balance.predicted_spread(
        active, baseline, mode, setpoint_c, settings.horizon_min
    )
    # Reduce every satisfied room's aperture toward closed.
    reduced = dict(baseline)
    for room in satisfied:
        reduced[room.room_id] = 0.0
    spread_after = balance.predicted_spread(
        active, reduced, mode, setpoint_c, settings.horizon_min
    )
    assert spread_after <= spread_before + 1e-6


@given(
    data=scenario(min_rooms=2, max_rooms=6),
    settings=alloc_settings(),
    delta=st.floats(min_value=1.0, max_value=40.0, **_finite),
)
@hyp_settings(deadline=None)
def test_property4_spread_improving_move_never_increases_spread(data, settings, delta):
    """A strictly spread-improving move (give more conditioning to the room at
    the worst extreme, without pushing it past the opposite extreme) never
    increases the predicted spread."""
    mode, setpoint_c, rooms = data
    active = [r for r in rooms if r.active]
    assume(len(active) >= 2)

    horizon = settings.horizon_min
    baseline = {r.room_id: 40.0 for r in active}

    def _projection(room, aperture_pct: float) -> float:
        aperture = max(0.0, min(1.0, aperture_pct / 100.0))
        rate = balance._room_rate(room, aperture)
        if mode == balance.MODE_COOLING:
            temp = room.temp_c - rate * horizon
            if balance.is_satisfied(mode, setpoint_c, room.temp_c, 0.0):
                temp = max(temp, setpoint_c)
        else:
            temp = room.temp_c + rate * horizon
            if balance.is_satisfied(mode, setpoint_c, room.temp_c, 0.0):
                temp = min(temp, setpoint_c)
        return temp

    projections = {r.room_id: _projection(r, baseline[r.room_id]) for r in active}
    proj_min = min(projections.values())
    proj_max = max(projections.values())
    # The extreme room we will give MORE conditioning to:
    #   cooling -> the hottest projection (drag it down toward the pack)
    #   heating -> the coldest projection (lift it up toward the pack)
    if mode == balance.MODE_COOLING:
        extreme_id = max(projections, key=lambda rid: projections[rid])
    else:
        extreme_id = min(projections, key=lambda rid: projections[rid])
    extreme = next(r for r in active if r.room_id == extreme_id)
    assume(baseline[extreme_id] + delta <= 100.0)

    proposed = dict(baseline)
    proposed[extreme_id] = baseline[extreme_id] + delta
    new_extreme_proj = _projection(extreme, proposed[extreme_id])

    # The move is only "strictly spread-improving" when the extreme room's new
    # projection stays within the current [min, max] band (it does not overshoot
    # the opposite extreme). Otherwise it is a different kind of move; skip it.
    assume(proj_min - 1e-9 <= new_extreme_proj <= proj_max + 1e-9)

    spread_before = balance.predicted_spread(active, baseline, mode, setpoint_c, horizon)
    spread_after = balance.predicted_spread(active, proposed, mode, setpoint_c, horizon)
    assert spread_after <= spread_before + 1e-6


# ===========================================================================
# Property 5 — Allocation monotonic in need.
# Validates: Requirements 4.1, 4.3
# ===========================================================================
@given(
    mode=st.sampled_from(MODES),
    setpoint_c=st.floats(min_value=18.0, max_value=28.0, **_finite),
    leak=_leaks(),
    err_small=st.floats(min_value=0.5, max_value=5.0, **_finite),
    err_extra=st.floats(min_value=0.1, max_value=5.0, **_finite),
    eff_a=_efficiencies(),
    eff_factor=st.floats(min_value=1.01, max_value=8.0, **_finite),
    vary=st.sampled_from(["error", "efficiency"]),
    granularity=st.sampled_from(DIVISOR_GRANULARITIES),
)
@hyp_settings(deadline=None)
def test_property5_monotonic_in_need(
    mode, setpoint_c, leak, err_small, err_extra, eff_a, eff_factor, vary, granularity
):
    """All else equal, the needier room (larger error OR lower efficiency) gets
    an aperture >= the better-off room."""
    settings = balance.AllocSettings(granularity=granularity, crosscoupling=False)

    if vary == "error":
        # Same efficiency; needier room has the larger error.
        eff = eff_a
        err_better = err_small
        err_needier = err_small + err_extra
        eff_needier = eff_better = eff
    else:
        # Same error; needier room has the LOWER efficiency.
        err_better = err_needier = err_small
        eff_better = min(EFF_MAX, eff_a * eff_factor)
        eff_needier = eff_a

    needier = balance.RoomAllocInput(
        room_id="needier",
        temp_c=_temp_for_error(mode, setpoint_c, err_needier),
        active=True,
        efficiency=eff_needier,
        leak=leak,
        current_open=0.0,
        vent_ids=("vn",),
    )
    better = balance.RoomAllocInput(
        room_id="better",
        temp_c=_temp_for_error(mode, setpoint_c, err_better),
        active=True,
        efficiency=eff_better,
        leak=leak,
        current_open=0.0,
        vent_ids=("vb",),
    )
    # Both must be genuinely unsatisfied to compare the throttle path.
    assume(not balance.is_satisfied(mode, setpoint_c, needier.temp_c, settings.hysteresis_c))
    assume(not balance.is_satisfied(mode, setpoint_c, better.temp_c, settings.hysteresis_c))

    result = balance.allocate([needier, better], setpoint_c, mode, settings)
    assert result.targets["needier"] >= result.targets["better"] - 1e-6


# ===========================================================================
# Property 6 — Determinism.
# Validates: Requirements 4.5, 1.5
# ===========================================================================
@given(data=scenario(min_rooms=0, max_rooms=6), settings=alloc_settings())
@hyp_settings(deadline=None)
def test_property6_allocate_is_deterministic(data, settings):
    """``allocate`` is pure: identical inputs yield byte-identical outputs."""
    mode, setpoint_c, rooms = data
    first = balance.allocate(rooms, setpoint_c, mode, settings)
    second = balance.allocate(rooms, setpoint_c, mode, settings)

    assert first.targets == second.targets
    assert first.predicted_finish_min == second.predicted_finish_min
    assert first.predicted_spread_c == second.predicted_spread_c
    assert first.airflow_limited == second.airflow_limited
    assert first.floor_binding == second.floor_binding


# ===========================================================================
# Property 9 — Grouping consistency (allocation level).
# Validates: Requirements 23.1, 23.2, 23.3, 23.5
# ===========================================================================
@given(
    mode=st.sampled_from(MODES),
    setpoint_c=st.floats(min_value=18.0, max_value=28.0, **_finite),
    temp=_temps(),
    efficiency=_efficiencies(),
    leak=_leaks(),
    n_identical=st.integers(min_value=2, max_value=5),
    settings=alloc_settings(),
    extra=scenario(min_rooms=0, max_rooms=3),
)
@hyp_settings(deadline=None)
def test_property9_identical_inputs_identical_outputs(
    mode, setpoint_c, temp, efficiency, leak, n_identical, settings, extra
):
    """Rooms with identical inputs always receive identical commanded targets,
    regardless of any other rooms present."""
    # A clutch of rooms sharing every meaningful attribute (different ids only).
    identical = [
        balance.RoomAllocInput(
            room_id=f"same{i}",
            temp_c=temp,
            active=True,
            efficiency=efficiency,
            leak=leak,
            current_open=0.0,
            vent_ids=(f"sv{i}",),
        )
        for i in range(n_identical)
    ]
    # Plus arbitrary other rooms from a second scenario (re-keyed, ignore mode).
    _m, _sp, others = extra
    others = [
        balance.RoomAllocInput(
            room_id=f"other{i}",
            temp_c=r.temp_c,
            active=r.active,
            efficiency=r.efficiency,
            leak=r.leak,
            current_open=r.current_open,
            vent_ids=(f"ov{i}",),
        )
        for i, r in enumerate(others)
    ]

    result = balance.allocate(identical + others, setpoint_c, mode, settings)
    values = {result.targets[r.room_id] for r in identical}
    assert len(values) == 1, f"identical rooms diverged: {values}"


# ===========================================================================
# Property 10 — Movement-gating soundness.
# Validates: Requirements 7.1, 7.2, 7.3, 7.4
# ===========================================================================
@given(
    data=scenario(min_rooms=0, max_rooms=6),
    settings=alloc_settings(),
    floor_requires_open=st.booleans(),
    jitter=st.dictionaries(
        keys=st.integers(min_value=0, max_value=5),
        values=st.floats(min_value=0.0, max_value=100.0, **_finite),
        max_size=6,
    ),
)
@hyp_settings(deadline=None)
def test_property10_gating_soundness(data, settings, floor_requires_open, jitter):
    """``should_apply`` returns True iff a floor-driven open is required OR the
    predicted spread is above the guardrail AND the predicted improvement meets
    the deadband."""
    mode, setpoint_c, rooms = data
    active = [r for r in rooms if r.active]

    current = {r.room_id: 50.0 for r in active}
    # A proposed allocation that perturbs some apertures (by room index).
    proposed = dict(current)
    for idx, value in jitter.items():
        key = f"r{idx}"
        if key in proposed:
            proposed[key] = value

    gate = balance.GateContext(
        mode=mode, setpoint_c=setpoint_c, floor_requires_open=floor_requires_open
    )
    actual = balance.should_apply(current, proposed, active, settings, gate)

    # Re-derive the contract from the same predicted-spread metric the helper
    # uses (the design defines the gate in terms of predicted_spread).
    if floor_requires_open:
        expected = True
    else:
        current_spread = balance.predicted_spread(
            active, current, mode, setpoint_c, settings.horizon_min
        )
        if current_spread <= settings.spread_guardrail_c:
            expected = False
        else:
            proposed_spread = balance.predicted_spread(
                active, proposed, mode, setpoint_c, settings.horizon_min
            )
            improvement = current_spread - proposed_spread
            expected = improvement >= settings.spread_improvement_deadband_c

    assert actual is expected


# ===========================================================================
# Task 32.1 — Properties 1-6 & 10 re-run against the learned non-linear curve.
#
# Each test below mirrors its linear sibling above but attaches a random valid
# monotonic saturating ``VentCurve`` to every room (``scenario(with_curve=True)``
# / shared-curve construction). The curve-aware ``allocate`` uses
# ``curve.flow``/``curve.knee``/``curve.inverse`` instead of the linear leak
# model, and the bottleneck now saturates at the room's KNEE (possibly < 100 %
# open) rather than at 100 %. These confirm the floor stays inviolable and the
# bottleneck / monotonicity / determinism / gating invariants survive the
# curve model. Validates: Requirements 20.2 (and the underlying 1/3/4/5/7
# properties' requirements, re-checked under R25.12/25.13).
# ===========================================================================


# --- Property 1 (curve) — Safety floor is inviolable. ----------------------
@given(data=scenario(min_rooms=0, max_rooms=6, with_curve=True), settings=alloc_settings())
@hyp_settings(deadline=None)
def test_property1_curve_floor_only_raises_and_bounds(data, settings):
    """With learned curves on every room, the floor choke point still only ever
    raises and never exceeds 100 % (the floor metric is curve-independent —
    commanded aperture only — so leakage/knee can never relax it)."""
    mode, setpoint_c, rooms = data
    result = balance.allocate(rooms, setpoint_c, mode, settings)
    new, binding = balance.apply_safety_floor(result.targets, rooms, settings)

    for room_id, before in result.targets.items():
        assert new[room_id] >= before - TOL, "floor must never lower a target"
        assert new[room_id] <= 100.0 + TOL
    for value in new.values():
        assert -TOL <= value <= 100.0 + TOL

    if binding:
        raised = any(
            new.get(room_id, 0.0) > before + TOL for room_id, before in result.targets.items()
        ) or any(room_id not in result.targets for room_id in new)
        assert raised


@given(
    data=scenario(min_rooms=1, max_rooms=6, with_curve=True),
    settings=alloc_settings(with_other_devices=False),
)
@hyp_settings(deadline=None)
def test_property1_curve_floor_is_met_when_reachable(data, settings):
    """Curve rooms: the combined open % still reaches the clamped floor when
    reachable, and the metric ignores the curve entirely (commanded aperture
    only). The floor padder raises eligible rooms toward 100 % regardless of
    their knee, so reachability is identical to the linear case."""
    mode, setpoint_c, rooms = data
    room_by_id = {r.room_id: r for r in rooms}
    floor = balance._clamp_safety_floor(settings.safety_floor_pct)

    result = balance.allocate(rooms, setpoint_c, mode, settings)

    def _expand(commanded: dict[str, float]) -> dict[str, float]:
        out: dict[str, float] = {}
        for room_id, pct in commanded.items():
            room = room_by_id.get(room_id)
            n_vents = len(room.vent_ids) if room and room.vent_ids else 1
            for i in range(n_vents):
                out[f"{room_id}#{i}"] = pct
        return out

    ceiling = {
        room_id: (100.0 if (room_by_id[room_id].signed_error_c > 0.0) else pct)
        for room_id, pct in result.targets.items()
    }
    combined_ceiling = balance.combined_open_pct(_expand(ceiling), settings)
    assume(floor <= combined_ceiling + 1e-9)

    new, _binding = balance.apply_safety_floor(result.targets, rooms, settings)
    expanded = _expand(new)

    combined = balance.combined_open_pct(expanded, settings)
    assert combined >= floor - 1e-6, f"combined {combined} below floor {floor}"

    manual = sum(expanded.values()) / len(expanded)
    assert combined == manual  # no leak/curve term anywhere in the numerator


# --- Property 2 (curve) — No overcool / overheat bias. ---------------------
@given(data=scenario(min_rooms=1, max_rooms=6, with_curve=True), settings=alloc_settings())
@hyp_settings(deadline=None)
def test_property2_curve_no_overcool_bias(data, settings):
    """Curve rooms: satisfied rooms still close to 0 % pre-floor, and no
    commanded room finishes before the (knee-based) bottleneck horizon tau*."""
    mode, setpoint_c, rooms = data
    result = balance.allocate(rooms, setpoint_c, mode, settings)

    tau_star = _tau_star(mode, setpoint_c, rooms, settings.hysteresis_c)

    for room in rooms:
        if not room.active:
            assert room.room_id not in result.targets
            continue
        satisfied = balance.is_satisfied(mode, setpoint_c, room.temp_c, settings.hysteresis_c)
        if satisfied:
            assert result.targets[room.room_id] == 0.0
            continue
        if result.targets[room.room_id] > 0.0:
            finish = result.predicted_finish_min[room.room_id]
            assert finish >= tau_star - 1e-6 * max(1.0, tau_star)


# --- Property 3 (curve) — Bottleneck saturates at the KNEE. ----------------
@given(
    data=scenario(min_rooms=1, max_rooms=6, with_curve=True),
    settings=alloc_settings(granularities=[1, 5]),
)
@hyp_settings(deadline=None)
def test_property3_curve_bottleneck_saturates_at_knee(data, settings):
    """Under the curve model the slowest unsatisfied room is pinned at its KNEE
    aperture (which may be well below 100 %, R25.12), not at 100 %; and nothing
    is commanded above 100 %.

    The rounding grid is restricted to ``{1, 5}`` (both divide every curve
    breakpoint), so a knee that lands on a breakpoint rounds to itself exactly
    and the assertion is tolerance-free.
    """
    mode, setpoint_c, rooms = data
    unsatisfied = [
        r
        for r in rooms
        if r.active and not balance.is_satisfied(mode, setpoint_c, r.temp_c, settings.hysteresis_c)
    ]
    assume(unsatisfied)

    result = balance.allocate(rooms, setpoint_c, mode, settings)
    tau_star = _tau_star(mode, setpoint_c, rooms, settings.hysteresis_c)

    def _tau(r) -> float:
        rate_knee = balance._room_rate(r, balance._room_knee_frac(r))
        err = _signed_error(mode, setpoint_c, r.temp_c)
        return err / rate_knee if rate_knee > balance._EPS else float("inf")

    bottlenecks = [r for r in unsatisfied if _tau(r) >= tau_star - 1e-9]
    assert bottlenecks
    for room in bottlenecks:
        knee_pct = balance._room_knee_frac(room) * 100.0
        # The bottleneck runs flat-out at its own knee (its fastest rate).
        assert abs(result.targets[room.room_id] - knee_pct) <= TOL

    # No unsatisfied room is commanded beyond 100 %.
    for room in unsatisfied:
        assert result.targets[room.room_id] <= 100.0 + TOL


# --- Property 4 (curve) — Spread monotonicity. -----------------------------
@given(data=scenario(min_rooms=2, max_rooms=6, with_curve=True), settings=alloc_settings())
@hyp_settings(deadline=None)
def test_property4_curve_reducing_satisfied_aperture_never_increases_spread(data, settings):
    """Curve rooms: reducing a satisfied room's aperture never increases the
    (curve-aware) predicted spread."""
    mode, setpoint_c, rooms = data
    active = [r for r in rooms if r.active]
    assume(len(active) >= 2)

    baseline = {r.room_id: 80.0 for r in active}
    satisfied = [r for r in active if balance.is_satisfied(mode, setpoint_c, r.temp_c, 0.0)]
    assume(satisfied)

    spread_before = balance.predicted_spread(active, baseline, mode, setpoint_c, settings.horizon_min)
    reduced = dict(baseline)
    for room in satisfied:
        reduced[room.room_id] = 0.0
    spread_after = balance.predicted_spread(active, reduced, mode, setpoint_c, settings.horizon_min)
    assert spread_after <= spread_before + 1e-6


@given(
    data=scenario(min_rooms=2, max_rooms=6, with_curve=True),
    settings=alloc_settings(),
    delta=st.floats(min_value=1.0, max_value=40.0, **_finite),
)
@hyp_settings(deadline=None)
def test_property4_curve_spread_improving_move_never_increases_spread(data, settings, delta):
    """Curve rooms: a strictly spread-improving move (more conditioning to the
    worst-extreme room, without overshooting the opposite extreme) never
    increases the curve-aware predicted spread."""
    mode, setpoint_c, rooms = data
    active = [r for r in rooms if r.active]
    assume(len(active) >= 2)

    horizon = settings.horizon_min
    baseline = {r.room_id: 40.0 for r in active}

    def _projection(room, aperture_pct: float) -> float:
        # Mirror balance.predicted_spread's per-room projection (curve-aware).
        aperture = max(0.0, min(1.0, aperture_pct / 100.0))
        rate = balance._room_rate(room, aperture)
        if mode == balance.MODE_COOLING:
            temp = room.temp_c - rate * horizon
            if balance.is_satisfied(mode, setpoint_c, room.temp_c, 0.0):
                temp = max(temp, setpoint_c)
        else:
            temp = room.temp_c + rate * horizon
            if balance.is_satisfied(mode, setpoint_c, room.temp_c, 0.0):
                temp = min(temp, setpoint_c)
        return temp

    projections = {r.room_id: _projection(r, baseline[r.room_id]) for r in active}
    proj_min = min(projections.values())
    proj_max = max(projections.values())
    if mode == balance.MODE_COOLING:
        extreme_id = max(projections, key=lambda rid: projections[rid])
    else:
        extreme_id = min(projections, key=lambda rid: projections[rid])
    extreme = next(r for r in active if r.room_id == extreme_id)
    assume(baseline[extreme_id] + delta <= 100.0)

    proposed = dict(baseline)
    proposed[extreme_id] = baseline[extreme_id] + delta
    new_extreme_proj = _projection(extreme, proposed[extreme_id])
    assume(proj_min - 1e-9 <= new_extreme_proj <= proj_max + 1e-9)

    spread_before = balance.predicted_spread(active, baseline, mode, setpoint_c, horizon)
    spread_after = balance.predicted_spread(active, proposed, mode, setpoint_c, horizon)
    assert spread_after <= spread_before + 1e-6


# --- Property 5 (curve) — Allocation monotonic in need. --------------------
@given(
    mode=st.sampled_from(MODES),
    setpoint_c=st.floats(min_value=18.0, max_value=28.0, **_finite),
    curve=monotonic_curves(),
    err_small=st.floats(min_value=0.5, max_value=5.0, **_finite),
    err_extra=st.floats(min_value=0.1, max_value=5.0, **_finite),
    eff_a=_efficiencies(),
    eff_factor=st.floats(min_value=1.01, max_value=8.0, **_finite),
    vary=st.sampled_from(["error", "efficiency"]),
    granularity=st.sampled_from(DIVISOR_GRANULARITIES),
)
@hyp_settings(deadline=None)
def test_property5_curve_monotonic_in_need(
    mode, setpoint_c, curve, err_small, err_extra, eff_a, eff_factor, vary, granularity
):
    """All else equal — including a SHARED learned curve — the needier room
    (larger error OR lower efficiency) gets an aperture >= the better-off room.

    Both rooms carry the *same* curve so the comparison is fair: the needier
    room becomes the bottleneck (pinned at the shared knee) and the better-off
    room is throttled to converge no later, so its aperture cannot exceed the
    needier room's knee (rounding is monotonic, preserving the order)."""
    settings = balance.AllocSettings(granularity=granularity, crosscoupling=False)
    leak = float(curve.flow(0.0))

    if vary == "error":
        err_better = err_small
        err_needier = err_small + err_extra
        eff_needier = eff_better = eff_a
    else:
        err_better = err_needier = err_small
        eff_better = min(EFF_MAX, eff_a * eff_factor)
        eff_needier = eff_a

    needier = balance.RoomAllocInput(
        room_id="needier",
        temp_c=_temp_for_error(mode, setpoint_c, err_needier),
        active=True,
        efficiency=eff_needier,
        leak=leak,
        current_open=0.0,
        vent_ids=("vn",),
        curve=curve,
    )
    better = balance.RoomAllocInput(
        room_id="better",
        temp_c=_temp_for_error(mode, setpoint_c, err_better),
        active=True,
        efficiency=eff_better,
        leak=leak,
        current_open=0.0,
        vent_ids=("vb",),
        curve=curve,
    )
    assume(not balance.is_satisfied(mode, setpoint_c, needier.temp_c, settings.hysteresis_c))
    assume(not balance.is_satisfied(mode, setpoint_c, better.temp_c, settings.hysteresis_c))

    result = balance.allocate([needier, better], setpoint_c, mode, settings)
    assert result.targets["needier"] >= result.targets["better"] - 1e-6


# --- Property 6 (curve) — Determinism. -------------------------------------
@given(data=scenario(min_rooms=0, max_rooms=6, with_curve=True), settings=alloc_settings())
@hyp_settings(deadline=None)
def test_property6_curve_allocate_is_deterministic(data, settings):
    """Curve rooms: ``allocate`` is still pure — identical inputs (same curve
    objects) yield byte-identical outputs."""
    mode, setpoint_c, rooms = data
    first = balance.allocate(rooms, setpoint_c, mode, settings)
    second = balance.allocate(rooms, setpoint_c, mode, settings)

    assert first.targets == second.targets
    assert first.predicted_finish_min == second.predicted_finish_min
    assert first.predicted_spread_c == second.predicted_spread_c
    assert first.airflow_limited == second.airflow_limited
    assert first.floor_binding == second.floor_binding


# --- Property 10 (curve) — Movement-gating soundness. ----------------------
@given(
    data=scenario(min_rooms=0, max_rooms=6, with_curve=True),
    settings=alloc_settings(),
    floor_requires_open=st.booleans(),
    jitter=st.dictionaries(
        keys=st.integers(min_value=0, max_value=5),
        values=st.floats(min_value=0.0, max_value=100.0, **_finite),
        max_size=6,
    ),
)
@hyp_settings(deadline=None)
def test_property10_curve_gating_soundness(data, settings, floor_requires_open, jitter):
    """Curve rooms: ``should_apply`` still returns True iff a floor-driven open
    is required OR the curve-aware predicted spread is above the guardrail AND
    the predicted improvement meets the deadband."""
    mode, setpoint_c, rooms = data
    active = [r for r in rooms if r.active]

    current = {r.room_id: 50.0 for r in active}
    proposed = dict(current)
    for idx, value in jitter.items():
        key = f"r{idx}"
        if key in proposed:
            proposed[key] = value

    gate = balance.GateContext(mode=mode, setpoint_c=setpoint_c, floor_requires_open=floor_requires_open)
    actual = balance.should_apply(current, proposed, active, settings, gate)

    if floor_requires_open:
        expected = True
    else:
        current_spread = balance.predicted_spread(active, current, mode, setpoint_c, settings.horizon_min)
        if current_spread <= settings.spread_guardrail_c:
            expected = False
        else:
            proposed_spread = balance.predicted_spread(
                active, proposed, mode, setpoint_c, settings.horizon_min
            )
            improvement = current_spread - proposed_spread
            expected = improvement >= settings.spread_improvement_deadband_c

    assert actual is expected
