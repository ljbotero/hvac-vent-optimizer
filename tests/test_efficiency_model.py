from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from homeassistant.components.climate.const import HVACAction

from custom_components.hvac_vent_optimizer.coordinator import (
    FlairCoordinator,
    EFF_APERTURE_JITTER_PCT,
    EFF_MIN_APERTURE_PCT,
    EFF_MIN_DUCT_DELTA_C,
    EFF_REGIME_CONFIDENCE,
)


class _FakeStates:
    def __init__(self, mapping=None):
        self._mapping = mapping or {}

    def get(self, entity_id):
        return self._mapping.get(entity_id)


class _FakeHass:
    def __init__(self):
        self.states = _FakeStates()
        self.config = SimpleNamespace(units=SimpleNamespace(temperature_unit="C"))

    def async_create_task(self, coro):
        return coro


class _FakeEntry:
    def __init__(self):
        self.data = {"structure_id": "struct1"}
        self.options = {}
        self.entry_id = "entry1"
        self.title = "test"


def _make_coordinator():
    return FlairCoordinator(_FakeHass(), None, _FakeEntry())


def _sample(t0, minutes, temp, aperture, duct=None):
    return {
        "t": t0 + timedelta(minutes=minutes),
        "temp": temp,
        "aperture": aperture,
        "duct": duct,
    }


def test_compute_efficiency_heating_basic():
    coord = _make_coordinator()
    t0 = datetime.now(timezone.utc) - timedelta(minutes=15)
    samples = [
        _sample(t0, 3, 20.0, 50),
        _sample(t0, 10, 21.0, 50),
    ]
    efficiency, _, _ = coord._compute_efficiency_sample(HVACAction.HEATING, t0, samples)
    assert efficiency is not None
    assert round(efficiency, 3) == round((1.0 / 7.0) / 0.5, 3)


def test_compute_efficiency_cooling_basic():
    coord = _make_coordinator()
    t0 = datetime.now(timezone.utc) - timedelta(minutes=15)
    samples = [
        _sample(t0, 3, 24.0, 50),
        _sample(t0, 10, 23.0, 50),
    ]
    efficiency, _, _ = coord._compute_efficiency_sample(HVACAction.COOLING, t0, samples)
    assert efficiency is not None
    assert round(efficiency, 3) == round((1.0 / 7.0) / 0.5, 3)


def test_compute_efficiency_aperture_too_low():
    coord = _make_coordinator()
    t0 = datetime.now(timezone.utc) - timedelta(minutes=15)
    samples = [
        _sample(t0, 3, 20.0, EFF_MIN_APERTURE_PCT - 1),
        _sample(t0, 10, 21.0, EFF_MIN_APERTURE_PCT - 1),
    ]
    efficiency, _, _ = coord._compute_efficiency_sample(HVACAction.HEATING, t0, samples)
    assert efficiency is None


def test_compute_efficiency_with_duct_normalization():
    coord = _make_coordinator()
    t0 = datetime.now(timezone.utc) - timedelta(minutes=15)
    samples = [
        _sample(t0, 3, 20.0, 50, 30.0),
        _sample(t0, 10, 21.0, 50, 30.0),
    ]
    efficiency, _, _ = coord._compute_efficiency_sample(HVACAction.HEATING, t0, samples)
    assert efficiency is not None
    expected = ((1.0 / 7.0) / (30.0 - 20.0)) / 0.5
    assert efficiency == pytest.approx(expected, rel=0.1)
    assert (30.0 - 20.0) >= EFF_MIN_DUCT_DELTA_C


def test_compute_efficiency_trims_at_setpoint():
    coord = _make_coordinator()
    t0 = datetime.now(timezone.utc) - timedelta(minutes=15)
    samples = [
        _sample(t0, 3, 20.0, 50),
        _sample(t0, 8, 21.0, 50),
        _sample(t0, 15, 23.0, 50),
    ]
    efficiency, observed, mean_aperture = coord._compute_efficiency_sample(
        HVACAction.HEATING, t0, samples, setpoint_target=21.0
    )
    assert efficiency == pytest.approx(0.4, rel=0.1)
    assert observed == pytest.approx(0.2, rel=0.1)
    assert mean_aperture == pytest.approx(50.0)


def test_update_efficiency_model_and_effective_rate():
    coord = _make_coordinator()
    baseline, effective, confidence = coord._update_efficiency_model(
        "v1", "heating", 0.3
    )
    assert baseline > 0
    assert effective > 0
    assert confidence >= 0

    model = coord._efficiency_models["v1"]["heating"]
    assert len(model["offsets"]) >= 1
    assert model["baseline"] == baseline
    assert model["effective"] == effective

    rate = coord._get_effective_efficiency_rate("v1", HVACAction.HEATING)
    assert rate == effective
    assert model["confidence"] <= 1.0
    assert model["confidence"] >= 0.0
    assert model["confidence"] >= EFF_REGIME_CONFIDENCE or model["effective"] == model["baseline"]


def test_filter_samples_window_and_jitter_rejection():
    coord = _make_coordinator()
    t0 = datetime.now(timezone.utc) - timedelta(minutes=15)
    samples = [
        _sample(t0, 1, 20.0, 10),
        _sample(t0, 4, 20.5, 10),
        _sample(t0, 9, 21.0, 10),
    ]
    windowed = coord._filter_samples_window(t0, samples)
    assert len(windowed) == 2

    samples.append(_sample(t0, 40, 22.0, 10))
    windowed = coord._filter_samples_window(t0, samples)
    assert all(sample["t"] <= t0 + timedelta(minutes=30) for sample in windowed)

    jitter_samples = [
        _sample(t0, 3, 20.0, 10),
        _sample(t0, 10, 21.0, 10 + EFF_APERTURE_JITTER_PCT + 1),
    ]
    assert coord._compute_efficiency_sample(HVACAction.HEATING, t0, jitter_samples)[0] is None


def test_compute_efficiency_rejects_wrong_direction():
    coord = _make_coordinator()
    t0 = datetime.now(timezone.utc) - timedelta(minutes=15)
    samples = [
        _sample(t0, 3, 21.0, 50),
        _sample(t0, 10, 20.0, 50),
    ]
    assert coord._compute_efficiency_sample(HVACAction.HEATING, t0, samples)[0] is None
    assert coord._compute_efficiency_sample(HVACAction.COOLING, t0, samples)[0] is not None


def test_compute_efficiency_rejects_short_or_small_delta():
    coord = _make_coordinator()
    t0 = datetime.now(timezone.utc) - timedelta(minutes=6)
    short_samples = [
        _sample(t0, 3, 20.0, 50),
        _sample(t0, 4, 20.1, 50),
    ]
    assert coord._compute_efficiency_sample(HVACAction.HEATING, t0, short_samples)[0] is None

    small_delta = [
        _sample(t0, 3, 20.0, 50),
        _sample(t0, 10, 20.05, 50),
    ]
    assert coord._compute_efficiency_sample(HVACAction.HEATING, t0, small_delta)[0] is None


def test_robust_slope_returns_none_when_no_variance():
    coord = _make_coordinator()
    now = datetime.now(timezone.utc)
    samples = [
        {"t": now, "temp": 20.0, "aperture": 50, "duct": None},
        {"t": now, "temp": 20.0, "aperture": 50, "duct": None},
    ]
    assert coord._robust_slope(samples) is None


def test_update_efficiency_model_low_confidence_uses_baseline():
    coord = _make_coordinator()
    baseline, effective, confidence = coord._update_efficiency_model("v1", "cooling", 0.25)
    assert confidence < EFF_REGIME_CONFIDENCE
    assert effective == baseline


def test_get_effective_efficiency_rate_uses_model_baseline():
    coord = _make_coordinator()
    coord._efficiency_models = {
        "v1": {"heating": {"baseline": 0.2, "effective": None}}
    }
    rate = coord._get_effective_efficiency_rate("v1", HVACAction.HEATING)
    assert rate == 0.2


def test_duct_temp_conversion_and_record_sample():
    coord = _make_coordinator()
    coord.data = {
        "vents": {
            "v1": {
                "attributes": {"percent-open": 40, "duct-temperature-f": 77.0},
                "room": {"attributes": {"current-temperature-c": 22.0}},
            }
        }
    }
    duct = coord._get_vent_duct_temp("v1", coord.data)
    assert round(duct, 2) == 25.0

    coord.data["vents"]["v1"]["attributes"] = {"percent-open": 40, "duct-temperature-c": 26.0}
    duct = coord._get_vent_duct_temp("v1", coord.data)
    assert duct == 26.0

    coord._dab_state["thermo"] = {
        "started_cycle": datetime.now(timezone.utc),
        "started_running": datetime.now(timezone.utc),
        "samples": {},
    }
    coord._record_cycle_sample("thermo", "v1", coord.data)
    samples = coord._dab_state["thermo"]["samples"]["v1"]
    assert len(samples) == 1
    assert samples[0]["aperture"] == 40

    coord.data["vents"]["v1"]["attributes"].pop("percent-open")
    coord._record_cycle_sample("thermo", "v1", coord.data)
    assert len(samples) == 1


def test_record_cycle_sample_missing_temp_or_duct_returns_none():
    coord = _make_coordinator()
    coord.data = {"vents": {"v1": {"attributes": {"percent-open": 20}, "room": {"attributes": {}}}}}
    coord._dab_state["thermo"] = {
        "started_cycle": datetime.now(timezone.utc),
        "started_running": datetime.now(timezone.utc),
        "samples": {},
    }
    coord._record_cycle_sample("thermo", "v1", coord.data)
    assert coord._dab_state["thermo"]["samples"].get("v1") == []

    assert coord._get_vent_duct_temp("v1", coord.data) is None


def test_effective_rate_falls_back_to_initial():
    coord = _make_coordinator()
    rate = coord._get_effective_efficiency_rate("v2", HVACAction.COOLING)
    assert rate > 0


def test_compute_efficiency_with_unstable_duct_skips_normalization():
    coord = _make_coordinator()
    t0 = datetime.now(timezone.utc) - timedelta(minutes=15)
    samples = [
        _sample(t0, 3, 20.0, 50, 25.0),
        _sample(t0, 10, 21.0, 50, 35.0),
    ]
    efficiency, _, _ = coord._compute_efficiency_sample(HVACAction.HEATING, t0, samples)
    assert efficiency is not None
