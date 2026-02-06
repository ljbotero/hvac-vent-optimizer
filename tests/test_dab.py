import pytest

from custom_components.hvac_vent_optimizer.dab import (
    DEFAULT_SETTINGS,
    adjust_for_minimum_airflow,
    calculate_hvac_mode,
    calculate_longest_minutes_to_target,
    calculate_open_percentage_for_all_vents,
    calculate_room_change_rate,
    calculate_vent_open_percentage,
    has_room_reached_setpoint,
    rolling_average,
    round_big_decimal,
    round_to_nearest_multiple,
)


def test_calculate_hvac_mode_basic():
    assert calculate_hvac_mode(80.0, 80.0, 70.0) == "cooling"
    assert calculate_hvac_mode(70.0, 80.0, 70.0) == "heating"
    assert calculate_hvac_mode(81.0, 80.0, 70.0) == "cooling"
    assert calculate_hvac_mode(69.0, 80.0, 70.0) == "heating"


def test_has_room_reached_setpoint():
    assert has_room_reached_setpoint("cooling", 80, 75) is True
    assert has_room_reached_setpoint("cooling", 80, 81) is False
    assert has_room_reached_setpoint("heating", 70, 69) is False
    assert has_room_reached_setpoint("heating", 70, 70.01) is True


def test_round_to_nearest_multiple():
    assert round_to_nearest_multiple(12.4, 5) == 10
    assert round_to_nearest_multiple(12.5, 5) == 15
    assert round_to_nearest_multiple(95.6, 5) == 95
    assert round_to_nearest_multiple(97.5, 5) == 100


def test_rolling_average():
    assert rolling_average(10, 15, 1, 2) == 12.5
    assert rolling_average(10, 15, 0.5, 2) == 11.25
    assert rolling_average(10, 15, 0, 2) == 10
    assert rolling_average(10, 5, 1, 2) == 7.5
    assert rolling_average(10, 5, 0.5, 2) == 8.75
    assert rolling_average(10, 5, 1, 1000) == pytest.approx(9.995)
    assert rolling_average(10, 5, 1, 0) == 0
    assert rolling_average(0, 15, 1, 2) == 15


def test_calculate_room_change_rate_values():
    expected_vals = [1.0, 0.056, -1.0, 1.429, 1.0]
    actual_vals = [
        round_big_decimal(calculate_room_change_rate(20, 30, 5.0, 100, 0.03), 3),
        round_big_decimal(calculate_room_change_rate(20, 20.1, 60.0, 100, 0.03), 3),
        round_big_decimal(calculate_room_change_rate(20.768, 21, 5, 25, 0.03), 3),
        round_big_decimal(calculate_room_change_rate(19, 21, 5.2, 70, 0.03), 3),
        round_big_decimal(calculate_room_change_rate(19, 29, 10, 100, 0.03), 3),
    ]
    assert actual_vals == expected_vals


def test_calculate_room_change_rate_edge_cases():
    assert calculate_room_change_rate(0, 0, 0, 4, 0.03) == -1
    assert calculate_room_change_rate(20, 25, -5, 100, 0.03) == -1
    assert calculate_room_change_rate(20, 25, 0.5, 100, 0.03) == -1
    assert calculate_room_change_rate(20, 22, 3, 100, 0.03) == -1
    assert calculate_room_change_rate(20, 21, 10, 0, 0.5) == -1


def test_calculate_vent_open_percentage_values():
    expected_vals = [35.518, 65.063, 86.336, 12.625, 14.249, 10.324, 9.961, 32.834, 100.0]
    ret_vals = [
        calculate_vent_open_percentage("", 65, 70, "heating", 0.715, 12.6),
        calculate_vent_open_percentage("", 61, 70, "heating", 0.550, 20),
        calculate_vent_open_percentage("", 98, 82, "cooling", 0.850, 20),
        calculate_vent_open_percentage("", 84, 82, "cooling", 0.950, 20),
        calculate_vent_open_percentage("", 85, 82, "cooling", 0.950, 20),
        calculate_vent_open_percentage("", 86, 82, "cooling", 2.5, 90),
        calculate_vent_open_percentage("", 87, 82, "cooling", 2.5, 900),
        calculate_vent_open_percentage("", 87, 85, "cooling", 0.384, 10),
        calculate_vent_open_percentage("", 87, 85, "cooling", 0, 10),
    ]
    for expected, actual in zip(expected_vals, ret_vals, strict=True):
        assert actual == pytest.approx(expected, abs=0.01)


def test_calculate_open_percentage_for_all_vents():
    rate_and_temp = {
        "1222bc5e": {"rate": 0.123, "temp": 26.444, "active": True},
        "00f65b12": {"rate": 0.070, "temp": 25.784, "active": True},
        "d3f411b2": {"rate": 0.035, "temp": 26.277, "active": True},
        "472379e6": {"rate": 0.318, "temp": 24.892, "active": True},
        "6ee4c352": {"rate": 0.318, "temp": 24.892, "active": True},
        "c5e770b6": {"rate": 0.009, "temp": 23.666, "active": True},
        "e522531c": {"rate": 0.061, "temp": 25.444, "active": False},
        "acb0b95d": {"rate": 0.432, "temp": 25.944, "active": True},
    }
    expected = {
        "1222bc5e": 23.554,
        "00f65b12": 31.608,
        "d3f411b2": 100.0,
        "472379e6": 11.488,
        "6ee4c352": 11.488,
        "c5e770b6": 0.0,
        "e522531c": 0.0,
        "acb0b95d": 12.130,
    }
    result = calculate_open_percentage_for_all_vents(rate_and_temp, "cooling", 23.666, 60)
    for key, val in expected.items():
        assert result[key] == pytest.approx(val, abs=0.01)


def test_calculate_longest_minutes_to_target():
    rate_and_temp = {
        "1222bc5e": {"rate": 0.123, "temp": 26.444, "active": True, "name": "1"},
        "00f65b12": {"rate": 0.070, "temp": 25.784, "active": True, "name": "2"},
        "d3f411b2": {"rate": 0.035, "temp": 26.277, "active": True, "name": "3"},
        "472379e6": {"rate": 0.318, "temp": 24.892, "active": True, "name": "4"},
        "6ee4c352": {"rate": 0.318, "temp": 24.892, "active": True, "name": "5"},
        "c5e770b6": {"rate": 0.009, "temp": 23.666, "active": True, "name": "6"},
        "e522531c": {"rate": 0.061, "temp": 25.444, "active": False, "name": "7"},
        "acb0b95d": {"rate": 0.432, "temp": 25.944, "active": True, "name": "8"},
    }
    assert calculate_longest_minutes_to_target(rate_and_temp, "cooling", 23.666, 72) == pytest.approx(72)


def test_adjust_for_minimum_airflow_single_vent():
    percent_per_vent = {"122127": 5}
    rate_and_temp = {"122127": {"temp": 80}}
    result = adjust_for_minimum_airflow(rate_and_temp, "cooling", percent_per_vent, 0)
    assert result["122127"] == pytest.approx(30.5, abs=0.01)


def test_adjust_for_minimum_airflow_multiple_vents():
    percent_per_vent = {
        "122127": 10,
        "122129": 5,
        "122128": 10,
        "122133": 25,
        "129424": 100,
        "122132": 5,
        "122131": 5,
    }
    rate_and_temp = {
        "122127": {"temp": 80},
        "122129": {"temp": 70},
        "122128": {"temp": 75},
        "122133": {"temp": 72},
        "129424": {"temp": 78},
        "122132": {"temp": 79},
        "122131": {"temp": 76},
    }
    expected = {
        "122127": 26.33823529360,
        "122129": 5.16176470640,
        "122128": 18.25,
        "122133": 28.08823529350,
        "129424": 100,
        "122132": 18.38235294050,
        "122131": 13.97058823550,
    }
    result = adjust_for_minimum_airflow(rate_and_temp, "cooling", percent_per_vent, 0)
    for key, val in expected.items():
        assert result[key] == pytest.approx(val, abs=0.01)


def test_adjust_for_minimum_airflow_with_conventional():
    percent_per_vent = {
        "122127": 0,
        "122129": 5,
        "122128": 0,
        "122133": 5,
        "129424": 20,
        "122132": 0,
        "122131": 5,
    }
    rate_and_temp = {
        "122127": {"temp": 80},
        "122129": {"temp": 70},
        "122128": {"temp": 75},
        "122133": {"temp": 72},
        "129424": {"temp": 78},
        "122132": {"temp": 79},
        "122131": {"temp": 76},
    }
    expected = {
        "122127": 23.76470588160,
        "122129": 5.23529411840,
        "122128": 12.00,
        "122133": 9.94117646960,
        "129424": 39.05882353040,
        "122132": 21.41176470480,
        "122131": 19.35294117680,
    }
    result = adjust_for_minimum_airflow(rate_and_temp, "cooling", percent_per_vent, 4)
    for key, val in expected.items():
        assert result[key] == pytest.approx(val, abs=0.01)


def test_adjust_for_minimum_airflow_no_vents():
    assert adjust_for_minimum_airflow({}, "cooling", {}, 5) == {}


def test_adjust_for_minimum_airflow_temperature_proportions():
    percent_per_vent = {"hotRoom": 5, "coldRoom": 5}
    rate_and_temp = {"hotRoom": {"temp": 30}, "coldRoom": {"temp": 15}}
    result = adjust_for_minimum_airflow(rate_and_temp, "cooling", percent_per_vent, 0)
    assert result["hotRoom"] > result["coldRoom"]


def test_adjust_for_minimum_airflow_heating_vs_cooling():
    percent_per_vent = {"room1": 5, "room2": 5}
    rate_and_temp = {"room1": {"temp": 25}, "room2": {"temp": 20}}
    cooling_result = adjust_for_minimum_airflow(rate_and_temp, "cooling", percent_per_vent, 0)
    heating_result = adjust_for_minimum_airflow(rate_and_temp, "heating", percent_per_vent, 0)
    assert cooling_result["room1"] > cooling_result["room2"]
    assert heating_result["room1"] > 5 and heating_result["room2"] > 5


def test_adjust_for_minimum_airflow_iteration_limit():
    percent_per_vent = {f"vent{i}": 1 for i in range(10)}
    rate_and_temp = {f"vent{i}": {"temp": 20 + i} for i in range(10)}
    result = adjust_for_minimum_airflow(rate_and_temp, "cooling", percent_per_vent, 0)
    assert len(result) == 10
    assert all(val > 1 for val in result.values())
