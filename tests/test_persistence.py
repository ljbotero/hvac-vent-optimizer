"""Fix #1: _vent_models must be persisted (it is loaded on init)."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest


@pytest.mark.asyncio
async def test_save_state_persists_vent_models(make_coordinator):
    coord, *_ = make_coordinator()
    coord._vent_models = {
        "v1": {"cooling": {"n": 4, "sum_x": 10.0, "sum_y": 2.0, "sum_xx": 30.0, "sum_xy": 6.0}}
    }

    await coord._async_save_state()
    saved = coord._store.saved

    assert saved is not None
    assert "vent_models" in saved, "vent_models is loaded on init but never saved"
    assert saved["vent_models"]["v1"]["cooling"]["n"] == 4


@pytest.mark.asyncio
async def test_save_state_roundtrips_through_initialize(make_coordinator):
    """What is saved must be loadable by async_initialize without loss."""
    coord, *_ = make_coordinator()
    coord._vent_models = {
        "v1": {"heating": {"n": 2, "sum_x": 1.0, "sum_y": 1.0, "sum_xx": 1.0, "sum_xy": 1.0}}
    }
    await coord._async_save_state()
    payload = coord._store.saved

    # New coordinator that loads the saved payload.
    coord2, *_ = make_coordinator()

    async def _load():
        return payload

    coord2._store.async_load = _load
    await coord2.async_initialize()
    assert coord2._vent_models == coord._vent_models


@pytest.mark.asyncio
async def test_save_state_serializes_cycle_targets_datetimes(make_coordinator):
    """Regression: cycle_targets datetimes must serialize to isoformat strings."""
    coord, *_ = make_coordinator()
    now = datetime(2026, 6, 7, 12, 0, tzinfo=timezone.utc)
    coord._cycle_targets = {
        "climate.t": {
            "targets": {"v1": 50},
            "initial_temps": {"v1": 26.0},
            "predicted_rates": {"v1": 0.1},
            "cycle_start": now,
            "recalc_count": 1,
            "last_recalc": now,
            "adjustment_batches": 2,
        }
    }
    await coord._async_save_state()
    saved = coord._store.saved["cycle_targets"]["climate.t"]
    assert saved["cycle_start"] == now.isoformat()
    assert saved["last_recalc"] == now.isoformat()
    assert saved["adjustment_batches"] == 2


# ===========================================================================
# Task 22 — Persistence schema v2 + v1->v2 migration (R18.3 / R25.7 / R13.5)
# ===========================================================================
from hvac_vent_optimizer.learning import (  # noqa: E402
    CURVE_BREAKPOINTS,
    LEAK_DEFAULT,
    LEAK_MAX,
    curve_knee_pct,
    seed_linear_curve,
    seed_vent_effectiveness,
)


def _regression_sums(points):
    """Build {n,sum_x,sum_y,sum_xx,sum_xy} from a list of (aperture_pct, rate)."""
    n = len(points)
    return {
        "n": n,
        "sum_x": float(sum(x for x, _ in points)),
        "sum_y": float(sum(y for _, y in points)),
        "sum_xx": float(sum(x * x for x, _ in points)),
        "sum_xy": float(sum(x * y for x, y in points)),
    }


# --- Pure helpers (learning.py) --------------------------------------------
def test_seed_linear_curve_shape_and_normalization():
    curve = seed_linear_curve(0.25)
    assert curve["breakpoints"] == list(CURVE_BREAKPOINTS)
    assert len(curve["flow"]) == len(CURVE_BREAKPOINTS)
    assert len(curve["counts"]) == len(CURVE_BREAKPOINTS)
    # flow(0) == leak, flow(100) normalized to 1.0, monotonic non-decreasing.
    assert curve["flow"][0] == pytest.approx(0.25)
    assert curve["flow"][-1] == pytest.approx(1.0)
    assert all(b >= a for a, b in zip(curve["flow"], curve["flow"][1:], strict=False))
    assert all(c == 0 for c in curve["counts"])


def test_seed_linear_curve_clamps_leak_to_max():
    curve = seed_linear_curve(0.9)  # above LEAK_MAX
    assert curve["flow"][0] == pytest.approx(LEAK_MAX)


def test_curve_knee_pct_for_near_linear_is_full_open():
    # A near-linear seed has no plateau, so the knee is at 100%.
    assert curve_knee_pct(seed_linear_curve(0.1)) == 100


def test_curve_knee_pct_detects_plateau():
    curve = {"breakpoints": [0, 25, 50, 75, 100], "flow": [0.1, 0.6, 1.0, 1.0, 1.0], "counts": [0] * 5}
    assert curve_knee_pct(curve) == 50


def test_seed_vent_effectiveness_from_regression_leak_from_intercept():
    # Perfect line rate = 0.005 + 0.00015 * aperture -> intercept 0.005,
    # full-open 0.02, leak = 0.005/0.02 = 0.25 (within [0, LEAK_MAX]).
    points = [(a, 0.005 + 0.00015 * a) for a in range(0, 100, 10)]
    sums = _regression_sums(points)
    slope = 0.00015
    intercept = 0.005
    entry = seed_vent_effectiveness(slope, intercept, sums["n"], sums=sums)
    assert entry["leak"] == pytest.approx(0.25, abs=1e-6)
    assert 0.0 <= entry["leak"] <= LEAK_MAX
    assert entry["curve"]["flow"][0] == pytest.approx(0.25, abs=1e-6)
    assert entry["knee_pct"] == 100
    # Regression sums carried for continued online learning.
    assert entry["sum_xy"] == pytest.approx(sums["sum_xy"])


def test_seed_vent_effectiveness_defaults_to_leak_default_without_regression():
    entry = seed_vent_effectiveness(None, None, 0)
    assert entry["leak"] == pytest.approx(LEAK_DEFAULT)
    assert entry["curve"]["flow"][0] == pytest.approx(LEAK_DEFAULT)


# --- Coordinator save: schema version + new sections -----------------------
@pytest.mark.asyncio
async def test_save_state_writes_schema_version_2(make_coordinator):
    coord, *_ = make_coordinator()
    await coord._async_save_state()
    assert coord._store.saved["version"] == 2


@pytest.mark.asyncio
async def test_save_state_persists_vent_effectiveness_section(make_coordinator):
    coord, *_ = make_coordinator()
    coord._vent_effectiveness = {
        "v1": {"cooling": {"leak": 0.1, "n": 9, "curve": seed_linear_curve(0.1), "knee_pct": 100}}
    }
    await coord._async_save_state()
    saved = coord._store.saved
    assert "vent_effectiveness" in saved
    assert saved["vent_effectiveness"]["v1"]["cooling"]["knee_pct"] == 100


@pytest.mark.asyncio
async def test_save_state_persists_room_efficiency_models(make_coordinator):
    """Room learning models (RoomEfficiencyModel) survive a save round-trip."""
    coord, *_ = make_coordinator()
    from hvac_vent_optimizer.learning import new_room_model, update_room_efficiency

    model = new_room_model()
    for _ in range(6):
        update_room_efficiency(model, 0.02, 1, "cooling")
    coord._room_efficiency_models = {"Master": model}

    await coord._async_save_state()
    saved = coord._store.saved
    assert "room_efficiency" in saved
    assert saved["room_efficiency"]["Master"]["cooling"]["baseline"] == pytest.approx(model.cooling.baseline)


@pytest.mark.asyncio
async def test_room_efficiency_models_roundtrip_through_initialize(make_coordinator):
    coord, *_ = make_coordinator()
    from hvac_vent_optimizer.learning import new_room_model, update_room_efficiency

    model = new_room_model()
    for _ in range(7):
        update_room_efficiency(model, 0.018, 2, "cooling")
    coord._room_efficiency_models = {"Guest": model}
    await coord._async_save_state()
    payload = coord._store.saved

    coord2, *_ = make_coordinator()

    async def _load():
        return payload

    coord2._store.async_load = _load
    await coord2.async_initialize()
    restored = coord2._room_efficiency_models["Guest"]
    assert restored.cooling.baseline == pytest.approx(model.cooling.baseline)
    assert restored.cooling.regimes[2].n == model.cooling.regimes[2].n


# --- v1 -> v2 migration -----------------------------------------------------
def _v1_store():
    points = [(a, 0.005 + 0.00015 * a) for a in range(0, 100, 10)]
    return {
        # NOTE: no "version" key -> treated as v1
        "vent_rates": {"v1": {"cooling": 0.02, "heating": 0.03}},
        "vent_models": {"v1": {"cooling": _regression_sums(points)}},
        "efficiency_models": {
            "v1": {"cooling": {"baseline": 0.02, "offsets": [-0.002, 0.0, 0.001, 0.002], "n": 10}}
        },
        "strategy_metrics": {"dab": {"runs": 5, "avg_error": 0.4}},
    }


@pytest.mark.asyncio
async def test_v1_store_migrates_and_seeds_vent_effectiveness(make_coordinator):
    coord, *_ = make_coordinator()

    async def _load():
        return _v1_store()

    coord._store.async_load = _load
    await coord.async_initialize()

    ve = coord._vent_effectiveness["v1"]["cooling"]
    assert "curve" in ve
    assert ve["curve"]["breakpoints"][0] == 0
    assert ve["curve"]["flow"][-1] == pytest.approx(1.0)
    # leak seeded from the regression intercept (0.005 / 0.02 = 0.25).
    assert ve["leak"] == pytest.approx(0.25, abs=1e-6)
    assert 0.0 <= ve["leak"] <= LEAK_MAX


@pytest.mark.asyncio
async def test_v1_store_seeds_room_efficiency_baseline(make_coordinator):
    coord, *_ = make_coordinator()

    async def _load():
        return _v1_store()

    coord._store.async_load = _load
    await coord.async_initialize()

    assert "v1" in coord._room_efficiency_models
    model = coord._room_efficiency_models["v1"]
    assert model.cooling.baseline == pytest.approx(0.02)


@pytest.mark.asyncio
async def test_v1_store_without_regression_seeds_flat_curve(make_coordinator):
    coord, *_ = make_coordinator()

    async def _load():
        return {"vent_rates": {"v1": {"cooling": 0.02}}}  # no vent_models

    coord._store.async_load = _load
    await coord.async_initialize()

    ve = coord._vent_effectiveness["v1"]["cooling"]
    assert ve["leak"] == pytest.approx(LEAK_DEFAULT)
    assert ve["curve"]["flow"][0] == pytest.approx(LEAK_DEFAULT)


@pytest.mark.asyncio
async def test_migration_backfills_new_metric_fields(make_coordinator):
    coord, *_ = make_coordinator()

    async def _load():
        return _v1_store()

    coord._store.async_load = _load
    await coord.async_initialize()

    metrics = coord._strategy_metrics["dab"]
    # Existing fields preserved.
    assert metrics["runs"] == 5
    # New fields backfilled cleanly.
    for field in ("avg_spread", "max_spread", "time_above_guardrail_min"):
        assert field in metrics
        assert metrics[field] == 0.0


@pytest.mark.asyncio
async def test_migration_is_idempotent(make_coordinator):
    coord, *_ = make_coordinator()

    async def _load():
        return _v1_store()

    coord._store.async_load = _load
    await coord.async_initialize()
    await coord._async_save_state()
    saved_once = coord._store.saved

    # Reload the migrated (v2) payload into a fresh coordinator: no re-seeding,
    # the persisted values are preserved verbatim.
    coord2, *_ = make_coordinator()

    async def _load2():
        return saved_once

    coord2._store.async_load = _load2
    await coord2.async_initialize()
    await coord2._async_save_state()
    saved_twice = coord2._store.saved

    assert saved_twice["version"] == 2
    assert saved_twice["vent_effectiveness"] == saved_once["vent_effectiveness"]
    assert saved_twice["room_efficiency"] == saved_once["room_efficiency"]


@pytest.mark.asyncio
async def test_v2_store_preserves_learned_curve_not_reseeded(make_coordinator):
    coord, *_ = make_coordinator()
    learned_curve = {"breakpoints": [0, 50, 100], "flow": [0.05, 0.6, 1.0], "counts": [9, 9, 9]}

    async def _load():
        return {
            "version": 2,
            "vent_rates": {"v1": {"cooling": 0.02}},
            "vent_models": {
                "v1": {"cooling": _regression_sums([(a, 0.005 + 0.00015 * a) for a in range(0, 100, 10)])}
            },
            "vent_effectiveness": {
                "v1": {"cooling": {"leak": 0.05, "n": 40, "curve": learned_curve, "knee_pct": 50}}
            },
        }

    coord._store.async_load = _load
    await coord.async_initialize()

    ve = coord._vent_effectiveness["v1"]["cooling"]
    assert ve["curve"] == learned_curve  # NOT overwritten by a fresh linear seed
    assert ve["knee_pct"] == 50
    assert ve["leak"] == pytest.approx(0.05)


@pytest.mark.asyncio
async def test_migration_malformed_sections_never_lose_data(make_coordinator):
    coord, *_ = make_coordinator()

    async def _load():
        return {
            "version": 2,
            "vent_effectiveness": "not-a-dict",  # malformed section
            "room_efficiency": {"v1": {"cooling": "nope"}},  # malformed entry
            "vent_rates": {"v1": {"cooling": 0.02}},  # valid -> must survive
            "vent_models": {
                "v1": {"cooling": _regression_sums([(a, 0.005 + 0.00015 * a) for a in range(0, 100, 10)])}
            },
        }

    coord._store.async_load = _load
    # Must not raise despite malformed sections.
    await coord.async_initialize()

    # Untouched valid data preserved.
    assert coord._vent_rates == {"v1": {"cooling": 0.02}}
    # Malformed sections degrade to safe empty/seeded dicts, never crash.
    assert isinstance(coord._vent_effectiveness, dict)
    assert isinstance(coord._room_efficiency_models, dict)
    # The valid regression still seeds a vent-effectiveness curve.
    assert "curve" in coord._vent_effectiveness["v1"]["cooling"]


# --- Pure room-model (de)serialization helpers -----------------------------
from hvac_vent_optimizer.learning import (  # noqa: E402
    EFF_REGIME_COUNT,
    new_room_model,
    room_model_from_dict,
    room_model_to_dict,
    seed_room_model_from_v1,
    update_room_efficiency,
)


def test_room_model_dict_roundtrip_preserves_state():
    model = new_room_model()
    for _ in range(6):
        update_room_efficiency(model, 0.02, 1, "cooling")
    for _ in range(3):
        update_room_efficiency(model, 0.05, 0, "heating")
    restored = room_model_from_dict(room_model_to_dict(model))
    assert restored.cooling.baseline == pytest.approx(model.cooling.baseline)
    assert restored.cooling.n == model.cooling.n
    assert restored.cooling.regimes[1].n == model.cooling.regimes[1].n
    assert restored.heating.baseline == pytest.approx(model.heating.baseline)


def test_room_model_from_dict_pads_short_regimes_and_never_raises():
    model = room_model_from_dict({"cooling": {"baseline": 0.01, "regimes": [{"rate": 0.01, "n": 2}]}})
    assert len(model.cooling.regimes) == EFF_REGIME_COUNT
    assert len(model.heating.regimes) == EFF_REGIME_COUNT
    # Garbage in -> a safe fresh model, no exception.
    assert room_model_from_dict("garbage").cooling.baseline is None
    assert room_model_from_dict(None).heating.baseline is None


def test_seed_room_model_from_v1_seeds_baseline_and_regimes():
    model = seed_room_model_from_v1(
        {"cooling": 0.02, "heating": 0.03},
        {"cooling": [-0.005, 0.0, 0.005, 0.01]},
    )
    assert model.cooling.baseline == pytest.approx(0.02)
    assert model.heating.baseline == pytest.approx(0.03)
    # Seeded regime rates are baseline+offset (clamped >= 0), with n=0 (untrusted).
    assert model.cooling.regimes[0].rate == pytest.approx(0.015)
    assert model.cooling.regimes[3].rate == pytest.approx(0.03)
    assert all(c.n == 0 for c in model.cooling.regimes)


def test_seed_room_model_from_v1_clamps_negative_seed_to_zero():
    model = seed_room_model_from_v1({"cooling": 0.01}, {"cooling": [-0.5, 0.0, 0.0, 0.0]})
    assert model.cooling.regimes[0].rate == 0.0
