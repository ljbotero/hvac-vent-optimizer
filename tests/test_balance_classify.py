"""Tests for balance.py room classification (Task 8 — R8/R4.2).

balance.py is a PURE module (no Home Assistant imports). It is loaded
standalone by absolute path so we never import the ``hvac_vent_optimizer``
package (whose __init__ pulls in Home Assistant, which is not installed in the
test environment). This mirrors the ``hvo_dab`` convention in test_dab.py.

    cd tests && python3 -m pytest test_balance_classify.py -q
    python3 -m pytest tests/test_balance_classify.py -q --import-mode=importlib
"""

from __future__ import annotations

import importlib.util
import pathlib
import sys

import pytest

_BALANCE_PATH = (
    pathlib.Path(__file__).resolve().parent.parent
    / "custom_components"
    / "hvac_vent_optimizer"
    / "balance.py"
)
_spec = importlib.util.spec_from_file_location("hvo_balance", _BALANCE_PATH)
balance = importlib.util.module_from_spec(_spec)
# Register before exec so dataclasses introspection (with `from __future__
# import annotations`) can resolve the module by name.
sys.modules[_spec.name] = balance
_spec.loader.exec_module(balance)


# ---------------------------------------------------------------------------
# Directional setpoint check mirrors dab.has_room_reached_setpoint semantics:
#   cooling: satisfied when temp <= setpoint - hysteresis
#   heating: satisfied when temp >= setpoint + hysteresis
# ---------------------------------------------------------------------------
class TestHasReachedSetpoint:
    def test_cooling_below_setpoint_is_satisfied_with_no_hysteresis(self):
        assert balance.has_reached_setpoint("cooling", 26.1, 25.7) is True

    def test_cooling_above_setpoint_is_not_satisfied(self):
        assert balance.has_reached_setpoint("cooling", 26.1, 27.9) is False

    def test_heating_above_setpoint_is_satisfied_with_no_hysteresis(self):
        assert balance.has_reached_setpoint("heating", 21.0, 21.4) is True

    def test_heating_below_setpoint_is_not_satisfied(self):
        assert balance.has_reached_setpoint("heating", 21.0, 20.4) is False

    def test_cooling_hysteresis_band_requires_passing_setpoint(self):
        # setpoint - hyst = 25.8; a room at 25.9 is inside the band -> not yet.
        assert balance.has_reached_setpoint("cooling", 26.1, 25.9, 0.3) is False
        assert balance.has_reached_setpoint("cooling", 26.1, 25.7, 0.3) is True

    def test_heating_hysteresis_band_requires_passing_setpoint(self):
        # setpoint + hyst = 21.3; a room at 21.1 is inside the band -> not yet.
        assert balance.has_reached_setpoint("heating", 21.0, 21.1, 0.3) is False
        assert balance.has_reached_setpoint("heating", 21.0, 21.4, 0.3) is True


# ---------------------------------------------------------------------------
# is_satisfied applies the default hysteresis band (≈0.3 °C) to avoid flapping.
# ---------------------------------------------------------------------------
class TestIsSatisfied:
    def test_default_hysteresis_is_about_point_three(self):
        assert balance.DEFAULT_HYSTERESIS_C == pytest.approx(0.3)

    def test_cooling_room_at_or_below_setpoint_is_satisfied(self):
        # Bathroom 25.7 vs setpoint 26.1 (worked example) -> satisfied.
        assert balance.is_satisfied("cooling", 26.1, 25.7) is True

    def test_heating_room_at_or_above_setpoint_is_satisfied(self):
        assert balance.is_satisfied("heating", 21.0, 21.4) is True

    def test_cooling_room_just_outside_hysteresis_band_is_not_satisfied(self):
        # 25.9 is above (setpoint - hyst)=25.8 -> still NOT satisfied.
        assert balance.is_satisfied("cooling", 26.1, 25.9) is False

    def test_heating_room_just_outside_hysteresis_band_is_not_satisfied(self):
        # 21.1 is below (setpoint + hyst)=21.3 -> still NOT satisfied.
        assert balance.is_satisfied("heating", 21.0, 21.1) is False

    def test_cooling_room_at_setpoint_is_not_satisfied_due_to_hysteresis(self):
        assert balance.is_satisfied("cooling", 26.1, 26.1) is False

    def test_explicit_hysteresis_overrides_default(self):
        # With zero hysteresis, a room exactly at setpoint counts as satisfied.
        assert balance.is_satisfied("cooling", 26.1, 26.1, hysteresis_c=0.0) is True


# ---------------------------------------------------------------------------
# Classification -> pre-floor aperture: a satisfied room maps to 0 % pre-floor
# (overshoot close, R8/R4.2). Unsatisfied rooms are deferred to the Task 9
# convergence math (pre_floor_target_pct is None).
# ---------------------------------------------------------------------------
class TestClassify:
    def test_satisfied_cooling_room_maps_to_zero_pct_pre_floor(self):
        c = balance.classify("cooling", 26.1, 25.7)
        assert c.satisfied is True
        assert c.pre_floor_target_pct == 0.0

    def test_satisfied_heating_room_maps_to_zero_pct_pre_floor(self):
        c = balance.classify("heating", 21.0, 21.4)
        assert c.satisfied is True
        assert c.pre_floor_target_pct == 0.0

    def test_unsatisfied_room_is_not_satisfied_and_defers_pre_floor(self):
        c = balance.classify("cooling", 26.1, 27.9)
        assert c.satisfied is False
        assert c.pre_floor_target_pct is None

    def test_room_just_outside_band_is_not_satisfied(self):
        c = balance.classify("cooling", 26.1, 25.9)
        assert c.satisfied is False
        assert c.pre_floor_target_pct is None

    def test_classification_is_deterministic(self):
        a = balance.classify("cooling", 26.1, 25.7)
        b = balance.classify("cooling", 26.1, 25.7)
        assert a == b
