"""Characterization tests for dab.py (pure-Python DAB algorithm helpers).

dab.py is loaded standalone by absolute path so we never import the
``hvac_vent_optimizer`` package (whose __init__ pulls in Home Assistant).

Run with ``tests`` as the rootdir so pytest does not try to collect the
parent package as a Package node (its __init__.py imports Home Assistant,
which is not installed). Either of these works:

    cd tests && python3 -m pytest test_dab.py -q
    python3 -m pytest tests/test_dab.py -q --rootdir=tests --import-mode=importlib
"""

from __future__ import annotations

import importlib.util
import math
import pathlib
import sys

import pytest

_DAB_PATH = (
    pathlib.Path(__file__).resolve().parent.parent / "custom_components" / "hvac_vent_optimizer" / "dab.py"
)
_spec = importlib.util.spec_from_file_location("hvo_dab", _DAB_PATH)
dab = importlib.util.module_from_spec(_spec)
# Register before exec so dataclasses introspection (with `from __future__
# import annotations`) can resolve the module by name.
sys.modules[_spec.name] = dab
_spec.loader.exec_module(dab)

S = dab.DEFAULT_SETTINGS


# ---------------------------------------------------------------------------
# round_to_nearest_multiple
# ---------------------------------------------------------------------------
def test_round_to_nearest_multiple_non_positive_granularity_rounds_to_int():
    assert dab.round_to_nearest_multiple(63.4, 0) == 63
    assert dab.round_to_nearest_multiple(63.6, 0) == 64
    assert dab.round_to_nearest_multiple(63.4, -5) == 63


def test_round_to_nearest_multiple_rounds_up():
    assert dab.round_to_nearest_multiple(63, 5) == 65


def test_round_to_nearest_multiple_rounds_down():
    assert dab.round_to_nearest_multiple(62, 5) == 60


def test_round_to_nearest_multiple_exact_multiple_unchanged():
    assert dab.round_to_nearest_multiple(60, 5) == 60


# ---------------------------------------------------------------------------
# rolling_average
# ---------------------------------------------------------------------------
def test_rolling_average_zero_entries_returns_zero():
    assert dab.rolling_average(10.0, 20.0, weight=1, num_entries=0) == 0
    assert dab.rolling_average(10.0, 20.0, weight=1, num_entries=-3) == 0


def test_rolling_average_no_prior_average_returns_new_number():
    # current_average None -> base becomes new_number, weighted term is zero.
    assert dab.rolling_average(None, 5.0, weight=1, num_entries=10) == pytest.approx(5.0)


def test_rolling_average_falsy_zero_average_treated_as_new_number():
    # 0.0 is falsy, so base = new_number.
    assert dab.rolling_average(0.0, 7.0, weight=1, num_entries=10) == pytest.approx(7.0)


def test_rolling_average_basic_weight_one():
    # base=10, weighted=(20-10)*1=10 -> base + weighted/num_entries = 10 + 1 = 11.
    assert dab.rolling_average(10.0, 20.0, weight=1, num_entries=10) == pytest.approx(11.0)


# ---------------------------------------------------------------------------
# has_room_reached_setpoint
# ---------------------------------------------------------------------------
def test_has_room_reached_setpoint_cooling_no_offset():
    assert dab.has_room_reached_setpoint("cooling", 24.0, 24.0) is True
    assert dab.has_room_reached_setpoint("cooling", 24.0, 23.5) is True
    assert dab.has_room_reached_setpoint("cooling", 24.0, 24.5) is False


def test_has_room_reached_setpoint_cooling_with_offset():
    # cooling reached when current_temp <= setpoint - offset
    assert dab.has_room_reached_setpoint("cooling", 24.0, 23.3, offset=0.7) is True
    assert dab.has_room_reached_setpoint("cooling", 24.0, 23.4, offset=0.7) is False


def test_has_room_reached_setpoint_heating_no_offset():
    assert dab.has_room_reached_setpoint("heating", 21.0, 21.0) is True
    assert dab.has_room_reached_setpoint("heating", 21.0, 21.5) is True
    assert dab.has_room_reached_setpoint("heating", 21.0, 20.5) is False


def test_has_room_reached_setpoint_heating_with_offset():
    # heating reached when current_temp >= setpoint + offset
    assert dab.has_room_reached_setpoint("heating", 21.0, 21.7, offset=0.7) is True
    assert dab.has_room_reached_setpoint("heating", 21.0, 21.6, offset=0.7) is False


# ---------------------------------------------------------------------------
# calculate_hvac_mode
# ---------------------------------------------------------------------------
def test_calculate_hvac_mode_picks_cooling_when_nearer_cooling_setpoint():
    assert dab.calculate_hvac_mode(temp=25.0, cooling_setpoint=24.0, heating_setpoint=20.0) == "cooling"


def test_calculate_hvac_mode_picks_heating_when_nearer_heating_setpoint():
    assert dab.calculate_hvac_mode(temp=20.5, cooling_setpoint=24.0, heating_setpoint=20.0) == "heating"


def test_calculate_hvac_mode_ties_resolve_to_heating():
    # abs distances equal -> strict < is False -> "heating".
    assert dab.calculate_hvac_mode(temp=22.0, cooling_setpoint=24.0, heating_setpoint=20.0) == "heating"


# ---------------------------------------------------------------------------
# should_pre_adjust
# ---------------------------------------------------------------------------
def test_should_pre_adjust_cooling_true():
    # current + offset(0.7) - threshold(0.2) >= setpoint
    # 23.6 + 0.5 = 24.1 >= 24.0 -> True
    assert dab.should_pre_adjust("cooling", setpoint=24.0, current_temp=23.6) is True


def test_should_pre_adjust_cooling_false():
    # 23.4 + 0.5 = 23.9 >= 24.0 -> False
    assert dab.should_pre_adjust("cooling", setpoint=24.0, current_temp=23.4) is False


def test_should_pre_adjust_heating_true():
    # current - offset(0.7) + threshold(0.2) <= setpoint
    # 21.4 - 0.5 = 20.9 <= 21.0 -> True
    assert dab.should_pre_adjust("heating", setpoint=21.0, current_temp=21.4) is True


def test_should_pre_adjust_heating_false():
    # 21.6 - 0.5 = 21.1 <= 21.0 -> False
    assert dab.should_pre_adjust("heating", setpoint=21.0, current_temp=21.6) is False


def test_should_pre_adjust_unknown_mode_returns_false():
    assert dab.should_pre_adjust("off", setpoint=24.0, current_temp=30.0) is False
    assert dab.should_pre_adjust("unknown", setpoint=24.0, current_temp=10.0) is False


# ---------------------------------------------------------------------------
# calculate_room_change_rate
# ---------------------------------------------------------------------------
def test_room_change_rate_runtime_below_min_minutes_returns_negative_one():
    assert (
        dab.calculate_room_change_rate(25.0, 24.0, total_minutes=0.5, percent_open=50, current_rate=0.0) == -1
    )


def test_room_change_rate_runtime_below_min_runtime_returns_negative_one():
    # 3 minutes is >= min_minutes_to_setpoint(1.0) but < min_runtime_for_rate_calc(5.0)
    assert (
        dab.calculate_room_change_rate(25.0, 24.0, total_minutes=3.0, percent_open=50, current_rate=0.0) == -1
    )


def test_room_change_rate_percent_open_non_positive_returns_negative_one():
    assert (
        dab.calculate_room_change_rate(25.0, 24.0, total_minutes=10.0, percent_open=0, current_rate=0.0) == -1
    )


def test_room_change_rate_tiny_diff_open_enough_returns_min_rate():
    # diff 0.05 < min_detectable_temp_change(0.1), percent_open>=30 -> min_temp_change_rate
    result = dab.calculate_room_change_rate(
        25.0, 24.95, total_minutes=10.0, percent_open=50, current_rate=0.0
    )
    assert result == pytest.approx(S.min_temp_change_rate)


def test_room_change_rate_tiny_diff_barely_open_returns_negative_one():
    # diff < min_detectable and percent_open < 30 -> -1
    result = dab.calculate_room_change_rate(
        25.0, 24.95, total_minutes=10.0, percent_open=20, current_rate=0.0
    )
    assert result == -1


def test_room_change_rate_exceeds_max_rate_returns_negative_one():
    # diff 1.0 over 10 min -> rate 0.1; p_open 0.5; approx_rate = 1/0.5 = 2.0 > max(1.5) -> -1
    result = dab.calculate_room_change_rate(25.0, 24.0, total_minutes=10.0, percent_open=50, current_rate=0.0)
    assert result == -1


def test_room_change_rate_normal_path_returns_computed_rate():
    # diff 1.0 over 10 min -> rate 0.1; current_rate 0.1 -> max_rate 0.1;
    # p_open 1.0 -> approx_rate = (0.1/0.1)/1.0 = 1.0 (within [min,max]).
    result = dab.calculate_room_change_rate(
        25.0, 24.0, total_minutes=10.0, percent_open=100, current_rate=0.1
    )
    assert result == pytest.approx(1.0)


def test_room_change_rate_below_min_rate_returns_min_rate():
    # current_rate much larger than rate drives approx_rate below min_temp_change_rate.
    # diff 1.0 / 10 = 0.1 rate; current_rate 1000 -> rate/max_rate = 0.0001;
    # /p_open(1.0) = 0.0001 < min(0.001) -> returns min_temp_change_rate.
    result = dab.calculate_room_change_rate(
        25.0, 24.0, total_minutes=10.0, percent_open=100, current_rate=1000.0
    )
    assert result == pytest.approx(S.min_temp_change_rate)


# ---------------------------------------------------------------------------
# calculate_vent_open_percentage
# ---------------------------------------------------------------------------
def test_vent_open_percentage_already_at_setpoint_returns_zero():
    # cooling, start_temp <= setpoint -> reached -> 0.0
    assert (
        dab.calculate_vent_open_percentage(
            "room",
            start_temp=24.0,
            setpoint=24.0,
            hvac_mode="cooling",
            max_rate=0.2,
            longest_time=20.0,
        )
        == 0.0
    )


def test_vent_open_percentage_zero_max_rate_returns_full_open():
    assert (
        dab.calculate_vent_open_percentage(
            "room",
            start_temp=26.0,
            setpoint=24.0,
            hvac_mode="cooling",
            max_rate=0.0,
            longest_time=20.0,
        )
        == 100.0
    )


def test_vent_open_percentage_zero_longest_time_returns_full_open():
    assert (
        dab.calculate_vent_open_percentage(
            "room",
            start_temp=26.0,
            setpoint=24.0,
            hvac_mode="cooling",
            max_rate=0.2,
            longest_time=0.0,
        )
        == 100.0
    )


def test_vent_open_percentage_mid_range_matches_exponential_formula():
    start_temp, setpoint, max_rate, longest_time = 26.0, 24.0, 0.2, 20.0
    target_rate = abs(setpoint - start_temp) / longest_time
    expected = dab.round_big_decimal(S.base_const * math.exp((target_rate / max_rate) * S.exp_const) * 100, 3)
    result = dab.calculate_vent_open_percentage(
        "room",
        start_temp=start_temp,
        setpoint=setpoint,
        hvac_mode="cooling",
        max_rate=max_rate,
        longest_time=longest_time,
    )
    assert result == pytest.approx(expected)
    assert 0.0 <= result <= 100.0


def test_vent_open_percentage_clamps_to_hundred():
    # Large target_rate relative to max_rate drives the formula well above 100.
    result = dab.calculate_vent_open_percentage(
        "room",
        start_temp=40.0,
        setpoint=24.0,
        hvac_mode="cooling",
        max_rate=0.1,
        longest_time=5.0,
    )
    assert result == 100.0


# ---------------------------------------------------------------------------
# calculate_longest_minutes_to_target
# ---------------------------------------------------------------------------
def test_longest_minutes_basic():
    vents = {
        "a": {"temp": 26.0, "rate": 0.1, "active": True},
        "b": {"temp": 25.0, "rate": 0.2, "active": True},
    }
    # a: |24-26|/0.1 = 20 ; b: |24-25|/0.2 = 5 -> longest 20
    result = dab.calculate_longest_minutes_to_target(vents, "cooling", setpoint=24.0, max_running_time=60.0)
    assert result == pytest.approx(20.0)


def test_longest_minutes_skips_inactive_when_close_inactive():
    vents = {
        "a": {"temp": 26.0, "rate": 0.1, "active": False},
        "b": {"temp": 25.0, "rate": 0.2, "active": True},
    }
    # a skipped (inactive), only b counts -> 5
    result = dab.calculate_longest_minutes_to_target(vents, "cooling", setpoint=24.0, max_running_time=60.0)
    assert result == pytest.approx(5.0)


def test_longest_minutes_skips_rooms_already_at_setpoint():
    vents = {
        "a": {"temp": 22.0, "rate": 0.1, "active": True},  # cooling, already <= setpoint
        "b": {"temp": 25.0, "rate": 0.2, "active": True},
    }
    result = dab.calculate_longest_minutes_to_target(vents, "cooling", setpoint=24.0, max_running_time=60.0)
    assert result == pytest.approx(5.0)


def test_longest_minutes_zero_rate_is_skipped_not_forcing_all_open():
    vents = {
        "a": {"temp": 26.0, "rate": 0.0, "active": True},
    }
    # rate==0 -> "no signal" -> skipped -> nothing qualifies -> -1.0
    result = dab.calculate_longest_minutes_to_target(vents, "cooling", setpoint=24.0, max_running_time=60.0)
    assert result == -1.0


def test_longest_minutes_clamps_to_max_running_time():
    vents = {
        "a": {"temp": 26.0, "rate": 0.01, "active": True},  # 2/0.01 = 200 -> clamped
    }
    result = dab.calculate_longest_minutes_to_target(vents, "cooling", setpoint=24.0, max_running_time=60.0)
    assert result == pytest.approx(60.0)


def test_longest_minutes_returns_negative_one_when_nothing_qualifies():
    vents = {
        "a": {"temp": 22.0, "rate": 0.1, "active": True},  # already at setpoint
        "b": {"temp": 26.0, "rate": 0.1, "active": False},  # inactive
    }
    result = dab.calculate_longest_minutes_to_target(vents, "cooling", setpoint=24.0, max_running_time=60.0)
    assert result == -1.0


# ---------------------------------------------------------------------------
# calculate_open_percentage_for_all_vents
# ---------------------------------------------------------------------------
def test_all_vents_inactive_with_close_inactive_returns_zero():
    vents = {"a": {"temp": 26.0, "rate": 0.1, "active": False, "name": "A"}}
    result = dab.calculate_open_percentage_for_all_vents(
        vents, "cooling", setpoint=24.0, longest_time=20.0, close_inactive=True
    )
    assert result["a"] == 0.0


def test_all_vents_low_rate_returns_full_open():
    # rate < min_temp_change_rate(0.001) -> 100.0
    vents = {"a": {"temp": 26.0, "rate": 0.0, "active": True, "name": "A"}}
    result = dab.calculate_open_percentage_for_all_vents(
        vents, "cooling", setpoint=24.0, longest_time=20.0, close_inactive=True
    )
    assert result["a"] == 100.0


def test_all_vents_delegates_to_single_vent_calculation():
    vents = {"a": {"temp": 26.0, "rate": 0.2, "active": True, "name": "A"}}
    expected = dab.calculate_vent_open_percentage("A", 26.0, 24.0, "cooling", 0.2, 20.0)
    result = dab.calculate_open_percentage_for_all_vents(
        vents, "cooling", setpoint=24.0, longest_time=20.0, close_inactive=True
    )
    assert result["a"] == pytest.approx(expected)


# ---------------------------------------------------------------------------
# adjust_for_minimum_airflow  (KEY SAFETY INVARIANT)
# ---------------------------------------------------------------------------
def _combined(rate_map, calc, ids):
    return sum(calc[v] for v in ids) / len(ids)


def test_adjust_min_airflow_raises_below_minimum_combined_flow():
    vents = {
        "a": {"temp": 26.0, "active": True},
        "b": {"temp": 24.0, "active": True},
    }
    calc = {"a": 5.0, "b": 5.0}
    before = _combined(vents, calc, ["a", "b"])
    assert before < S.min_combined_vent_flow

    result = dab.adjust_for_minimum_airflow(vents, "cooling", calc, additional_standard_vents=0, settings=S)
    after = _combined(vents, result, ["a", "b"])

    # Combined flow is driven up toward the minimum (loop terminates within
    # max_iterations); allow small tolerance for increment overshoot.
    assert after > before
    assert after == pytest.approx(S.min_combined_vent_flow, abs=2.0)


def test_adjust_min_airflow_above_minimum_returns_unchanged():
    vents = {
        "a": {"temp": 26.0, "active": True},
        "b": {"temp": 24.0, "active": True},
    }
    calc = {"a": 40.0, "b": 40.0}
    result = dab.adjust_for_minimum_airflow(vents, "cooling", calc, additional_standard_vents=0, settings=S)
    assert result is calc
    assert result == {"a": 40.0, "b": 40.0}


def test_adjust_min_airflow_active_only_falls_back_to_inactive_when_needed():
    vents = {
        "a": {"temp": 25.0, "active": False},
    }
    calc = {"a": 5.0}
    before = calc["a"]
    result = dab.adjust_for_minimum_airflow(
        vents,
        "cooling",
        calc,
        additional_standard_vents=0,
        settings=S,
        active_only=True,
        allow_inactive_if_needed=True,
    )
    # No active vents -> falls back to the inactive vent and raises its flow.
    assert result["a"] > before


def test_adjust_min_airflow_no_devices_without_inactive_fallback_returns_unchanged():
    vents = {
        "a": {"temp": 25.0, "active": False},
    }
    calc = {"a": 5.0}
    result = dab.adjust_for_minimum_airflow(
        vents,
        "cooling",
        calc,
        additional_standard_vents=0,
        settings=S,
        active_only=True,
        allow_inactive_if_needed=False,
    )
    assert result is calc
    assert result == {"a": 5.0}


# ---------------------------------------------------------------------------
# Overshoot fix (R8) — satisfied rooms must close even on the low-rate
# shortcut path of calculate_open_percentage_for_all_vents.
#
# The existing `test_all_vents_low_rate_returns_full_open` covers a room that
# still needs conditioning (temp 26 vs setpoint 24, cooling). A *satisfied*
# room (temp already past the setpoint) must NOT be reopened by the
# rate < min_temp_change_rate shortcut.
# ---------------------------------------------------------------------------
def test_all_vents_satisfied_cooling_low_rate_returns_zero():
    # Cooling, room at 22.0 is already below the 24.0 setpoint (overcooled).
    # Today the rate<min shortcut returns 100.0 (reopens a satisfied room).
    # The directional guard must close it (0.0).
    vents = {"a": {"temp": 22.0, "rate": 0.0, "active": True, "name": "A"}}
    result = dab.calculate_open_percentage_for_all_vents(
        vents, "cooling", setpoint=24.0, longest_time=20.0, close_inactive=True
    )
    assert result["a"] == 0.0


def test_all_vents_satisfied_heating_low_rate_returns_zero():
    # Heating, room at 23.0 is already above the 21.0 setpoint (overheated).
    vents = {"a": {"temp": 23.0, "rate": 0.0, "active": True, "name": "A"}}
    result = dab.calculate_open_percentage_for_all_vents(
        vents, "heating", setpoint=21.0, longest_time=20.0, close_inactive=True
    )
    assert result["a"] == 0.0


def test_all_vents_unsatisfied_low_rate_still_full_open():
    # Regression guard: a room that still needs conditioning keeps the
    # low-rate -> full-open behavior (rate unknown, push wide open).
    vents = {"a": {"temp": 26.0, "rate": 0.0, "active": True, "name": "A"}}
    result = dab.calculate_open_percentage_for_all_vents(
        vents, "cooling", setpoint=24.0, longest_time=20.0, close_inactive=True
    )
    assert result["a"] == 100.0
