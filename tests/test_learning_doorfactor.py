"""Tests-first (Task 1.1) for the door-factor model in pure ``learning.py``.

Validates: Requirements 26.2, 26.4, 28.1

These assert the contract of NEW symbols to be added to ``learning.py`` by Task
1.2: the constants ``DOOR_FACTOR_MIN`` / ``DOOR_FACTOR_MAX`` /
``DOOR_FACTOR_DEFAULT`` / ``DOOR_MIN_N``, the ``DoorFactorCell`` /
``DoorFactorModel`` dataclasses, and the ``new_door_factor_model()`` factory.
Those symbols do NOT exist yet, so every test here is EXPECTED TO FAIL with
``AttributeError`` when it touches ``learn.<new_symbol>``. This is the
failing-test step of the TDD loop — do NOT implement the new symbols here (that
is Task 1.2).

``learning.py`` already exists, so the module loads fine; failures are
``AttributeError`` for the missing names, NOT import/logic errors. We load the
module standalone by absolute path as ``hvo_learning`` (mirroring
tests/test_learning_rooms.py) so these tests need no Home Assistant stubs and
never import the package ``__init__``.

====================================================================
PINNED CONTRACT (for Task 1.2 to implement)
====================================================================
Module constants:
    DOOR_FACTOR_MIN     = 0.5   # lower clamp; open door can only slow conditioning
    DOOR_FACTOR_MAX     = 1.0   # upper clamp; an open door never speeds conditioning
    DOOR_FACTOR_DEFAULT = 0.9   # legacy constant; cold-start fallback (== old DOOR_FACTOR)
    DOOR_MIN_N          = 5     # confidence gate; mirrors REGIME_MIN_N

Types (mutable dataclasses; per persistence schema):
    DoorFactorCell:   factor: float | None = None ; n: int = 0
    DoorFactorModel:  cooling: DoorFactorCell ; heating: DoorFactorCell
        -> cooling and heating are fully independent cells (R27.1 per-mode)

Factory:
    new_door_factor_model() -> DoorFactorModel
        Fresh model: each mode is a DoorFactorCell with factor=None, n=0, and
        the two cells are distinct (non-aliased) objects.
====================================================================
"""

from __future__ import annotations

import importlib.util as _importlib_util
import math
import pathlib as _pathlib
import sys as _sys

import pytest

_LEARNING_PATH = (
    _pathlib.Path(__file__).resolve().parent.parent
    / "custom_components"
    / "hvac_vent_optimizer"
    / "learning.py"
)


def _load_learning():
    """Load the pure ``learning.py`` module by absolute path as ``hvo_learning``."""
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
# Module constants (concrete contract pinned by the tests) — R28.1
# ---------------------------------------------------------------------------
def test_door_factor_constants_have_expected_concrete_values(learn):
    assert learn.DOOR_FACTOR_MIN == pytest.approx(0.5)
    assert learn.DOOR_FACTOR_MAX == pytest.approx(1.0)
    assert learn.DOOR_FACTOR_DEFAULT == pytest.approx(0.9)
    assert learn.DOOR_MIN_N == 5


def test_door_factor_band_is_well_formed(learn):
    # The clamp band must be sane and the legacy default must sit inside it.
    assert learn.DOOR_FACTOR_MIN < learn.DOOR_FACTOR_MAX
    assert learn.DOOR_FACTOR_MIN <= learn.DOOR_FACTOR_DEFAULT <= learn.DOOR_FACTOR_MAX
    assert learn.DOOR_MIN_N > 0


# ---------------------------------------------------------------------------
# Model construction — R26.2 / R26.4
# ---------------------------------------------------------------------------
def test_new_door_factor_model_has_two_fresh_cells(learn):
    m = learn.new_door_factor_model()
    for mode in ("cooling", "heating"):
        cell = getattr(m, mode)
        assert cell.factor is None
        assert cell.n == 0


def test_new_door_factor_model_cells_are_distinct_objects(learn):
    # cooling and heating must be distinct objects, not the same cell aliased,
    # so a per-mode update never bleeds into the other mode (R27.1).
    m = learn.new_door_factor_model()
    assert m.cooling is not m.heating


def test_new_door_factor_model_instances_are_independent(learn):
    # Two freshly built models must not share cell objects either.
    m1 = learn.new_door_factor_model()
    m2 = learn.new_door_factor_model()
    assert m1.cooling is not m2.cooling
    assert m1.heating is not m2.heating


# ===========================================================================
# Tests-first (Task 2.1) for update_door_factor(model, ratio, mode="cooling")
# ===========================================================================
# Validates: Requirements 26.3, 28.2, 28.3, 28.5
#
# These pin the contract of the NEW function ``update_door_factor`` to be added
# to ``learning.py`` by Task 2.2. The function does NOT exist yet, so every test
# below is EXPECTED TO FAIL with ``AttributeError`` when it touches
# ``learn.update_door_factor``. This is the failing-test (red) step of the TDD
# loop — do NOT implement the function here (that is Task 2.2).
#
# ------------------------------------------------------------------------
# PINNED CONTRACT (for Task 2.2 to implement) — mirrors update_room_efficiency
# ------------------------------------------------------------------------
#   update_door_factor(model, ratio, mode="cooling") -> DoorFactorModel
#
#   * Operates on the already-computed residual ``ratio`` (sample / reference);
#     the coordinator forms the ratio, this function only learns it.
#   * Robustness (R28.3): a ``None`` or non-finite (NaN/+inf/-inf) ``ratio``
#     leaves the model completely untouched — no factor change, no ``n``
#     increment, on either a fresh or an already-seeded cell.
#   * Clamp-before-EMA (R28.2): the ratio is clamped into
#     ``[DOOR_FACTOR_MIN, DOOR_FACTOR_MAX]`` BEFORE the EMA step (a ratio > 1.0
#     becomes 1.0; a ratio < 0.5 becomes 0.5).
#   * Seed then EMA (R26.3): the first valid sample seeds the cell outright
#     (``factor == clamp(ratio)``, ``n == 1``); thereafter the cell moves toward
#     the clamped ratio via ``alpha = max(RATE_ALPHA_MIN, RATE_ALPHA0/sqrt(n))``
#     with ``n`` incremented BEFORE alpha is computed (mirrors the room-learner's
#     regime cell update). Stays in band for all valid streams (R28.5).
#   * Only the passed ``mode`` cell advances; the other mode is untouched.
#   * Mutates in place AND returns the model (mirrors ``update_room_efficiency``).
# ------------------------------------------------------------------------


def _expected_ema(seed: float, ratios: list[float], learn) -> float:
    """Reference EMA the implementation must match (clamp-before-EMA + seed).

    Mirrors the regime-cell update in ``update_room_efficiency``: the first
    sample seeds outright, then each later sample increments ``n`` and blends
    with ``alpha = max(RATE_ALPHA_MIN, RATE_ALPHA0 / sqrt(n))``. Every sample is
    clamped into ``[DOOR_FACTOR_MIN, DOOR_FACTOR_MAX]`` first.
    """

    def clamp(x: float) -> float:
        return max(learn.DOOR_FACTOR_MIN, min(learn.DOOR_FACTOR_MAX, x))

    factor = clamp(seed)
    n = 1
    for r in ratios:
        n += 1
        alpha = max(learn.RATE_ALPHA_MIN, learn.RATE_ALPHA0 / math.sqrt(n))
        factor = factor + alpha * (clamp(r) - factor)
    return factor


# ---------------------------------------------------------------------------
# Seeding: the first valid ratio sets the cell outright — R26.3
# ---------------------------------------------------------------------------
def test_first_valid_ratio_seeds_cell_and_returns_model(learn):
    m = learn.new_door_factor_model()
    out = learn.update_door_factor(m, 0.7, mode="cooling")
    # Mutates in place and returns the same model object (chaining contract).
    assert out is m
    assert m.cooling.factor == pytest.approx(0.7)
    assert m.cooling.n == 1


def test_default_mode_is_cooling(learn):
    m = learn.new_door_factor_model()
    learn.update_door_factor(m, 0.7)  # no mode kwarg -> cooling
    assert m.cooling.n == 1
    assert m.cooling.factor == pytest.approx(0.7)
    assert m.heating.n == 0
    assert m.heating.factor is None


# ---------------------------------------------------------------------------
# Subsequent samples move toward the ratio via adaptive alpha — R26.3 / R28.5
# ---------------------------------------------------------------------------
def test_second_sample_emas_toward_ratio_with_sqrt2_alpha(learn):
    m = learn.new_door_factor_model()
    learn.update_door_factor(m, 0.8, mode="cooling")  # seed -> 0.8, n=1
    learn.update_door_factor(m, 0.6, mode="cooling")  # n=2

    # alpha = max(0.01, 0.10/sqrt(2)) = 0.0707106781...
    # factor = 0.8 + alpha*(0.6 - 0.8) = 0.785857864...
    expected = _expected_ema(0.8, [0.6], learn)
    assert m.cooling.factor == pytest.approx(expected)
    assert m.cooling.factor == pytest.approx(0.785857864376269)
    assert m.cooling.n == 2


def test_alpha_tracks_sample_count_over_three_samples(learn):
    m = learn.new_door_factor_model()
    for r in (0.9, 0.7, 0.6):
        learn.update_door_factor(m, r, mode="cooling")
    # step1 seeds 0.9 (n=1); step2 uses sqrt(2); step3 uses sqrt(3).
    expected = _expected_ema(0.9, [0.7, 0.6], learn)
    assert m.cooling.factor == pytest.approx(expected)
    assert m.cooling.n == 3


# ---------------------------------------------------------------------------
# Clamp the ratio into [0.5, 1.0] BEFORE the EMA step — R28.2
# ---------------------------------------------------------------------------
def test_seed_ratio_above_max_clamps_to_one(learn):
    m = learn.new_door_factor_model()
    learn.update_door_factor(m, 1.4, mode="cooling")
    assert m.cooling.factor == pytest.approx(learn.DOOR_FACTOR_MAX)  # 1.0
    assert m.cooling.n == 1


def test_seed_ratio_below_min_clamps_to_half(learn):
    m = learn.new_door_factor_model()
    learn.update_door_factor(m, 0.2, mode="cooling")
    assert m.cooling.factor == pytest.approx(learn.DOOR_FACTOR_MIN)  # 0.5
    assert m.cooling.n == 1


def test_above_max_ratio_is_clamped_before_ema_step(learn):
    m = learn.new_door_factor_model()
    learn.update_door_factor(m, 0.6, mode="cooling")  # seed -> 0.6, n=1
    learn.update_door_factor(m, 1.4, mode="cooling")  # ratio clamps to 1.0 BEFORE ema

    expected = _expected_ema(0.6, [1.4], learn)  # target is clamp(1.4)=1.0
    assert m.cooling.factor == pytest.approx(expected)
    # The clamp must happen before the blend, so the result differs from blending
    # toward the raw 1.4, and never escapes the upper bound.
    alpha = max(learn.RATE_ALPHA_MIN, learn.RATE_ALPHA0 / math.sqrt(2))
    unclamped = 0.6 + alpha * (1.4 - 0.6)
    assert m.cooling.factor != pytest.approx(unclamped)
    assert m.cooling.factor <= learn.DOOR_FACTOR_MAX


def test_below_min_ratio_is_clamped_before_ema_step(learn):
    m = learn.new_door_factor_model()
    learn.update_door_factor(m, 0.9, mode="cooling")  # seed -> 0.9, n=1
    learn.update_door_factor(m, 0.2, mode="cooling")  # ratio clamps to 0.5 BEFORE ema

    expected = _expected_ema(0.9, [0.2], learn)  # target is clamp(0.2)=0.5
    assert m.cooling.factor == pytest.approx(expected)
    assert m.cooling.factor >= learn.DOOR_FACTOR_MIN


# ---------------------------------------------------------------------------
# None / NaN / inf leave the cell unchanged, no n increment — R28.3
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("bad", [None, float("nan"), float("inf"), float("-inf")])
def test_invalid_ratio_leaves_fresh_cell_unchanged(learn, bad):
    m = learn.new_door_factor_model()
    out = learn.update_door_factor(m, bad, mode="cooling")
    assert out is m
    assert m.cooling.factor is None
    assert m.cooling.n == 0
    # The other mode is untouched too.
    assert m.heating.factor is None
    assert m.heating.n == 0


@pytest.mark.parametrize("bad", [None, float("nan"), float("inf"), float("-inf")])
def test_invalid_ratio_leaves_seeded_cell_unchanged(learn, bad):
    m = learn.new_door_factor_model()
    learn.update_door_factor(m, 0.7, mode="cooling")  # seed -> 0.7, n=1
    learn.update_door_factor(m, bad, mode="cooling")  # no-op
    assert m.cooling.factor == pytest.approx(0.7)
    assert m.cooling.n == 1


# ---------------------------------------------------------------------------
# Only the passed mode advances; the other mode is untouched — R27.1
# ---------------------------------------------------------------------------
def test_only_cooling_advances_when_cooling_passed(learn):
    m = learn.new_door_factor_model()
    learn.update_door_factor(m, 0.7, mode="cooling")
    assert m.cooling.factor == pytest.approx(0.7)
    assert m.cooling.n == 1
    assert m.heating.factor is None
    assert m.heating.n == 0


def test_only_heating_advances_when_heating_passed(learn):
    m = learn.new_door_factor_model()
    learn.update_door_factor(m, 0.65, mode="heating")
    assert m.heating.factor == pytest.approx(0.65)
    assert m.heating.n == 1
    assert m.cooling.factor is None
    assert m.cooling.n == 0


def test_two_modes_learn_independently(learn):
    m = learn.new_door_factor_model()
    learn.update_door_factor(m, 0.8, mode="cooling")
    learn.update_door_factor(m, 0.6, mode="heating")
    assert m.cooling.factor == pytest.approx(0.8)
    assert m.cooling.n == 1
    assert m.heating.factor == pytest.approx(0.6)
    assert m.heating.n == 1


# ---------------------------------------------------------------------------
# Property-style sanity: repeated identical ratios converge — R28.5
# ---------------------------------------------------------------------------
def test_repeated_identical_ratios_converge_to_that_ratio(learn):
    m = learn.new_door_factor_model()
    # Seed away from the target, then feed the same ratio many times: the EMA
    # must converge to that ratio and stay inside the band (never divergent).
    learn.update_door_factor(m, 1.0, mode="cooling")
    for _ in range(2000):
        learn.update_door_factor(m, 0.6, mode="cooling")
    assert m.cooling.factor == pytest.approx(0.6, abs=1e-3)
    assert learn.DOOR_FACTOR_MIN <= m.cooling.factor <= learn.DOOR_FACTOR_MAX


def test_identical_ratios_from_the_start_stay_exact(learn):
    m = learn.new_door_factor_model()
    for _ in range(50):
        learn.update_door_factor(m, 0.65, mode="cooling")
    # Seeded at 0.65 then every blend targets 0.65, so it never moves off it.
    assert m.cooling.factor == pytest.approx(0.65)
    assert m.cooling.n == 50


# ===========================================================================
# Tests-first (Task 3.1) for
#   resolve_door_factor(model, mode="cooling", *, default=DOOR_FACTOR_DEFAULT)
# ===========================================================================
# Validates: Requirements 27.1, 27.2, 27.3, 27.4, 28.1
#
# These pin the contract of the NEW function ``resolve_door_factor`` to be added
# to ``learning.py`` by Task 3.2. The function does NOT exist yet, so every test
# below is EXPECTED TO FAIL with ``AttributeError`` when it touches
# ``learn.resolve_door_factor``. This is the failing-test (red) step of the TDD
# loop — do NOT implement the function here (that is Task 3.2).
#
# ------------------------------------------------------------------------
# PINNED CONTRACT (for Task 3.2 to implement) — fallback chain D12 / A7 Resolve
# ------------------------------------------------------------------------
#   resolve_door_factor(model, mode="cooling", *, default=DOOR_FACTOR_DEFAULT)
#       -> float
#
#   Fallback chain (R27.2 / D12), result ALWAYS clamped to
#   ``[DOOR_FACTOR_MIN, DOOR_FACTOR_MAX]`` (R28.1):
#     1. requested-mode cell trusted (``n >= DOOR_MIN_N`` AND ``factor`` present)
#        -> ``clamp(cell.factor)``.
#     2. else other-mode cell trusted -> ``clamp(other.factor)`` (cross-mode).
#     3. else -> ``default`` (== ``DOOR_FACTOR_DEFAULT`` == 0.9).
#   * A ``None`` model resolves to ``default`` (R27.4 cold install).
#   * Resolution is read-only: it never mutates the model.
#   * Per-mode independence (R27.1/R27.3): a cold/noisy cell for one mode never
#     drags a trusted cell of the other mode, except via the explicit cross-mode
#     fallback in step 2.
# ------------------------------------------------------------------------


def _trusted_cell(learn, factor: float, *, extra: int = 0):
    """Build a trusted ``DoorFactorCell`` (``n >= DOOR_MIN_N``, factor present)."""
    return learn.DoorFactorCell(factor=factor, n=learn.DOOR_MIN_N + extra)


# ---------------------------------------------------------------------------
# (a) trusted requested-mode cell -> its clamped factor — R27.2(a)
# ---------------------------------------------------------------------------
def test_trusted_requested_mode_cell_resolves_to_its_factor(learn):
    m = learn.new_door_factor_model()
    m.cooling = _trusted_cell(learn, 0.7)
    assert learn.resolve_door_factor(m, mode="cooling") == pytest.approx(0.7)


def test_trusted_requested_mode_cell_resolves_for_heating(learn):
    m = learn.new_door_factor_model()
    m.heating = _trusted_cell(learn, 0.62)
    assert learn.resolve_door_factor(m, mode="heating") == pytest.approx(0.62)


def test_requested_mode_trusted_at_exact_gate_boundary_is_trusted(learn):
    # n == DOOR_MIN_N is inclusive: the cell is trusted at the boundary.
    m = learn.new_door_factor_model()
    m.cooling = learn.DoorFactorCell(factor=0.66, n=learn.DOOR_MIN_N)
    assert learn.resolve_door_factor(m, mode="cooling") == pytest.approx(0.66)


def test_default_mode_is_cooling_for_resolution(learn):
    m = learn.new_door_factor_model()
    m.cooling = _trusted_cell(learn, 0.72)
    # no mode kwarg -> cooling
    assert learn.resolve_door_factor(m) == pytest.approx(0.72)


# ---------------------------------------------------------------------------
# (b) requested mode cold but other mode trusted -> other mode's clamped factor
#     — R27.2(b) cross-mode fallback
# ---------------------------------------------------------------------------
def test_cold_cooling_falls_back_to_trusted_heating(learn):
    m = learn.new_door_factor_model()
    # cooling stays cold (fresh); heating is trusted.
    m.heating = _trusted_cell(learn, 0.6)
    assert learn.resolve_door_factor(m, mode="cooling") == pytest.approx(0.6)


def test_cold_heating_falls_back_to_trusted_cooling(learn):
    m = learn.new_door_factor_model()
    m.cooling = _trusted_cell(learn, 0.55)
    assert learn.resolve_door_factor(m, mode="heating") == pytest.approx(0.55)


def test_requested_mode_below_gate_uses_other_mode_fallback(learn):
    # Requested cell has a factor but is below the gate (untrusted) -> cross-mode.
    m = learn.new_door_factor_model()
    m.cooling = learn.DoorFactorCell(factor=0.85, n=learn.DOOR_MIN_N - 1)
    m.heating = _trusted_cell(learn, 0.6)
    assert learn.resolve_door_factor(m, mode="cooling") == pytest.approx(0.6)


def test_requested_mode_trusted_count_but_no_factor_uses_other_mode(learn):
    # n >= gate but factor is None -> not trusted -> cross-mode fallback.
    m = learn.new_door_factor_model()
    m.cooling = learn.DoorFactorCell(factor=None, n=learn.DOOR_MIN_N)
    m.heating = _trusted_cell(learn, 0.58)
    assert learn.resolve_door_factor(m, mode="cooling") == pytest.approx(0.58)


# ---------------------------------------------------------------------------
# (c) neither trusted -> 0.9 — R27.2(c)
# ---------------------------------------------------------------------------
def test_neither_mode_trusted_resolves_to_default(learn):
    m = learn.new_door_factor_model()  # both cells fresh
    assert learn.resolve_door_factor(m, mode="cooling") == pytest.approx(learn.DOOR_FACTOR_DEFAULT)
    assert learn.resolve_door_factor(m, mode="heating") == pytest.approx(0.9)


def test_both_cells_below_gate_resolve_to_default(learn):
    m = learn.new_door_factor_model()
    m.cooling = learn.DoorFactorCell(factor=0.7, n=learn.DOOR_MIN_N - 1)
    m.heating = learn.DoorFactorCell(factor=0.6, n=learn.DOOR_MIN_N - 1)
    assert learn.resolve_door_factor(m, mode="cooling") == pytest.approx(0.9)


def test_explicit_default_override_is_used_on_cold_model(learn):
    m = learn.new_door_factor_model()
    assert learn.resolve_door_factor(m, mode="cooling", default=0.8) == pytest.approx(0.8)


# ---------------------------------------------------------------------------
# (d) None model -> 0.9 — R27.4 cold install
# ---------------------------------------------------------------------------
def test_none_model_resolves_to_default(learn):
    assert learn.resolve_door_factor(None, mode="cooling") == pytest.approx(0.9)
    assert learn.resolve_door_factor(None, mode="heating") == pytest.approx(learn.DOOR_FACTOR_DEFAULT)


def test_none_model_honors_explicit_default(learn):
    assert learn.resolve_door_factor(None, mode="cooling", default=0.75) == pytest.approx(0.75)


# ---------------------------------------------------------------------------
# (e) every return clamped to [0.5, 1.0] — R28.1
# ---------------------------------------------------------------------------
def test_stored_factor_above_max_resolves_clamped_to_one(learn):
    # A stored factor of 1.4 (out of band) must resolve to the upper clamp 1.0.
    m = learn.new_door_factor_model()
    m.cooling = _trusted_cell(learn, 1.4)
    assert learn.resolve_door_factor(m, mode="cooling") == pytest.approx(learn.DOOR_FACTOR_MAX)


def test_stored_factor_below_min_resolves_clamped_to_half(learn):
    m = learn.new_door_factor_model()
    m.cooling = _trusted_cell(learn, 0.3)
    assert learn.resolve_door_factor(m, mode="cooling") == pytest.approx(learn.DOOR_FACTOR_MIN)


def test_cross_mode_fallback_is_also_clamped(learn):
    # The cross-mode fallback value is clamped too, not just the requested cell.
    m = learn.new_door_factor_model()
    m.heating = _trusted_cell(learn, 1.4)
    assert learn.resolve_door_factor(m, mode="cooling") == pytest.approx(1.0)


def test_resolution_is_always_within_band(learn):
    # Whatever path is taken, the result stays inside [0.5, 1.0].
    for stored in (0.1, 0.5, 0.73, 1.0, 1.4):
        m = learn.new_door_factor_model()
        m.cooling = _trusted_cell(learn, stored)
        val = learn.resolve_door_factor(m, mode="cooling")
        assert learn.DOOR_FACTOR_MIN <= val <= learn.DOOR_FACTOR_MAX


# ---------------------------------------------------------------------------
# (f) a trusted cooling cell does NOT change a cold heating resolution beyond
#     the cross-mode fallback value — R27.1 / R27.3 per-mode independence
# ---------------------------------------------------------------------------
def test_trusted_cooling_only_affects_cold_heating_via_cross_mode_value(learn):
    m = learn.new_door_factor_model()
    m.cooling = _trusted_cell(learn, 0.7)
    # heating is cold: the ONLY influence cooling may have is the documented
    # cross-mode fallback value (clamp(0.7) == 0.7), nothing more.
    resolved_heating = learn.resolve_door_factor(m, mode="heating")
    assert resolved_heating == pytest.approx(0.7)
    # And cooling's own resolution is unaffected by the cold heating cell.
    assert learn.resolve_door_factor(m, mode="cooling") == pytest.approx(0.7)


def test_resolution_does_not_mutate_the_model(learn):
    # Resolution is read-only: a cold heating cell stays cold after resolving.
    m = learn.new_door_factor_model()
    m.cooling = _trusted_cell(learn, 0.7)
    learn.resolve_door_factor(m, mode="heating")
    assert m.heating.factor is None
    assert m.heating.n == 0
    # cooling cell is untouched too.
    assert m.cooling.factor == pytest.approx(0.7)
    assert m.cooling.n == learn.DOOR_MIN_N


def test_both_modes_trusted_each_resolves_to_its_own_factor(learn):
    # When both are trusted there is no cross-mode bleed: each keeps its own.
    m = learn.new_door_factor_model()
    m.cooling = _trusted_cell(learn, 0.7)
    m.heating = _trusted_cell(learn, 0.6)
    assert learn.resolve_door_factor(m, mode="cooling") == pytest.approx(0.7)
    assert learn.resolve_door_factor(m, mode="heating") == pytest.approx(0.6)


# ===========================================================================
# Tests-first (Task 4.1) for the persistence converters
#   door_factor_to_dict(model)   -> dict
#   door_factor_from_dict(data)  -> DoorFactorModel
# ===========================================================================
# Validates: Requirements 29.3
#
# These pin the contract of the NEW converters to be added to ``learning.py`` by
# Task 4.2. The functions do NOT exist yet, so every test below is EXPECTED TO
# FAIL with ``AttributeError`` when it touches ``learn.door_factor_to_dict`` /
# ``learn.door_factor_from_dict``. This is the failing-test (red) step of the
# TDD loop — do NOT implement the converters here (that is Task 4.2).
#
# ------------------------------------------------------------------------
# PINNED CONTRACT (for Task 4.2 to implement) — mirror _mode_to_dict /
# _mode_from_dict tolerance
# ------------------------------------------------------------------------
#   door_factor_to_dict(model) -> dict
#       Serialize a DoorFactorModel to its persisted dict shape:
#           {"cooling": {"factor": <float|None>, "n": <int>},
#            "heating": {"factor": <float|None>, "n": <int>}}
#       A fresh model serializes each mode to {"factor": None, "n": 0}.
#
#   door_factor_from_dict(data) -> DoorFactorModel
#       Deserialize; NEVER raises on bad input (R29.3):
#         * a non-dict ``data`` yields a fresh model (both cells factor=None,n=0);
#         * a missing mode key yields a fresh cell for that mode;
#         * a non-dict mode entry yields a fresh cell;
#         * a garbled/non-numeric ``factor`` -> None; a garbled ``n`` -> 0.
#
#   Round-trip: door_factor_from_dict(door_factor_to_dict(m)) == m for any model
#   (DoorFactorModel/DoorFactorCell are value-equal dataclasses).
# ------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# to_dict: fresh model serializes both modes to {"factor": None, "n": 0}
# ---------------------------------------------------------------------------
def test_to_dict_fresh_model_has_two_empty_cells(learn):
    m = learn.new_door_factor_model()
    assert learn.door_factor_to_dict(m) == {
        "cooling": {"factor": None, "n": 0},
        "heating": {"factor": None, "n": 0},
    }


def test_to_dict_learned_model_carries_factor_and_n(learn):
    m = learn.new_door_factor_model()
    m.cooling = learn.DoorFactorCell(factor=0.68, n=11)
    m.heating = learn.DoorFactorCell(factor=0.72, n=7)
    assert learn.door_factor_to_dict(m) == {
        "cooling": {"factor": pytest.approx(0.68), "n": 11},
        "heating": {"factor": pytest.approx(0.72), "n": 7},
    }


def test_to_dict_n_is_serialized_as_int(learn):
    m = learn.new_door_factor_model()
    m.cooling = learn.DoorFactorCell(factor=0.6, n=5)
    out = learn.door_factor_to_dict(m)
    assert isinstance(out["cooling"]["n"], int)
    assert isinstance(out["heating"]["n"], int)


# ---------------------------------------------------------------------------
# Round-trip: from_dict(to_dict(m)) == m  (value equality)
# ---------------------------------------------------------------------------
def test_round_trip_fresh_model_is_lossless(learn):
    m = learn.new_door_factor_model()
    assert learn.door_factor_from_dict(learn.door_factor_to_dict(m)) == m


def test_round_trip_learned_model_is_lossless(learn):
    m = learn.new_door_factor_model()
    m.cooling = learn.DoorFactorCell(factor=0.68, n=11)
    m.heating = learn.DoorFactorCell(factor=0.55, n=9)
    assert learn.door_factor_from_dict(learn.door_factor_to_dict(m)) == m


def test_round_trip_one_mode_learned_one_cold(learn):
    # Only cooling learned; heating stays a fresh cell. Round-trips exactly.
    m = learn.new_door_factor_model()
    m.cooling = learn.DoorFactorCell(factor=0.62, n=6)
    assert learn.door_factor_from_dict(learn.door_factor_to_dict(m)) == m


def test_round_trip_after_real_updates_is_lossless(learn):
    # Build a model through the actual learner, then confirm a persistence
    # round-trip reproduces it bit-for-bit (factor + n on both modes).
    m = learn.new_door_factor_model()
    for r in (0.9, 0.7, 0.65, 0.6, 0.6):
        learn.update_door_factor(m, r, mode="cooling")
    for r in (0.8, 0.75, 0.7):
        learn.update_door_factor(m, r, mode="heating")
    assert learn.door_factor_from_dict(learn.door_factor_to_dict(m)) == m


# ---------------------------------------------------------------------------
# from_dict tolerance: non-dict input -> fresh model, never raises — R29.3
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("bad", [None, 0, 1.5, "nope", [1, 2, 3], ("a",), object()])
def test_from_dict_non_dict_yields_fresh_model(learn, bad):
    m = learn.door_factor_from_dict(bad)
    assert m == learn.new_door_factor_model()
    # Distinct cells (not aliased) just like the factory builds.
    assert m.cooling is not m.heating


# ---------------------------------------------------------------------------
# from_dict tolerance: missing modes -> fresh cell for the missing mode
# ---------------------------------------------------------------------------
def test_from_dict_empty_dict_yields_fresh_model(learn):
    assert learn.door_factor_from_dict({}) == learn.new_door_factor_model()


def test_from_dict_missing_heating_yields_fresh_heating(learn):
    m = learn.door_factor_from_dict({"cooling": {"factor": 0.7, "n": 8}})
    assert m.cooling.factor == pytest.approx(0.7)
    assert m.cooling.n == 8
    assert m.heating.factor is None
    assert m.heating.n == 0


def test_from_dict_missing_cooling_yields_fresh_cooling(learn):
    m = learn.door_factor_from_dict({"heating": {"factor": 0.6, "n": 5}})
    assert m.heating.factor == pytest.approx(0.6)
    assert m.heating.n == 5
    assert m.cooling.factor is None
    assert m.cooling.n == 0


def test_from_dict_non_dict_mode_entry_yields_fresh_cell(learn):
    m = learn.door_factor_from_dict({"cooling": "garbage", "heating": [1, 2]})
    assert m.cooling.factor is None
    assert m.cooling.n == 0
    assert m.heating.factor is None
    assert m.heating.n == 0


# ---------------------------------------------------------------------------
# from_dict tolerance: garbled factor/n -> safe defaults, never raises — R29.3
# ---------------------------------------------------------------------------
def test_from_dict_garbled_factor_falls_back_to_none(learn):
    m = learn.door_factor_from_dict(
        {"cooling": {"factor": "abc", "n": 7}, "heating": {"factor": None, "n": 0}}
    )
    assert m.cooling.factor is None
    assert m.cooling.n == 7


def test_from_dict_garbled_n_falls_back_to_zero(learn):
    m = learn.door_factor_from_dict({"cooling": {"factor": 0.7, "n": "oops"}})
    assert m.cooling.factor == pytest.approx(0.7)
    assert m.cooling.n == 0


def test_from_dict_missing_factor_and_n_keys_yield_fresh_cell(learn):
    m = learn.door_factor_from_dict({"cooling": {}, "heating": {}})
    assert m.cooling.factor is None
    assert m.cooling.n == 0
    assert m.heating.factor is None
    assert m.heating.n == 0


@pytest.mark.parametrize("bad_factor", ["abc", [0.7], {"x": 1}, float("nan"), float("inf")])
def test_from_dict_never_raises_on_garbled_factor(learn, bad_factor):
    # Whatever the junk, decoding must not raise and must stay a valid model.
    m = learn.door_factor_from_dict({"cooling": {"factor": bad_factor, "n": 3}})
    assert isinstance(m, learn.DoorFactorModel)
    # n is still read when present and well-formed.
    assert m.cooling.n == 3


def test_from_dict_accepts_int_factor_as_float(learn):
    m = learn.door_factor_from_dict({"cooling": {"factor": 1, "n": 6}})
    assert m.cooling.factor == pytest.approx(1.0)
    assert m.cooling.n == 6
