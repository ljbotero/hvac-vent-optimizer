"""Hardening: async_import_efficiency must reject malformed efficiency models.

Imported models feed _update_efficiency_model / _get_effective_efficiency_rate,
which assume mode->dict with numeric baseline and a list of offsets. Malformed
entries must be dropped rather than poisoning runtime state.
"""

from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_import_drops_malformed_models(make_coordinator):
    coord, *_ = make_coordinator()
    payload = {
        "efficiencyModels": {
            "good": {"cooling": {"baseline": 0.5, "offsets": [0.1, -0.1], "n": 3}},
            "bad_top": ["not", "a", "dict"],
            "bad_mode": {"cooling": "nope"},
            "bad_offsets": {"heating": {"baseline": 0.4, "offsets": "not-a-list"}},
            "bad_baseline": {"cooling": {"baseline": "warm", "offsets": [0.0]}},
        },
        "efficiencyData": {"roomEfficiencies": []},
    }

    await coord.async_import_efficiency(payload)

    assert "good" in coord._efficiency_models
    for bad in ("bad_top", "bad_mode", "bad_offsets", "bad_baseline"):
        assert bad not in coord._efficiency_models, f"{bad} should have been rejected"


@pytest.mark.asyncio
async def test_import_good_models_preserved(make_coordinator):
    coord, *_ = make_coordinator()
    payload = {
        "efficiencyModels": {
            "v1": {
                "cooling": {"baseline": 0.5, "offsets": [0.1, 0.2], "n": 5},
                "heating": {"baseline": None, "offsets": [0.0, 0.0], "n": 0},
            }
        },
        "efficiencyData": {"roomEfficiencies": []},
    }
    await coord.async_import_efficiency(payload)
    assert coord._efficiency_models["v1"]["cooling"]["baseline"] == 0.5


# ===========================================================================
# Task 22 — Versioned, backward-compatible export/import (R25.8 / R18.3)
# ===========================================================================
from hvac_vent_optimizer.learning import seed_linear_curve  # noqa: E402


@pytest.mark.asyncio
async def test_export_is_versioned_and_carries_vent_effectiveness(make_coordinator):
    coord, *_ = make_coordinator()
    coord._vent_effectiveness = {
        "v1": {"cooling": {"leak": 0.1, "n": 9, "curve": seed_linear_curve(0.1), "knee_pct": 100}}
    }
    export = coord.build_efficiency_export()
    assert export["version"] == 2
    assert "vent_effectiveness" in export
    assert export["vent_effectiveness"]["v1"]["cooling"]["knee_pct"] == 100


@pytest.mark.asyncio
async def test_old_payload_imports_into_v2_with_seeded_curve(make_coordinator):
    """A pre-v2 payload (no vent_effectiveness) seeds a curve on import."""
    coord, *_ = make_coordinator()
    old_payload = {
        "efficiencyModels": {"v1": {"cooling": {"baseline": 0.02, "offsets": [0.0, 0.0, 0.0, 0.0], "n": 10}}},
        "efficiencyData": {
            "roomEfficiencies": [{"ventId": "v1", "coolingRate": 0.02, "heatingRate": 0.0}],
            "globalRates": {},
        },
    }
    await coord.async_import_efficiency(old_payload)

    assert "v1" in coord._vent_effectiveness
    ve = coord._vent_effectiveness["v1"]["cooling"]
    assert "curve" in ve
    assert ve["curve"]["flow"][-1] == pytest.approx(1.0)


@pytest.mark.asyncio
async def test_v2_payload_import_preserves_vent_effectiveness(make_coordinator):
    coord, *_ = make_coordinator()
    learned_curve = {"breakpoints": [0, 50, 100], "flow": [0.05, 0.6, 1.0], "counts": [9, 9, 9]}
    payload = {
        "version": 2,
        "efficiencyData": {"roomEfficiencies": [], "globalRates": {}},
        "efficiencyModels": {},
        "vent_effectiveness": {
            "v1": {"cooling": {"leak": 0.05, "n": 40, "curve": learned_curve, "knee_pct": 50}}
        },
    }
    await coord.async_import_efficiency(payload)
    ve = coord._vent_effectiveness["v1"]["cooling"]
    assert ve["curve"] == learned_curve
    assert ve["knee_pct"] == 50


@pytest.mark.asyncio
async def test_v2_payload_import_drops_malformed_vent_effectiveness(make_coordinator):
    coord, *_ = make_coordinator()
    payload = {
        "version": 2,
        "efficiencyData": {"roomEfficiencies": [], "globalRates": {}},
        "vent_effectiveness": {
            "good": {"cooling": {"leak": 0.1, "n": 9, "curve": seed_linear_curve(0.1), "knee_pct": 100}},
            "bad_no_leak": {"cooling": {"n": 9, "curve": seed_linear_curve(0.1)}},
            "bad_no_curve": {"cooling": {"leak": 0.1, "n": 9}},
            "bad_modes": ["not", "a", "dict"],
        },
    }
    await coord.async_import_efficiency(payload)
    assert "good" in coord._vent_effectiveness
    for bad in ("bad_no_leak", "bad_no_curve", "bad_modes"):
        assert bad not in coord._vent_effectiveness, f"{bad} should be rejected"
