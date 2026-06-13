"""Tests-first (Task 6) for multi-vent room-GROUP learning in pure `learning.py`.

Validates: Requirements 23.1, 25.2

These assert the contract of NEW symbols to be added to `learning.py` by Task 6
(`group_combined_flow`, `group_predicted_rate`, `resolve_group_leaks`). Those
symbols do NOT exist yet, so every test here is EXPECTED TO FAIL with
``AttributeError`` when it first touches ``learn.<new_symbol>``. This is the
failing-test step of the TDD loop — do NOT implement the new symbols here.

`learning.py` already exists (Tasks 4/5), so the module loads fine; failures are
``AttributeError`` for the missing names, NOT import/logic errors. We load the
module standalone by absolute path as ``hvo_learning`` (mirroring
tests/test_learning_effectiveness.py / test_learning_rooms.py) so these tests
need no Home Assistant stubs and never import the package ``__init__``.

====================================================================
PINNED CONTRACT (for Task 6 to implement)
====================================================================
Why this exists — multi-vent rooms (R23.1)
------------------------------------------
A room served by ≥2 smart vents (e.g. Master Bedroom `4723` + `6ee4`) is one
logical unit: the vents share the room temperature and receive identical
targets. So learning is done at the room-GROUP level using the COMBINED flow,
and a single `e_room` / `leak` is attributed to the group. Per-vent `leak`
defaults equal within the group (to the shared group leak) until a vent has
enough INDEPENDENT samples (n >= MODEL_MIN_N) to trust its own learned leak.

Group-flow definition (equal-capacity average)
-----------------------------------------------
The room's thermal response is driven by the TOTAL airflow it receives, the sum
of each vent's airflow. Assuming equal-capacity vents (the natural default,
consistent with "leak defaults equal within the group"), the group's flow
*fraction* is the MEAN of the per-vent flow fractions:

    group_combined_flow(leaks, apertures) = mean_i flow(leak_i, a_i)

This is the defensible choice because it:
  * stays in [0, 1] exactly like the single-vent `flow`, so a shared `e_room`
    keeps its "full-open rate" meaning (all vents fully open -> flow == 1.0 ->
    rate == e_room);
  * reduces EXACTLY to single-vent `flow` when the group has one vent;
  * is non-decreasing in every aperture (mean of monotone terms);
  * is order-independent (a mean is commutative) -> stable group attribution;
  * with identical apertures (R23.1) collapses to flow(mean_leak, a), i.e. the
    group behaves like one vent with the average leak.

Functions / signatures:
    group_combined_flow(leaks: Sequence[float], apertures: Sequence[float]) -> float
        a_i are aperture FRACTIONS in [0,1] (same convention as `flow`).
        returns (1/N) * sum_i flow(leak_i, a_i); 0.0 for an empty group.
        raises ValueError if len(leaks) != len(apertures).

    group_predicted_rate(e_room, leaks, aperture_pcts) -> float
        aperture_pcts are PERCENTS in [0,100] (same convention as predicted_rate).
        returns max(0.0, e_room) * group_combined_flow(leaks, [p/100 for p in pcts]).

    resolve_group_leaks(group_leak, vent_leaks, vent_counts) -> list[float]
        per-vent EFFECTIVE leak: vent uses its own learned leak iff it has enough
        independent data (n >= MODEL_MIN_N, boundary inclusive); otherwise it
        defaults to the shared `group_leak`. raises ValueError on length mismatch.

Worked numeric examples:
    flow(0.1,0.5)=0.55 ; flow(0.3,0.5)=0.65 ; flow(0.2,0.5)=0.60
    group_combined_flow([0.1,0.3],[0.5,0.5]) = (0.55+0.65)/2 = 0.60  (= flow(0.2,0.5))
    group_combined_flow([0.2,0.2],[0.0,1.0]) = (0.2 + 1.0)/2     = 0.60
    group_predicted_rate(0.25,[0.1,0.3],[50,50]) = 0.25*0.60     = 0.15
    resolve_group_leaks(0.1, [0.05,0.30], [3,50])               = [0.1, 0.30]
====================================================================
"""
from __future__ import annotations

import importlib.util as _importlib_util
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


# ---------------------------------------------------------------------------
# group_combined_flow: equal-capacity average of per-vent flow fractions
# ---------------------------------------------------------------------------
def test_group_combined_flow_equal_leaks_equal_apertures(learn):
    # flow(0.2, 0.5) = 0.6 for each vent -> mean = 0.6
    assert learn.group_combined_flow([0.2, 0.2], [0.5, 0.5]) == pytest.approx(0.6)


def test_group_combined_flow_worked_example_distinct_leaks(learn):
    # flow(0.1,0.5)=0.55 ; flow(0.3,0.5)=0.65 ; mean = 0.60
    assert learn.group_combined_flow([0.1, 0.3], [0.5, 0.5]) == pytest.approx(0.60)


def test_group_combined_flow_distinct_apertures(learn):
    # flow(0.2,0.0)=0.2 ; flow(0.2,1.0)=1.0 ; mean = 0.6
    assert learn.group_combined_flow([0.2, 0.2], [0.0, 1.0]) == pytest.approx(0.6)


def test_group_combined_flow_all_open_is_one(learn):
    # every vent fully open -> each flow == 1.0 -> mean == 1.0 (full-open rate)
    assert learn.group_combined_flow([0.1, 0.3, 0.0], [1.0, 1.0, 1.0]) == pytest.approx(1.0)


def test_group_combined_flow_all_closed_is_mean_leak(learn):
    # every vent closed -> each flow == leak_i -> mean == mean(leaks)
    assert learn.group_combined_flow([0.1, 0.3], [0.0, 0.0]) == pytest.approx(0.2)


def test_group_combined_flow_reduces_to_single_vent_flow(learn):
    # a one-vent "group" must equal the single-vent flow exactly
    for leak in (0.0, 0.1, 0.2, 0.35):
        for a in (0.0, 0.25, 0.5, 1.0):
            assert learn.group_combined_flow([leak], [a]) == pytest.approx(
                learn.flow(leak, a)
            )


def test_group_combined_flow_identical_apertures_equals_mean_leak_flow(learn):
    # R23.1: identical targets -> group behaves like one vent at the mean leak
    a = 0.4
    leaks = [0.05, 0.25, 0.30]
    mean_leak = sum(leaks) / len(leaks)
    assert learn.group_combined_flow(leaks, [a, a, a]) == pytest.approx(
        learn.flow(mean_leak, a)
    )


def test_group_combined_flow_is_order_independent(learn):
    # stable attribution: reordering vents must not change the combined flow
    forward = learn.group_combined_flow([0.05, 0.20, 0.30], [0.1, 0.6, 0.9])
    reverse = learn.group_combined_flow([0.30, 0.20, 0.05], [0.9, 0.6, 0.1])
    assert forward == pytest.approx(reverse)


def test_group_combined_flow_stays_in_unit_interval(learn):
    leaks = [0.0, 0.1, 0.35]
    for step in range(101):
        a = step / 100.0
        val = learn.group_combined_flow(leaks, [a, a, a])
        assert 0.0 <= val <= 1.0


def test_group_combined_flow_non_decreasing_in_aperture(learn):
    leaks = [0.05, 0.30]
    prev = None
    for step in range(101):
        a = step / 100.0
        val = learn.group_combined_flow(leaks, [a, a])
        if prev is not None:
            assert val >= prev - 1e-12
        prev = val


def test_group_combined_flow_empty_group_is_zero(learn):
    assert learn.group_combined_flow([], []) == pytest.approx(0.0)


def test_group_combined_flow_clamps_out_of_range(learn):
    # flow() clamps leak and aperture into [0,1]; the mean inherits that safety
    val = learn.group_combined_flow([1.5, -0.5], [1.5, -0.5])
    # vent0: flow(clamp1.5->1, clamp1.5->1)=1.0 ; vent1: flow(clamp->0, clamp->0)=0.0
    assert val == pytest.approx(0.5)


def test_group_combined_flow_length_mismatch_raises(learn):
    with pytest.raises(ValueError):
        learn.group_combined_flow([0.1, 0.2], [0.5])


# ---------------------------------------------------------------------------
# group_predicted_rate: e_room * group_combined_flow(leaks, apertures)
# ---------------------------------------------------------------------------
def test_group_predicted_rate_worked_example(learn):
    # 0.25 * mean(flow(0.1,0.5), flow(0.3,0.5)) = 0.25 * 0.60 = 0.15
    assert learn.group_predicted_rate(0.25, [0.1, 0.3], [50.0, 50.0]) == pytest.approx(
        0.15
    )


def test_group_predicted_rate_matches_single_vent_predicted_rate(learn):
    # one-vent group must equal predicted_rate exactly
    for pct in (0.0, 25.0, 50.0, 100.0):
        assert learn.group_predicted_rate(0.33, [0.15], [pct]) == pytest.approx(
            learn.predicted_rate(0.33, 0.15, pct)
        )


def test_group_predicted_rate_all_open_is_e_room(learn):
    # all vents fully open -> combined flow 1.0 -> rate == e_room
    assert learn.group_predicted_rate(0.25, [0.1, 0.3], [100.0, 100.0]) == pytest.approx(
        0.25
    )


def test_group_predicted_rate_all_closed_is_e_room_times_mean_leak(learn):
    assert learn.group_predicted_rate(0.40, [0.1, 0.3], [0.0, 0.0]) == pytest.approx(
        0.40 * 0.2
    )


def test_group_predicted_rate_matches_flow_composition(learn):
    e_room = 0.30
    leaks = [0.05, 0.20, 0.30]
    pcts = [10.0, 60.0, 90.0]
    fracs = [p / 100.0 for p in pcts]
    expected = e_room * learn.group_combined_flow(leaks, fracs)
    assert learn.group_predicted_rate(e_room, leaks, pcts) == pytest.approx(expected)


def test_group_predicted_rate_negative_e_room_clamps_non_negative(learn):
    assert learn.group_predicted_rate(-0.25, [0.2, 0.2], [50.0, 50.0]) >= 0.0


def test_group_predicted_rate_out_of_range_aperture_is_safe(learn):
    # percents outside [0,100] clamp via flow; no negatives, no overflow past e_room
    assert learn.group_predicted_rate(0.25, [0.2, 0.2], [-10.0, 150.0]) >= 0.0
    assert learn.group_predicted_rate(0.25, [0.2, 0.2], [150.0, 150.0]) == pytest.approx(
        0.25
    )


def test_group_predicted_rate_non_decreasing_in_aperture(learn):
    prev = None
    for pct in range(101):
        val = learn.group_predicted_rate(0.25, [0.1, 0.3], [float(pct), float(pct)])
        if prev is not None:
            assert val >= prev - 1e-12
        prev = val


def test_group_predicted_rate_length_mismatch_raises(learn):
    with pytest.raises(ValueError):
        learn.group_predicted_rate(0.25, [0.1, 0.2], [50.0])


# ---------------------------------------------------------------------------
# resolve_group_leaks: per-vent leak defaults to group leak until enough data
# ---------------------------------------------------------------------------
def test_resolve_group_leaks_defaults_until_enough_data(learn):
    # vent0 n=3 < MODEL_MIN_N -> shared group leak ; vent1 n=50 -> own learned leak
    out = learn.resolve_group_leaks(0.1, [0.05, 0.30], [3, 50])
    assert out == pytest.approx([0.1, 0.30])


def test_resolve_group_leaks_all_below_threshold_are_equal(learn):
    # every vent under-sampled -> every effective leak equals the group leak
    out = learn.resolve_group_leaks(0.12, [0.01, 0.34, 0.20], [0, 4, 7])
    assert out == pytest.approx([0.12, 0.12, 0.12])


def test_resolve_group_leaks_boundary_is_inclusive(learn):
    # n exactly == MODEL_MIN_N trusts the vent's own leak (boundary inclusive,
    # consistent with derive_effectiveness)
    out = learn.resolve_group_leaks(0.1, [0.28], [learn.MODEL_MIN_N])
    assert out == pytest.approx([0.28])


def test_resolve_group_leaks_just_below_boundary_uses_group(learn):
    out = learn.resolve_group_leaks(0.1, [0.28], [learn.MODEL_MIN_N - 1])
    assert out == pytest.approx([0.1])


def test_resolve_group_leaks_all_trusted_keep_own(learn):
    out = learn.resolve_group_leaks(0.1, [0.05, 0.30], [20, 99])
    assert out == pytest.approx([0.05, 0.30])


def test_resolve_group_leaks_feeds_combined_flow_stably(learn):
    # Until vents have independent data, the group's combined flow is identical
    # to a single vent at the shared group leak (stable group attribution).
    group_leak = 0.15
    leaks = learn.resolve_group_leaks(group_leak, [0.02, 0.33], [1, 2])
    a = 0.5
    assert learn.group_combined_flow(leaks, [a, a]) == pytest.approx(
        learn.flow(group_leak, a)
    )


def test_resolve_group_leaks_length_mismatch_raises(learn):
    with pytest.raises(ValueError):
        learn.resolve_group_leaks(0.1, [0.05, 0.30], [3])


def test_resolve_group_leaks_empty_group_is_empty(learn):
    assert learn.resolve_group_leaks(0.1, [], []) == []
