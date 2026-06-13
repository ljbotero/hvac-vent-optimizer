"""Tests-first (Task 31) for the learned non-linear `VentCurve` in `learning.py`.

Validates: Requirements 25.2, 25.3, 25.12, 25.13

These assert the contract of a NEW class `VentCurve` that wraps the persisted
``{"breakpoints":[...],"flow":[...],"counts":[...]}`` curve structure (schema v2,
Task 22) behind a ``flow(a)`` / ``inverse(f)`` / ``knee()`` interface, plus online
per-breakpoint EMA learning with an isotonic (monotonic non-decreasing) clamp,
``flow(100%)=1`` normalization, and a regression-seeded near-linear cold start
that the learner falls back to below ``MODEL_MIN_N`` samples.

``learning.py`` is PURE (no Home Assistant imports), like ``dab.py`` /
``context.py``. We load it standalone by absolute path (mirroring
tests/test_learning_effectiveness.py) so these tests need no HA stubs.

====================================================================
PINNED CONTRACT (for the implementation)
====================================================================
``VentCurve`` wraps ``breakpoints`` (== CURVE_BREAKPOINTS = [0,5,10,20,35,50,75,100]),
``flows`` (relative airflow per breakpoint, monotonic non-decreasing, flows[-1]==1),
and ``counts`` (per-breakpoint sample counts). Aperture inputs/outputs are
**percent** in ``[0, 100]`` (consistent with simulator.flow_from_curve).

    VentCurve.seed_from_regression(slope, intercept, n=0) -> VentCurve
        Near-linear cold-start curve: flow(0)=leak (derive_effectiveness),
        flow(100%)=1, counts all zero.

    curve.flow(aperture_pct: float) -> float        # in [0, 1], non-decreasing
    curve.inverse(flow_fraction: float) -> float    # -> aperture percent
        rising-region piecewise-linear inverse; flow >= flow(knee) maps to knee.
    curve.knee() -> int                             # smallest bp >= (1-KNEE_EPS)*flow[-1]
    curve.update(aperture_pct: float, observed_flow: float) -> VentCurve
        bins to nearest breakpoint, EMA + count, isotonic clamp, renormalize.
    curve.total_samples() -> int                    # == sum(counts)
    curve.to_dict() -> {"breakpoints":..,"flow":..,"counts":..}
    VentCurve.from_dict(data) -> VentCurve

Below MODEL_MIN_N total samples, flow/inverse/knee use the seeded near-linear
curve; at/after MODEL_MIN_N they use the learned curve.
====================================================================
"""

from __future__ import annotations

import importlib.util as _importlib_util
import pathlib as _pathlib
import sys as _sys

import pytest

_LEARNING_PATH = _pathlib.Path(__file__).resolve().parent.parent / "custom_components" / "hvac_vent_optimizer" / "learning.py"


def _load_learning():
    spec = _importlib_util.spec_from_file_location("hvo_learning_curve", _LEARNING_PATH)
    mod = _importlib_util.module_from_spec(spec)
    _sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def learn():
    return _load_learning()


# ---------------------------------------------------------------------------
# Reference helpers (independent reimplementation for cross-checking flow())
# ---------------------------------------------------------------------------
def _interp(breakpoints, flows, a):
    """Piecewise-linear interpolation reference (a clamped to [bp0, bp-1])."""
    a = max(breakpoints[0], min(breakpoints[-1], a))
    if a <= breakpoints[0]:
        return flows[0]
    if a >= breakpoints[-1]:
        return flows[-1]
    for i in range(1, len(breakpoints)):
        lo, hi = breakpoints[i - 1], breakpoints[i]
        if a <= hi:
            span = hi - lo
            frac = 0.0 if span <= 0 else (a - lo) / span
            return flows[i - 1] + frac * (flows[i] - flows[i - 1])
    return flows[-1]


def _is_monotonic(values):
    return all(values[i] >= values[i - 1] - 1e-9 for i in range(1, len(values)))


# A representative saturating target (concave, most airflow by ~50 %), aligned
# to CURVE_BREAKPOINTS, normalized so the last value is 1.0. The 50 % value sits
# clearly past the knee threshold ((1-KNEE_EPS)*1 = 0.98) so the knee is the
# 50 % breakpoint unambiguously (not on the float boundary).
_SAT_TARGET = [0.10, 0.45, 0.62, 0.80, 0.92, 0.99, 0.995, 1.0]


# ===========================================================================
# Cold-start seed (regression -> near-linear curve) — R25.2/25.3
# ===========================================================================
def test_seed_breakpoints_match_curve_breakpoints(learn):
    curve = learn.VentCurve.seed_from_regression(0.002, 0.05, 50)
    assert list(curve.to_dict()["breakpoints"]) == list(learn.CURVE_BREAKPOINTS)


def test_seed_flow_endpoints_leak_and_one(learn):
    # slope=0.002, intercept=0.05, n=50 -> leak = 0.05/0.25 = 0.2
    curve = learn.VentCurve.seed_from_regression(0.002, 0.05, 50)
    assert curve.flow(0) == pytest.approx(0.2, abs=1e-6)
    assert curve.flow(100) == pytest.approx(1.0, abs=1e-9)


def test_seed_leak_clamped_into_band(learn):
    # Thin sample count -> leak falls back to LEAK_DEFAULT (in [0, LEAK_MAX]).
    curve = learn.VentCurve.seed_from_regression(0.01, 0.5, 1)
    leak = curve.flow(0)
    assert 0.0 <= leak <= learn.LEAK_MAX
    assert leak == pytest.approx(learn.LEAK_DEFAULT, abs=1e-6)


def test_seed_is_near_linear_and_monotonic(learn):
    curve = learn.VentCurve.seed_from_regression(0.002, 0.05, 50)
    flows = curve.to_dict()["flow"]
    assert _is_monotonic(flows)
    # Near-linear: midpoint flow ~ leak + (1-leak)*0.5 for the linear model.
    assert curve.flow(50) == pytest.approx(0.2 + 0.8 * 0.5, abs=1e-6)


# ===========================================================================
# flow(a): interpolation + monotonicity (R25.2)
# ===========================================================================
def test_flow_matches_piecewise_linear_reference(learn):
    curve = learn.VentCurve.seed_from_regression(0.002, 0.05, 50)
    d = curve.to_dict()
    for a in (0, 3, 5, 12, 27, 50, 63, 88, 100):
        assert curve.flow(a) == pytest.approx(_interp(d["breakpoints"], d["flow"], a), abs=1e-9)


def test_flow_clamps_out_of_range_aperture(learn):
    curve = learn.VentCurve.seed_from_regression(0.002, 0.05, 50)
    assert curve.flow(-20) == pytest.approx(curve.flow(0), abs=1e-9)
    assert curve.flow(140) == pytest.approx(curve.flow(100), abs=1e-9)
    assert 0.0 <= curve.flow(37) <= 1.0


def test_flow_non_decreasing_in_aperture(learn):
    curve = learn.VentCurve.from_dict(
        {
            "breakpoints": list(learn.CURVE_BREAKPOINTS),
            "flow": list(_SAT_TARGET),
            "counts": [10] * len(learn.CURVE_BREAKPOINTS),
        }
    )
    samples = [curve.flow(a) for a in range(101)]
    assert _is_monotonic(samples)


# ===========================================================================
# knee(): smallest breakpoint reaching (1-KNEE_EPS)*full (R25.12)
# ===========================================================================
def test_knee_near_linear_seed_is_100(learn):
    curve = learn.VentCurve.seed_from_regression(0.002, 0.05, 50)
    assert curve.knee() == 100


def test_knee_saturating_curve_below_100(learn):
    curve = learn.VentCurve.from_dict(
        {
            "breakpoints": list(learn.CURVE_BREAKPOINTS),
            "flow": list(_SAT_TARGET),
            "counts": [10] * len(learn.CURVE_BREAKPOINTS),
        }
    )
    # _SAT_TARGET reaches >= (1-0.02)*1.0 = 0.98 first at the 50 % breakpoint.
    assert curve.knee() == 50


# ===========================================================================
# inverse(f): rising-region inverse + plateau safety (R25.13)
# ===========================================================================
def test_inverse_recovers_aperture_on_rising_region(learn):
    curve = learn.VentCurve.seed_from_regression(0.002, 0.05, 50)
    for a in (5, 10, 20, 35, 50, 75, 95):
        assert curve.inverse(curve.flow(a)) == pytest.approx(a, abs=1e-6)


def test_inverse_maps_flow_at_or_above_knee_to_knee(learn):
    curve = learn.VentCurve.from_dict(
        {
            "breakpoints": list(learn.CURVE_BREAKPOINTS),
            "flow": list(_SAT_TARGET),
            "counts": [10] * len(learn.CURVE_BREAKPOINTS),
        }
    )
    knee = curve.knee()
    knee_flow = curve.flow(knee)
    assert curve.inverse(knee_flow) == pytest.approx(knee, abs=1e-9)
    assert curve.inverse(knee_flow + 0.01) == pytest.approx(knee, abs=1e-9)
    assert curve.inverse(1.0) == pytest.approx(knee, abs=1e-9)


def test_inverse_below_leak_returns_zero_aperture(learn):
    curve = learn.VentCurve.from_dict(
        {
            "breakpoints": list(learn.CURVE_BREAKPOINTS),
            "flow": list(_SAT_TARGET),
            "counts": [10] * len(learn.CURVE_BREAKPOINTS),
        }
    )
    assert curve.inverse(0.0) == pytest.approx(0.0, abs=1e-9)
    assert curve.inverse(_SAT_TARGET[0] - 0.05) == pytest.approx(0.0, abs=1e-9)


def test_inverse_is_monotonic_non_decreasing_in_flow(learn):
    curve = learn.VentCurve.from_dict(
        {
            "breakpoints": list(learn.CURVE_BREAKPOINTS),
            "flow": list(_SAT_TARGET),
            "counts": [10] * len(learn.CURVE_BREAKPOINTS),
        }
    )
    apertures = [curve.inverse(f / 100.0) for f in range(101)]
    assert _is_monotonic(apertures)


# ===========================================================================
# Online update: EMA + count, isotonic clamp, renormalize (R25.2/25.3/25.6)
# ===========================================================================
def test_update_increments_counts_and_total(learn):
    curve = learn.VentCurve.seed_from_regression(0.002, 0.05, 50)
    assert curve.total_samples() == 0
    curve.update(50, 0.9)
    curve.update(50, 0.92)
    assert curve.total_samples() == 2
    counts = curve.to_dict()["counts"]
    # The 50 % breakpoint is index 5 in CURVE_BREAKPOINTS.
    assert counts[5] == 2
    assert sum(counts) == 2


def test_update_bins_to_nearest_breakpoint(learn):
    curve = learn.VentCurve.seed_from_regression(0.002, 0.05, 50)
    # aperture 8 is nearest to the 10 % breakpoint (index 2).
    curve.update(8, 0.5)
    counts = curve.to_dict()["counts"]
    assert counts[2] == 1
    assert sum(counts) == 1


def test_update_converges_to_saturating_target(learn):
    curve = learn.VentCurve.seed_from_regression(0.002, 0.05, 50)
    bps = list(learn.CURVE_BREAKPOINTS)
    for _ in range(60):
        for i, bp in enumerate(bps):
            curve.update(bp, _SAT_TARGET[i])
    d = curve.to_dict()
    assert _is_monotonic(d["flow"])
    assert d["flow"][-1] == pytest.approx(1.0, abs=1e-9)
    assert 0.0 <= d["flow"][0] <= learn.LEAK_MAX
    for i in range(len(bps)):
        assert d["flow"][i] == pytest.approx(_SAT_TARGET[i], abs=0.03)
    # Learned knee tracks the saturating shape (50 %).
    assert curve.knee() == 50


def test_update_enforces_monotonic_after_decreasing_sample(learn):
    # Push enough samples to leave the seed-fallback region, then inject a
    # violating (decreasing) sample at a high breakpoint.
    curve = learn.VentCurve.seed_from_regression(0.002, 0.05, 50)
    for bp in learn.CURVE_BREAKPOINTS:
        for _ in range(3):
            curve.update(bp, 0.5 if bp < 100 else 1.0)
    # Now slam the 75 % breakpoint far below its neighbors repeatedly.
    for _ in range(10):
        curve.update(75, 0.05)
    flows = curve.to_dict()["flow"]
    assert _is_monotonic(flows), flows
    assert flows[-1] == pytest.approx(1.0, abs=1e-9)


def test_update_renormalizes_flow_at_100_to_one(learn):
    curve = learn.VentCurve.seed_from_regression(0.002, 0.05, 50)
    for _ in range(10):
        curve.update(100, 0.8)  # undershoot the full-open observation
        curve.update(50, 0.7)
    assert curve.flow(100) == pytest.approx(1.0, abs=1e-9)
    assert _is_monotonic(curve.to_dict()["flow"])


def test_update_ignores_non_finite_and_clamps_negative(learn):
    curve = learn.VentCurve.seed_from_regression(0.002, 0.05, 50)
    before = list(curve.to_dict()["flow"])
    curve.update(50, float("nan"))
    curve.update(50, float("inf"))
    assert curve.total_samples() == 0
    assert curve.to_dict()["flow"] == pytest.approx(before)
    # A negative observation is clamped to 0, not rejected.
    curve.update(50, -1.0)
    assert curve.total_samples() == 1
    assert all(f >= 0.0 for f in curve.to_dict()["flow"])


# ===========================================================================
# Cold-start fallback below MODEL_MIN_N (R25.2)
# ===========================================================================
def test_below_min_n_uses_seeded_curve(learn):
    curve = learn.VentCurve.seed_from_regression(0.002, 0.05, 50)
    seed_mid = curve.flow(50)  # near-linear seed value at 50 %
    # Fewer than MODEL_MIN_N samples, all pushing toward a very different shape.
    for _ in range(learn.MODEL_MIN_N - 1):
        curve.update(50, 0.99)
    assert curve.total_samples() == learn.MODEL_MIN_N - 1
    # Still reports the seeded near-linear value (fallback in effect).
    assert curve.flow(50) == pytest.approx(seed_mid, abs=1e-6)


def test_at_min_n_uses_learned_curve(learn):
    curve = learn.VentCurve.seed_from_regression(0.002, 0.05, 50)
    seed_mid = curve.flow(50)
    for _ in range(learn.MODEL_MIN_N):
        curve.update(50, 0.99)
    assert curve.total_samples() == learn.MODEL_MIN_N
    # Now the learned 50 % value dominates and differs from the seed.
    assert curve.flow(50) != pytest.approx(seed_mid, abs=1e-3)
    assert curve.flow(50) > seed_mid


# ===========================================================================
# Persistence round-trip (schema v2 compatibility; R25.7)
# ===========================================================================
def test_to_dict_has_exact_schema_keys(learn):
    curve = learn.VentCurve.seed_from_regression(0.002, 0.05, 50)
    d = curve.to_dict()
    assert set(d.keys()) == {"breakpoints", "flow", "counts"}
    assert len(d["breakpoints"]) == len(d["flow"]) == len(d["counts"])


def test_from_dict_round_trips_values(learn):
    src = {
        "breakpoints": list(learn.CURVE_BREAKPOINTS),
        "flow": list(_SAT_TARGET),
        "counts": [2, 3, 4, 5, 1, 6, 0, 7],
    }
    curve = learn.VentCurve.from_dict(src)
    out = curve.to_dict()
    assert out["breakpoints"] == src["breakpoints"]
    assert out["flow"] == pytest.approx(src["flow"])
    assert out["counts"] == src["counts"]
    assert curve.total_samples() == sum(src["counts"])


def test_from_dict_consistent_with_seed_round_trip(learn):
    seeded = learn.VentCurve.seed_from_regression(0.002, 0.05, 50)
    reloaded = learn.VentCurve.from_dict(seeded.to_dict())
    for a in (0, 10, 50, 100):
        assert reloaded.flow(a) == pytest.approx(seeded.flow(a), abs=1e-9)
    assert reloaded.knee() == seeded.knee()


def test_from_dict_seeds_round_trip_curve_for_inverse(learn):
    # A persisted curve with samples uses its stored shape for inverse/knee.
    src = {
        "breakpoints": list(learn.CURVE_BREAKPOINTS),
        "flow": list(_SAT_TARGET),
        "counts": [10] * len(learn.CURVE_BREAKPOINTS),
    }
    curve = learn.VentCurve.from_dict(src)
    for a in (5, 10, 20, 35):
        assert curve.inverse(curve.flow(a)) == pytest.approx(a, abs=1e-6)
