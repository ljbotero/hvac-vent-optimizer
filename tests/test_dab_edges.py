import pytest

from custom_components.hvac_vent_optimizer.dab import (
    adjust_for_minimum_airflow,
    calculate_longest_minutes_to_target,
    calculate_open_percentage_for_all_vents,
    rolling_average,
    round_to_nearest_multiple,
)


def test_round_to_nearest_multiple_negative_values():
    assert round_to_nearest_multiple(-12.4, 5) == -10
    assert round_to_nearest_multiple(-12.6, 5) == -15


def test_rolling_average_none_current():
    assert rolling_average(None, 10, 1, 2) == 10


def test_open_percentage_inactive_when_not_closing():
    rate_and_temp = {
        "vent1": {"rate": 0.2, "temp": 24.0, "active": False, "name": "A"},
    }
    result = calculate_open_percentage_for_all_vents(rate_and_temp, "cooling", 22.0, 30, close_inactive=False)
    assert result["vent1"] > 0


def test_longest_minutes_with_zero_rate():
    rate_and_temp = {
        "vent1": {"rate": 0.0, "temp": 24.0, "active": True, "name": "A"},
    }
    assert calculate_longest_minutes_to_target(rate_and_temp, "cooling", 22.0, 60) == pytest.approx(-1.0)


def test_adjust_for_minimum_airflow_default_temps():
    percent_per_vent = {"vent1": 5, "vent2": 5}
    rate_and_temp = {"vent1": {}, "vent2": {}}
    result = adjust_for_minimum_airflow(rate_and_temp, "cooling", percent_per_vent, 0)
    assert result["vent1"] > 5
    assert result["vent2"] > 5
