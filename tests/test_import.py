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


# ===========================================================================
# Task 10 — Export/import the learned door_factor section (R29.5)
# ===========================================================================
# Mirrors the vent_effectiveness/room_efficiency export/import contract:
#   * build_efficiency_export() carries a ``door_factor`` section, one entry
#     per room in ``self._door_factor_models``, serialized via
#     ``learning.door_factor_to_dict``.
#   * async_import_efficiency() restores that section into
#     ``self._door_factor_models`` (decoded tolerantly).
#   * a payload that OMITS the section stays backward-compatible (every room
#     resolves to the 0.9 default) and — matching the ``.update()`` semantics
#     used for room_efficiency/vent_effectiveness — does NOT clear existing
#     learned door factors.
from hvac_vent_optimizer.learning import (  # noqa: E402
    DOOR_MIN_N,
    door_factor_to_dict,
    new_door_factor_model,
    resolve_door_factor,
    update_door_factor,
)


def _trusted_door_model(ratio: float, mode: str = "cooling"):
    """Build a door-factor model whose ``mode`` cell is trusted (n >= gate)."""
    model = new_door_factor_model()
    for _ in range(DOOR_MIN_N):
        update_door_factor(model, ratio, mode)
    return model


@pytest.mark.asyncio
async def test_export_includes_door_factor_section(make_coordinator):
    """build_efficiency_export must serialize door_factor via door_factor_to_dict."""
    coord, *_ = make_coordinator()
    model = _trusted_door_model(0.6, "cooling")
    coord._door_factor_models = {"Bedroom 2": model}

    export = coord.build_efficiency_export()

    assert "door_factor" in export, "export payload is missing the door_factor section"
    assert export["door_factor"] == {"Bedroom 2": door_factor_to_dict(model)}


@pytest.mark.asyncio
async def test_import_restores_door_factor_section(make_coordinator):
    """async_import_efficiency must restore the door_factor section (tolerantly)."""
    coord, *_ = make_coordinator()
    learned = _trusted_door_model(0.55, "cooling")
    payload = {
        "version": 2,
        "efficiencyData": {"roomEfficiencies": [], "globalRates": {}},
        "door_factor": {
            "Bedroom 2": door_factor_to_dict(learned),
            # A garbled entry must be tolerated, never raise, and resolve to 0.9
            # (whether dropped or decoded to a fresh, untrusted cell).
            "garbled": ["not", "a", "dict"],
        },
    }

    await coord.async_import_efficiency(payload)

    assert "Bedroom 2" in coord._door_factor_models, "door_factor section was not restored"
    restored = coord._door_factor_models["Bedroom 2"]
    assert resolve_door_factor(restored, "cooling") == pytest.approx(resolve_door_factor(learned, "cooling"))
    assert resolve_door_factor(coord._door_factor_models.get("garbled"), "cooling") == 0.9


@pytest.mark.asyncio
async def test_import_without_door_factor_is_backward_compatible(make_coordinator):
    """A payload omitting door_factor: rooms resolve to 0.9 and learned entries survive."""
    coord, *_ = make_coordinator()
    seeded = _trusted_door_model(0.7, "cooling")
    coord._door_factor_models = {"Master Bedroom": seeded}

    payload = {
        "version": 2,
        "efficiencyData": {"roomEfficiencies": [], "globalRates": {}},
        # no door_factor section (pre-feature export)
    }
    await coord.async_import_efficiency(payload)

    # .update() semantics: an omitted section never clears existing learned data.
    assert "Master Bedroom" in coord._door_factor_models
    assert resolve_door_factor(coord._door_factor_models["Master Bedroom"], "cooling") == pytest.approx(
        resolve_door_factor(seeded, "cooling")
    )
    # An unknown room (no model) resolves to the legacy 0.9 default.
    assert resolve_door_factor(coord._door_factor_models.get("Unknown Room"), "cooling") == 0.9
