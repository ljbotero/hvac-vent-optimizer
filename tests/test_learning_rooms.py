"""Tests-first (Task 5.1) for room-efficiency learning in pure `learning.py`.

Validates: Requirements 11.1, 11.2, 11.3, 25.1, 25.6

These assert the contract of NEW symbols to be added to `learning.py` by Task
5.2 (`RoomEfficiencyModel`, `new_room_model`, `update_room_efficiency`,
`effective_rate`, and the constants `REGIME_MIN_N` / `RATE_MIN` / `RATE_MAX` /
`EFF_REGIME_COUNT` / `RATE_ALPHA0` / `RATE_ALPHA_MIN`). Those symbols do NOT
exist yet, so every test here is EXPECTED TO FAIL with ``AttributeError`` when it
touches ``learn.<new_symbol>``. This is the failing-test step of the TDD loop —
do NOT implement the new symbols here (that is Task 5.2).

`learning.py` already exists (Task 4), so the module loads fine; failures are
``AttributeError`` for the missing names, NOT import/logic errors. We load the
module standalone by absolute path as ``hvo_learning`` (mirroring
tests/test_learning_effectiveness.py / test_dab.py / test_context.py) so these
tests need no Home Assistant stubs and never import the package ``__init__``.

====================================================================
PINNED CONTRACT (for Task 5.2 to implement)
====================================================================
Why this exists — the R11 reachability fix
-------------------------------------------
Today the coordinator gates regime selection on a *normalized weight*
(`EFF_REGIME_CONFIDENCE = 0.50`). With `EFF_REGIME_COUNT = 4` regimes the
softmax-style weights sum to 1.0 and can essentially never push a single regime
past 0.50, so the regime branch is effectively unreachable and learning collapses
to the baseline (the R11 bug). The fix replaces that weight gate with a simple,
reachable **sample-count** gate: a regime cell is trusted once it has seen
`REGIME_MIN_N` samples.

Module constants:
    EFF_REGIME_COUNT = 4      # 4 regimes, matches context.py [day-mild,day-hot,night-mild,night-hot]
    REGIME_MIN_N     = 5      # samples a regime cell needs before its rate is trusted (R11 gate)
    RATE_MIN         = 0.0    # rates are temp-change °C/min and are never negative
    RATE_MAX         = 2.0    # generous upper clamp on a learned rate
    RATE_ALPHA0      = 0.10   # adaptive-alpha numerator (mirrors coordinator EFF_ALPHA0)
    RATE_ALPHA_MIN   = 0.01   # adaptive-alpha floor    (mirrors coordinator EFF_ALPHA_MIN)

Types (mutable dataclasses; per persistence schema v2):
    RegimeCell:        rate: float = 0.0 ; n: int = 0
    ModeEfficiency:    baseline: float | None = None ; n: int = 0
                       regimes: list[RegimeCell]   # length EFF_REGIME_COUNT
    RoomEfficiencyModel: cooling: ModeEfficiency ; heating: ModeEfficiency
        -> cooling and heating are fully independent sub-models (R25.1 dual index)

Factory:
    new_room_model() -> RoomEfficiencyModel
        Fresh model: each mode has baseline=None, n=0 and EFF_REGIME_COUNT
        zeroed RegimeCell()s.

Functions / signatures:
    update_room_efficiency(model, sample, regime_idx, mode="cooling") -> RoomEfficiencyModel
        - Ignores a non-finite (NaN/inf) or None sample: model is unchanged (R25.6 robustness).
        - Clamps a negative finite sample to 0.0 before use (rates never negative).
        - regime_idx is clamped into [0, EFF_REGIME_COUNT-1].
        - Updates ONLY the chosen `mode` sub-model; the other mode is untouched (R25.1).
        - Adaptive-alpha EMA, applied to BOTH the mode baseline and the selected
          regime cell, each using ITS OWN running count:
              n        = count + 1
              alpha    = max(RATE_ALPHA_MIN, RATE_ALPHA0 / sqrt(n))
              value    = sample           if value is None  (first sample seeds it)
              value    = value + alpha * (sample - value)
          The baseline count and each regime cell's count advance independently
          (baseline advances on every update; a cell advances only when selected).
          Asymmetric seeds in the regime cells are retained (R11.2).
        - Mutates `model` in place and also returns it.

    effective_rate(model, regime_idx, mode="cooling") -> float
        cell = model.<mode>.regimes[clamp(regime_idx)]
        if cell.n >= REGIME_MIN_N and cell.rate > 0:        # reachable gate (R11.1/11.3)
            return clamp(cell.rate, RATE_MIN, RATE_MAX)
        return clamp(model.<mode>.baseline or 0.0, RATE_MIN, RATE_MAX)

Worked adaptive-EMA numeric example (cooling baseline AND regime 0 cell):
    m = new_room_model()
    update_room_efficiency(m, 0.10, 0)      # first sample seeds value
        baseline -> 0.10 (n=1) ; regimes[0] -> rate=0.10, n=1
    update_room_efficiency(m, 0.20, 0)      # second sample
        n=2 ; alpha = max(0.01, 0.10/sqrt(2)) = 0.0707106781...
        baseline = 0.10 + 0.0707106781*(0.20 - 0.10) = 0.1070710678...
        regimes[0].rate identical (same alpha, same start) = 0.1070710678...
        baseline.n = 2 ; regimes[0].n = 2
====================================================================
"""
from __future__ import annotations

import importlib.util as _importlib_util
import math
import pathlib as _pathlib
import sys as _sys

import pytest

_LEARNING_PATH = _pathlib.Path(__file__).resolve().parent.parent / "custom_components" / "hvac_vent_optimizer" / "learning.py"


def _load_learning():
    """Load the pure `learning.py` module by absolute path as ``hvo_learning``."""
    spec = _importlib_util.spec_from_file_location("hvo_learning", _LEARNING_PATH)
    mod = _importlib_util.module_from_spec(spec)
    # Register before exec so dataclass annotation introspection can resolve it.
    _sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def learn():
    return _load_learning()


def _expected_alpha(learn, n: int) -> float:
    """The adaptive alpha the contract pins for the n-th sample."""
    return max(learn.RATE_ALPHA_MIN, learn.RATE_ALPHA0 / math.sqrt(n))


# ---------------------------------------------------------------------------
# Module constants (concrete contract pinned by the tests)
# ---------------------------------------------------------------------------
def test_constants_have_expected_concrete_values(learn):
    assert learn.EFF_REGIME_COUNT == 4          # matches context.py 4-regime mapping
    assert learn.REGIME_MIN_N == 5
    assert learn.RATE_MIN == pytest.approx(0.0)
    assert learn.RATE_MAX == pytest.approx(2.0)
    assert learn.RATE_ALPHA0 == pytest.approx(0.10)
    assert learn.RATE_ALPHA_MIN == pytest.approx(0.01)


def test_rate_band_is_well_formed(learn):
    assert learn.RATE_MIN <= learn.RATE_MAX
    assert learn.RATE_MIN >= 0.0


# ---------------------------------------------------------------------------
# Model construction
# ---------------------------------------------------------------------------
def test_new_model_has_two_independent_modes_with_regime_cells(learn):
    m = learn.new_room_model()
    for mode in ("cooling", "heating"):
        sub = getattr(m, mode)
        assert sub.baseline is None
        assert sub.n == 0
        assert len(sub.regimes) == learn.EFF_REGIME_COUNT
        for cell in sub.regimes:
            assert cell.rate == pytest.approx(0.0)
            assert cell.n == 0
    # cooling and heating must be distinct objects, not the same list aliased
    assert m.cooling is not m.heating
    assert m.cooling.regimes is not m.heating.regimes


# ---------------------------------------------------------------------------
# Independence of cooling vs heating baselines (R25.1)
# ---------------------------------------------------------------------------
def test_cooling_and_heating_update_independently(learn):
    m = learn.new_room_model()
    learn.update_room_efficiency(m, 0.30, 0, mode="cooling")
    # heating untouched by a cooling update
    assert m.heating.baseline is None
    assert m.heating.n == 0
    assert all(c.n == 0 for c in m.heating.regimes)
    # cooling reflects the sample
    assert m.cooling.baseline == pytest.approx(0.30)
    assert m.cooling.n == 1

    learn.update_room_efficiency(m, 0.80, 0, mode="heating")
    # the two baselines are independent values
    assert m.cooling.baseline == pytest.approx(0.30)
    assert m.heating.baseline == pytest.approx(0.80)
    assert m.cooling.n == 1
    assert m.heating.n == 1


# ---------------------------------------------------------------------------
# Adaptive-alpha EMA worked example (R25.1 baseline + regime cell)
# ---------------------------------------------------------------------------
def test_first_sample_seeds_baseline_and_regime_cell(learn):
    m = learn.new_room_model()
    learn.update_room_efficiency(m, 0.10, 0, mode="cooling")
    assert m.cooling.baseline == pytest.approx(0.10)
    assert m.cooling.n == 1
    assert m.cooling.regimes[0].rate == pytest.approx(0.10)
    assert m.cooling.regimes[0].n == 1


def test_adaptive_alpha_ema_worked_example(learn):
    m = learn.new_room_model()
    learn.update_room_efficiency(m, 0.10, 0, mode="cooling")
    learn.update_room_efficiency(m, 0.20, 0, mode="cooling")

    alpha2 = _expected_alpha(learn, 2)  # max(0.01, 0.10/sqrt(2)) = 0.07071...
    expected = 0.10 + alpha2 * (0.20 - 0.10)

    assert m.cooling.baseline == pytest.approx(expected, abs=1e-9)
    assert m.cooling.regimes[0].rate == pytest.approx(expected, abs=1e-9)
    assert m.cooling.n == 2
    assert m.cooling.regimes[0].n == 2
    # genuine EMA: it moves toward the new sample but does not jump all the way
    assert 0.10 < m.cooling.baseline < 0.20


def test_alpha_decreases_with_sample_count(learn):
    # Confirms the "adaptive" part: later samples move the estimate less.
    a2 = _expected_alpha(learn, 2)
    a10 = _expected_alpha(learn, 10)
    assert a10 < a2


# ---------------------------------------------------------------------------
# Each regime cell keeps its own EMA + sample count (R11.2)
# ---------------------------------------------------------------------------
def test_each_regime_cell_tracks_its_own_count(learn):
    m = learn.new_room_model()
    # regime 0 gets 3 samples, regime 1 gets 1 sample
    learn.update_room_efficiency(m, 0.10, 0, mode="cooling")
    learn.update_room_efficiency(m, 0.10, 0, mode="cooling")
    learn.update_room_efficiency(m, 0.10, 0, mode="cooling")
    learn.update_room_efficiency(m, 0.40, 1, mode="cooling")

    assert m.cooling.regimes[0].n == 3
    assert m.cooling.regimes[1].n == 1
    # untouched regimes keep n == 0
    assert m.cooling.regimes[2].n == 0
    assert m.cooling.regimes[3].n == 0
    # baseline advances on EVERY update regardless of regime
    assert m.cooling.n == 4


# ---------------------------------------------------------------------------
# effective_rate: sample-count gate (R11.1 / R11.3 reachability fix)
# ---------------------------------------------------------------------------
def test_effective_rate_falls_back_to_baseline_below_threshold(learn):
    m = learn.new_room_model()
    # Feed enough samples to set a baseline, but keep the regime cell below threshold.
    for _ in range(REGIME_BELOW := learn.REGIME_MIN_N - 1):
        learn.update_room_efficiency(m, 0.25, 0, mode="cooling")
    assert m.cooling.regimes[0].n == REGIME_BELOW
    assert m.cooling.regimes[0].n < learn.REGIME_MIN_N
    # below threshold -> returns the baseline, not the cell rate
    assert learn.effective_rate(m, 0, mode="cooling") == pytest.approx(
        max(learn.RATE_MIN, min(learn.RATE_MAX, m.cooling.baseline))
    )


def test_effective_rate_uses_cell_rate_at_threshold(learn):
    m = learn.new_room_model()
    for _ in range(learn.REGIME_MIN_N):
        learn.update_room_efficiency(m, 0.25, 0, mode="cooling")
    assert m.cooling.regimes[0].n == learn.REGIME_MIN_N
    # at/above threshold and rate > 0 -> returns the regime cell rate
    assert learn.effective_rate(m, 0, mode="cooling") == pytest.approx(
        m.cooling.regimes[0].rate
    )


def test_effective_rate_zero_cell_rate_falls_back_even_when_n_high(learn):
    # A cell that reached the count gate but learned a zero/non-positive rate
    # must still fall back to the baseline (cell.rate > 0 part of the gate).
    m = learn.new_room_model()
    m.cooling.baseline = 0.30
    m.cooling.n = 50
    m.cooling.regimes[0].rate = 0.0
    m.cooling.regimes[0].n = learn.REGIME_MIN_N + 5
    assert learn.effective_rate(m, 0, mode="cooling") == pytest.approx(0.30)


# ---------------------------------------------------------------------------
# Asymmetric-seeded regimes diverge over samples (R11.2)
# ---------------------------------------------------------------------------
def test_asymmetric_regimes_diverge(learn):
    m = learn.new_room_model()
    # Two regimes fed distinctly different sample streams (past the count gate)
    # must end up with clearly different effective rates.
    for _ in range(learn.REGIME_MIN_N + 3):
        learn.update_room_efficiency(m, 0.10, 0, mode="cooling")  # "slow" regime
        learn.update_room_efficiency(m, 0.45, 1, mode="cooling")  # "fast" regime

    r0 = learn.effective_rate(m, 0, mode="cooling")
    r1 = learn.effective_rate(m, 1, mode="cooling")
    assert r0 < r1
    assert r1 - r0 > 0.1            # genuinely diverged, not noise
    # both remain within the clamp band
    assert learn.RATE_MIN <= r0 <= learn.RATE_MAX
    assert learn.RATE_MIN <= r1 <= learn.RATE_MAX


def test_preseeded_regimes_specialize_under_updates(learn):
    # Seed cells asymmetrically, then nudge each toward a target; the seeded
    # ordering is retained (R11.2) rather than being washed out to one value.
    m = learn.new_room_model()
    m.cooling.baseline = 0.20
    m.cooling.n = 10
    m.cooling.regimes[0].rate, m.cooling.regimes[0].n = 0.05, learn.REGIME_MIN_N
    m.cooling.regimes[1].rate, m.cooling.regimes[1].n = 0.50, learn.REGIME_MIN_N
    before = (
        learn.effective_rate(m, 0, mode="cooling"),
        learn.effective_rate(m, 1, mode="cooling"),
    )
    learn.update_room_efficiency(m, 0.05, 0, mode="cooling")
    learn.update_room_efficiency(m, 0.50, 1, mode="cooling")
    after = (
        learn.effective_rate(m, 0, mode="cooling"),
        learn.effective_rate(m, 1, mode="cooling"),
    )
    assert before[0] < before[1]
    assert after[0] < after[1]


# ---------------------------------------------------------------------------
# Clamping to [RATE_MIN, RATE_MAX] (R25.6)
# ---------------------------------------------------------------------------
def test_effective_rate_clamps_cell_rate_to_rate_max(learn):
    m = learn.new_room_model()
    m.cooling.baseline = 0.30
    m.cooling.n = 10
    m.cooling.regimes[0].rate = 5.0                 # absurdly high
    m.cooling.regimes[0].n = learn.REGIME_MIN_N
    assert learn.effective_rate(m, 0, mode="cooling") == pytest.approx(learn.RATE_MAX)


def test_effective_rate_clamps_negative_baseline_to_rate_min(learn):
    m = learn.new_room_model()
    m.cooling.baseline = -3.0                       # degenerate negative
    m.cooling.n = 10
    # regime cell below threshold -> fall back to baseline -> clamped to RATE_MIN
    assert learn.effective_rate(m, 0, mode="cooling") == pytest.approx(learn.RATE_MIN)


def test_effective_rate_none_baseline_returns_rate_min(learn):
    m = learn.new_room_model()  # untouched: baseline None, all cells empty
    assert learn.effective_rate(m, 0, mode="cooling") == pytest.approx(learn.RATE_MIN)


# ---------------------------------------------------------------------------
# Robustness: NaN / None / negative / noisy samples (R25.6)
# ---------------------------------------------------------------------------
def test_nan_sample_is_ignored(learn):
    m = learn.new_room_model()
    learn.update_room_efficiency(m, 0.30, 0, mode="cooling")
    learn.update_room_efficiency(m, float("nan"), 0, mode="cooling")
    assert m.cooling.baseline == pytest.approx(0.30)
    assert m.cooling.n == 1
    assert m.cooling.regimes[0].n == 1


def test_inf_sample_is_ignored(learn):
    m = learn.new_room_model()
    learn.update_room_efficiency(m, 0.30, 0, mode="cooling")
    learn.update_room_efficiency(m, float("inf"), 0, mode="cooling")
    assert m.cooling.baseline == pytest.approx(0.30)
    assert m.cooling.n == 1


def test_none_sample_is_ignored(learn):
    m = learn.new_room_model()
    learn.update_room_efficiency(m, 0.30, 0, mode="cooling")
    learn.update_room_efficiency(m, None, 0, mode="cooling")
    assert m.cooling.baseline == pytest.approx(0.30)
    assert m.cooling.n == 1


def test_negative_sample_clamped_non_negative(learn):
    m = learn.new_room_model()
    learn.update_room_efficiency(m, -0.50, 0, mode="cooling")
    assert m.cooling.baseline is not None
    assert m.cooling.baseline >= 0.0


def test_noisy_stream_stays_bounded_and_non_negative(learn):
    m = learn.new_room_model()
    noisy = [0.10, 0.40, -0.20, 0.05, 0.90, 0.15, 0.30, 0.02, 0.50, 0.20, 0.33, 0.18]
    for i, s in enumerate(noisy):
        learn.update_room_efficiency(m, s, i % learn.EFF_REGIME_COUNT, mode="cooling")
    for idx in range(learn.EFF_REGIME_COUNT):
        r = learn.effective_rate(m, idx, mode="cooling")
        assert math.isfinite(r)
        assert learn.RATE_MIN <= r <= learn.RATE_MAX
    assert m.cooling.baseline is not None
    assert math.isfinite(m.cooling.baseline)
    assert m.cooling.baseline >= 0.0


# ---------------------------------------------------------------------------
# Out-of-range regime index is handled safely (no crash)
# ---------------------------------------------------------------------------
def test_out_of_range_regime_index_is_clamped(learn):
    m = learn.new_room_model()
    # high and negative indices must not raise
    learn.update_room_efficiency(m, 0.30, 99, mode="cooling")
    learn.update_room_efficiency(m, 0.30, -5, mode="cooling")
    r_hi = learn.effective_rate(m, 99, mode="cooling")
    r_lo = learn.effective_rate(m, -5, mode="cooling")
    assert learn.RATE_MIN <= r_hi <= learn.RATE_MAX
    assert learn.RATE_MIN <= r_lo <= learn.RATE_MAX


# ---------------------------------------------------------------------------
# update returns the model for chaining
# ---------------------------------------------------------------------------
def test_update_returns_same_model(learn):
    m = learn.new_room_model()
    out = learn.update_room_efficiency(m, 0.30, 0, mode="cooling")
    assert out is m
