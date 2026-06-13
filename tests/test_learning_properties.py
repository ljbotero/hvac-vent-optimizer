"""Property-based tests for ``learning.py`` (Hypothesis).

These encode the learning-math correctness properties from design.md
("Correctness Properties"):

* **Property 7 — Effectiveness monotonic.** ``flow(a)`` is non-decreasing in
  aperture and ``flow(0) = clamp(leak)``; every leak derived from a regression
  fit lands in ``[0, LEAK_MAX]``. (Validates Requirements 25.2, 25.3.)
* **Property 7 (curve) — Learned ``VentCurve`` monotonic.** The learned
  aperture→airflow curve ``flow(a)`` is non-decreasing in aperture, stays in
  ``[0, 1]``, keeps ``flow(0) = leak ∈ [0, LEAK_MAX]``, and (when any airflow has
  been observed) renormalizes ``flow(100%) = 1`` — and all of this holds after
  *every* online ``update`` over randomized sample streams. (Validates
  Requirements 25.2, 25.3.)
* **Property 8 — Learning stability.** Online ``update_room_efficiency`` /
  ``effective_rate`` keep rates in ``[RATE_MIN, RATE_MAX]``, never negative or
  divergent, and are robust to ``None`` / NaN / inf / negative samples and
  out-of-range regime indices. (Validates Requirements 25.6, 11.1.)
* **Property 11 — Effective rate reachability.** Once a regime cell has
  ``>= REGIME_MIN_N`` samples (with a positive learned rate) it is selected over
  the baseline, and regimes seeded with distinct rates stay distinguishable.
  (Validates Requirements 11.1, 11.2, 11.3.)
* **Property 13 — Curve inversion & knee consistency.** ``inverse(flow(a)) ≈ a``
  on the rising region; any required flow ``>= flow(knee)`` inverts to the knee
  aperture; ``inverse`` is monotonic non-decreasing in required flow; and
  ``knee`` is the smallest breakpoint reaching ``(1 - KNEE_EPS)`` of full
  airflow. (Validates Requirements 25.12, 25.13.)

``learning.py`` is pure (HA-free, stdlib only), so it is loaded standalone by
absolute path under the name ``hvo_learning`` — the same convention as
``test_dab.py`` — which both proves the module is unit-testable in isolation and
avoids importing the ``hvac_vent_optimizer`` package (whose ``__init__`` pulls in
Home Assistant).

The file name contains "properties" so ``pytest -k property`` collects it.

_Requirements: 20.2, 25.10, 11.1, 25.2, 25.3, 25.6, 25.12, 25.13_
"""
from __future__ import annotations

import importlib.util
import math
import pathlib
import sys
from itertools import accumulate
from typing import Any

import pytest
from hypothesis import assume, given, strategies as st

# --- Load learning.py standalone (pure module, no HA) ----------------------
_LEARNING_PATH = pathlib.Path(__file__).resolve().parent.parent / "custom_components" / "hvac_vent_optimizer" / "learning.py"
_spec = importlib.util.spec_from_file_location("hvo_learning", _LEARNING_PATH)
learning = importlib.util.module_from_spec(_spec)
# Register before exec so dataclasses introspection (with `from __future__
# import annotations`) can resolve the module by name.
sys.modules[_spec.name] = learning
_spec.loader.exec_module(learning)


# ---------------------------------------------------------------------------
# Shared strategies
# ---------------------------------------------------------------------------
# Finite floats only: monotonicity and clamping are only well-defined for finite
# inputs (NaN breaks min/max ordering). Range is intentionally wider than the
# physical [0, 1] band so we also exercise the clamps.
finite = st.floats(allow_nan=False, allow_infinity=False, min_value=-1e6, max_value=1e6)

# Any sample the online learner might see, including the values it must be robust
# to: None, NaN, +/-inf, and negatives.
noisy_sample = st.one_of(
    st.none(),
    st.just(math.nan),
    st.just(math.inf),
    st.just(-math.inf),
    st.floats(allow_nan=False, allow_infinity=False, min_value=-1e3, max_value=1e3),
)

modes = st.sampled_from(["cooling", "heating"])

# Regime indices deliberately span out-of-range values to exercise clamping.
any_regime_idx = st.integers(min_value=-10, max_value=10)


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


# ===========================================================================
# Property 7 — Effectiveness monotonic
# Invariant: flow is non-decreasing in aperture; flow(leak, 0) == clamp(leak);
# derived leak always lands in [0, LEAK_MAX].
# **Validates: Requirements 25.2, 25.3**
# ===========================================================================
@given(leak=finite, a1=finite, a2=finite)
def test_property7_flow_monotonic_in_aperture(leak: float, a1: float, a2: float) -> None:
    """``flow`` is non-decreasing as aperture grows, for arbitrary leak."""
    lo, hi = sorted((a1, a2))
    assert learning.flow(leak, lo) <= learning.flow(leak, hi) + 1e-12


@given(leak=finite, aperture=finite)
def test_property7_flow_stays_in_unit_interval(leak: float, aperture: float) -> None:
    """``flow`` always returns a fraction in ``[0, 1]``."""
    result = learning.flow(leak, aperture)
    assert 0.0 <= result <= 1.0


@given(leak=finite)
def test_property7_flow_closed_equals_clamped_leak(leak: float) -> None:
    """A fully closed vent (a == 0) passes exactly ``clamp(leak)``."""
    assert learning.flow(leak, 0.0) == pytest.approx(_clamp(leak, 0.0, 1.0))


@given(leak=finite)
def test_property7_flow_full_open_is_one(leak: float) -> None:
    """A fully open vent (a == 1) passes all of the flow regardless of leak."""
    assert learning.flow(leak, 1.0) == pytest.approx(1.0)


@given(
    slope=finite,
    intercept=finite,
    n=st.integers(min_value=0, max_value=10_000),
)
def test_property7_derived_leak_in_band(slope: float, intercept: float, n: int) -> None:
    """Any leak derived from a regression fit lands in ``[0, LEAK_MAX]``."""
    eff = learning.derive_effectiveness(slope, intercept, n)
    assert 0.0 <= eff.leak <= learning.LEAK_MAX
    # e_room is a physical rate: never negative.
    assert eff.e_room >= 0.0
    # NamedTuple is unpackable as (e_room, leak).
    e_room, leak = eff
    assert e_room == eff.e_room and leak == eff.leak


# ===========================================================================
# Property 8 — Learning stability
# Invariant: under arbitrary (noisy/missing/negative) sample streams and
# out-of-range regime indices, effective_rate stays finite and clamped to
# [RATE_MIN, RATE_MAX], and the baseline is never negative or divergent.
# **Validates: Requirements 25.6, 11.1**
# ===========================================================================
@given(
    samples=st.lists(noisy_sample, max_size=60),
    regime_indices=st.lists(any_regime_idx, max_size=60),
    mode=modes,
)
def test_property8_rates_clamped_and_finite_under_noise(
    samples: list[float | None],
    regime_indices: list[int],
    mode: str,
) -> None:
    """No sample stream can push rates negative, NaN, inf, or out of band."""
    model = learning.new_room_model()
    for i, sample in enumerate(samples):
        idx = regime_indices[i % len(regime_indices)] if regime_indices else 0
        learning.update_room_efficiency(model, sample, idx, mode=mode)

        # effective_rate must always be finite and inside the physical band,
        # for every regime (including freshly-clamped out-of-range probes).
        for probe in range(learning.EFF_REGIME_COUNT):
            rate = learning.effective_rate(model, probe, mode=mode)
            assert math.isfinite(rate)
            assert learning.RATE_MIN <= rate <= learning.RATE_MAX

        # Baseline never goes negative or non-finite (it only ever folds in
        # clamped, non-negative samples).
        for sub_name in ("cooling", "heating"):
            sub = getattr(model, sub_name)
            if sub.baseline is not None:
                assert math.isfinite(sub.baseline)
                assert sub.baseline >= 0.0
            for cell in sub.regimes:
                assert math.isfinite(cell.rate)
                assert cell.rate >= 0.0


@given(sample=noisy_sample, idx=any_regime_idx, mode=modes)
def test_property8_nonfinite_or_none_sample_is_ignored(
    sample: float | None,
    idx: int,
    mode: str,
) -> None:
    """A ``None`` / NaN / inf sample leaves the model completely untouched."""
    model = learning.new_room_model()
    learning.update_room_efficiency(model, sample, idx, mode=mode)
    if sample is None or not math.isfinite(sample):
        # Nothing advanced: every count is still zero and no baseline was set.
        for sub_name in ("cooling", "heating"):
            sub = getattr(model, sub_name)
            assert sub.n == 0
            assert sub.baseline is None
            assert all(cell.n == 0 for cell in sub.regimes)


@given(
    samples=st.lists(noisy_sample, max_size=40),
    regime_indices=st.lists(any_regime_idx, max_size=40),
    mode=modes,
)
def test_property8_other_mode_untouched(
    samples: list[float | None],
    regime_indices: list[int],
    mode: str,
) -> None:
    """Updating one mode never mutates the other mode's sub-model (R25.1)."""
    other = "heating" if mode == "cooling" else "cooling"
    model = learning.new_room_model()
    for i, sample in enumerate(samples):
        idx = regime_indices[i % len(regime_indices)] if regime_indices else 0
        learning.update_room_efficiency(model, sample, idx, mode=mode)

    other_sub = getattr(model, other)
    assert other_sub.n == 0
    assert other_sub.baseline is None
    assert all(cell.n == 0 and cell.rate == 0.0 for cell in other_sub.regimes)


# ===========================================================================
# Property 11 — Effective rate reachability
# Invariant: once a regime cell has >= REGIME_MIN_N samples and a positive rate,
# effective_rate returns that cell's rate (not the baseline); regimes seeded
# with distinct rates remain distinguishable.
# **Validates: Requirements 11.1, 11.2, 11.3**
# ===========================================================================
@given(
    target_idx=st.integers(min_value=0, max_value=learning.EFF_REGIME_COUNT - 1),
    other_idx=st.integers(min_value=0, max_value=learning.EFF_REGIME_COUNT - 1),
    cell_rate=st.floats(min_value=1.0, max_value=learning.RATE_MAX),
    baseline_rate=st.floats(min_value=0.05, max_value=0.5),
    extra=st.integers(min_value=0, max_value=10),
    mode=modes,
)
def test_property11_regime_selected_over_baseline_once_trusted(
    target_idx: int,
    other_idx: int,
    cell_rate: float,
    baseline_rate: float,
    extra: int,
    mode: str,
) -> None:
    """A trusted regime cell is returned, distinct from a diverged baseline."""
    # Make the baseline diverge from the target cell: feed a *different* regime
    # a stream of low samples first, so the mode baseline trends low.
    other = other_idx if other_idx != target_idx else (target_idx + 1) % learning.EFF_REGIME_COUNT
    model = learning.new_room_model()
    for _ in range(learning.REGIME_MIN_N + extra):
        learning.update_room_efficiency(model, baseline_rate, other, mode=mode)

    # Now seed the target regime with REGIME_MIN_N identical high samples. A
    # constant stream keeps the cell EMA pinned at that constant value.
    for _ in range(learning.REGIME_MIN_N):
        learning.update_room_efficiency(model, cell_rate, target_idx, mode=mode)

    sub = getattr(model, mode)
    cell = sub.regimes[target_idx]
    assert cell.n >= learning.REGIME_MIN_N
    assert cell.rate > 0.0

    result = learning.effective_rate(model, target_idx, mode=mode)
    # The trusted regime's (clamped) rate is returned...
    assert result == pytest.approx(_clamp(cell.rate, learning.RATE_MIN, learning.RATE_MAX))
    # ...and it is the regime rate, NOT the (lower) baseline.
    assert result > _clamp(sub.baseline or 0.0, learning.RATE_MIN, learning.RATE_MAX)


@given(
    idx=st.integers(min_value=0, max_value=learning.EFF_REGIME_COUNT - 1),
    n=st.integers(min_value=0, max_value=learning.REGIME_MIN_N - 1),
    cell_rate=st.floats(min_value=0.1, max_value=learning.RATE_MAX),
    baseline_rate=st.floats(min_value=0.1, max_value=learning.RATE_MAX),
    mode=modes,
)
def test_property11_untrusted_regime_falls_back_to_baseline(
    idx: int,
    n: int,
    cell_rate: float,
    baseline_rate: float,
    mode: str,
) -> None:
    """Below REGIME_MIN_N samples the cell is not yet trusted: use baseline."""
    model = learning.new_room_model()
    # Advance the baseline via a different regime so it has a defined value.
    other = (idx + 1) % learning.EFF_REGIME_COUNT
    learning.update_room_efficiency(model, baseline_rate, other, mode=mode)
    # Feed the target regime fewer than REGIME_MIN_N samples.
    for _ in range(n):
        learning.update_room_efficiency(model, cell_rate, idx, mode=mode)

    sub = getattr(model, mode)
    cell = sub.regimes[idx]
    if cell.n < learning.REGIME_MIN_N:
        result = learning.effective_rate(model, idx, mode=mode)
        assert result == pytest.approx(
            _clamp(sub.baseline or 0.0, learning.RATE_MIN, learning.RATE_MAX)
        )


@given(
    rate_a=st.floats(min_value=0.1, max_value=0.6),
    rate_b=st.floats(min_value=1.2, max_value=learning.RATE_MAX),
    mode=modes,
)
def test_property11_seeded_regimes_diverge(
    rate_a: float,
    rate_b: float,
    mode: str,
) -> None:
    """Distinct sample streams to distinct regimes stay distinguishable."""
    model = learning.new_room_model()
    # Two different regimes, each seeded to trust with its own constant rate.
    for _ in range(learning.REGIME_MIN_N):
        learning.update_room_efficiency(model, rate_a, 0, mode=mode)
        learning.update_room_efficiency(model, rate_b, 1, mode=mode)

    eff_a = learning.effective_rate(model, 0, mode=mode)
    eff_b = learning.effective_rate(model, 1, mode=mode)

    # Each regime reports its own (constant) learned rate, so the two regimes
    # are clearly distinguishable (rate_b >> rate_a by construction).
    assert eff_a == pytest.approx(_clamp(rate_a, learning.RATE_MIN, learning.RATE_MAX))
    assert eff_b == pytest.approx(_clamp(rate_b, learning.RATE_MIN, learning.RATE_MAX))
    assert eff_b > eff_a


# ===========================================================================
# Shared strategies / helpers for the learned VentCurve (Properties 7 & 13)
# ===========================================================================
# Regression seeds: arbitrary finite slope/intercept feed
# ``VentCurve.seed_from_regression`` (which derives + clamps the leak), exercising
# the full leak band [0, LEAK_MAX] plus the LEAK_DEFAULT fallback for thin fits.
seed_slope = st.floats(allow_nan=False, allow_infinity=False, min_value=-0.05, max_value=0.05)
seed_intercept = st.floats(allow_nan=False, allow_infinity=False, min_value=-0.5, max_value=0.5)
seed_n = st.integers(min_value=0, max_value=1000)

# Apertures fed to update()/flow() deliberately spill outside [0, 100] so the
# clamping in _nearest_index / _interp_curve is exercised.
curve_aperture = st.floats(allow_nan=False, allow_infinity=False, min_value=-20.0, max_value=120.0)

# Observations the online learner must tolerate: NaN/inf are ignored, negatives
# clamp to 0, and >1 clamps to 1 (R25.6). Used for the robustness/monotonicity
# stream so invariants must survive garbage.
curve_noisy_flow = st.one_of(
    st.just(math.nan),
    st.just(math.inf),
    st.just(-math.inf),
    st.floats(allow_nan=False, allow_infinity=False, min_value=-2.0, max_value=2.0),
)

# A "physically meaningful" observation in [0, 1] — relative airflow, already
# normalized to full-open — used where we assert the flow(100%) renormalization.
curve_clean_flow = st.floats(allow_nan=False, allow_infinity=False, min_value=0.0, max_value=1.0)

_N_BP = len(learning.CURVE_BREAKPOINTS)


def _is_monotonic(values: list[float], tol: float = 1e-9) -> bool:
    """True if ``values`` is non-decreasing within ``tol``."""
    return all(values[i] >= values[i - 1] - tol for i in range(1, len(values)))


def _dense_apertures() -> list[float]:
    """Breakpoints plus midpoints plus out-of-range probes (dense monotonic sweep)."""
    pts: list[float] = [-10.0, 0.0]
    pts += [float(bp) for bp in learning.CURVE_BREAKPOINTS]
    pts += [2.5, 7.5, 15.0, 27.5, 42.5, 62.5, 88.0, 100.0, 110.0]
    return sorted(pts)


def _assert_curve_invariants(curve: Any) -> None:
    """Property-7 invariants that must hold for ANY VentCurve state.

    * ``flow`` is non-decreasing across a dense aperture sweep,
    * every ``flow`` value is in ``[0, 1]``,
    * ``flow(0)`` (the leak) is in ``[0, LEAK_MAX]``.
    """
    samples = [curve.flow(a) for a in _dense_apertures()]
    assert all(0.0 <= f <= 1.0 for f in samples), samples
    assert _is_monotonic(samples), samples
    assert 0.0 <= curve.flow(0) <= learning.LEAK_MAX + 1e-9


@st.composite
def monotonic_curves(draw: st.DrawFn, *, strictly: bool = False) -> Any:
    """A persisted, normalized VentCurve with a random monotonic non-decreasing shape.

    Builds ``flows`` by cumulating non-negative (or strictly positive) increments
    then normalizing so ``flows[-1] == 1`` and clamping the leak into
    ``[0, LEAK_MAX]`` — i.e. exactly the valid persisted-curve space. Loaded via
    ``VentCurve.from_dict`` with trusted counts so the learned (not seed) shape is
    used by ``flow``/``inverse``/``knee``.
    """
    lo = 0.02 if strictly else 0.0
    incs = draw(
        st.lists(
            st.floats(allow_nan=False, allow_infinity=False, min_value=lo, max_value=1.0),
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


# ===========================================================================
# Property 7 (curve) — Learned VentCurve monotonic
# Invariant: after EVERY online update, the learned curve is non-decreasing in
# aperture, stays in [0, 1], keeps flow(0) ∈ [0, LEAK_MAX], and (when airflow has
# been observed) renormalizes flow(100%) = 1.
# **Validates: Requirements 25.2, 25.3**
# ===========================================================================
@given(
    slope=seed_slope,
    intercept=seed_intercept,
    n=seed_n,
    stream=st.lists(st.tuples(curve_aperture, curve_noisy_flow), max_size=50),
)
def test_property7_curve_monotonic_after_every_update(
    slope: float,
    intercept: float,
    n: int,
    stream: list[tuple[float, float]],
) -> None:
    """Monotonicity + physical bounds survive an arbitrary (noisy) update stream.

    The invariants are checked on the freshly seeded curve and re-checked after
    *each* online update, so no single noisy/out-of-range sample can transiently
    break the curve (R25.2/25.6).
    """
    curve = learning.VentCurve.seed_from_regression(slope, intercept, n)
    _assert_curve_invariants(curve)  # cold-start seed already well-formed
    for aperture_pct, observed_flow in stream:
        curve.update(aperture_pct, observed_flow)
        _assert_curve_invariants(curve)


@given(
    slope=seed_slope,
    intercept=seed_intercept,
    n=seed_n,
    stream=st.lists(st.tuples(curve_aperture, curve_clean_flow), min_size=1, max_size=60),
)
def test_property7_curve_full_open_normalized_to_one(
    slope: float,
    intercept: float,
    n: int,
    stream: list[tuple[float, float]],
) -> None:
    """``flow(100%)`` renormalizes to 1 whenever any airflow has been observed.

    Below ``MODEL_MIN_N`` samples the seed (already ``flow(100%) = 1``) is used;
    at/after it the learned curve is renormalized so ``flows[-1] == 1`` — *unless*
    full-open airflow was never observed at all (a degenerate all-zero stream, for
    which 0/0 normalization is undefined). That physically-empty case is outside
    the meaningful input space and is assumed away, not asserted against.
    """
    curve = learning.VentCurve.seed_from_regression(slope, intercept, n)
    for aperture_pct, observed_flow in stream:
        curve.update(aperture_pct, observed_flow)

    d = curve.to_dict()
    if sum(d["counts"]) >= learning.MODEL_MIN_N:
        # Skip the degenerate "no airflow ever observed" curve (normalization 0/0).
        assume(d["flow"][-1] > 0.0)
    assert curve.flow(100) == pytest.approx(1.0, abs=1e-9)


@given(slope=seed_slope, intercept=seed_intercept, n=seed_n)
def test_property7_seed_is_well_formed(slope: float, intercept: float, n: int) -> None:
    """Any regression-seeded cold-start curve already satisfies the invariants."""
    curve = learning.VentCurve.seed_from_regression(slope, intercept, n)
    _assert_curve_invariants(curve)
    assert curve.flow(100) == pytest.approx(1.0, abs=1e-9)
    assert curve.total_samples() == 0


# ===========================================================================
# Property 13 — Curve inversion & knee consistency
# Invariant: inverse(flow(a)) ≈ a on the rising region; required flow ≥ flow(knee)
# inverts to the knee aperture; inverse is monotonic non-decreasing in flow; knee
# is the smallest breakpoint reaching (1 - KNEE_EPS)·full.
# **Validates: Requirements 25.12, 25.13**
# ===========================================================================
@given(slope=seed_slope, intercept=seed_intercept, n=seed_n, frac=st.floats(min_value=0.0, max_value=1.0))
def test_property13_inverse_recovers_aperture_on_seed_curve(
    slope: float,
    intercept: float,
    n: int,
    frac: float,
) -> None:
    """On the strictly-rising near-linear seed, ``inverse(flow(a)) == a``.

    The regression-seeded curve is strictly increasing (leak < 1) with the knee at
    100 %, so the entire ``[0, 100]`` span is the rising region and the inverse
    must recover any aperture exactly.
    """
    curve = learning.VentCurve.seed_from_regression(slope, intercept, n)
    aperture = frac * 100.0
    assert curve.inverse(curve.flow(aperture)) == pytest.approx(aperture, abs=1e-6)


@given(curve=monotonic_curves(strictly=True), frac=st.floats(min_value=0.0, max_value=1.0))
def test_property13_inverse_recovers_aperture_on_rising_region(
    curve: Any,
    frac: float,
) -> None:
    """For a strictly-increasing learned curve, ``inverse(flow(a)) == a`` for ``a ≤ knee``.

    Above the knee the curve is (by definition) effectively flat, so the inverse
    deliberately collapses to the knee; we therefore only probe the genuinely
    rising region ``[0, knee]``.
    """
    knee = curve.knee()
    aperture = frac * float(knee)
    assert curve.inverse(curve.flow(aperture)) == pytest.approx(aperture, abs=1e-6)


@given(curve=monotonic_curves(), excess=st.floats(min_value=0.0, max_value=2.0))
def test_property13_flow_at_or_above_knee_inverts_to_knee(
    curve: Any,
    excess: float,
) -> None:
    """Any required flow at/above the knee's airflow inverts to the knee aperture (R25.13)."""
    knee = curve.knee()
    knee_flow = curve.flow(knee)
    assert curve.inverse(knee_flow + excess) == pytest.approx(float(knee), abs=1e-9)


@given(curve=monotonic_curves(), flows=st.lists(curve_clean_flow, min_size=2, max_size=40))
def test_property13_inverse_monotonic_in_required_flow(
    curve: Any,
    flows: list[float],
) -> None:
    """``inverse`` is non-decreasing in the required flow fraction (R25.13)."""
    ordered = sorted(flows)
    apertures = [curve.inverse(f) for f in ordered]
    assert _is_monotonic(apertures, tol=1e-9), apertures


@given(curve=monotonic_curves())
def test_property13_knee_is_smallest_breakpoint_reaching_threshold(curve: Any) -> None:
    """``knee`` is the smallest breakpoint whose flow reaches ``(1 - KNEE_EPS)·full`` (R25.12)."""
    d = curve.to_dict()
    breakpoints = d["breakpoints"]
    flows = d["flow"]
    full = flows[-1] or 1.0
    target = (1.0 - learning.KNEE_EPS) * full
    expected = next(
        (int(bp) for bp, f in zip(breakpoints, flows, strict=False) if f >= target),
        int(breakpoints[-1]),
    )
    assert curve.knee() == expected
    # The knee's own airflow must actually clear the threshold.
    assert curve.flow(curve.knee()) >= target - 1e-9
