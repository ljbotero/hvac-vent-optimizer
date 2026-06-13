"""Tests-first (Task 4.1) for the pure `learning.py` vent-effectiveness math.

Validates: Requirements 25.2, 25.3, 25.4

These assert the contract of a NEW, dependency-free module `learning.py` that
does NOT exist yet, so every test here is EXPECTED TO FAIL with a missing-module
error (FileNotFoundError) until Task 4.2 implements it. This is the failing-test
step of the TDD loop — do NOT implement learning.py here.

`learning.py` is PURE (no Home Assistant imports), like `dab.py`/`context.py`.
We load it standalone by absolute path (mirroring tests/test_dab.py and
tests/test_context.py) so these tests need no HA stubs and never import the
package __init__. The load is done lazily in a fixture so a missing module
produces a clean per-test failure (FileNotFoundError) rather than a collection
error.

====================================================================
PINNED CONTRACT (for Task 4.2 to implement)
====================================================================
Module constants:
    LEAK_MAX     = 0.35   # leakage clamp upper bound
    LEAK_DEFAULT = 0.1    # leak used until enough samples / on degenerate input
    MODEL_MIN_N  = 8      # min regression samples before leak is trusted

Functions / signatures:
    derive_effectiveness(slope: float, intercept: float, n: int) -> Effectiveness
        e_room = max(0.0, slope*100 + intercept)               # full-open rate
        if n < MODEL_MIN_N or denom <= 0: leak = LEAK_DEFAULT
        else: leak = clamp(intercept / (slope*100 + intercept), 0.0, LEAK_MAX)
        Returns an object with .e_room and .leak (named-tuple/dataclass), and
        is also unpackable as (e_room, leak).

    flow(leak: float, aperture_frac: float) -> float
        leak_c = clamp(leak, 0.0, 1.0); a = clamp(aperture_frac, 0.0, 1.0)
        returns leak_c + (1 - leak_c) * a          # in [0, 1], non-decreasing in a

    predicted_rate(e_room: float, leak: float, aperture_pct: float) -> float
        returns max(0.0, e_room) * flow(leak, aperture_pct / 100)
        == e_room * (leak + (1-leak)*aperture_pct/100) for valid inputs

Worked numeric example (n >= MODEL_MIN_N):
    slope=0.002, intercept=0.05, n=50
      e_room = 0.002*100 + 0.05            = 0.25
      leak   = 0.05 / 0.25                 = 0.2   (within [0, 0.35])
      flow(0.2, 0.5)                       = 0.2 + 0.8*0.5 = 0.6
      predicted_rate(0.25, 0.2, 50)        = 0.25 * 0.6    = 0.15
====================================================================
"""
from __future__ import annotations

import importlib.util as _importlib_util
import pathlib as _pathlib
import sys as _sys

import pytest

_LEARNING_PATH = _pathlib.Path(__file__).resolve().parent.parent / "custom_components" / "hvac_vent_optimizer" / "learning.py"


def _load_learning():
    """Load the pure `learning.py` module by absolute path.

    Raises FileNotFoundError (clean, descriptive) while the module does not
    yet exist — that is the expected failure mode for the tests-first step.
    """
    spec = _importlib_util.spec_from_file_location("hvo_learning", _LEARNING_PATH)
    mod = _importlib_util.module_from_spec(spec)
    # Register before exec so dataclass annotation introspection can resolve it.
    _sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def learn():
    return _load_learning()


# ---------------------------------------------------------------------------
# Module constants (concrete contract pinned by the tests)
# ---------------------------------------------------------------------------
def test_constants_have_expected_concrete_values(learn):
    assert learn.LEAK_MAX == pytest.approx(0.35)
    assert learn.LEAK_DEFAULT == pytest.approx(0.1)
    assert learn.MODEL_MIN_N == 8


def test_leak_default_is_inside_leak_band(learn):
    assert 0.0 <= learn.LEAK_DEFAULT <= learn.LEAK_MAX


# ---------------------------------------------------------------------------
# derive_effectiveness: regression (slope, intercept, n) -> (e_room, leak)
# ---------------------------------------------------------------------------
def test_derive_effectiveness_worked_example(learn):
    # slope=0.002, intercept=0.05, n=50 (>= MODEL_MIN_N)
    eff = learn.derive_effectiveness(0.002, 0.05, 50)
    # e_room = slope*100 + intercept = 0.20 + 0.05 = 0.25
    assert eff.e_room == pytest.approx(0.25)
    # leak = intercept / (slope*100 + intercept) = 0.05 / 0.25 = 0.2
    assert eff.leak == pytest.approx(0.2)


def test_derive_effectiveness_is_unpackable_as_e_room_leak(learn):
    e_room, leak = learn.derive_effectiveness(0.002, 0.05, 50)
    assert e_room == pytest.approx(0.25)
    assert leak == pytest.approx(0.2)


def test_derive_effectiveness_clamps_leak_to_leak_max(learn):
    # slope=0.001, intercept=0.09 -> e_room = 0.10 + 0.09 = 0.19
    # raw leak = 0.09 / 0.19 = 0.4736... > LEAK_MAX -> clamp to 0.35
    eff = learn.derive_effectiveness(0.001, 0.09, 50)
    assert eff.e_room == pytest.approx(0.19)
    assert eff.leak == pytest.approx(learn.LEAK_MAX)


def test_derive_effectiveness_below_model_min_n_uses_leak_default(learn):
    # n=3 < MODEL_MIN_N(8) -> leak falls back to LEAK_DEFAULT regardless of regression
    eff = learn.derive_effectiveness(0.002, 0.05, 3)
    assert eff.leak == pytest.approx(learn.LEAK_DEFAULT)


def test_derive_effectiveness_at_model_min_n_boundary_trusts_regression(learn):
    # n exactly == MODEL_MIN_N -> regression leak is trusted (boundary inclusive)
    eff = learn.derive_effectiveness(0.002, 0.05, learn.MODEL_MIN_N)
    assert eff.leak == pytest.approx(0.2)


# ---------------------------------------------------------------------------
# derive_effectiveness: degenerate / negative inputs clamp safely
# ---------------------------------------------------------------------------
def test_derive_effectiveness_zero_denominator_is_safe(learn):
    # slope*100 + intercept == 0 -> no division error; leak -> LEAK_DEFAULT; e_room >= 0
    eff = learn.derive_effectiveness(0.0, 0.0, 50)
    assert eff.e_room >= 0.0
    assert eff.e_room == pytest.approx(0.0)
    assert eff.leak == pytest.approx(learn.LEAK_DEFAULT)


def test_derive_effectiveness_negative_slope_clamps_e_room_non_negative(learn):
    # slope=-0.01, intercept=0.2 -> raw e_room = -1.0 + 0.2 = -0.8 -> clamp to 0.0
    eff = learn.derive_effectiveness(-0.01, 0.2, 50)
    assert eff.e_room >= 0.0
    assert eff.e_room == pytest.approx(0.0)


def test_derive_effectiveness_negative_intercept_clamps_leak_non_negative(learn):
    # negative intercept could drive raw leak negative -> clamp to [0, LEAK_MAX]
    eff = learn.derive_effectiveness(0.01, -0.05, 50)
    assert 0.0 <= eff.leak <= learn.LEAK_MAX


def test_derive_effectiveness_leak_always_in_band(learn):
    for slope in (-0.01, 0.0, 0.001, 0.002, 0.05):
        for intercept in (-0.1, 0.0, 0.05, 0.2, 0.5):
            for n in (0, 3, 8, 50):
                eff = learn.derive_effectiveness(slope, intercept, n)
                assert 0.0 <= eff.leak <= learn.LEAK_MAX
                assert eff.e_room >= 0.0


# ---------------------------------------------------------------------------
# flow(leak, aperture_frac) = leak + (1-leak)*a
# ---------------------------------------------------------------------------
def test_flow_endpoints(learn):
    # flow(leak, 0) = leak ; flow(leak, 1) = 1
    assert learn.flow(0.2, 0.0) == pytest.approx(0.2)
    assert learn.flow(0.2, 1.0) == pytest.approx(1.0)


def test_flow_midpoint_concrete(learn):
    # flow(0.2, 0.5) = 0.2 + 0.8*0.5 = 0.6
    assert learn.flow(0.2, 0.5) == pytest.approx(0.6)


def test_flow_strictly_non_decreasing_in_aperture(learn):
    for leak in (0.0, 0.1, 0.2, 0.35):
        prev = None
        for step in range(0, 101):
            a = step / 100.0
            val = learn.flow(leak, a)
            if prev is not None:
                assert val >= prev - 1e-12  # non-decreasing
            prev = val
        # strictly increasing across the full sweep when leak < 1
        assert learn.flow(leak, 1.0) > learn.flow(leak, 0.0)


def test_flow_result_stays_in_unit_interval(learn):
    for leak in (0.0, 0.1, 0.35):
        for step in range(0, 101):
            val = learn.flow(leak, step / 100.0)
            assert 0.0 <= val <= 1.0


def test_flow_clamps_out_of_range_aperture(learn):
    # aperture fractions outside [0,1] clamp; no negatives, no overflow
    assert learn.flow(0.2, -0.5) == pytest.approx(0.2)   # clamps to a=0 -> leak
    assert learn.flow(0.2, 1.5) == pytest.approx(1.0)    # clamps to a=1 -> 1.0


# ---------------------------------------------------------------------------
# predicted_rate(e_room, leak, aperture_pct) = e_room*(leak+(1-leak)*aperture/100)
# ---------------------------------------------------------------------------
def test_predicted_rate_worked_example(learn):
    # predicted_rate(0.25, 0.2, 50) = 0.25 * (0.2 + 0.8*0.5) = 0.25 * 0.6 = 0.15
    assert learn.predicted_rate(0.25, 0.2, 50.0) == pytest.approx(0.15)


def test_predicted_rate_endpoints(learn):
    # aperture 0% -> e_room*leak ; aperture 100% -> e_room
    assert learn.predicted_rate(0.25, 0.2, 0.0) == pytest.approx(0.25 * 0.2)
    assert learn.predicted_rate(0.25, 0.2, 100.0) == pytest.approx(0.25)


def test_predicted_rate_matches_flow_composition(learn):
    # predicted_rate(e, leak, pct) == e * flow(leak, pct/100)
    e_room, leak = 0.33, 0.15
    for pct in (0.0, 10.0, 37.5, 73.0, 100.0):
        expected = e_room * learn.flow(leak, pct / 100.0)
        assert learn.predicted_rate(e_room, leak, pct) == pytest.approx(expected)


def test_predicted_rate_non_decreasing_in_aperture(learn):
    prev = None
    for pct in range(0, 101):
        val = learn.predicted_rate(0.25, 0.2, float(pct))
        if prev is not None:
            assert val >= prev - 1e-12
        prev = val


def test_predicted_rate_negative_efficiency_clamps_non_negative(learn):
    # degenerate negative e_room must not yield a negative predicted rate
    assert learn.predicted_rate(-0.25, 0.2, 50.0) >= 0.0


def test_predicted_rate_out_of_range_aperture_is_safe(learn):
    # aperture% outside [0,100] clamps; no negatives
    assert learn.predicted_rate(0.25, 0.2, -10.0) >= 0.0
    assert learn.predicted_rate(0.25, 0.2, 150.0) == pytest.approx(0.25)
