"""Vent-effectiveness / leakage learning math (pure, HA-free).

This module turns a learned linear relationship between vent aperture and a
room's observed temperature-change rate into the two physical quantities the
optimizer reasons about (R25):

* ``e_room`` — the *full-open* effectiveness (the rate the room achieves with
  the vent at 100%), and
* ``leak``  — the fraction of that full-open flow the room still gets with the
  vent fully closed (duct/return leakage and cross-talk from other rooms).

The aperture→rate model is a simple line ``rate ≈ slope * aperture_pct +
intercept`` fit by the caller (the regression itself lives elsewhere). Here we
only interpret its coefficients, so the logic is trivially testable in
isolation. It is deliberately **pure**: it imports nothing from Home Assistant
and operates only on already-resolved primitive floats (mirrors ``dab.py`` /
``context.py``).

Concepts
--------
* :func:`derive_effectiveness` maps ``(slope, intercept, n)`` regression output
  to an :class:`Effectiveness` ``(e_room, leak)`` pair, guarding against
  too-few samples and degenerate/negative fits.
* :func:`flow` is the aperture→flow-fraction curve ``leak + (1-leak)*a``: a
  vent at fraction ``a`` passes the leak floor plus the leak-scaled remainder.
* :func:`predicted_rate` composes the two: ``e_room * flow(leak, pct/100)``.

Clamps (all documented at their call sites below)
-------------------------------------------------
* ``e_room`` is clamped to ``>= 0`` — a negative fitted rate is unphysical.
* ``leak`` is clamped to ``[0, LEAK_MAX]`` and falls back to
  :data:`LEAK_DEFAULT` when there are fewer than :data:`MODEL_MIN_N` samples or
  the fit is degenerate (non-positive full-open rate).
* :func:`flow` clamps both ``leak`` and the aperture fraction to ``[0, 1]`` so
  the result always stays in ``[0, 1]`` and is non-decreasing in aperture.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import NamedTuple

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
# Upper bound for learned leakage. A room that still gets >35% of full-open
# flow while "closed" is almost certainly a bad fit, so we cap it.
LEAK_MAX: float = 0.35

# Leakage used until the model is trustworthy (too few samples) or when the fit
# is degenerate. Sits safely inside the [0, LEAK_MAX] band.
LEAK_DEFAULT: float = 0.1

# Minimum number of regression samples before the fitted leak is trusted.
# Boundary is inclusive: n == MODEL_MIN_N already trusts the regression.
MODEL_MIN_N: int = 8

# Non-uniform aperture breakpoints for the learned vent-effectiveness curve,
# denser at the low end where the response is steepest (R25.2 / schema v2). The
# persistence layer (Task 22) seeds and round-trips a piecewise-linear curve over
# these breakpoints; the saturating learned shape (Task 31's ``VentCurve``) drops
# in behind the same data structure without changing the schema.
CURVE_BREAKPOINTS: tuple[int, ...] = (0, 5, 10, 20, 35, 50, 75, 100)

# A breakpoint counts as the effective-max ("knee") aperture once its flow reaches
# ``(1 - KNEE_EPS)`` of the full-open flow (R25.12). For a near-linear seed there
# is no plateau, so the knee sits at 100%.
KNEE_EPS: float = 0.02

# Adaptive-alpha EMA parameters for the per-breakpoint online curve learner
# (R25.2). Mirrors the room-efficiency EMA (RATE_ALPHA0/RATE_ALPHA_MIN) but is
# kept separate so the airflow-curve smoothing can be tuned independently. The
# step is ``alpha = max(CURVE_ALPHA_MIN, CURVE_ALPHA0 / sqrt(count + 1))``; the
# very first sample for a breakpoint seeds it outright (no blend).
CURVE_ALPHA0: float = 0.5
CURVE_ALPHA_MIN: float = 0.05


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------
def _clamp(value: float, lo: float, hi: float) -> float:
    """Clamp ``value`` into the closed interval ``[lo, hi]``."""
    return max(lo, min(hi, value))


# ---------------------------------------------------------------------------
# Effectiveness result
# ---------------------------------------------------------------------------
class Effectiveness(NamedTuple):
    """Learned vent effectiveness for a room.

    ``e_room`` is the full-open temperature-change rate (clamped ``>= 0``);
    ``leak`` is the closed-vent flow fraction in ``[0, LEAK_MAX]``. Being a
    :class:`~typing.NamedTuple`, instances are also unpackable as
    ``(e_room, leak)``.
    """

    e_room: float
    leak: float


# ---------------------------------------------------------------------------
# Derivation from regression coefficients
# ---------------------------------------------------------------------------
def derive_effectiveness(slope: float, intercept: float, n: int) -> Effectiveness:
    """Derive ``(e_room, leak)`` from an aperture→rate linear fit.

    The model is ``rate ≈ slope * aperture_pct + intercept``, so the full-open
    (100%) rate is ``slope * 100 + intercept`` and the closed (0%) rate is just
    ``intercept``; the leak fraction is their ratio.

    Clamps and fallbacks:

    * ``e_room`` is clamped to ``>= 0`` (a negative fitted rate is unphysical).
    * If ``n < MODEL_MIN_N`` (too few samples to trust) or the full-open rate
      ``denom`` is non-positive (degenerate fit, would divide by <= 0), ``leak``
      falls back to :data:`LEAK_DEFAULT`.
    * Otherwise ``leak = intercept / denom`` clamped to ``[0, LEAK_MAX]``.
    """
    denom = slope * 100.0 + intercept
    e_room = max(0.0, denom)
    untrusted = n < MODEL_MIN_N or denom <= 0.0
    leak = LEAK_DEFAULT if untrusted else _clamp(intercept / denom, 0.0, LEAK_MAX)
    return Effectiveness(e_room=e_room, leak=leak)


# ---------------------------------------------------------------------------
# Aperture -> flow-fraction curve
# ---------------------------------------------------------------------------
def flow(leak: float, aperture_frac: float) -> float:
    """Flow fraction passed by a vent at aperture fraction ``aperture_frac``.

    Returns ``leak + (1 - leak) * a`` where both ``leak`` and ``a`` are clamped
    to ``[0, 1]``. The result is therefore always in ``[0, 1]``, equals ``leak``
    when fully closed (``a == 0``), equals ``1`` when fully open (``a == 1``),
    and is non-decreasing in aperture.
    """
    leak_c = _clamp(leak, 0.0, 1.0)
    a = _clamp(aperture_frac, 0.0, 1.0)
    return leak_c + (1.0 - leak_c) * a


# ---------------------------------------------------------------------------
# Predicted rate
# ---------------------------------------------------------------------------
def predicted_rate(e_room: float, leak: float, aperture_pct: float) -> float:
    """Predict the temperature-change rate at a given aperture percentage.

    Composes the full-open effectiveness with the flow curve:
    ``max(0.0, e_room) * flow(leak, aperture_pct / 100)``. ``e_room`` is clamped
    ``>= 0`` so a degenerate negative effectiveness never yields a negative
    rate, and :func:`flow` clamps the aperture into ``[0, 1]``.
    """
    return max(0.0, e_room) * flow(leak, aperture_pct / 100.0)


# ---------------------------------------------------------------------------
# Vent-effectiveness curve seeding + knee (persistence schema v2; R25.2/25.12)
# ---------------------------------------------------------------------------
def seed_linear_curve(leak: float) -> dict[str, list[float]]:
    """Build a near-linear seed curve over :data:`CURVE_BREAKPOINTS`.

    The seed uses the linear flow model ``flow(a) = leak + (1 - leak) * a`` (see
    :func:`flow`) evaluated at each breakpoint, with ``leak`` clamped to
    ``[0, LEAK_MAX]``. ``flow(0%)`` therefore equals the (clamped) ``leak`` and
    ``flow(100%)`` is normalized to exactly ``1.0`` (R25.3). ``counts`` start at
    zero — no binned samples have been observed yet — so the online learner
    (Task 31) refines the shape from here. The returned dict is the exact
    structure persisted under ``vent_effectiveness.<vent>.<mode>.curve``.
    """
    leak_c = _clamp(leak, 0.0, LEAK_MAX)
    flows = [round(flow(leak_c, bp / 100.0), 6) for bp in CURVE_BREAKPOINTS]
    flows[-1] = 1.0  # normalize flow(100%) == 1 exactly
    return {
        "breakpoints": list(CURVE_BREAKPOINTS),
        "flow": flows,
        "counts": [0] * len(CURVE_BREAKPOINTS),
    }


def curve_knee_pct(curve: dict) -> int:
    """Smallest breakpoint whose flow reaches ``(1 - KNEE_EPS)`` of full-open.

    The "knee" is the effective-max aperture beyond which extra opening yields
    negligible extra airflow (R25.12). Returns the smallest breakpoint whose flow
    is ``>= (1 - KNEE_EPS) * flow[-1]``; for a near-linear seed (no plateau) this
    is the full-open breakpoint (100). Falls back to ``100`` on an empty/degenerate
    curve so callers always get a usable aperture.
    """
    breakpoints = curve.get("breakpoints") or list(CURVE_BREAKPOINTS)
    flows = curve.get("flow") or []
    if not flows:
        return 100
    full = flows[-1] or 1.0
    target = (1.0 - KNEE_EPS) * full
    for bp, f in zip(breakpoints, flows, strict=False):
        if f >= target:
            return int(bp)
    return int(breakpoints[-1])


def seed_vent_effectiveness(
    slope: float | None,
    intercept: float | None,
    n: int,
    *,
    sums: dict | None = None,
) -> dict:
    """Build a schema-v2 vent-effectiveness entry seeded from a regression.

    When ``slope``/``intercept`` are available the leak is derived from the
    aperture→rate fit via :func:`derive_effectiveness` (``leak`` from the
    intercept, clamped to ``[0, LEAK_MAX]`` and falling back to
    :data:`LEAK_DEFAULT` for thin/degenerate fits). With no regression
    (``slope``/``intercept`` is ``None``) the entry seeds a flat
    :data:`LEAK_DEFAULT` curve. The curve itself is the near-linear
    :func:`seed_linear_curve`, and ``knee_pct`` is computed from it. Any provided
    regression ``sums`` (``sum_x``/``sum_y``/``sum_xx``/``sum_xy``) are carried
    forward so online learning can continue from the migrated state.
    """
    if slope is None or intercept is None:
        leak = LEAK_DEFAULT
    else:
        leak = derive_effectiveness(slope, intercept, n).leak
    curve = seed_linear_curve(leak)
    entry: dict = {
        "leak": leak,
        "n": int(n),
        "curve": curve,
        "knee_pct": curve_knee_pct(curve),
    }
    if sums:
        for key in ("sum_x", "sum_y", "sum_xx", "sum_xy"):
            if key in sums:
                entry[key] = float(sums[key])
    return entry


# ---------------------------------------------------------------------------
# Learned non-linear vent curve (R25.2 / 25.3 / 25.12 / 25.13)
# ---------------------------------------------------------------------------
# ``VentCurve`` wraps the persisted ``{"breakpoints","flow","counts"}`` structure
# (schema v2, Task 22) behind the ``flow(a)`` / ``inverse(f)`` / ``knee()``
# interface the allocator and simulator consume, plus an online per-breakpoint
# EMA learner. Apertures are PERCENT in ``[0, 100]`` (same convention as
# ``simulator.flow_from_curve``); ``flow`` values are relative airflow in
# ``[0, 1]`` normalized so ``flow(100%) = 1`` (R25.3).
#
# Invariants maintained after every update:
#   * monotonic non-decreasing in aperture (isotonic clamp, Property 7);
#   * ``flow(100%) == 1`` (renormalized);
#   * ``flow(0%) = leak`` clamped to ``[0, LEAK_MAX]``.
# Below :data:`MODEL_MIN_N` total samples the read methods fall back to the
# regression-seeded near-linear curve so a thin/cold model behaves like the
# linear model it supersedes.


def _interp_curve(breakpoints: Sequence[int], flows: Sequence[float], aperture_pct: float) -> float:
    """Piecewise-linear flow at ``aperture_pct`` (percent), clamped to ``[0, 1]``.

    Mirrors ``simulator.flow_from_curve`` so the production learner and the
    offline simulator interpret the same persisted curve identically. The
    aperture is clamped to the breakpoint span first so out-of-range inputs
    saturate at the endpoints.
    """
    if not flows:
        return 0.0
    lo_bp = float(breakpoints[0])
    hi_bp = float(breakpoints[-1])
    a = _clamp(aperture_pct, lo_bp, hi_bp)
    if a <= lo_bp:
        return _clamp(flows[0], 0.0, 1.0)
    if a >= hi_bp:
        return _clamp(flows[-1], 0.0, 1.0)
    for i in range(1, len(breakpoints)):
        lo = float(breakpoints[i - 1])
        hi = float(breakpoints[i])
        if a <= hi:
            span = hi - lo
            frac = 0.0 if span <= 0 else (a - lo) / span
            value = flows[i - 1] + frac * (flows[i] - flows[i - 1])
            return _clamp(value, 0.0, 1.0)
    return _clamp(flows[-1], 0.0, 1.0)


def _curve_knee(breakpoints: Sequence[int], flows: Sequence[float]) -> int:
    """Smallest breakpoint whose flow reaches ``(1 - KNEE_EPS)`` of full-open."""
    if not flows:
        return 100
    full = flows[-1] or 1.0
    target = (1.0 - KNEE_EPS) * full
    for bp, f in zip(breakpoints, flows, strict=False):
        if f >= target:
            return int(bp)
    return int(breakpoints[-1])


def _curve_inverse(
    breakpoints: Sequence[int],
    flows: Sequence[float],
    flow_fraction: float,
    knee_pct: int,
) -> float:
    """Invert the curve (flow → aperture percent); plateau-safe (R25.13).

    Returns the smallest aperture achieving ``flow_fraction`` on the rising
    region (piecewise-linear inverse). A required flow at/above the knee's flow
    maps to the knee aperture (beyond it airflow barely rises), and a required
    flow at/below the closed-vent leak maps to ``0`` (leakage alone suffices).
    The result is monotonic non-decreasing in ``flow_fraction``.
    """
    if not flows:
        return 0.0
    f = _clamp(flow_fraction, 0.0, 1.0)
    # Plateau safety: at/above the knee's airflow, command the knee (R25.13).
    knee_flow = _interp_curve(breakpoints, flows, float(knee_pct))
    if f >= knee_flow:
        return float(knee_pct)
    # Below the closed-vent leak: leakage already covers it, command 0.
    if f <= flows[0]:
        return float(breakpoints[0])
    for i in range(1, len(breakpoints)):
        lo_f = flows[i - 1]
        hi_f = flows[i]
        if f <= hi_f:
            span = hi_f - lo_f
            if span <= 0:
                return float(breakpoints[i - 1])
            frac = (f - lo_f) / span
            return float(breakpoints[i - 1]) + frac * (float(breakpoints[i]) - float(breakpoints[i - 1]))
    return float(knee_pct)


def _isotonic(values: Sequence[float], weights: Sequence[float]) -> list[float]:
    """Weighted isotonic regression (Pool Adjacent Violators) → non-decreasing.

    Produces the closest monotonic non-decreasing fit to ``values`` under the
    given ``weights`` by pooling adjacent decreasing blocks into their
    weighted mean. Using the per-breakpoint sample counts as weights means a
    well-observed breakpoint resists being dragged by a noisier neighbour.
    """
    blocks: list[tuple[float, float, int]] = []  # (mean, weight, span)
    for v, w in zip(values, weights, strict=True):
        cur_v, cur_w, cur_n = float(v), float(w), 1
        while blocks and blocks[-1][0] > cur_v:
            pv, pw, pn = blocks.pop()
            total_w = pw + cur_w
            cur_v = (pv * pw + cur_v * cur_w) / total_w if total_w > 0 else cur_v
            cur_w = total_w
            cur_n = pn + cur_n
        blocks.append((cur_v, cur_w, cur_n))
    out: list[float] = []
    for mean, _w, span in blocks:
        out.extend([mean] * span)
    return out


@dataclass
class VentCurve:
    """A learned per-vent aperture→airflow curve (R25.2/25.3/25.12/25.13).

    Wraps the persisted schema-v2 structure ``breakpoints`` / ``flows`` /
    ``counts`` and exposes :meth:`flow`, :meth:`inverse`, :meth:`knee`, plus the
    online :meth:`update`. ``flows`` is kept monotonic non-decreasing and
    normalized to ``flows[-1] == 1`` after every update. ``_seed`` holds the
    cold-start near-linear curve used as a fallback while fewer than
    :data:`MODEL_MIN_N` samples have been observed.
    """

    breakpoints: list[int]
    flows: list[float]
    counts: list[int]
    _seed: list[float] = field(default_factory=list)

    def __post_init__(self) -> None:
        # Default the fallback seed to the curve we were constructed with. Once a
        # curve has been persisted its learned flows ARE the authoritative shape,
        # so reloading keeps the same fallback (a no-op when trusted).
        if not self._seed:
            self._seed = list(self.flows)

    # -- construction -------------------------------------------------------
    @classmethod
    def seed_from_regression(cls, slope: float, intercept: float, n: int = 0) -> VentCurve:
        """Cold-start a near-linear curve from an aperture→rate regression.

        The leak (``flow(0)``) is derived via :func:`derive_effectiveness`
        (clamped to ``[0, LEAK_MAX]``, falling back to :data:`LEAK_DEFAULT` for
        thin/degenerate fits) and the rest of the curve is the near-linear
        :func:`seed_linear_curve` shape with ``flow(100%) = 1``. All counts start
        at zero, so the curve reports its seeded shape until learning takes over.
        """
        leak = derive_effectiveness(slope, intercept, n).leak
        seed = seed_linear_curve(leak)
        breakpoints = [int(b) for b in seed["breakpoints"]]
        flows = [float(f) for f in seed["flow"]]
        return cls(breakpoints=breakpoints, flows=flows, counts=[0] * len(breakpoints))

    @classmethod
    def from_dict(cls, data: object) -> VentCurve:
        """Build a :class:`VentCurve` from a persisted curve dict (never raises).

        Tolerates missing/short/garbled fields: a non-dict or length-mismatched
        ``flow`` falls back to a near-linear seed; a missing/short ``counts`` list
        is padded with zeros. The loaded flows are taken verbatim (no
        renormalization) so a round-trip is loss-free.
        """
        if not isinstance(data, dict):
            return cls.seed_from_regression(0.0, 0.0, 0)
        raw_bps = data.get("breakpoints") or CURVE_BREAKPOINTS
        breakpoints = [int(b) for b in raw_bps]
        flows = [float(f) for f in (data.get("flow") or [])]
        if len(flows) != len(breakpoints):
            leak = flows[0] if flows else LEAK_DEFAULT
            seed = seed_linear_curve(leak)
            breakpoints = [int(b) for b in seed["breakpoints"]]
            flows = [float(f) for f in seed["flow"]]
        counts: list[int] = []
        for c in data.get("counts") or []:
            try:
                counts.append(int(c))
            except (TypeError, ValueError):
                counts.append(0)
        while len(counts) < len(breakpoints):
            counts.append(0)
        counts = counts[: len(breakpoints)]
        return cls(breakpoints=breakpoints, flows=flows, counts=counts)

    # -- serialization ------------------------------------------------------
    def to_dict(self) -> dict[str, list]:
        """Serialize to the persisted ``{"breakpoints","flow","counts"}`` dict."""
        return {
            "breakpoints": list(self.breakpoints),
            "flow": list(self.flows),
            "counts": list(self.counts),
        }

    # -- queries ------------------------------------------------------------
    def total_samples(self) -> int:
        """Total observed samples across all breakpoints (``sum(counts)``)."""
        return sum(self.counts)

    def _effective_flows(self) -> list[float]:
        """Learned flows once trusted, else the cold-start seed (R25.2 fallback)."""
        if self.total_samples() < MODEL_MIN_N:
            return self._seed
        return self.flows

    def flow(self, aperture_pct: float) -> float:
        """Relative airflow at ``aperture_pct`` (percent), in ``[0, 1]``."""
        return _interp_curve(self.breakpoints, self._effective_flows(), aperture_pct)

    def knee(self) -> int:
        """Effective-max ("knee") aperture percent (R25.12)."""
        return _curve_knee(self.breakpoints, self._effective_flows())

    def inverse(self, flow_fraction: float) -> float:
        """Aperture percent delivering ``flow_fraction``; plateau-safe (R25.13)."""
        flows = self._effective_flows()
        knee_pct = _curve_knee(self.breakpoints, flows)
        return _curve_inverse(self.breakpoints, flows, flow_fraction, knee_pct)

    # -- online learning ----------------------------------------------------
    def _nearest_index(self, aperture_pct: float) -> int:
        """Index of the breakpoint nearest to ``aperture_pct``."""
        a = _clamp(aperture_pct, 0.0, 100.0)
        best_i = 0
        best_d = abs(a - float(self.breakpoints[0]))
        for i, bp in enumerate(self.breakpoints):
            d = abs(a - float(bp))
            if d < best_d:
                best_d = d
                best_i = i
        return best_i

    def _normalize(self) -> None:
        """Renormalize ``flow(100%) → 1`` and clamp the leak into ``[0, LEAK_MAX]``."""
        last = self.flows[-1]
        if last > 0:
            self.flows = [f / last for f in self.flows]
        # Clamp the closed-vent leak (only ever lowers it, so monotonicity holds).
        self.flows[0] = _clamp(self.flows[0], 0.0, LEAK_MAX)

    def update(self, aperture_pct: float, observed_flow: float) -> VentCurve:
        """Fold one ``observed_flow`` (relative airflow) at ``aperture_pct`` in.

        Robust to noise (R25.6): a non-finite observation is ignored; a negative
        one is clamped to ``0`` and observations above full-open clamp to ``1``.
        The sample is binned to the nearest breakpoint and folded via an
        adaptive-alpha EMA (first sample seeds the breakpoint outright); the whole
        curve is then re-projected onto the monotonic non-decreasing cone
        (weighted isotonic regression) and renormalized so ``flow(100%) = 1``.
        Mutates in place and returns ``self`` for chaining.
        """
        if not math.isfinite(observed_flow):
            return self
        sample = _clamp(observed_flow, 0.0, 1.0)
        idx = self._nearest_index(aperture_pct)
        count = self.counts[idx]
        if count <= 0:
            self.flows[idx] = sample
        else:
            alpha = max(CURVE_ALPHA_MIN, CURVE_ALPHA0 / math.sqrt(count + 1))
            self.flows[idx] = self.flows[idx] + alpha * (sample - self.flows[idx])
        self.counts[idx] = count + 1

        # Re-impose monotonicity (Property 7), weighting by per-breakpoint counts
        # so well-observed points dominate; the +1 prior keeps unseen seed points
        # in play without letting them anchor.
        self.flows = _isotonic(self.flows, [c + 1.0 for c in self.counts])
        self._normalize()
        return self


# ===========================================================================
# Multi-vent room GROUP learning (R23.1 / R25.2)
# ===========================================================================
# A room served by >= 2 smart vents (e.g. Master Bedroom `vent_a` + `vent_b`) is one
# logical unit: the vents share the room temperature and receive identical
# targets (R23.1). Learning is therefore done at the room-GROUP level using the
# COMBINED flow, and a single `e_room` / `leak` is attributed to the group.
#
# Group-flow definition (equal-capacity average)
# ----------------------------------------------
# The room's thermal response is driven by the TOTAL airflow it receives — the
# sum of each vent's airflow. Assuming equal-capacity vents (the natural default,
# consistent with "per-vent leak defaults equal within the group"), the group's
# flow *fraction* is the MEAN of the per-vent flow fractions:
#
#     group_combined_flow(leaks, apertures) = mean_i flow(leak_i, a_i)
#
# This definition is chosen because it:
#   * stays in [0, 1] exactly like the single-vent :func:`flow`, so a shared
#     ``e_room`` keeps its "full-open rate" meaning (all vents fully open =>
#     combined flow == 1.0 => predicted rate == ``e_room``);
#   * reduces EXACTLY to :func:`flow` when the group has a single vent, so the
#     single-vent path is just the N == 1 case (no special-casing downstream);
#   * is non-decreasing in every aperture (a mean of monotone terms), preserving
#     Property 7 for groups;
#   * is order-independent (a mean is commutative) => stable group attribution;
#   * with identical apertures (R23.1) collapses to ``flow(mean_leak, a)`` — the
#     group behaves like one vent with the average leak — which is exactly how
#     :func:`group_predicted_rate` and the allocator consume it.


def group_combined_flow(
    leaks: Sequence[float],
    apertures: Sequence[float],
) -> float:
    """Combined flow fraction for a group of vents (equal-capacity mean).

    ``leaks[i]`` is vent ``i``'s leak fraction and ``apertures[i]`` its aperture
    *fraction* in ``[0, 1]`` (same convention as :func:`flow`). Returns the mean
    of the per-vent :func:`flow` values, which therefore stays in ``[0, 1]``, is
    non-decreasing in every aperture, and equals :func:`flow` exactly for a
    single-vent group. An empty group has no airflow and returns ``0.0``.

    Raises :class:`ValueError` if ``leaks`` and ``apertures`` differ in length —
    that is a caller contract violation (one aperture per vent), not noisy data.
    """
    if len(leaks) != len(apertures):
        raise ValueError("leaks and apertures must have the same length")
    n = len(leaks)
    if n == 0:
        return 0.0
    total = math.fsum(flow(leak, a) for leak, a in zip(leaks, apertures, strict=True))
    return total / n


def group_predicted_rate(
    e_room: float,
    leaks: Sequence[float],
    aperture_pcts: Sequence[float],
) -> float:
    """Predict a multi-vent room's rate from its shared ``e_room`` and group flow.

    Composes the group-attributed full-open effectiveness with the combined flow
    curve: ``max(0.0, e_room) * group_combined_flow(leaks, apertures)`` where the
    ``aperture_pcts`` are percentages in ``[0, 100]`` (same convention as
    :func:`predicted_rate`) converted to fractions before being combined.
    ``e_room`` is clamped ``>= 0`` so a degenerate negative effectiveness never
    yields a negative rate; :func:`flow` clamps each aperture into ``[0, 1]``.
    For a single-vent group this equals :func:`predicted_rate` exactly. Raises
    :class:`ValueError` (via :func:`group_combined_flow`) on a length mismatch.
    """
    apertures = [pct / 100.0 for pct in aperture_pcts]
    return max(0.0, e_room) * group_combined_flow(leaks, apertures)


def resolve_group_leaks(
    group_leak: float,
    vent_leaks: Sequence[float],
    vent_counts: Sequence[int],
) -> list[float]:
    """Per-vent effective leak, defaulting to the shared group leak until trusted.

    Within a multi-vent room the per-vent ``leak`` defaults equal — to the shared
    ``group_leak`` — until a vent has accumulated enough INDEPENDENT samples to
    trust its own learned value. A vent uses its own ``vent_leaks[i]`` iff
    ``vent_counts[i] >= MODEL_MIN_N`` (boundary inclusive, consistent with
    :func:`derive_effectiveness`); otherwise it falls back to ``group_leak``.

    This keeps the group's combined flow stable and identical to a single vent at
    ``group_leak`` while data is thin, then lets individual vents specialize once
    each has earned it. Raises :class:`ValueError` if ``vent_leaks`` and
    ``vent_counts`` differ in length.
    """
    if len(vent_leaks) != len(vent_counts):
        raise ValueError("vent_leaks and vent_counts must have the same length")
    return [leak if n >= MODEL_MIN_N else group_leak for leak, n in zip(vent_leaks, vent_counts, strict=True)]


# ===========================================================================
# Room-efficiency learning (R11 / R25.1 / R25.6)
# ===========================================================================
# A separate, regime-aware learner from the vent-effectiveness math above. It
# tracks how fast a room changes temperature (°C/min) per *regime* (the 4-way
# day/night x mild/hot context bucket) and per *mode* (cooling vs heating),
# with an adaptive-alpha EMA per estimate.
#
# Why a sample-count gate (R11 reachability fix)
# ----------------------------------------------
# The previous design gated regime selection on a normalized softmax weight
# (EFF_REGIME_CONFIDENCE = 0.50). With EFF_REGIME_COUNT = 4 regimes the weights
# sum to 1.0 and essentially never cross 0.50, so the regime branch was
# unreachable and learning collapsed to the baseline. We replace that with a
# plain, reachable sample-count gate: a regime cell is trusted once it has seen
# REGIME_MIN_N samples (and its learned rate is positive).

# Number of context regimes, matching context.py's
# [day-mild, day-hot, night-mild, night-hot] mapping.
EFF_REGIME_COUNT: int = 4

# Samples a regime cell must accumulate before its learned rate is trusted by
# :func:`effective_rate` (the reachable R11 gate). Boundary is inclusive.
REGIME_MIN_N: int = 5

# Learned rates are temperature-change °C/min and are never negative; RATE_MAX
# is a generous upper clamp guarding against absurd fits.
RATE_MIN: float = 0.0
RATE_MAX: float = 2.0

# Adaptive-alpha EMA parameters (mirror the coordinator's EFF_ALPHA0 /
# EFF_ALPHA_MIN). alpha = max(RATE_ALPHA_MIN, RATE_ALPHA0 / sqrt(n)).
RATE_ALPHA0: float = 0.10
RATE_ALPHA_MIN: float = 0.01


# ---------------------------------------------------------------------------
# Mutable model types (align with persistence schema v2)
# ---------------------------------------------------------------------------
@dataclass
class RegimeCell:
    """A single regime's learned rate EMA and its running sample count."""

    rate: float = 0.0
    n: int = 0


@dataclass
class ModeEfficiency:
    """One mode's (cooling or heating) baseline EMA plus per-regime cells.

    ``baseline`` is the regime-agnostic rate EMA (advances on every update);
    ``regimes`` holds ``EFF_REGIME_COUNT`` independent :class:`RegimeCell`s,
    each advancing only when its regime is selected.
    """

    baseline: float | None = None
    n: int = 0
    regimes: list[RegimeCell] = field(default_factory=list)


@dataclass
class RoomEfficiencyModel:
    """A room's two fully independent sub-models (R25.1 dual index)."""

    cooling: ModeEfficiency
    heating: ModeEfficiency


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------
def _new_mode() -> ModeEfficiency:
    """A fresh mode sub-model: no baseline and EFF_REGIME_COUNT zeroed cells."""
    return ModeEfficiency(
        baseline=None,
        n=0,
        regimes=[RegimeCell() for _ in range(EFF_REGIME_COUNT)],
    )


def new_room_model() -> RoomEfficiencyModel:
    """Build a fresh model with distinct (non-aliased) cooling/heating modes."""
    return RoomEfficiencyModel(cooling=_new_mode(), heating=_new_mode())


# ---------------------------------------------------------------------------
# Adaptive-alpha EMA update
# ---------------------------------------------------------------------------
def _ema_step(value: float | None, sample: float, n: int) -> float:
    """One adaptive-alpha EMA step for the ``n``-th sample.

    The first sample (``value is None``) seeds the estimate; later samples move
    it toward ``sample`` by ``alpha = max(RATE_ALPHA_MIN, RATE_ALPHA0/sqrt(n))``.
    """
    if value is None:
        return sample
    alpha = max(RATE_ALPHA_MIN, RATE_ALPHA0 / math.sqrt(n))
    return value + alpha * (sample - value)


def update_room_efficiency(
    model: RoomEfficiencyModel,
    sample: float | None,
    regime_idx: int,
    mode: str = "cooling",
) -> RoomEfficiencyModel:
    """Fold one observed rate ``sample`` into the chosen ``mode`` sub-model.

    Robustness (R25.6): a ``None`` or non-finite (NaN/inf) ``sample`` leaves the
    model untouched; a negative finite sample is clamped to ``0.0``. The
    ``regime_idx`` is clamped into ``[0, EFF_REGIME_COUNT - 1]``. Only the
    selected ``mode`` is updated (R25.1). Both the mode baseline and the
    selected regime cell get their own adaptive-alpha EMA step using their own
    running counts (baseline advances every update; the cell only when chosen).
    The model is mutated in place and also returned for chaining.
    """
    if sample is None or not math.isfinite(sample):
        return model

    s = max(0.0, sample)
    idx = int(_clamp(float(regime_idx), 0.0, float(EFF_REGIME_COUNT - 1)))
    sub: ModeEfficiency = getattr(model, mode)

    # Baseline EMA advances on every update.
    sub.n += 1
    sub.baseline = _ema_step(sub.baseline, s, sub.n)

    # Selected regime cell EMA advances only when its regime is chosen.
    cell = sub.regimes[idx]
    cell.n += 1
    cell.rate = _ema_step(cell.rate if cell.n > 1 else None, s, cell.n)

    return model


# ---------------------------------------------------------------------------
# Effective rate lookup (R11.1 / R11.3 reachable gate)
# ---------------------------------------------------------------------------
def effective_rate(
    model: RoomEfficiencyModel,
    regime_idx: int,
    mode: str = "cooling",
) -> float:
    """Best learned rate for ``regime_idx`` in ``mode``, clamped to the band.

    Returns the regime cell's rate once it is trusted (``cell.n >= REGIME_MIN_N``
    and ``cell.rate > 0``); otherwise falls back to the mode baseline (or 0.0
    when no baseline exists). The result is always clamped to
    ``[RATE_MIN, RATE_MAX]``.
    """
    idx = int(_clamp(float(regime_idx), 0.0, float(EFF_REGIME_COUNT - 1)))
    sub: ModeEfficiency = getattr(model, mode)
    cell = sub.regimes[idx]
    if cell.n >= REGIME_MIN_N and cell.rate > 0.0:
        return _clamp(cell.rate, RATE_MIN, RATE_MAX)
    return _clamp(sub.baseline or 0.0, RATE_MIN, RATE_MAX)


# ===========================================================================
# Persistence (schema v2): RoomEfficiencyModel <-> plain dict (R25.7 / R18.3)
# ===========================================================================
# The room learner above is held as dataclasses in memory; the HA ``Store`` only
# round-trips JSON-able primitives. These pure converters define the exact
# ``room_efficiency.<room>.<mode>`` schema and are the single source of truth for
# the persisted shape, so they are unit-tested directly.


def _mode_to_dict(mode: ModeEfficiency) -> dict:
    """Serialize one :class:`ModeEfficiency` to its persisted dict shape."""
    return {
        "baseline": mode.baseline,
        "n": int(mode.n),
        "regimes": [{"rate": float(c.rate), "n": int(c.n)} for c in mode.regimes],
    }


def _mode_from_dict(data: object) -> ModeEfficiency:
    """Deserialize one mode sub-model, tolerating missing/short regime lists.

    A malformed/partial entry never raises: a non-dict yields a fresh empty mode,
    a missing/short ``regimes`` list is padded to :data:`EFF_REGIME_COUNT` cells,
    and non-numeric values fall back to safe defaults.
    """
    if not isinstance(data, dict):
        return _new_mode()
    baseline = data.get("baseline")
    if baseline is not None and not isinstance(baseline, (int, float)):
        baseline = None
    try:
        n = int(data.get("n", 0) or 0)
    except (TypeError, ValueError):
        n = 0
    cells: list[RegimeCell] = []
    raw_regimes = data.get("regimes")
    if isinstance(raw_regimes, list):
        for raw in raw_regimes[:EFF_REGIME_COUNT]:
            if not isinstance(raw, dict):
                cells.append(RegimeCell())
                continue
            try:
                rate = float(raw.get("rate", 0.0) or 0.0)
            except (TypeError, ValueError):
                rate = 0.0
            try:
                cn = int(raw.get("n", 0) or 0)
            except (TypeError, ValueError):
                cn = 0
            cells.append(RegimeCell(rate=rate, n=cn))
    while len(cells) < EFF_REGIME_COUNT:
        cells.append(RegimeCell())
    return ModeEfficiency(baseline=baseline, n=n, regimes=cells)


def room_model_to_dict(model: RoomEfficiencyModel) -> dict:
    """Serialize a :class:`RoomEfficiencyModel` to its persisted dict shape."""
    return {
        "cooling": _mode_to_dict(model.cooling),
        "heating": _mode_to_dict(model.heating),
    }


def room_model_from_dict(data: object) -> RoomEfficiencyModel:
    """Deserialize a :class:`RoomEfficiencyModel`; never raises on bad input."""
    if not isinstance(data, dict):
        return new_room_model()
    return RoomEfficiencyModel(
        cooling=_mode_from_dict(data.get("cooling")),
        heating=_mode_from_dict(data.get("heating")),
    )


def seed_room_model_from_v1(
    baselines: dict[str, float | None],
    offsets_by_mode: dict[str, Sequence[float]] | None = None,
) -> RoomEfficiencyModel:
    """Seed a room model from v1 per-mode baseline rates (+ optional offsets).

    ``baselines`` maps ``"cooling"``/``"heating"`` to the existing learned rate
    (the v1 single-rate / regime baseline). When ``offsets_by_mode`` provides the
    legacy per-regime offsets, each seeded regime cell starts at
    ``max(0, baseline + offset)`` with ``n = 0`` (untrusted until re-learned, so
    :func:`effective_rate` keeps using the baseline). This preserves the existing
    learned signal without data loss (R25.7) while the richer model warms up.
    """
    offsets_by_mode = offsets_by_mode or {}
    model = new_room_model()
    for mode in ("cooling", "heating"):
        baseline = baselines.get(mode)
        if baseline is None:
            continue
        sub: ModeEfficiency = getattr(model, mode)
        sub.baseline = float(baseline)
        offsets = offsets_by_mode.get(mode)
        if isinstance(offsets, Sequence) and not isinstance(offsets, (str, bytes)):
            for i, off in enumerate(list(offsets)[:EFF_REGIME_COUNT]):
                try:
                    sub.regimes[i].rate = max(0.0, float(baseline) + float(off))
                except (TypeError, ValueError):
                    continue
    return model


# ===========================================================================
# Door-leakage residual learning (R26 / R27 / R28; decisions D10-D12)
# ===========================================================================
# A learned, per-room, per-mode multiplicative residual on the room's
# *door-closed* reference rate, replacing the flat ``context.DOOR_FACTOR = 0.9``
# magic number. The quantity learned is the ratio
# ``factor = rate_door_open / rate_door_closed`` (R26.1/D10): a leaky room shows a
# large rate degradation (factor near the lower clamp) and a tight interior door
# shows almost none (factor near 1.0). It mirrors the room-efficiency learner
# above (adaptive-alpha EMA, sample-count confidence gate, graceful fallback) and
# stays pure so it is unit-testable in isolation (R26.2).
#
# Bounds (R28.1): the factor is clamped to ``[DOOR_FACTOR_MIN, DOOR_FACTOR_MAX]``
# so an open door can only slow (never speed) conditioning. Until a mode's cell
# is trusted the resolution falls back to ``DOOR_FACTOR_DEFAULT`` (== the legacy
# constant), so a cold install is behavior-identical to today (R27.4).

# Lower clamp; an open door can only slow conditioning, never speed it.
DOOR_FACTOR_MIN: float = 0.5

# Upper clamp; an open door never speeds conditioning (non-amplifying, R28.1).
DOOR_FACTOR_MAX: float = 1.0

# Legacy constant; cold-start fallback (== the old ``context.DOOR_FACTOR``).
# Sits inside ``[DOOR_FACTOR_MIN, DOOR_FACTOR_MAX]``.
DOOR_FACTOR_DEFAULT: float = 0.9

# Door-open samples a mode's cell must accumulate before its learned factor is
# trusted over the fallback. Mirrors :data:`REGIME_MIN_N`; boundary inclusive.
DOOR_MIN_N: int = 5


# ---------------------------------------------------------------------------
# Mutable model types (align with persistence schema; R27.1 per-mode)
# ---------------------------------------------------------------------------
@dataclass
class DoorFactorCell:
    """One mode's learned door-leakage multiplier EMA + running sample count.

    ``factor`` is ``None`` until the first door-open sample seeds the cell; ``n``
    is the number of door-open samples folded in so far (the confidence gate
    counter, :data:`DOOR_MIN_N`).
    """

    factor: float | None = None
    n: int = 0


@dataclass
class DoorFactorModel:
    """A room's independent cooling/heating door-factor cells.

    Mirrors :class:`RoomEfficiencyModel`: the two cells are fully independent and
    each advances only on samples observed in its own mode (R27.1), so a per-mode
    update never bleeds into the other mode.
    """

    cooling: DoorFactorCell
    heating: DoorFactorCell


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------
def new_door_factor_model() -> DoorFactorModel:
    """Build a fresh model with distinct (non-aliased) cooling/heating cells.

    Each cell starts ``factor=None, n=0`` (no door-open sample observed yet), so
    :func:`resolve_door_factor` falls back to :data:`DOOR_FACTOR_DEFAULT` until a
    mode's cell is trusted (R27.4). The two cells are distinct objects so a
    per-mode update never aliases into the other mode (R27.1).
    """
    return DoorFactorModel(cooling=DoorFactorCell(), heating=DoorFactorCell())


# ---------------------------------------------------------------------------
# Online learning (R26.3 / R28.2 / R28.3 / R28.5)
# ---------------------------------------------------------------------------
def update_door_factor(
    model: DoorFactorModel,
    ratio: float | None,
    mode: str = "cooling",
) -> DoorFactorModel:
    """Fold one observed residual ``ratio`` into the chosen ``mode`` cell.

    The coordinator forms the residual ``ratio = sample / reference``; this
    function only learns it, mirroring :func:`update_room_efficiency`.

    Robustness (R28.3): a ``None`` or non-finite (NaN/inf) ``ratio`` leaves the
    model completely untouched — no factor change, no ``n`` increment. The ratio
    is clamped into ``[DOOR_FACTOR_MIN, DOOR_FACTOR_MAX]`` BEFORE the EMA step
    (R28.2), so an open door can never push the factor above ``1.0``. The first
    valid sample seeds the cell outright (``factor == clamp(ratio)``, ``n == 1``);
    thereafter the cell moves toward the clamped ratio via the shared adaptive
    alpha ``max(RATE_ALPHA_MIN, RATE_ALPHA0/sqrt(n))`` with ``n`` incremented
    before alpha is computed (R26.3), keeping the EMA bounded and stable for any
    valid stream (R28.5). Only the passed ``mode`` cell advances; the model is
    mutated in place and also returned for chaining.
    """
    if ratio is None or not math.isfinite(ratio):
        return model

    r = _clamp(ratio, DOOR_FACTOR_MIN, DOOR_FACTOR_MAX)
    cell: DoorFactorCell = getattr(model, mode)
    cell.n += 1
    cell.factor = _ema_step(cell.factor if cell.n > 1 else None, r, cell.n)

    return model


# ---------------------------------------------------------------------------
# Read-time resolution (R27.1 / R27.2 / R27.3 / R27.4 / R28.1)
# ---------------------------------------------------------------------------
def resolve_door_factor(
    model: DoorFactorModel | None,
    mode: str = "cooling",
    *,
    default: float = DOOR_FACTOR_DEFAULT,
) -> float:
    """Resolve the door-leakage factor for ``mode`` via the D12 fallback chain.

    Resolution order (R27.2 / D12), with the result ALWAYS clamped into
    ``[DOOR_FACTOR_MIN, DOOR_FACTOR_MAX]`` (R28.1):

    1. the requested ``mode`` cell IF it is trusted (``n >= DOOR_MIN_N`` AND a
       ``factor`` is present) -> ``clamp(cell.factor)``;
    2. else the other mode's cell IF it is trusted -> ``clamp(other.factor)``
       (the explicit cross-mode fallback);
    3. else ``default`` (== :data:`DOOR_FACTOR_DEFAULT` == ``0.9``).

    A ``None`` model (cold install) resolves to ``default`` (R27.4). Resolution
    is read-only: it never mutates the model, so per-mode independence holds —
    a cold/noisy cell for one mode never drags a trusted cell of the other mode
    except through the documented step-2 cross-mode fallback (R27.1 / R27.3).
    """
    clamped_default = _clamp(default, DOOR_FACTOR_MIN, DOOR_FACTOR_MAX)
    if model is None:
        return clamped_default

    other_mode = "heating" if mode == "cooling" else "cooling"
    for candidate in (mode, other_mode):
        cell: DoorFactorCell = getattr(model, candidate)
        if cell.n >= DOOR_MIN_N and cell.factor is not None:
            return _clamp(cell.factor, DOOR_FACTOR_MIN, DOOR_FACTOR_MAX)

    return clamped_default


# ---------------------------------------------------------------------------
# Persistence converters (R29.3) — mirror the room-model ``_mode_*`` tolerance
# ---------------------------------------------------------------------------
# These define the persisted ``door_factor.<room>.<mode>`` schema and are the
# single source of truth for the door-factor wire shape, so they are unit-tested
# directly. ``door_factor_from_dict`` NEVER raises: any malformed/partial input
# decays to a fresh cell, exactly like ``_mode_from_dict``.
def _door_cell_to_dict(cell: DoorFactorCell) -> dict:
    """Serialize one :class:`DoorFactorCell` to its persisted dict shape."""
    return {
        "factor": None if cell.factor is None else float(cell.factor),
        "n": int(cell.n),
    }


def _door_cell_from_dict(data: object) -> DoorFactorCell:
    """Deserialize one door-factor cell, tolerating garbled ``factor``/``n``.

    A non-dict entry, a missing/garbled ``factor`` (non-numeric or non-finite),
    or a garbled ``n`` all decay to safe defaults (``factor=None`` / ``n=0``)
    without raising. An integer ``factor`` is coerced to ``float``.
    """
    if not isinstance(data, dict):
        return DoorFactorCell()
    raw_factor = data.get("factor")
    factor: float | None
    if isinstance(raw_factor, bool) or not isinstance(raw_factor, (int, float)):
        factor = None
    else:
        f = float(raw_factor)
        factor = f if math.isfinite(f) else None
    try:
        n = int(data.get("n", 0) or 0)
    except (TypeError, ValueError):
        n = 0
    return DoorFactorCell(factor=factor, n=n)


def door_factor_to_dict(model: DoorFactorModel) -> dict:
    """Serialize a :class:`DoorFactorModel` to its persisted dict shape."""
    return {
        "cooling": _door_cell_to_dict(model.cooling),
        "heating": _door_cell_to_dict(model.heating),
    }


def door_factor_from_dict(data: object) -> DoorFactorModel:
    """Deserialize a :class:`DoorFactorModel`; never raises on bad input.

    A non-dict ``data`` (or a missing/garbled mode entry) yields a fresh cell for
    that mode, so a malformed persisted section always loads to a usable model
    that resolves to :data:`DOOR_FACTOR_DEFAULT` (R29.3). ``from_dict(to_dict(m))``
    is lossless for any model (value-equal dataclasses).
    """
    if not isinstance(data, dict):
        return new_door_factor_model()
    return DoorFactorModel(
        cooling=_door_cell_from_dict(data.get("cooling")),
        heating=_door_cell_from_dict(data.get("heating")),
    )
