"""Offline closed-loop thermal simulator for the ``balance`` strategy (R15).

This module proves — *before* any code controls the house — that the ``balance``
strategy collapses the active-room temperature spread faster, and with no more
vent movement, than the legacy ``dab`` curve (the R15.6 evidence gate, asserted
in Task 26). It is a **pure, offline, CLI-runnable** tool: it imports **nothing**
from Home Assistant and never touches the Flair API, so it runs anywhere plain
Python runs and is fully deterministic for a fixed scenario seed (R15.4/R15.5).

Closed loop
-----------
Each ``dt`` (default 1 min) the simulator:

1. builds the ambient :class:`~context.Context` for the sim-clock (folding in an
   optional outdoor/weather drift profile, R15.7);
2. recomputes the **selected production strategy** (``balance`` via
   :func:`balance.allocate`, or ``dab`` via the ``dab.py`` curve) — the same code
   the coordinator runs (R15.2);
3. routes the pre-floor targets through the single
   :func:`balance.apply_safety_floor` choke point, so the airflow-safety floor is
   honored in simulation exactly as in production (R15.2);
4. advances every room's temperature by the closed-loop law (R15.1)::

       T_i += sign * e_i(ctx) * flow_i(a_i) * dt - idle_drift_i * dt

   where ``sign`` is ``-1`` for cooling / ``+1`` for heating, ``e_i(ctx)`` is the
   context-adjusted room efficiency, and ``flow_i`` is the **learned non-linear
   saturating** aperture→flow curve (R25.12 — ``flow(0)=leak``, ``flow(100%)=1``,
   with a knee below 100 %);
5. records the active-room spread, per-room error, combined open %, and vent
   movements for the metrics table.

The run **ends when the average of the active-room temperatures reaches the
shared setpoint** (matching the real runtime governance, R15.1) or when the
horizon is hit.

Purity / imports
----------------
The pure sibling modules (``balance``/``learning``/``context``/``dab``) are loaded
by file path relative to this file rather than via a package-relative import,
because the package ``__init__`` pulls in Home Assistant. This keeps the
simulator runnable in a bare Python environment (and under the standalone
path-loading the test-suite uses for every pure module).

CLI
---
``compare(scenario, strategies)`` runs each strategy against the same scenario
via :func:`run` and prints a deterministic side-by-side metrics table (avg/max
spread, time-above-guardrail, total moves, moves/room, avg/max active error,
R15.3); the ``--compare`` entry point drives it from the command line. The
deterministic stepper, scenario model, saturating flow curve, and :func:`run`
are built in Task 25.1.
"""

from __future__ import annotations

import argparse
import importlib.util
import pathlib
import random
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

# ---------------------------------------------------------------------------
# Load the pure sibling modules by path (no Home Assistant — see module docs).
# ---------------------------------------------------------------------------
_HERE = pathlib.Path(__file__).resolve().parent


def _load_sibling(name: str) -> Any:
    """Load a pure sibling module by file path, reusing an existing load.

    Using an explicit path (instead of ``from . import name``) avoids executing
    the package ``__init__`` — which imports Home Assistant — so the simulator
    stays a pure offline tool. Repeated loads are cached under a private module
    name so the cost is paid once per process.
    """
    private = f"_hvo_sim_{name}"
    cached = sys.modules.get(private)
    if cached is not None:
        return cached
    spec = importlib.util.spec_from_file_location(private, _HERE / f"{name}.py")
    if spec is None or spec.loader is None:  # pragma: no cover - defensive
        raise ImportError(f"cannot load sibling module {name!r}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[private] = mod
    spec.loader.exec_module(mod)
    return mod


if TYPE_CHECKING:
    # Static analysis resolves the real (top-level) pure modules for full typing;
    # at runtime they are loaded by path above to stay Home-Assistant-free.
    import balance
    import context
    import dab
    import learning
else:
    balance = _load_sibling("balance")
    learning = _load_sibling("learning")
    context = _load_sibling("context")
    dab = _load_sibling("dab")


# ---------------------------------------------------------------------------
# Saturating aperture→flow curve (R25.12)
# ---------------------------------------------------------------------------
# Fractional flow shape g(a) over the standard learning breakpoints, denser at
# the low end where the response is steepest. g is concave and saturating: most
# of the airflow is delivered by ~50 % aperture, so the knee sits well below
# 100 %. A vent's curve is ``flow(a) = leak + (1 - leak) * g(a)`` with the
# endpoints pinned to ``flow(0)=leak`` and ``flow(100%)=1`` (R25.3).
_SATURATING_SHAPE: tuple[float, ...] = (
    0.0,  # 0 %
    0.25,  # 5 %
    0.42,  # 10 %
    0.66,  # 20 %
    0.86,  # 35 %
    0.98,  # 50 %  ← knee for typical leaks
    0.998,  # 75 %
    1.0,  # 100 %
)


def representative_saturating_curve(leak: float) -> dict[str, list[float]]:
    """Build a representative saturating vent curve for a scenario vent.

    The curve is piecewise-linear over :data:`learning.CURVE_BREAKPOINTS` with a
    concave, saturating flow shape (most airflow delivered by ~50 % aperture),
    so :func:`learning.curve_knee_pct` reports a knee below 100 % (R25.12). The
    endpoints are pinned to ``flow(0)=leak`` (clamped to ``[0, LEAK_MAX]``) and
    ``flow(100%)=1`` exactly. The structure matches the persisted
    ``vent_effectiveness.<vent>.<mode>.curve`` schema so the same data drives the
    simulator and the production learner.
    """
    leak_c = max(0.0, min(learning.LEAK_MAX, leak))
    flows = [round(leak_c + (1.0 - leak_c) * g, 6) for g in _SATURATING_SHAPE]
    flows[0] = leak_c
    flows[-1] = 1.0
    return {
        "breakpoints": list(learning.CURVE_BREAKPOINTS),
        "flow": flows,
        "counts": [0] * len(learning.CURVE_BREAKPOINTS),
    }


def flow_from_curve(curve: dict[str, Any], aperture_pct: float) -> float:
    """Flow fraction at ``aperture_pct`` via piecewise-linear interpolation.

    Interpolates the curve's ``(breakpoints, flow)`` arrays, clamping the
    aperture to ``[0, 100]`` first so out-of-range inputs saturate at the
    endpoints. The result is clamped to ``[0, 1]``. For a near-linear seed this
    reproduces ``leak + (1 - leak) * a``; for the saturating curve it tracks the
    concave shape. Falls back to the linear flow model on a degenerate/empty
    curve so callers always get a usable value.
    """
    breakpoints = [float(bp) for bp in (curve.get("breakpoints") or learning.CURVE_BREAKPOINTS)]
    flows = [float(f) for f in (curve.get("flow") or [])]
    if not flows or len(flows) != len(breakpoints):
        # Degenerate curve: fall back to the linear leak model.
        leak = flows[0] if flows else 0.0
        return learning.flow(leak, aperture_pct / 100.0)

    a = max(0.0, min(100.0, aperture_pct))
    if a <= breakpoints[0]:
        return max(0.0, min(1.0, flows[0]))
    if a >= breakpoints[-1]:
        return max(0.0, min(1.0, flows[-1]))
    for i in range(1, len(breakpoints)):
        lo, hi = breakpoints[i - 1], breakpoints[i]
        if a <= hi:
            span = hi - lo
            frac = 0.0 if span <= 0 else (a - lo) / span
            value = flows[i - 1] + frac * (flows[i] - flows[i - 1])
            return max(0.0, min(1.0, value))
    return max(0.0, min(1.0, flows[-1]))  # pragma: no cover - unreachable


# ---------------------------------------------------------------------------
# Scenario model
# ---------------------------------------------------------------------------
@dataclass
class RoomScenario:
    """One room in a simulator scenario.

    Attributes:
        room_id: stable identifier (key used everywhere downstream).
        temp_c: initial room temperature (°C).
        efficiency: full-effective-flow conditioning rate ``e_room`` (°C/min),
            i.e. the rate at ``flow == 1``. Context multipliers are applied on
            top of this each step.
        leak: closed-vent flow fraction ``flow(0)`` (R25.3); also seeds the
            default flow curve when ``curve`` is not supplied.
        idle_drift: passive drift rate (°C/min). **Positive = drift that lowers
            the temperature**, negative = drift that raises it, applied as
            ``- idle_drift * dt`` so the closed-loop formula matches R15.1
            verbatim. For a cooling scenario, heat ingress is therefore a
            *negative* idle drift (it warms the room); use
            :func:`drift_away_from_setpoint` to express "drift away" intuitively.
        active: whether the room participates in allocation and in the
            active-average termination / spread metrics. Inactive rooms are
            still advanced thermally (they may receive leak/last-resort airflow)
            but never gate the run or inflate the spread.
        vent_ids: physical vent ids serving the room (R23). Defaults to a single
            vent named after the room.
        current_open: initial commanded aperture (used as the movement baseline).
        curve: optional saturating flow curve (``breakpoints``/``flow``). When
            ``None`` a near-linear seed from ``leak`` is built at construction.
        door_open_factor: optional injected *true* door-leakage ratio
            ``rate_open / rate_closed`` for this room (R26.3). A leaky room
            degrades a lot (ratio near/below the lower clamp); a tight interior
            door barely degrades (ratio near 1.0). ``None`` ⇒ the room is not a
            door-learning subject and :func:`learn_door_factors` ignores it.
        door_open_samples: number of door-open observations to fold into the
            room's door-factor cell. Fewer than :data:`learning.DOOR_MIN_N`
            leaves the cell untrusted, so it resolves to the legacy ``0.9``.
    """

    room_id: str
    temp_c: float
    efficiency: float
    leak: float = 0.1
    idle_drift: float = 0.0
    active: bool = True
    vent_ids: tuple[str, ...] = ()
    current_open: float = 0.0
    curve: dict[str, Any] | None = None
    door_open_factor: float | None = None
    door_open_samples: int = 0

    def __post_init__(self) -> None:
        if not self.vent_ids:
            self.vent_ids = (self.room_id,)
        if self.curve is None:
            self.curve = learning.seed_linear_curve(self.leak)


def drift_away_from_setpoint(magnitude: float, mode: str) -> float:
    """Convert an intuitive "drift away from setpoint" magnitude to ``idle_drift``.

    A home in cooling mode passively *warms* (drift away = up); in heating it
    passively *cools* (drift away = down). Because the closed-loop formula
    subtracts ``idle_drift`` (R15.1), drift-away maps to ``-magnitude`` for
    cooling and ``+magnitude`` for heating. ``magnitude`` should be
    non-negative.
    """
    m = abs(magnitude)
    return -m if mode == balance.MODE_COOLING else m


@dataclass
class Scenario:
    """A complete, deterministic simulator scenario.

    Attributes:
        rooms: the rooms (active + inactive).
        setpoint_c: shared setpoint (°C).
        mode: ``"cooling"`` or ``"heating"``.
        settings: allocation + safety-floor tunables (the floor is honored in
            sim, R15.2). Defaults to the production defaults.
        dt_min: step size in minutes (default 1).
        horizon_min: hard stop if the setpoint is never reached.
        seed: RNG seed — runs are deterministic for a fixed seed (R15.5).
        start_hour: local hour at minute 0 (drives the context regime).
        outdoor_profile: optional ``minute -> outdoor_temp_c`` callable feeding
            the context regime each step (R15.7). ``None`` ⇒ a constant mild
            band.
        sun_state: optional explicit sun state for the context day/night split.
        occupied / doors_open: optional context secondary-multiplier inputs.
        sensor_noise_c: std-dev of optional seeded Gaussian measurement noise
            added to the temperature the *strategy* observes (never to the
            underlying physics), so runs stay deterministic per seed.
    """

    rooms: list[RoomScenario]
    setpoint_c: float
    mode: str = balance.MODE_COOLING
    settings: balance.AllocSettings = field(default_factory=balance.AllocSettings)
    dt_min: float = 1.0
    horizon_min: float = 240.0
    seed: int = 0
    start_hour: int = 14
    outdoor_profile: Callable[[float], float | None] | None = None
    sun_state: str | None = None
    occupied: bool | None = None
    doors_open: bool | None = None
    sensor_noise_c: float = 0.0


# ---------------------------------------------------------------------------
# Result model
# ---------------------------------------------------------------------------
@dataclass
class RunResult:
    """Metrics produced by :func:`run` (the basis for the R15.3/15.6 table)."""

    strategy: str
    ended_reason: str  # "setpoint" | "horizon"
    steps: int
    minutes: float
    avg_spread: float
    max_spread: float
    time_above_guardrail_min: float
    total_moves: int
    moves_per_room: dict[str, int]
    avg_active_error: float
    max_active_error: float
    final_temps: dict[str, float]
    spread_history: list[float]
    combined_open_history: list[float]
    min_combined_open_pct: float


# ---------------------------------------------------------------------------
# Context for the sim clock (R15.7)
# ---------------------------------------------------------------------------
def context_at(scenario: Scenario, minute: float) -> context.Context:
    """Build the ambient context for ``minute`` of the run (R12 / R15.7).

    Derives the local hour from ``start_hour`` + elapsed minutes and reads the
    outdoor temperature from the optional ``outdoor_profile`` (mild band when
    absent). Occupancy / door / sun inputs are passed through unchanged.
    """
    hour = (scenario.start_hour + int(minute // 60)) % 24
    outdoor = scenario.outdoor_profile(minute) if scenario.outdoor_profile else None
    return context.build(
        hour=hour,
        outdoor_temp_c=outdoor,
        occupied=scenario.occupied,
        doors_open=scenario.doors_open,
        sun_state=scenario.sun_state,
    )


# Backwards-friendly alias used in some call sites / docs.
context_for = context_at


# ---------------------------------------------------------------------------
# Stepper math (R15.1)
# ---------------------------------------------------------------------------
def advance_temp(
    temp_c: float,
    aperture_pct: float,
    room: RoomScenario,
    ctx: context.Context,
    mode: str,
    dt_min: float,
) -> float:
    """Advance one room one ``dt`` by the closed-loop law (R15.1).

    ``T += sign * e_i(ctx) * flow_i(aperture) * dt - idle_drift * dt`` where
    ``sign`` is ``-1`` for cooling / ``+1`` for heating, ``e_i(ctx)`` is the
    context-adjusted efficiency, and ``flow_i`` is the room's learned saturating
    curve. Pure and deterministic.
    """
    assert room.curve is not None  # set in __post_init__
    flow = flow_from_curve(room.curve, aperture_pct)
    eff = context.apply_context_multipliers(room.efficiency, ctx, mode)
    sign = -1.0 if mode == balance.MODE_COOLING else 1.0
    return temp_c + sign * eff * flow * dt_min - room.idle_drift * dt_min


# ---------------------------------------------------------------------------
# Allocation per strategy (pre-floor) — both route through apply_safety_floor.
# ---------------------------------------------------------------------------
def _build_alloc_inputs(
    scenario: Scenario,
    temps: dict[str, float],
    observed: dict[str, float],
    ctx: context.Context,
) -> list[balance.RoomAllocInput]:
    """Build the per-room :class:`balance.RoomAllocInput` list for this step.

    The efficiency carried into allocation is the **context-adjusted** rate so
    both strategies and the floor see the same physics the stepper applies. The
    ``observed`` temperature (physics + optional measurement noise) is what the
    strategy reasons about; ``signed_error_c`` is derived from it so the floor's
    bias-to-need set is consistent with the strategy's view.
    """
    inputs: list[balance.RoomAllocInput] = []
    for room in scenario.rooms:
        eff = context.apply_context_multipliers(room.efficiency, ctx, scenario.mode)
        t = observed[room.room_id]
        signed = t - scenario.setpoint_c if scenario.mode == balance.MODE_COOLING else scenario.setpoint_c - t
        inputs.append(
            balance.RoomAllocInput(
                room_id=room.room_id,
                temp_c=t,
                active=room.active,
                efficiency=eff,
                leak=room.leak,
                current_open=room.current_open,
                vent_ids=room.vent_ids,
                signed_error_c=signed,
            )
        )
    return inputs


def _pre_floor_balance(
    scenario: Scenario,
    inputs: list[balance.RoomAllocInput],
) -> dict[str, float]:
    """``balance`` pre-floor targets via :func:`balance.allocate`."""
    result = balance.allocate(inputs, scenario.setpoint_c, scenario.mode, scenario.settings)
    return dict(result.targets)


def _pre_floor_dab(
    scenario: Scenario,
    inputs: list[balance.RoomAllocInput],
) -> dict[str, float]:
    """``dab`` pre-floor targets via the legacy ``dab.py`` curve.

    Builds the ``rate_and_temp_per_vent_id`` map the DAB helpers expect (keyed by
    ``room_id`` so the result lines up with the floor's room view), using the
    context-adjusted full-open efficiency as the learned ``rate``. The longest
    minutes-to-target sets the shared horizon, then per-room apertures come from
    the DAB exponential curve. Inactive rooms are closed (``close_inactive`` is
    the production default) and re-padded by the floor only as a last resort.
    """
    rate_and_temp: dict[str, dict[str, float | bool | str]] = {}
    for inp in inputs:
        rate_and_temp[inp.room_id] = {
            "name": inp.room_id,
            "temp": inp.temp_c,
            "rate": inp.efficiency,
            "active": inp.active,
        }
    longest = dab.calculate_longest_minutes_to_target(
        rate_and_temp, scenario.mode, scenario.setpoint_c, dab.DEFAULT_SETTINGS.max_minutes_to_setpoint
    )
    pre = dab.calculate_open_percentage_for_all_vents(
        rate_and_temp, scenario.mode, scenario.setpoint_c, longest, close_inactive=True
    )
    # Keep only active rooms pre-floor (inactive held closed); the floor adds
    # inactive last-resort capacity itself when truly needed.
    return {rid: pct for rid, pct in pre.items() if rate_and_temp[rid]["active"]}


_STRATEGIES: dict[str, Callable[[Scenario, list[balance.RoomAllocInput]], dict[str, float]]] = {
    "balance": _pre_floor_balance,
    "dab": _pre_floor_dab,
}


# ---------------------------------------------------------------------------
# Combined open % over physical vents (for the in-sim floor check / metric).
# ---------------------------------------------------------------------------
def _expand_to_vents(targets: dict[str, float], scenario: Scenario) -> dict[str, float]:
    """Expand room targets to one entry per physical vent (R23 counting)."""
    by_id = {r.room_id: r for r in scenario.rooms}
    expanded: dict[str, float] = {}
    for rid, pct in targets.items():
        room = by_id.get(rid)
        vent_ids = room.vent_ids if room is not None else (rid,)
        for i in range(len(vent_ids)):
            expanded[f"{rid}\x00{i}"] = pct
    return expanded


def _combined_open_pct(targets: dict[str, float], scenario: Scenario) -> float:
    """Per-vent combined open % using the production floor metric."""
    return balance.combined_open_pct(_expand_to_vents(targets, scenario), scenario.settings)


# ---------------------------------------------------------------------------
# Active-average termination governance (R15.1)
# ---------------------------------------------------------------------------
def _active_average_reached(scenario: Scenario, temps: dict[str, float]) -> bool:
    """Has the average of the **active** room temps reached the setpoint?"""
    active = [temps[r.room_id] for r in scenario.rooms if r.active]
    if not active:
        return True
    avg = sum(active) / len(active)
    if scenario.mode == balance.MODE_COOLING:
        return avg <= scenario.setpoint_c
    return avg >= scenario.setpoint_c


def _active_spread(scenario: Scenario, temps: dict[str, float]) -> float:
    """Max-min over active rooms (0 when fewer than two are active)."""
    active = [temps[r.room_id] for r in scenario.rooms if r.active]
    if len(active) < 2:
        return 0.0
    return max(active) - min(active)


def _active_errors(scenario: Scenario, temps: dict[str, float]) -> list[float]:
    """Absolute distance from setpoint for each active room."""
    return [abs(temps[r.room_id] - scenario.setpoint_c) for r in scenario.rooms if r.active]


# ---------------------------------------------------------------------------
# The run loop
# ---------------------------------------------------------------------------
def run(scenario: Scenario, strategy: str = "balance") -> RunResult:
    """Run the closed-loop simulation for ``strategy`` and return its metrics.

    Steps the model forward in ``scenario.dt_min`` increments until the average
    of the active-room temperatures reaches the setpoint (R15.1) or the horizon
    is hit. Each step recomputes the production strategy, routes through
    :func:`balance.apply_safety_floor`, advances the physics, and records the
    spread / error / combined-open / movement metrics. Deterministic for a fixed
    ``scenario.seed`` (R15.5).
    """
    if strategy not in _STRATEGIES:
        raise ValueError(f"unknown strategy {strategy!r}; expected one of {sorted(_STRATEGIES)}")
    pre_floor = _STRATEGIES[strategy]

    rng = random.Random(scenario.seed)
    temps: dict[str, float] = {r.room_id: float(r.temp_c) for r in scenario.rooms}
    guardrail = scenario.settings.spread_guardrail_c

    # Movement baseline: each physical vent starts at its room's current_open.
    prev_vents: dict[str, float] = {}
    for room in scenario.rooms:
        for i in range(len(room.vent_ids)):
            prev_vents[f"{room.room_id}\x00{i}"] = float(room.current_open)
    moves_per_room: dict[str, int] = {r.room_id: 0 for r in scenario.rooms if r.active}

    spread_history: list[float] = []
    combined_history: list[float] = []
    error_means: list[float] = []
    max_spread = 0.0
    max_error = 0.0
    time_above_guardrail = 0.0

    steps = 0
    minutes = 0.0
    ended_reason = "horizon"

    max_steps = round(scenario.horizon_min / scenario.dt_min) if scenario.dt_min > 0 else 0
    while steps < max_steps:
        # End BEFORE stepping if the active average has already converged.
        if _active_average_reached(scenario, temps):
            ended_reason = "setpoint"
            break

        ctx = context_at(scenario, minutes)

        # Strategy observes (optionally noisy) temperatures; physics uses true.
        if scenario.sensor_noise_c > 0.0:
            observed = {rid: t + rng.gauss(0.0, scenario.sensor_noise_c) for rid, t in temps.items()}
        else:
            observed = dict(temps)

        inputs = _build_alloc_inputs(scenario, temps, observed, ctx)
        targets = pre_floor(scenario, inputs)
        floored, _binding = balance.apply_safety_floor(targets, inputs, scenario.settings)

        # --- Metrics for this step (pre-advance state + commanded targets).
        spread = _active_spread(scenario, temps)
        spread_history.append(spread)
        max_spread = max(max_spread, spread)
        if spread > guardrail:
            time_above_guardrail += scenario.dt_min

        errs = _active_errors(scenario, temps)
        if errs:
            error_means.append(sum(errs) / len(errs))
            max_error = max(max_error, max(errs))

        combined = _combined_open_pct(floored, scenario)
        combined_history.append(combined)

        # --- Movement accounting (per physical vent, R23).
        new_vents = _expand_to_vents(floored, scenario)
        by_room = {r.room_id: r for r in scenario.rooms}
        for vent_key, pct in new_vents.items():
            rid = vent_key.split("\x00", 1)[0]
            mv_room = by_room.get(rid)
            if abs(pct - prev_vents.get(vent_key, 0.0)) > 1e-9 and mv_room is not None and mv_room.active:
                moves_per_room[rid] = moves_per_room.get(rid, 0) + 1
            prev_vents[vent_key] = pct

        # --- Advance the physics.
        for room in scenario.rooms:
            a_pct = floored.get(room.room_id, 0.0)
            temps[room.room_id] = advance_temp(
                temps[room.room_id], a_pct, room, ctx, scenario.mode, scenario.dt_min
            )

        steps += 1
        minutes += scenario.dt_min

    else:
        # Loop exhausted the horizon without converging.
        ended_reason = "horizon"

    # Final convergence check (covers the dt that just completed the loop).
    if ended_reason != "setpoint" and _active_average_reached(scenario, temps):
        ended_reason = "setpoint"

    avg_spread = sum(spread_history) / len(spread_history) if spread_history else 0.0
    avg_error = sum(error_means) / len(error_means) if error_means else 0.0
    total_moves = sum(moves_per_room.values())
    min_combined = min(combined_history) if combined_history else 0.0

    return RunResult(
        strategy=strategy,
        ended_reason=ended_reason,
        steps=steps,
        minutes=minutes,
        avg_spread=avg_spread,
        max_spread=max_spread,
        time_above_guardrail_min=time_above_guardrail,
        total_moves=total_moves,
        moves_per_room=moves_per_room,
        avg_active_error=avg_error,
        max_active_error=max_error,
        final_temps=dict(temps),
        spread_history=spread_history,
        combined_open_history=combined_history,
        min_combined_open_pct=min_combined,
    )


# ---------------------------------------------------------------------------
# Per-room door-leakage learning (R26.1 / R26.3 / R27.4)
# ---------------------------------------------------------------------------
def learn_door_factors(scenario: Scenario, *, mode: str | None = None) -> dict[str, float]:
    """Learn and resolve a per-room door-leakage factor for the ``doors_open`` case.

    For every room that injects a ``door_open_factor`` (its *true* ratio
    ``rate_open / rate_closed``), this folds ``door_open_samples`` door-open
    observations into a fresh :class:`learning.DoorFactorModel` via the pure
    :func:`learning.update_door_factor`, then resolves the bounded factor with
    :func:`learning.resolve_door_factor`. The residual ratio is formed exactly as
    the coordinator does it (``sample / reference``) from the room's door-closed
    reference rate (its ``efficiency``) and the injected door-open sample rate, so
    a non-positive reference is skipped (R28.4) and the EMA converges toward the
    clamped injected ratio.

    Rooms with no injected ``door_open_factor`` are not door-learning subjects and
    are omitted from the result (R30.2). A room with fewer than
    :data:`learning.DOOR_MIN_N` samples stays untrusted and resolves to the legacy
    ``0.9`` default (R27.4). Every returned value lies in
    ``[learning.DOOR_FACTOR_MIN, learning.DOOR_FACTOR_MAX]`` (R28.1). Pure and
    deterministic — no RNG, no I/O.
    """
    resolve_mode = mode if mode is not None else scenario.mode
    resolved: dict[str, float] = {}
    for room in scenario.rooms:
        if room.door_open_factor is None:
            continue
        model = learning.new_door_factor_model()
        reference = room.efficiency  # door-closed reference rate (denominator)
        for _ in range(max(0, room.door_open_samples)):
            if reference <= 0.0:
                break  # cannot form a ratio against a non-positive reference
            sample = reference * room.door_open_factor  # injected door-open rate
            learning.update_door_factor(model, sample / reference, resolve_mode)
        resolved[room.room_id] = learning.resolve_door_factor(model, resolve_mode)
    return resolved


# ---------------------------------------------------------------------------
# Side-by-side strategy comparison (R15.3)
# ---------------------------------------------------------------------------
@dataclass
class CompareResult:
    """Result of :func:`compare`: the per-strategy runs plus the rendered table.

    Attributes:
        strategies: the strategy names compared, in the requested column order.
        results: ``strategy -> RunResult`` produced by :func:`run` (the source
            of every reported metric, R15.3).
        table: the deterministic side-by-side metrics table as a single string
            (one row per R15.3 metric, one column per strategy). This is the
            text the ``--compare`` CLI prints and the basis for the R15.6 gate.
    """

    strategies: list[str]
    results: dict[str, RunResult]
    table: str


def _fmt(value: float, places: int = 3) -> str:
    """Format a metric value with a fixed number of decimals (deterministic)."""
    return f"{value:.{places}f}"


def render_comparison_table(results: dict[str, RunResult], strategies: Sequence[str]) -> str:
    """Render a deterministic side-by-side metrics table (R15.3).

    Rows are the R15.3 metrics — average spread, maximum spread,
    time-above-guardrail, total moves, average/max active error — followed by a
    ``moves[<room>]`` row per room (movements per room). Columns are the
    strategies in the order requested. Output is fully determined by the
    ``results`` so two identical scenarios render byte-for-byte identically.
    """
    strat_list = list(strategies)

    # Scalar metric rows: (label, formatted cell per strategy).
    scalar_rows: list[tuple[str, list[str]]] = [
        ("ended", [results[s].ended_reason for s in strat_list]),
        ("minutes", [_fmt(results[s].minutes, 0) for s in strat_list]),
        ("avg_spread", [_fmt(results[s].avg_spread) for s in strat_list]),
        ("max_spread", [_fmt(results[s].max_spread) for s in strat_list]),
        (
            "time_above_guardrail",
            [_fmt(results[s].time_above_guardrail_min, 0) for s in strat_list],
        ),
        ("total_moves", [str(results[s].total_moves) for s in strat_list]),
        ("avg_active_error", [_fmt(results[s].avg_active_error) for s in strat_list]),
        ("max_active_error", [_fmt(results[s].max_active_error) for s in strat_list]),
    ]

    # Per-room movement rows (union of rooms across strategies, sorted stable).
    room_ids = sorted({rid for s in strat_list for rid in results[s].moves_per_room})
    room_rows: list[tuple[str, list[str]]] = [
        (
            f"moves[{rid}]",
            [str(results[s].moves_per_room.get(rid, 0)) for s in strat_list],
        )
        for rid in room_ids
    ]

    rows = scalar_rows + room_rows

    # Column widths: metric label column + one per strategy.
    label_w = max([len("metric")] + [len(label) for label, _ in rows])
    col_w = [max([len(strat)] + [len(row[i]) for _, row in rows]) for i, strat in enumerate(strat_list)]

    def _line(label: str, cells: Sequence[str]) -> str:
        parts = [label.ljust(label_w)]
        parts += [cells[i].rjust(col_w[i]) for i in range(len(strat_list))]
        return "  ".join(parts)

    lines = [_line("metric", strat_list)]
    lines.append("  ".join(["-" * label_w] + ["-" * w for w in col_w]))
    lines += [_line(label, cells) for label, cells in rows]
    return "\n".join(lines)


def compare(
    scenario: Scenario,
    strategies: Sequence[str],
    *,
    to_stdout: bool = True,
) -> CompareResult:
    """Run each strategy against the same scenario and tabulate the metrics (R15.3).

    Runs every strategy in ``strategies`` against the **same** ``scenario`` via
    the existing :func:`run`, then renders a deterministic side-by-side table of
    the R15.3 metrics (avg/max spread, time-above-guardrail, total moves,
    moves/room, avg/max active error). Prints the table to stdout when
    ``to_stdout`` is set (the default, used by the CLI) and always returns a
    :class:`CompareResult` for programmatic use (e.g. the R15.6 evidence gate).

    Raises:
        ValueError: if ``strategies`` is empty, or names an unknown strategy
            (the latter surfaced by :func:`run`).
    """
    strat_list = list(strategies)
    if not strat_list:
        raise ValueError("compare() requires at least one strategy")

    results = {strat: run(scenario, strategy=strat) for strat in strat_list}
    table = render_comparison_table(results, strat_list)
    if to_stdout:
        print(table)
    return CompareResult(strategies=strat_list, results=results, table=table)


# ---------------------------------------------------------------------------
# Built-in demo scenario + minimal CLI
# ---------------------------------------------------------------------------
def default_scenario() -> Scenario:
    """A representative cooling scenario (the documented Bedroom 2-pinned case).

    Seeds each vent with a representative **saturating** curve (R25.12) so the
    closed loop reflects real aperture→airflow saturation rather than a linear
    response. Used by the CLI smoke run and as a convenient fixture.
    """
    leak = 0.1
    rooms = [
        RoomScenario("bedroom_2", 27.5, 0.017, leak, curve=representative_saturating_curve(leak)),
        RoomScenario("bedroom_3", 27.0, 0.020, leak, curve=representative_saturating_curve(leak)),
        RoomScenario("bedroom_1", 26.6, 0.072, leak, curve=representative_saturating_curve(leak)),
        RoomScenario(
            "master",
            26.7,
            0.053,
            leak,
            vent_ids=("master_a", "master_b"),
            curve=representative_saturating_curve(leak),
        ),
        RoomScenario("guest", 26.4, 0.033, leak, curve=representative_saturating_curve(leak)),
        RoomScenario("bathroom", 25.7, 0.438, leak, curve=representative_saturating_curve(leak)),
    ]
    return Scenario(rooms=rooms, setpoint_c=26.1, mode="cooling", horizon_min=600.0, seed=1)


def _format_result(result: RunResult) -> str:
    """One-line human summary of a run (full table is Task 25.2)."""
    return (
        f"strategy={result.strategy} ended={result.ended_reason} "
        f"steps={result.steps} minutes={result.minutes:.0f} "
        f"avg_spread={result.avg_spread:.3f} max_spread={result.max_spread:.3f} "
        f"time_above_guardrail={result.time_above_guardrail_min:.0f} "
        f"moves={result.total_moves} "
        f"avg_err={result.avg_active_error:.3f} max_err={result.max_active_error:.3f} "
        f"min_combined={result.min_combined_open_pct:.1f}"
    )


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="HVAC vent balancing offline simulator")
    parser.add_argument(
        "--strategy",
        default="balance",
        choices=sorted(_STRATEGIES),
        help="control strategy to simulate",
    )
    parser.add_argument(
        "--compare",
        nargs="?",
        const=",".join(sorted(_STRATEGIES)),
        default=None,
        metavar="STRATS",
        help=(
            "run a side-by-side comparison and print the metrics table; "
            "optionally pass a comma-separated strategy list "
            f"(default '{','.join(sorted(_STRATEGIES))}')"
        ),
    )
    args = parser.parse_args(argv)
    if args.compare is not None:
        strategies = [s.strip() for s in args.compare.split(",") if s.strip()]
        compare(default_scenario(), strategies)
        return 0
    result = run(default_scenario(), strategy=args.strategy)
    print(_format_result(result))
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(_main())
