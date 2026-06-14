"""Pure allocation helpers for the ``balance`` control strategy (DAB v2).

This module is intentionally **dependency-free** — it imports nothing from
Home Assistant — so the synchronized-convergence allocation can be unit-tested
in isolation and reused by the offline simulator (which cannot import the HA
runtime). It mirrors the ``dab.py`` pure-module pattern.

Task 8 scope (R8 / R4.2): the *classification* primitive only.

A room is **satisfied** when it has reached or passed the shared setpoint in
the conditioning direction. A satisfied room is allocated **0 %** before the
safety floor is applied (overshoot close), so it stops overcooling/overheating
and stops stealing capacity from laggard rooms.

The full synchronized-convergence math (``allocate``, ``apply_safety_floor``,
``predicted_spread``, the ``RoomAllocInput`` / ``AllocResult`` / ``AllocSettings``
dataclasses, etc.) is added by later tasks (Task 9 onward). This module is
structured so those can extend it cleanly: shared constants and the directional
setpoint helper live here.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    # Imported for typing only. ``balance.py`` stays import-light at runtime so it
    # can be loaded standalone by the pure-module tests/simulator; ``VentCurve`` is
    # consumed purely by duck-typing (``flow`` / ``inverse`` / ``knee``), so the
    # runtime never needs the concrete class. ``learning`` is itself HA-free.
    from .learning import VentCurve

# Module logger. ``balance.py`` stays free of Home Assistant imports — ``logging``
# is standard library, so the CRITICAL safety-floor choke point can record its
# last-resort decisions (R3.9) without coupling to the HA runtime.
_LOGGER = logging.getLogger(__name__)

# Conditioning modes. These string values match dab.py / the coordinator so
# the classification here stays consistent with ``has_room_reached_setpoint``.
MODE_COOLING = "cooling"
MODE_HEATING = "heating"

# Default hysteresis band (°C) applied to the directional setpoint check.
# A room must pass the setpoint by this margin before it counts as satisfied,
# which prevents boundary flapping when a room sits right at the setpoint
# (design A1, step 1 "Classify").
DEFAULT_HYSTERESIS_C: float = 0.3


def has_reached_setpoint(mode: str, setpoint_c: float, temp_c: float, hysteresis_c: float = 0.0) -> bool:
    """Directional setpoint check (Celsius).

    Mirrors ``dab.has_room_reached_setpoint`` semantics exactly so the
    ``balance`` strategy classifies satisfaction identically to the legacy
    code. Re-implemented here (rather than importing ``dab``) to keep
    ``balance.py`` self-contained and free of any cross-module coupling.

    Cooling: satisfied when ``temp_c <= setpoint_c - hysteresis_c``.
    Heating: satisfied when ``temp_c >= setpoint_c + hysteresis_c``.

    Any mode other than ``"cooling"`` is treated as heating, matching the
    ``dab`` convention.
    """
    if mode == MODE_COOLING:
        return temp_c <= setpoint_c - hysteresis_c
    return temp_c >= setpoint_c + hysteresis_c


def is_satisfied(
    mode: str,
    setpoint_c: float,
    temp_c: float,
    hysteresis_c: float = DEFAULT_HYSTERESIS_C,
) -> bool:
    """Return ``True`` when a room has reached/passed setpoint in-direction.

    Applies the default hysteresis band (≈0.3 °C) unless overridden, so a room
    sitting right at the setpoint does not flap in and out of the satisfied
    state on every poll.
    """
    return has_reached_setpoint(mode, setpoint_c, temp_c, hysteresis_c)


@dataclass(frozen=True)
class RoomClassification:
    """Result of classifying a single active room.

    Attributes:
        satisfied: ``True`` if the room has reached/passed setpoint in the
            conditioning direction (within the hysteresis band).
        pre_floor_target_pct: the room's aperture **before** the safety floor
            is applied. ``0.0`` for a satisfied room (overshoot close,
            R8/R4.2). ``None`` for an unsatisfied room — its pre-floor aperture
            is decided by the synchronized-convergence math added in Task 9.
    """

    satisfied: bool
    pre_floor_target_pct: float | None


def classify(
    mode: str,
    setpoint_c: float,
    temp_c: float,
    hysteresis_c: float = DEFAULT_HYSTERESIS_C,
) -> RoomClassification:
    """Classify a room and give its pre-floor aperture if satisfied.

    Pure and deterministic: identical inputs always yield identical output.
    """
    satisfied = is_satisfied(mode, setpoint_c, temp_c, hysteresis_c)
    return RoomClassification(
        satisfied=satisfied,
        pre_floor_target_pct=0.0 if satisfied else None,
    )


# ===========================================================================
# Task 9.2 — Synchronized-convergence allocation (design A1.1-A1.3 + A3).
#
# Pure, deterministic core control law. Given each active room's temperature,
# context-adjusted efficiency and vent leakage, decide the aperture (open %)
# that makes every laggard room converge on the shared setpoint at the *same*
# horizon ``tau*`` (the slowest room's full-open finish time). This is what
# collapses the active-room temperature spread (R1/R2/R4) and stops fast rooms
# from overshooting and ending the cycle early (R2.3).
# ===========================================================================


@dataclass(frozen=True)
class RoomAllocInput:
    """Per-room input to :func:`allocate` (one entry per smart room-group).

    Attributes:
        room_id: stable room/group identifier (the key used in results).
        temp_c: current room temperature in Celsius.
        active: whether the room participates in the objective. Inactive rooms
            are excluded from allocation entirely (held by the coordinator).
        efficiency: context-adjusted room efficiency ``e_i`` (°C/min at full
            effective flow). Expected ``> 0``.
        leak: vent leakage fraction ``∈ [0, 1)`` — the airflow fraction that
            reaches the room at 0 % aperture (``flow_i(0) = leak``, R25.3).
        current_open: current commanded aperture (0..100); informational for
            gating, unused by the pure A1 math.
        vent_ids: group member vent ids (R23). They share this target.
        signed_error_c: signed error toward setpoint in the conditioning
            direction (``> 0`` ⇒ still needs conditioning / not-yet-satisfied;
            ``<= 0`` ⇒ satisfied). Used only by the safety-floor choke point
            (:func:`apply_safety_floor`, A2/R3.4) to bias floor padding toward
            the rooms that most need air. :func:`allocate` ignores this field —
            it derives its own error from ``temp_c`` + ``setpoint_c`` + ``mode``.
            Defaults to ``0.0`` so every existing (Task 9) construction stays
            valid. Placed last so positional construction is unaffected.
        curve: optional learned non-linear aperture→airflow :class:`VentCurve`
            (R25.2/25.12/25.13). When provided it SUPERSEDES the scalar ``leak``:
            the allocator uses ``curve.flow(a)`` for the predicted rate, the
            ``curve.knee()`` for the bottleneck horizon and as the bottleneck's
            commanded aperture, and ``curve.inverse(f)`` to map a required flow
            back to an aperture (clamped to ``[leak, flow(knee)]``). When ``None``
            the allocator falls back to the original linear leak model
            ``flow(a) = leak + (1 - leak) * a`` (knee at 100 %), so every existing
            (Task 9-15) construction keeps its exact behavior. ``flow(0)`` of the
            curve is the curve's own leak; the scalar ``leak`` is still carried so
            the linear fallback and the floor math remain available. Defaults to
            ``None`` and is placed last so positional construction is unaffected.
    """

    room_id: str
    temp_c: float
    active: bool
    efficiency: float
    leak: float
    current_open: float
    vent_ids: tuple[str, ...]
    signed_error_c: float = 0.0
    curve: VentCurve | None = None


@dataclass(frozen=True)
class AllocSettings:
    """Tunables for :func:`allocate` (mirrors the design ``balance.py`` table).

    The safety-floor / conventional / inactive fields are consumed by the
    floor choke point (A2, Task 10); A1 itself only needs ``granularity``,
    ``crosscoupling``, ``hysteresis_c``, the airflow-limited thresholds and the
    prediction ``horizon_min``.
    """

    safety_floor_pct: float = 40.0
    conventional_vents: int = 0
    conventional_open_pct: float = 50.0
    inactive_open_pct_sum: float = 0.0
    inactive_count: int = 0
    granularity: int = 5
    crosscoupling: bool = True
    hysteresis_c: float = 0.3
    airflow_limited_margin_pct: float = 5.0
    airflow_limited_error_c: float = 0.5
    horizon_min: float = 30.0
    # A5 movement-gating tunables (design config table, R7.1/R7.3). Consumed by
    # :func:`should_apply`. ``spread_guardrail_c`` is the target spread the
    # allocator is content with — while predicted spread is at/below it we hold
    # rather than chase further equalization (R7.1/7.2). A proposed move must
    # reduce the predicted spread by at least ``spread_improvement_deadband_c``
    # to be worth the vent travel (R7.3). Defaults match the design table.
    spread_guardrail_c: float = 1.0
    spread_improvement_deadband_c: float = 0.3


@dataclass(frozen=True)
class AllocResult:
    """Output of :func:`allocate`.

    Attributes:
        targets: room_id → commanded open % (rounded to ``granularity``).
            Contains one entry per *active* room only (inactive rooms are
            held by the coordinator and never appear here).
        predicted_finish_min: room_id → predicted minutes to reach setpoint at
            the commanded aperture. ``0.0`` for satisfied / leak-pinned rooms.
        predicted_spread_c: predicted active-room temperature spread at the
            evaluation horizon ``settings.horizon_min`` (drives gating, A5).
        airflow_limited: rooms pinned near full open yet still off-target (A3).
        floor_binding: ``True`` if the safety floor had to raise apertures.
            The floor itself is applied by the choke point in a later task;
            A1 reports ``False`` here (clean seam — see allocate step 4).
    """

    targets: dict[str, float]
    predicted_finish_min: dict[str, float]
    predicted_spread_c: float
    airflow_limited: frozenset[str]
    floor_binding: bool


# Tiny positive guards so degenerate inputs (e_i≈0, leak≈1) cannot raise
# ZeroDivisionError. The design guarantees e_i > 0 and leak < 1; these only
# keep the pure math total and deterministic on pathological inputs.
_EPS = 1e-9


def _flow(leak: float, aperture: float) -> float:
    """Effective airflow fraction at aperture ``a∈[0,1]``: ``leak+(1-leak)*a``."""
    return leak + (1.0 - leak) * aperture


def _rate(efficiency: float, leak: float, aperture: float) -> float:
    """Predicted conditioning rate (°C/min) at aperture ``a`` (R25.4)."""
    return efficiency * _flow(leak, aperture)


# ---------------------------------------------------------------------------
# Curve-aware per-room helpers (R25.2/25.12/25.13). Each transparently handles
# both the learned non-linear ``VentCurve`` (when ``room.curve`` is set) and the
# original linear leak model (``room.curve is None``). Apertures are FRACTIONS in
# ``[0, 1]`` here; the curve's own API is in PERCENT, so conversions happen at the
# boundary. Keeping the curve/linear split in these tiny helpers lets ``allocate``
# / ``predicted_spread`` read identically for both models.
# ---------------------------------------------------------------------------
def _room_leak(room: RoomAllocInput) -> float:
    """Closed-vent flow fraction ``flow_i(0)`` (curve leak or scalar leak)."""
    if room.curve is not None:
        return float(room.curve.flow(0.0))
    return room.leak


def _room_flow(room: RoomAllocInput, aperture_frac: float) -> float:
    """Effective airflow fraction at aperture fraction ``a`` (curve or linear)."""
    if room.curve is not None:
        return float(room.curve.flow(aperture_frac * 100.0))
    return _flow(room.leak, aperture_frac)


def _room_rate(room: RoomAllocInput, aperture_frac: float) -> float:
    """Predicted conditioning rate ``e_i * flow_i(a)`` (°C/min)."""
    return room.efficiency * _room_flow(room, aperture_frac)


def _room_knee_frac(room: RoomAllocInput) -> float:
    """Effective-max ("knee") aperture as a fraction in ``[0, 1]`` (R25.12).

    For the linear model (no curve) the knee is full open (``1.0``); for a
    learned curve it is ``curve.knee() / 100`` — possibly well below 1.0 when the
    vent's airflow plateaus early (e.g. a 50 % knee).
    """
    if room.curve is not None:
        return max(0.0, min(1.0, int(room.curve.knee()) / 100.0))
    return 1.0


def _room_flow_at_knee(room: RoomAllocInput) -> float:
    """Airflow fraction at the room's knee, ``flow_i(knee_i)``."""
    return _room_flow(room, _room_knee_frac(room))


def _room_inverse_frac(room: RoomAllocInput, flow_fraction: float) -> float:
    """Invert the flow curve: flow fraction → aperture fraction in ``[0, 1]``.

    Uses the learned ``curve.inverse`` (plateau-safe, R25.13) when present, else
    the linear inverse ``a = (f - leak) / (1 - leak)``. The result is clamped to
    ``[0, 1]``; callers additionally clamp to the knee.
    """
    if room.curve is not None:
        return max(0.0, min(1.0, float(room.curve.inverse(flow_fraction)) / 100.0))
    denom = 1.0 - room.leak
    if denom <= _EPS:
        return 1.0
    return max(0.0, min(1.0, (flow_fraction - room.leak) / denom))


def _signed_error(mode: str, setpoint_c: float, temp_c: float) -> float:
    """Signed error toward setpoint (``>0`` ⇒ still needs conditioning)."""
    if mode == MODE_COOLING:
        return temp_c - setpoint_c
    return setpoint_c - temp_c


def _round_to_granularity(value: float, granularity: int) -> float:
    """Round ``value`` to the nearest multiple of ``granularity`` in [0, 100]."""
    rounded = value if granularity <= 0 else round(value / granularity) * float(granularity)
    return max(0.0, min(100.0, rounded))


def predicted_spread(
    rooms: list[RoomAllocInput],
    targets: dict[str, float],
    mode: str,
    setpoint_c: float,
    horizon_min: float,
) -> float:
    """Predicted active-room temperature spread at ``horizon_min`` (A1/A5).

    Each active room's temperature is projected forward by its commanded
    rate: ``T_i - rate_i(a_i)*H`` for cooling (``+`` for heating). Satisfied
    rooms are clamped at the setpoint so leak-only drift past the setpoint does
    not inflate the spread. The spread is ``max - min`` over active rooms
    (``0.0`` when fewer than two are active). Pure and deterministic.
    """
    predicted: list[float] = []
    for room in rooms:
        if not room.active:
            continue
        aperture = max(0.0, min(1.0, targets.get(room.room_id, 0.0) / 100.0))
        rate = _room_rate(room, aperture)
        satisfied = is_satisfied(mode, setpoint_c, room.temp_c, 0.0)
        if mode == MODE_COOLING:
            temp = room.temp_c - rate * horizon_min
            if satisfied:
                temp = max(temp, setpoint_c)
        else:
            temp = room.temp_c + rate * horizon_min
            if satisfied:
                temp = min(temp, setpoint_c)
        predicted.append(temp)
    if len(predicted) < 2:
        return 0.0
    return max(predicted) - min(predicted)


# ===========================================================================
# Task 11 — Cross-coupling guard (design A4, R6) + optional duct signals.
#
# A4 is realized *implicitly* by A1.3 (satisfied/efficient rooms are driven low,
# so duct static pressure naturally rises toward the bottleneck). This is the
# **explicit guard** on top: when ≥1 room is airflow-limited (A3) and still
# off-target, every active room that is at or past setpoint is pushed to 0 % to
# redirect airflow toward the laggard. Cross-coupling only ever *closes* rooms;
# the inviolable minimum is owned by the single floor choke point
# (:func:`apply_safety_floor`), which never reopens a satisfied room (its bias
# set is "active AND signed_error_c > 0"), so the floor and the guard compose
# cleanly: the guard concentrates air on the laggard, the floor guarantees the
# combined never drops below the safety minimum (R6.2).
#
# Approach (recorded per the task): implemented as a **separate pure function**
# (testable in isolation, reusable by the simulator) AND called from
# :func:`allocate` so the realized targets already include the guard. ``allocate``
# exposes an optional ``duct`` parameter that is threaded straight through.
# ===========================================================================

# Duct-signal thresholds for the "is conditioned air actually flowing?" check.
# These are deliberately conservative — they only ever *veto* a cross-coupling
# move when a provided signal clearly says no air is moving. Residential supply
# static pressure is typically tens of Pa; a reading at/under this floor means
# the blower is effectively not moving air. Supply air must differ from the
# setpoint by at least this many °C (in the conditioning direction) to count as
# genuinely conditioned air reaching the duct.
DUCT_PRESSURE_MIN_PA: float = 5.0
DUCT_TEMP_DELTA_C: float = 2.0


@dataclass(frozen=True)
class DuctSignals:
    """Optional duct-temperature / duct-pressure signals (R6.3).

    All fields are optional so the **absence** of duct instrumentation degrades
    gracefully — the cross-coupling heuristic still applies on temperature and
    efficiency alone. When a signal IS provided and it indicates conditioned air
    is not actually flowing, the cross-coupling push is treated as pointless and
    vetoed.

    Attributes:
        duct_temp_c: supply-air temperature in the duct (°C), if measured.
        duct_pressure_pa: duct static pressure (Pa), if measured.
    """

    duct_temp_c: float | None = None
    duct_pressure_pa: float | None = None


def _airflow_confirmed(duct: DuctSignals | None, mode: str, setpoint_c: float) -> bool | None:
    """Tri-state "is conditioned air flowing?" from optional duct signals.

    Returns:
        ``None``  — no usable signal (``duct`` is ``None`` or all fields unset);
                    caller should degrade gracefully and apply the heuristic.
        ``True``  — every provided signal indicates conditioned air IS flowing.
        ``False`` — at least one provided signal indicates air is NOT flowing
                    (cooling: duct not cold enough; pressure at/under the floor),
                    so a cross-coupling move would be pointless and is vetoed.
    """
    if duct is None:
        return None
    signals: list[bool] = []
    if duct.duct_pressure_pa is not None:
        signals.append(duct.duct_pressure_pa >= DUCT_PRESSURE_MIN_PA)
    if duct.duct_temp_c is not None:
        if mode == MODE_COOLING:
            signals.append(duct.duct_temp_c <= setpoint_c - DUCT_TEMP_DELTA_C)
        else:
            signals.append(duct.duct_temp_c >= setpoint_c + DUCT_TEMP_DELTA_C)
    if not signals:
        return None
    return all(signals)


def closing_airflow_cost(curve: VentCurve, from_pct: float, to_pct: float) -> float:
    """Relative airflow given up by closing a vent from ``from_pct`` → ``to_pct``.

    Returns ``max(0, flow(from) - flow(to))`` using the learned curve. Because the
    curve is flat above the knee (R25.12/25.13), closing a room from any aperture
    ABOVE its knee down TO the knee costs ≈0 airflow — so cross-coupling can
    collapse a satisfied above-knee room toward its knee essentially for free, and
    only the knee→0 portion actually redirects air to the bottleneck. Pure;
    clamped to ``>= 0`` (re-opening is never a "cost").
    """
    cost = float(curve.flow(from_pct)) - float(curve.flow(to_pct))
    return cost if cost > 0.0 else 0.0


def apply_cross_coupling(
    targets: dict[str, float],
    rooms: list[RoomAllocInput],
    mode: str,
    setpoint_c: float,
    settings: AllocSettings,
    airflow_limited: frozenset[str],
    duct: DuctSignals | None = None,
) -> dict[str, float]:
    """Explicit cross-coupling guard (design A4, R6). Pure; returns a new dict.

    When ``settings.crosscoupling`` is enabled (R6.4) AND at least one room is
    airflow-limited (A3), drive every **active** room that is at or past setpoint
    (``signed_error <= 0``) and is not itself airflow-limited to 0 %, so its air
    is redirected to the laggard (R6.1). Rooms still needing conditioning, the
    airflow-limited rooms themselves, and inactive rooms are left untouched.

    The push never sets anything below 0 % and never lowers the safety floor —
    :func:`apply_safety_floor` (run afterwards) guarantees the combined stays at
    or above the floor and, because it only re-pads not-yet-satisfied rooms,
    never reopens a room this guard just closed (R6.2).

    Optional duct signals (R6.3): when provided and indicating conditioned air
    is not flowing, the push is vetoed (a pointless move) and ``targets`` is
    returned unchanged; their absence degrades gracefully (heuristic applies).

    Curve note (R25.12/25.13): closing a satisfied room that sits ABOVE its knee
    is cheap — the above-knee→knee portion of the close gives up ≈0 airflow (see
    :func:`closing_airflow_cost`), and only the knee→0 portion actually redirects
    air to the laggard. Pushing satisfied rooms fully to 0 % therefore maximizes
    redirected pressure at essentially no extra cost over closing to the knee.
    """
    new = dict(targets)
    if not settings.crosscoupling:
        return new
    if not airflow_limited:
        return new
    # Duct veto: only block when a provided signal clearly says no air is moving.
    if _airflow_confirmed(duct, mode, setpoint_c) is False:
        _LOGGER.debug(
            "Cross-coupling vetoed: duct signals indicate no conditioned airflow "
            "(airflow-limited rooms: %s).",
            sorted(airflow_limited),
        )
        return new
    for room in rooms:
        if not room.active or room.room_id not in new:
            continue
        if room.room_id in airflow_limited:
            continue
        if _signed_error(mode, setpoint_c, room.temp_c) <= 0.0:
            new[room.room_id] = 0.0
    return new


def allocate(
    rooms: list[RoomAllocInput],
    setpoint_c: float,
    mode: str,
    settings: AllocSettings,
    duct: DuctSignals | None = None,
) -> AllocResult:
    """Synchronized-convergence allocation (design A1.1-A1.3, A3).

    Steps:
        1. **Classify** — satisfied rooms (within the hysteresis band) close to
           0 % (overshoot close, R8/R4.2).
        2. **Bottleneck horizon** — for each unsatisfied room the full-open
           finish time ``tau_i = err_i / rate_i(1.0)``; ``tau* = max tau_i``.
           The argmax room runs at 100 %.
        3. **Throttle the rest to finish at tau*** so every room arrives
           together, never earlier (no overcooling, R2.3):
           ``a_i = clamp((err_i/tau*/e_i - leak_i)/(1-leak_i), 0, 1)``.
           ``required_flow ≤ leak`` ⇒ ``a_i = 0`` (finishes on leak alone);
           a room that cannot reach ``tau*`` even at full open pins to 100 %
           and joins the airflow-limited set (A3).
        4. **Safety floor** (A2) — applied by the floor choke point in a later
           task. A1 leaves a clean seam: ``floor_binding=False`` and no padding.
        5. **Round** apertures to ``settings.granularity`` and emit results.

    Between steps 3 and 5 the explicit cross-coupling guard (A4,
    :func:`apply_cross_coupling`) runs: when ≥1 room is airflow-limited and
    cross-coupling is enabled, at/past-setpoint rooms are pushed to 0 % to feed
    the laggard. The optional ``duct`` signals are threaded through to that guard
    and may veto a pointless push when conditioned air is not actually flowing
    (R6.3); their absence degrades gracefully.

    Inactive rooms are excluded from the objective and from ``targets``. Pure
    and deterministic: identical inputs always yield identical output (R19.2).
    """
    active = [room for room in rooms if room.active]

    # --- Step 1: classify (satisfied → closed) and gather unsatisfied rooms.
    targets: dict[str, float] = {}
    finish: dict[str, float] = {}
    # Per-room precise aperture (pre-round) and signed error, kept for A3 +
    # finish-time computation so rounding never perturbs the convergence math.
    aperture_precise: dict[str, float] = {}
    errors: dict[str, float] = {}

    unsatisfied: list[RoomAllocInput] = []
    for room in active:
        err = _signed_error(mode, setpoint_c, room.temp_c)
        errors[room.room_id] = err
        if is_satisfied(mode, setpoint_c, room.temp_c, settings.hysteresis_c):
            targets[room.room_id] = 0.0
            finish[room.room_id] = 0.0
            aperture_precise[room.room_id] = 0.0
        else:
            unsatisfied.append(room)

    # --- Step 2: bottleneck horizon tau* (max finish time at effective-max airflow).
    # The finish time uses ``rate_i(knee_i)`` — airflow barely rises beyond the
    # knee, so the knee (not 100 %) is the room's fastest achievable rate (R25.12).
    tau_star = 0.0
    for room in unsatisfied:
        rate_knee = _room_rate(room, _room_knee_frac(room))
        tau_i = errors[room.room_id] / rate_knee if rate_knee > _EPS else float("inf")
        tau_star = max(tau_star, tau_i)

    # --- Step 3: throttle every unsatisfied room to converge at tau*.
    # ``knee_pct_by_room`` is captured for the A3 airflow-limited test below
    # (a room is limited relative to ITS OWN knee, which may be < 100 %).
    knee_pct_by_room: dict[str, float] = {}
    for room in unsatisfied:
        err = errors[room.room_id]
        knee_frac = _room_knee_frac(room)
        knee_pct_by_room[room.room_id] = knee_frac * 100.0
        leak = _room_leak(room)
        flow_knee = _room_flow_at_knee(room)
        if tau_star <= _EPS or tau_star == float("inf"):
            # Degenerate: no meaningful horizon (e.g. only a zero-efficiency
            # room). Pin to the knee so the laggard gets all the *useful* air.
            aperture = knee_frac
        else:
            required_rate = err / tau_star
            required_flow = required_rate / room.efficiency if room.efficiency > _EPS else float("inf")
            # Invert the learned curve over the feasible flow band (R25.13):
            #   required_flow <= leak     ⇒ leakage alone suffices ⇒ a_i = 0
            #   required_flow >= flow(knee)⇒ at the knee, cannot go faster ⇒ a_i = knee
            clamped_flow = min(max(required_flow, leak), flow_knee)
            aperture = _room_inverse_frac(room, clamped_flow)
            aperture = max(0.0, min(knee_frac, aperture))
        aperture_precise[room.room_id] = aperture
        targets[room.room_id] = _round_to_granularity(aperture * 100.0, settings.granularity)
        # Predicted finish at the *precise* aperture: throttled rooms land at
        # tau*; an airflow-limited room (pinned at its knee) finishes at its own
        # tau_i ≥ tau*. Leak-pinned (aperture 0) rooms report 0.0.
        if aperture > 0.0:
            rate = _room_rate(room, aperture)
            finish[room.room_id] = err / rate if rate > _EPS else float("inf")
        else:
            finish[room.room_id] = 0.0

    # --- Step 4: safety floor (A2) — clean seam, owned by a later task.
    floor_binding = False

    # --- A3: airflow-limited detection (pinned at/above its OWN knee AND off-target).
    # The knee may be well below 100 % (e.g. a vent that plateaus at 50 %), so a
    # room can be airflow-limited at 50 %. For the linear model the knee is 100 %,
    # so this reduces exactly to the previous ``target >= 100 - margin`` test.
    margin = settings.airflow_limited_margin_pct
    error_c = settings.airflow_limited_error_c
    airflow_limited = frozenset(
        room.room_id
        for room in unsatisfied
        if targets[room.room_id] >= knee_pct_by_room[room.room_id] - margin and errors[room.room_id] > error_c
    )

    # --- A4: explicit cross-coupling guard (R6). When ≥1 room is airflow-limited
    # and cross-coupling is enabled, push at/past-setpoint rooms to 0 % to feed
    # the laggard. Satisfied rooms are already 0 % from step 1, so this is a no-op
    # on the worked example; it is the explicit guard the design calls for and
    # the seam through which optional duct signals can veto a pointless move.
    targets = apply_cross_coupling(targets, active, mode, setpoint_c, settings, airflow_limited, duct=duct)

    spread = predicted_spread(active, targets, mode, setpoint_c, settings.horizon_min)

    return AllocResult(
        targets=targets,
        predicted_finish_min=finish,
        predicted_spread_c=spread,
        airflow_limited=airflow_limited,
        floor_binding=floor_binding,
    )


# ===========================================================================
# Task 10.2 — CRITICAL airflow-safety floor (design A2, R3, decision D1).
#
# This is the SINGLE choke point every control strategy routes through. No code
# path may finish below the floor while the HVAC is active (R3.5). The floor is
# enforced on **commanded aperture only** — leakage can never relax it (R25.9).
# Pure logic; ``logging`` (stdlib) is allowed for the last-resort branch (R3.9).
# ===========================================================================

# Configured-floor safe band (R3.8). Values outside [FLOOR_MIN, FLOOR_MAX] (or
# non-finite) are rejected and replaced with the documented default.
FLOOR_MIN_PCT: float = 20.0
FLOOR_MAX_PCT: float = 90.0
FLOOR_DEFAULT_PCT: float = 40.0

# Hard cap on padding iterations — a defensive guard so the choke point can
# never spin forever on pathological floats. Each accepted increment either
# raises a device toward 100 (bounded) or exits, so this is never reached in
# normal operation; it only bounds the worst case.
_MAX_FLOOR_ITERATIONS: int = 10_000


def _clamp_safety_floor(value: float) -> float:
    """Validate/clamp the configured floor to the safe band (R3.8).

    Returns ``value`` when it is a finite number inside ``[FLOOR_MIN_PCT,
    FLOOR_MAX_PCT]``; otherwise rejects the unsafe/invalid configuration and
    falls back to ``FLOOR_DEFAULT_PCT`` (40 %). Keeping the floor in a sane band
    stops a mis-configured value (e.g. 0 %, 100 %, NaN, a string) from either
    starving the air handler or pinning every vent wide open.
    """
    try:
        floor = float(value)
    except (TypeError, ValueError):
        return FLOOR_DEFAULT_PCT
    # NaN compares false to everything; reject it explicitly.
    if floor != floor:
        return FLOOR_DEFAULT_PCT
    if floor < FLOOR_MIN_PCT or floor > FLOOR_MAX_PCT:
        return FLOOR_DEFAULT_PCT
    return floor


def combined_open_pct(targets: dict[str, float], settings: AllocSettings) -> float:
    """Combined open % over every airflow device on the thermostat (A2).

    ::

        combined = ( Σ targets_v
                     + conventional_vents * conventional_open_pct
                     + inactive_open_pct_sum )
                   / ( n_smart + conventional_vents + n_inactive_open )

    Each key in ``targets`` counts as exactly **one** airflow device
    (``n_smart = len(targets)``); :func:`apply_safety_floor` expands multi-vent
    rooms into one entry per physical vent before calling this so every vent is
    counted individually (R23).

    The numerator uses **commanded aperture only** — there is no ``leak`` term,
    so leakage can never inflate the number and relax the floor (R25.9). A
    device commanded to 0 % contributes 0, not its leak fraction.

    Conventional (always-open) vents contribute ``conventional_vents *
    conventional_open_pct`` over ``conventional_vents`` devices (R3.6).

    Inactive vents are counted **only while they are actually held open** (R3.7
    — "currently-open inactive vents"): their summed aperture is
    ``inactive_open_pct_sum`` over ``inactive_count`` devices. When no inactive
    vent is open (``inactive_open_pct_sum == 0``) they are closed dampers that
    pass no air and therefore neither add to the numerator nor the device count.

    Returns ``0.0`` when there are no devices at all (degenerate guard — no
    airflow obligation, and never a ``ZeroDivisionError``).
    """
    n_smart = len(targets)
    conventional_vents = max(0, settings.conventional_vents)
    # R3.7: only inactive vents that are currently open count toward the floor.
    inactive_open_sum = settings.inactive_open_pct_sum
    inactive_devices = settings.inactive_count if inactive_open_sum > 0.0 else 0

    device_count = n_smart + conventional_vents + inactive_devices
    if device_count <= 0:
        return 0.0

    numerator = (
        sum(targets.values()) + conventional_vents * settings.conventional_open_pct + inactive_open_sum
    )
    return numerator / device_count


def apply_safety_floor(
    targets: dict[str, float],
    rooms: list[RoomAllocInput],
    settings: AllocSettings,
) -> tuple[dict[str, float], bool]:
    """Raise apertures until the combined open % meets the floor (A2, CRITICAL).

    The single safety choke point for every strategy. Returns
    ``(new_targets, floor_binding)``:

    * ``new_targets`` — per-room commanded apertures. Normally exactly the
      active-room keys from ``targets``; in the last-resort branch (R3.9) the
      reopened inactive room ids are added.
    * ``floor_binding`` — ``True`` iff any aperture had to be raised.

    Guarantees (R3):

    * **Only ever raises** — ``new[r] >= targets[r]`` for every room.
    * **Never exceeds 100 %** per vent.
    * **Bias to need (R3.4)** — pads the eligible active room with the *largest*
      ``signed_error_c`` (and target ``< 100``) first, one ``granularity``
      increment at a time, recomputing the combined after each step. Satisfied
      active rooms (``signed_error_c <= 0``) are never reopened.
    * **Inactive is last resort (R3.9, D1 > D4)** — inactive vents are reopened
      only when no active not-yet-satisfied capacity remains and the floor is
      still unreachable on the true total-airflow view; that branch logs.

    The combined the floor pads to uses **commanded aperture only**, so a leaky
    vent commanded to 0 % does not relax the floor (R25.9). Pure and
    deterministic apart from the (side-effect-free) last-resort log line.
    """
    floor = _clamp_safety_floor(settings.safety_floor_pct)
    new: dict[str, float] = dict(targets)
    room_by_id: dict[str, RoomAllocInput] = {room.room_id: room for room in rooms}
    # Granularity must be a positive step or padding could never make progress.
    step = settings.granularity if settings.granularity and settings.granularity > 0 else 1
    binding = False

    def _vent_count(room_id: str) -> int:
        room = room_by_id.get(room_id)
        if room is not None and room.vent_ids:
            return len(room.vent_ids)
        return 1

    def _expand(commanded: dict[str, float]) -> dict[str, float]:
        """Expand each room into one entry per physical vent (R23)."""
        expanded: dict[str, float] = {}
        for room_id, pct in commanded.items():
            for i in range(_vent_count(room_id)):
                expanded[f"{room_id}\x00{i}"] = pct
        return expanded

    def _floor_combined(commanded: dict[str, float]) -> float:
        """The floor metric (R3.7): per-vent, currently-open inactive only."""
        return combined_open_pct(_expand(commanded), settings)

    def _eligible() -> list[str]:
        """Active, not-yet-satisfied rooms with room < 100 (R3.4 bias set)."""
        out: list[str] = []
        for room_id in new:
            room = room_by_id.get(room_id)
            if room is not None and room.active and room.signed_error_c > 0.0 and new[room_id] < 100.0:
                out.append(room_id)
        return out

    # --- Phase 1: pad active not-yet-satisfied rooms, biased to largest error.
    iterations = 0
    while _floor_combined(new) < floor and iterations < _MAX_FLOOR_ITERATIONS:
        iterations += 1
        candidates = _eligible()
        if not candidates:
            break
        # Bias to need: largest signed error first (R3.4). ``room_id`` breaks
        # ties deterministically so the pure function stays reproducible (R19.2).
        best = max(candidates, key=lambda rid: (room_by_id[rid].signed_error_c, rid))
        new[best] = min(100.0, new[best] + step)
        binding = True

    # --- Phase 2: last resort (R3.9). No active not-yet-satisfied capacity
    # remains, yet the true total-airflow view (every physical device, including
    # currently-closed inactive dampers) is still below the floor — the air
    # handler would be starved. D1 (floor) > D4 (inactive-hold): reopen inactive
    # vents and LOG the reason.
    inactive_rooms = [room for room in rooms if not room.active]
    total_devices = len(_expand(new)) + max(0, settings.conventional_vents) + max(0, settings.inactive_count)

    def _total_airflow_combined(reopened: dict[str, float]) -> float:
        """Combined over EVERY device; closed inactive dampers drag it down."""
        if total_devices <= 0:
            return 0.0
        reopened_sum = sum(_vent_count(rid) * pct for rid, pct in reopened.items())
        numerator = (
            sum(_expand(new).values())
            + max(0, settings.conventional_vents) * settings.conventional_open_pct
            + settings.inactive_open_pct_sum
            + reopened_sum
        )
        return numerator / total_devices

    if (
        settings.inactive_count > 0
        and inactive_rooms
        and not _eligible()
        and _total_airflow_combined({}) < floor
    ):
        _LOGGER.warning(
            "Airflow safety floor: active capacity exhausted (combined %.1f%% < "
            "floor %.1f%%); reopening inactive vents as a last resort (R3.9, D1>D4).",
            _total_airflow_combined({}),
            floor,
        )
        reopened: dict[str, float] = {}
        guard = 0
        for room in inactive_rooms:
            if _total_airflow_combined(reopened) >= floor:
                break
            while (
                reopened.get(room.room_id, 0.0) < 100.0
                and _total_airflow_combined(reopened) < floor
                and guard < _MAX_FLOOR_ITERATIONS
            ):
                guard += 1
                reopened[room.room_id] = min(100.0, reopened.get(room.room_id, 0.0) + step)
                binding = True
        # Commit the reopened inactive vents into the returned targets.
        for room_id, pct in reopened.items():
            new[room_id] = pct

    return new, binding


# ===========================================================================
# Task 12 — Movement gating helper (design A5, R7).
#
# Pure, side-effect-free decision: "is this newly computed allocation worth
# commanding, or should we hold the current vent positions?" Anti-chatter
# (min interval / min percent / position deadband) and per-cycle/per-window
# batch limits are intentionally NOT here — they are enforced later in the
# coordinator at *group* granularity (R7.4). This helper owns only the two
# spread-based gates (R7.1/7.2 guardrail, R7.3 improvement deadband) plus the
# floor-bypass (R7.5).
#
# The design lists ``should_apply(current, proposed, rooms, settings, gate)``.
# The ``gate`` carries exactly the prediction context the helper needs that is
# NOT already on ``settings`` or derivable from ``rooms``: the conditioning
# ``mode`` and shared ``setpoint_c`` (both required by :func:`predicted_spread`)
# and ``floor_requires_open`` — the coordinator's signal that applying this
# allocation is required to *raise* a vent to the airflow-safety floor, which
# must always be allowed immediately (R7.5/R9). The prediction horizon is read
# from ``settings.horizon_min`` so the gate object stays minimal.
# ===========================================================================


@dataclass(frozen=True)
class GateContext:
    """Context for the movement-gating decision (:func:`should_apply`).

    Attributes:
        mode: conditioning mode (``MODE_COOLING`` / ``MODE_HEATING``) — needed
            to project each room's temperature forward for the spread metric.
        setpoint_c: shared setpoint in Celsius — the convergence target the
            spread is measured around (satisfied rooms are clamped at it).
        floor_requires_open: ``True`` when applying ``proposed`` is required to
            raise one or more vents to meet the airflow-safety floor. Such a
            move is ALWAYS allowed and bypasses both spread gates (R7.5). The
            coordinator sets this from :func:`apply_safety_floor`'s
            ``floor_binding`` (an *opening* move only — the floor never closes).
    """

    mode: str
    setpoint_c: float
    floor_requires_open: bool = False


def should_apply(
    current: dict[str, float],
    proposed: dict[str, float],
    rooms: list[RoomAllocInput],
    settings: AllocSettings,
    gate: GateContext,
) -> bool:
    """Decide whether to command ``proposed`` or hold ``current`` (A5, R7).

    Pure and deterministic. ``current`` / ``proposed`` are targets dicts
    (room_id → open %); ``rooms`` are the active-objective rooms (inactive
    rooms are ignored by :func:`predicted_spread`).

    Returns ``True`` (command the new allocation) iff:

    * ``gate.floor_requires_open`` — a move strictly required to reach the
      airflow-safety floor is always allowed, immediately, bypassing both gates
      (R7.5/R9); OR
    * BOTH of the spread gates pass:

        1. the predicted active-room spread at the **current** positions is
           **above** ``settings.spread_guardrail_c`` — while it is at or below
           the guardrail we hold rather than chase further equalization
           (R7.1/7.2); AND
        2. the predicted spread **improvement** (``current_spread -
           proposed_spread``) is **at least** ``settings.spread_improvement_
           deadband_c`` -- trivial predicted gains are not worth the vent travel
           (R7.3).

    Otherwise returns ``False`` (hold). Anti-chatter and batch limits are
    enforced later in the coordinator, not here.
    """
    # R7.5: floor-driven opens always win — never gated, never deadbanded.
    if gate.floor_requires_open:
        return True

    horizon = settings.horizon_min
    current_spread = predicted_spread(rooms, current, gate.mode, gate.setpoint_c, horizon)

    # R7.1/7.2: while already at/below the guardrail, prefer holding — do not
    # move vents solely to chase further equalization.
    if current_spread <= settings.spread_guardrail_c:
        return False

    # R7.3: require the move to reduce predicted spread by at least the deadband.
    proposed_spread = predicted_spread(rooms, proposed, gate.mode, gate.setpoint_c, horizon)
    improvement = current_spread - proposed_spread
    return improvement >= settings.spread_improvement_deadband_c
