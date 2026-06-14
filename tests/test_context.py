"""Fix #2: efficiency-context time bucket must use LOCAL time, not UTC."""

from __future__ import annotations

import importlib.util as _importlib_util
import pathlib as _pathlib
import sys as _sys
from datetime import datetime

import pytest


@pytest.mark.parametrize(
    "hour,expected",
    [
        (22, 0),
        (23, 0),
        (0, 0),
        (5, 0),  # night
        (6, 1),
        (8, 1),
        (11, 1),  # morning
        (12, 2),
        (15, 2),
        (17, 2),  # afternoon
        (18, 3),
        (20, 3),
        (21, 3),  # evening
    ],
)
def test_compute_time_bucket_boundaries(make_coordinator, hour, expected):
    from hvac_vent_optimizer.coordinator import FlairCoordinator

    assert FlairCoordinator._compute_time_bucket(hour) == expected


def test_get_vent_context_uses_local_time(make_coordinator, monkeypatch):
    """time_bucket must derive from HA local time (dt_util.now), not UTC."""
    from hvac_vent_optimizer import coordinator as coord_mod

    coord, *_ = make_coordinator(
        data={"vents": {"v1": {"id": "v1", "room": {"attributes": {}}}}, "pucks": {}}
    )

    # Pretend HA local time is 23:00 -> night bucket (0).
    monkeypatch.setattr(coord_mod.dt_util, "now", lambda: datetime(2026, 6, 7, 23, 0))
    ctx = coord._get_vent_context("v1", coord.data)
    assert ctx.time_bucket == 0

    # 08:00 local -> morning bucket (1).
    monkeypatch.setattr(coord_mod.dt_util, "now", lambda: datetime(2026, 6, 7, 8, 0))
    ctx = coord._get_vent_context("v1", coord.data)
    assert ctx.time_bucket == 1


# ===========================================================================
# Pure module `context.py` (DAB v2 — context -> regime mapping, R12 / D9)
#
# TDD tests-first (Task 3.1). These assert the contract of a NEW, dependency-
# free module `context.py` that does NOT exist yet, so every test here is
# EXPECTED TO FAIL with a missing-module error until Task 3.2 implements it.
#
# `context.py` is PURE (no Home Assistant imports), like `dab.py`. We load it
# standalone by absolute path (mirroring tests/test_dab.py) so these tests need
# no HA stubs and never import the package __init__. The load is done lazily in
# a fixture so a missing module produces a clean per-test failure (FileNotFound)
# rather than a collection error.
# ===========================================================================
_CONTEXT_PATH = (
    _pathlib.Path(__file__).resolve().parent.parent
    / "custom_components"
    / "hvac_vent_optimizer"
    / "context.py"
)


def _load_context():
    """Load the pure `context.py` module by absolute path.

    Raises FileNotFoundError (clean, descriptive) while the module does not
    yet exist — that is the expected failure mode for the tests-first step.
    """
    spec = _importlib_util.spec_from_file_location("hvo_context", _CONTEXT_PATH)
    mod = _importlib_util.module_from_spec(spec)
    # Register before exec so dataclass annotation introspection can resolve it.
    _sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def ctx_mod():
    return _load_context()


# ---------------------------------------------------------------------------
# Module constants (concrete contract pinned by the tests)
# ---------------------------------------------------------------------------
def test_constants_have_expected_concrete_values(ctx_mod):
    # Outdoor band thresholds (fold season + weather): cold < 10, hot > 25.
    assert ctx_mod.COLD_C == 10.0
    assert ctx_mod.HOT_C == 25.0
    # Daytime window [DAY_START, DAY_END): 07:00 inclusive .. 21:00 exclusive.
    assert ctx_mod.DAY_START == 7
    assert ctx_mod.DAY_END == 21
    # Secondary multipliers (cooling): people add heat -> slows cooling ~0.9.
    assert ctx_mod.OCC_FACTOR == pytest.approx(0.9)
    assert ctx_mod.DOOR_FACTOR == pytest.approx(0.9)
    # Multipliers are confined to a clamped band.
    assert ctx_mod.FACTOR_MIN == pytest.approx(0.5)
    assert ctx_mod.FACTOR_MAX == pytest.approx(1.5)


def test_secondary_multipliers_are_inside_clamp_band(ctx_mod):
    assert ctx_mod.FACTOR_MIN <= ctx_mod.OCC_FACTOR <= ctx_mod.FACTOR_MAX
    assert ctx_mod.FACTOR_MIN <= ctx_mod.DOOR_FACTOR <= ctx_mod.FACTOR_MAX


# ---------------------------------------------------------------------------
# outdoor_band: 0 cold (< COLD_C), 1 mild, 2 hot (> HOT_C)   (R12.5 / R12.6)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "outdoor_c,expected",
    [
        (-5.0, 0),  # cold
        (5.0, 0),  # cold
        (9.99, 0),  # cold (just below COLD_C)
        (10.0, 1),  # mild (COLD_C boundary is NOT cold: strict <)
        (15.0, 1),  # mild
        (25.0, 1),  # mild (HOT_C boundary is NOT hot: strict >)
        (25.01, 2),  # hot (just above HOT_C)
        (30.0, 2),  # hot
    ],
)
def test_outdoor_band_thresholds(ctx_mod, outdoor_c, expected):
    assert ctx_mod.outdoor_band(outdoor_c) == expected


def test_outdoor_band_missing_defaults_to_mild(ctx_mod):
    # Missing outdoor source -> neutral mild band (graceful degradation, R12.5).
    assert ctx_mod.outdoor_band(None) == 1


# ---------------------------------------------------------------------------
# is_daytime: sun state when provided, else hour in [DAY_START, DAY_END)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "hour,expected",
    [
        (0, False),
        (6, False),  # before DAY_START
        (7, True),  # DAY_START inclusive
        (12, True),
        (20, True),
        (21, False),  # DAY_END exclusive
        (23, False),
    ],
)
def test_is_daytime_from_hour(ctx_mod, hour, expected):
    assert ctx_mod.is_daytime(hour) is expected


def test_is_daytime_sun_state_overrides_hour(ctx_mod):
    # Sun above horizon at 03:00 -> day (sun wins over the hour heuristic).
    assert ctx_mod.is_daytime(3, sun_state="above_horizon") is True
    # Sun below horizon at noon -> night (sun wins over the hour heuristic).
    assert ctx_mod.is_daytime(12, sun_state="below_horizon") is False


# ---------------------------------------------------------------------------
# build(...): pure, takes already-resolved values (NOT HA states)
# ---------------------------------------------------------------------------
def test_build_populates_context_fields(ctx_mod):
    ctx = ctx_mod.build(hour=14, outdoor_temp_c=30.0, occupied=True, doors_open=False)
    assert ctx.hour == 14
    assert ctx.is_daytime is True
    assert ctx.outdoor_band == 2  # 30 C -> hot
    assert ctx.occupied is True
    assert ctx.doors_open is False


def test_build_missing_inputs_use_graceful_defaults(ctx_mod):
    # No outdoor temp, no occupancy, no door sensor provided.
    ctx = ctx_mod.build(hour=2)
    assert ctx.hour == 2
    assert ctx.is_daytime is False  # 02:00 -> night
    assert ctx.outdoor_band == 1  # missing outdoor -> mild
    assert ctx.occupied is None  # missing occupancy
    assert ctx.doors_open is None  # door sensor unset defaults to None


# ---------------------------------------------------------------------------
# regime_index: 0..3 over the 4 regimes [day-mild, day-hot, night-mild, night-hot]
# Cold band collapses with mild (the 4 regimes only distinguish hot vs not-hot).
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "hour,outdoor_c,expected_regime",
    [
        (14, 15.0, 0),  # day  + mild -> 0 day-mild
        (14, 30.0, 1),  # day  + hot  -> 1 day-hot
        (2, 15.0, 2),  # night + mild -> 2 night-mild
        (2, 30.0, 3),  # night + hot  -> 3 night-hot
        (14, 5.0, 0),  # day  + cold collapses to day-mild -> 0
        (2, 5.0, 2),  # night + cold collapses to night-mild -> 2
    ],
)
def test_regime_index_four_regimes(ctx_mod, hour, outdoor_c, expected_regime):
    ctx = ctx_mod.build(hour=hour, outdoor_temp_c=outdoor_c)
    assert ctx_mod.regime_index(ctx) == expected_regime


def test_regime_index_always_in_range(ctx_mod):
    for hour in (2, 14):
        for outdoor_c in (None, 5.0, 15.0, 30.0):
            ctx = ctx_mod.build(hour=hour, outdoor_temp_c=outdoor_c)
            assert ctx_mod.regime_index(ctx) in (0, 1, 2, 3)


# ---------------------------------------------------------------------------
# apply_context_multipliers(rate, ctx): bounded secondary multipliers
# ---------------------------------------------------------------------------
def test_apply_multipliers_occupied_applies_occ_factor(ctx_mod):
    ctx = ctx_mod.build(hour=14, outdoor_temp_c=20.0, occupied=True, doors_open=False)
    assert ctx_mod.apply_context_multipliers(0.10, ctx) == pytest.approx(0.10 * ctx_mod.OCC_FACTOR)


def test_apply_multipliers_door_open_applies_door_factor(ctx_mod):
    ctx = ctx_mod.build(hour=14, outdoor_temp_c=20.0, occupied=False, doors_open=True)
    assert ctx_mod.apply_context_multipliers(0.10, ctx) == pytest.approx(0.10 * ctx_mod.DOOR_FACTOR)


def test_apply_multipliers_both_compound(ctx_mod):
    ctx = ctx_mod.build(hour=14, outdoor_temp_c=20.0, occupied=True, doors_open=True)
    expected = 0.10 * ctx_mod.OCC_FACTOR * ctx_mod.DOOR_FACTOR
    assert ctx_mod.apply_context_multipliers(0.10, ctx) == pytest.approx(expected)


def test_apply_multipliers_missing_inputs_default_to_unity(ctx_mod):
    # occupied/doors_open both None (missing) -> multiplier 1.0 (rate unchanged).
    ctx = ctx_mod.build(hour=14, outdoor_temp_c=20.0)
    assert ctx_mod.apply_context_multipliers(0.10, ctx) == pytest.approx(0.10)


def test_apply_multipliers_false_inputs_default_to_unity(ctx_mod):
    # Explicit False (not occupied, door closed) -> multiplier 1.0.
    ctx = ctx_mod.build(hour=14, outdoor_temp_c=20.0, occupied=False, doors_open=False)
    assert ctx_mod.apply_context_multipliers(0.10, ctx) == pytest.approx(0.10)


def test_apply_multipliers_effective_factor_stays_in_clamp_band(ctx_mod):
    # The applied multiplier must stay within the clamped band for all combos.
    rate = 0.10
    for occ in (None, False, True):
        for door in (None, False, True):
            ctx = ctx_mod.build(hour=14, outdoor_temp_c=20.0, occupied=occ, doors_open=door)
            factor = ctx_mod.apply_context_multipliers(rate, ctx) / rate
            assert ctx_mod.FACTOR_MIN <= factor <= ctx_mod.FACTOR_MAX


# ---------------------------------------------------------------------------
# apply_context_multipliers(..., door_factor=...): learned door factor seam
#
# TDD tests-first (Task 5.1). These assert the contract of a NEW optional
# parameter `door_factor: float | None = None` on apply_context_multipliers
# that does NOT exist yet, so every test here is EXPECTED TO FAIL until Task
# 5.2 threads the resolved factor through. Failure mode is a TypeError
# (unexpected keyword argument) or an assertion mismatch because the passed
# factor is not yet applied.
#
# Requirements: 26.1 (resolved factor replaces the module constant in the hot
# path), 26.5 (apply only while doors_open is True; neutral otherwise), 26.6
# (occupancy multiplier unchanged).
# ---------------------------------------------------------------------------
def test_apply_multipliers_passed_door_factor_overrides_constant(ctx_mod):
    # doors_open=True with an explicit learned door_factor=0.7 -> the door term
    # is 0.7, NOT the module DOOR_FACTOR (0.9). occupied=False keeps occ neutral.
    ctx = ctx_mod.build(hour=14, outdoor_temp_c=20.0, occupied=False, doors_open=True)
    result = ctx_mod.apply_context_multipliers(0.10, ctx, door_factor=0.7)
    assert result == pytest.approx(0.10 * 0.7)
    # And it must differ from the legacy constant behavior.
    assert result != pytest.approx(0.10 * ctx_mod.DOOR_FACTOR)


def test_apply_multipliers_door_factor_none_falls_back_to_constant(ctx_mod):
    # doors_open=True with door_factor=None -> falls back to module DOOR_FACTOR
    # (0.9), preserving today's behavior exactly.
    ctx = ctx_mod.build(hour=14, outdoor_temp_c=20.0, occupied=False, doors_open=True)
    result = ctx_mod.apply_context_multipliers(0.10, ctx, door_factor=None)
    assert result == pytest.approx(0.10 * ctx_mod.DOOR_FACTOR)


def test_apply_multipliers_door_factor_ignored_when_doors_closed(ctx_mod):
    # doors_open=False -> the door term is neutral 1.0 regardless of door_factor.
    ctx = ctx_mod.build(hour=14, outdoor_temp_c=20.0, occupied=False, doors_open=False)
    assert ctx_mod.apply_context_multipliers(0.10, ctx, door_factor=0.7) == pytest.approx(0.10)


def test_apply_multipliers_door_factor_ignored_when_doors_none(ctx_mod):
    # doors_open=None (no sensor) -> the door term is neutral 1.0 regardless of
    # the passed door_factor value.
    ctx = ctx_mod.build(hour=14, outdoor_temp_c=20.0, occupied=False, doors_open=None)
    assert ctx_mod.apply_context_multipliers(0.10, ctx, door_factor=0.7) == pytest.approx(0.10)


def test_apply_multipliers_door_factor_leaves_occupancy_unchanged(ctx_mod):
    # occupied=True compounds the unchanged OCC_FACTOR with the passed door
    # factor while doors_open=True (R26.6: occupancy term untouched).
    ctx = ctx_mod.build(hour=14, outdoor_temp_c=20.0, occupied=True, doors_open=True)
    expected = 0.10 * ctx_mod.OCC_FACTOR * 0.7
    assert ctx_mod.apply_context_multipliers(0.10, ctx, door_factor=0.7) == pytest.approx(expected)


def test_apply_multipliers_door_factor_occupancy_only_unaffected(ctx_mod):
    # occupied=True, doors closed -> only OCC_FACTOR applies; a passed
    # door_factor must not leak into the door-closed path.
    ctx = ctx_mod.build(hour=14, outdoor_temp_c=20.0, occupied=True, doors_open=False)
    assert ctx_mod.apply_context_multipliers(0.10, ctx, door_factor=0.7) == pytest.approx(
        0.10 * ctx_mod.OCC_FACTOR
    )


def test_apply_multipliers_combined_product_clamped_to_band(ctx_mod):
    # occupied=True (0.9) compounded with a low door_factor (0.5) -> 0.45, which
    # is below FACTOR_MIN (0.5); the combined product must stay clamped.
    rate = 0.10
    ctx = ctx_mod.build(hour=14, outdoor_temp_c=20.0, occupied=True, doors_open=True)
    factor = ctx_mod.apply_context_multipliers(rate, ctx, door_factor=0.5) / rate
    assert factor == pytest.approx(ctx_mod.FACTOR_MIN)
    assert ctx_mod.FACTOR_MIN <= factor <= ctx_mod.FACTOR_MAX


def test_apply_multipliers_passed_door_factor_stays_in_clamp_band(ctx_mod):
    # For any passed door_factor and any occupancy/door combo the applied
    # multiplier must remain within the clamped band.
    rate = 0.10
    for occ in (None, False, True):
        for door in (None, False, True):
            for df in (0.5, 0.7, 0.9, 1.0):
                ctx = ctx_mod.build(hour=14, outdoor_temp_c=20.0, occupied=occ, doors_open=door)
                factor = ctx_mod.apply_context_multipliers(rate, ctx, door_factor=df) / rate
                assert ctx_mod.FACTOR_MIN <= factor <= ctx_mod.FACTOR_MAX
