"""Dynamic Airflow Balancing (DAB) algorithm helpers."""
from __future__ import annotations

from dataclasses import dataclass
import math


@dataclass(frozen=True)
class DabSettings:
    """Configuration values for DAB calculations (Celsius-based)."""

    max_temp_change_rate: float = 1.5
    min_temp_change_rate: float = 0.001
    setpoint_offset: float = 0.7
    vent_pre_adjust_threshold: float = 0.2
    max_minutes_to_setpoint: float = 60.0
    min_minutes_to_setpoint: float = 1.0
    min_runtime_for_rate_calc: float = 5.0
    temp_sensor_accuracy: float = 0.5
    min_detectable_temp_change: float = 0.1
    min_combined_vent_flow: float = 30.0
    increment_percentage: float = 1.5
    max_standard_vents: int = 15
    max_iterations: int = 500
    standard_vent_default_open: float = 50.0
    rebalancing_tolerance: float = 0.5
    temp_boundary_adjustment: float = 0.1
    thermostat_hysteresis: float = 0.6
    base_const: float = 0.0991
    exp_const: float = 2.3


DEFAULT_SETTINGS = DabSettings()


def round_big_decimal(value: float, scale: int = 3) -> float:
    return round(float(value), scale)


def round_to_nearest_multiple(value: float, granularity: int) -> int:
    if granularity <= 0:
        return int(round(value))
    quotient = value / granularity
    if quotient >= 0:
        rounded = math.floor(quotient + 0.5)
    else:
        rounded = math.ceil(quotient - 0.5)
    return int(rounded * granularity)


def rolling_average(current_average: float | None, new_number: float, weight: float = 1, num_entries: int = 10) -> float:
    if num_entries <= 0:
        return 0
    base = new_number if not current_average else current_average
    total = base * (num_entries - 1)
    weighted_value = (new_number - base) * weight
    total += base + weighted_value
    return total / num_entries


def has_room_reached_setpoint(hvac_mode: str, setpoint: float, current_temp: float, offset: float = 0) -> bool:
    if hvac_mode == "cooling":
        return current_temp <= setpoint - offset
    return current_temp >= setpoint + offset


def calculate_hvac_mode(temp: float, cooling_setpoint: float, heating_setpoint: float) -> str:
    return "cooling" if abs(temp - cooling_setpoint) < abs(temp - heating_setpoint) else "heating"


def should_pre_adjust(
    hvac_mode: str,
    setpoint: float,
    current_temp: float,
    settings: DabSettings = DEFAULT_SETTINGS,
) -> bool:
    if hvac_mode == "cooling":
        return current_temp + settings.setpoint_offset - settings.vent_pre_adjust_threshold >= setpoint
    if hvac_mode == "heating":
        return current_temp - settings.setpoint_offset + settings.vent_pre_adjust_threshold <= setpoint
    return False


def calculate_room_change_rate(
    last_start_temp: float,
    current_temp: float,
    total_minutes: float,
    percent_open: int,
    current_rate: float,
    settings: DabSettings = DEFAULT_SETTINGS,
) -> float:
    if total_minutes < settings.min_minutes_to_setpoint:
        return -1
    if total_minutes < settings.min_runtime_for_rate_calc:
        return -1
    if percent_open <= 0:
        return -1

    diff_temps = abs(last_start_temp - current_temp)

    if diff_temps < settings.min_detectable_temp_change:
        if percent_open >= 30:
            return settings.min_temp_change_rate
        return -1

    if diff_temps < settings.temp_sensor_accuracy:
        diff_temps = max(diff_temps, settings.min_detectable_temp_change)

    rate = diff_temps / total_minutes
    p_open = percent_open / 100
    max_rate = max(rate, current_rate)
    approx_rate = (rate / max_rate) / p_open if max_rate else 0

    if approx_rate > settings.max_temp_change_rate:
        return -1
    if approx_rate < settings.min_temp_change_rate:
        return settings.min_temp_change_rate
    return approx_rate


def calculate_vent_open_percentage(
    room_name: str,
    start_temp: float,
    setpoint: float,
    hvac_mode: str,
    max_rate: float,
    longest_time: float,
    settings: DabSettings = DEFAULT_SETTINGS,
) -> float:
    if has_room_reached_setpoint(hvac_mode, setpoint, start_temp):
        return 0.0
    if max_rate <= 0 or longest_time <= 0:
        return 100.0

    target_rate = abs(setpoint - start_temp) / longest_time
    percentage_open = settings.base_const * math.exp((target_rate / max_rate) * settings.exp_const)
    percentage_open = round_big_decimal(percentage_open * 100, 3)

    if percentage_open < 0:
        return 0.0
    if percentage_open > 100:
        return 100.0
    return percentage_open


def calculate_open_percentage_for_all_vents(
    rate_and_temp_per_vent_id: dict[str, dict[str, float | bool | str]],
    hvac_mode: str,
    setpoint: float,
    longest_time: float,
    close_inactive: bool = True,
    settings: DabSettings = DEFAULT_SETTINGS,
) -> dict[str, float]:
    percent_open_map: dict[str, float] = {}
    for vent_id, state_val in rate_and_temp_per_vent_id.items():
        percentage_open = 0.0
        active = bool(state_val.get("active", True))
        rate = float(state_val.get("rate", 0) or 0)

        if close_inactive and not active:
            percentage_open = 0.0
        elif rate < settings.min_temp_change_rate:
            percentage_open = 100.0
        else:
            percentage_open = calculate_vent_open_percentage(
                str(state_val.get("name", "")),
                float(state_val.get("temp", 0) or 0),
                setpoint,
                hvac_mode,
                rate,
                longest_time,
                settings,
            )

        percent_open_map[vent_id] = percentage_open
    return percent_open_map


def calculate_longest_minutes_to_target(
    rate_and_temp_per_vent_id: dict[str, dict[str, float | bool | str]],
    hvac_mode: str,
    setpoint: float,
    max_running_time: float,
    close_inactive: bool = True,
    settings: DabSettings = DEFAULT_SETTINGS,
) -> float:
    longest_time = -1.0
    for state_val in rate_and_temp_per_vent_id.values():
        active = bool(state_val.get("active", True))
        temp = float(state_val.get("temp", 0) or 0)
        rate = float(state_val.get("rate", 0) or 0)

        minutes_to_target = -1.0
        if close_inactive and not active:
            continue
        if has_room_reached_setpoint(hvac_mode, setpoint, temp):
            continue
        if rate > 0:
            minutes_to_target = abs(setpoint - temp) / rate
            if minutes_to_target > max_running_time * 2:
                minutes_to_target = max_running_time
        elif rate == 0:
            # Treat unknown/zero rates as "no signal" so one vent doesn't force all-open.
            continue

        if minutes_to_target > max_running_time:
            minutes_to_target = max_running_time

        longest_time = max(longest_time, minutes_to_target)

    return longest_time


def adjust_for_minimum_airflow(
    rate_and_temp_per_vent_id: dict[str, dict[str, float | bool | str]],
    hvac_mode: str,
    calculated_percent_open: dict[str, float],
    additional_standard_vents: int,
    settings: DabSettings = DEFAULT_SETTINGS,
) -> dict[str, float]:
    total_device_count = additional_standard_vents if additional_standard_vents > 0 else 0
    sum_percentages = total_device_count * settings.standard_vent_default_open

    for percent in calculated_percent_open.values():
        total_device_count += 1
        sum_percentages += percent or 0

    if total_device_count <= 0:
        return calculated_percent_open

    temps = [float(v.get("temp", 0) or 0) for v in rate_and_temp_per_vent_id.values()]
    if not temps:
        min_temp = 20.0
        max_temp = 25.0
    else:
        min_temp = min(temps) - settings.temp_boundary_adjustment
        max_temp = max(temps) + settings.temp_boundary_adjustment

    combined_flow_percentage = sum_percentages / total_device_count
    if combined_flow_percentage >= settings.min_combined_vent_flow:
        return calculated_percent_open

    target_percent_sum = settings.min_combined_vent_flow * total_device_count
    diff_percentage_sum = target_percent_sum - sum_percentages

    iterations = 0
    while diff_percentage_sum > 0 and iterations < settings.max_iterations:
        iterations += 1
        for vent_id, state_val in rate_and_temp_per_vent_id.items():
            percent_open_val = calculated_percent_open.get(vent_id, 0) or 0
            if percent_open_val >= 100:
                continue

            if max_temp == min_temp:
                proportion = 0
            elif hvac_mode == "cooling":
                proportion = (float(state_val.get("temp", 0) or 0) - min_temp) / (max_temp - min_temp)
            else:
                proportion = (max_temp - float(state_val.get("temp", 0) or 0)) / (max_temp - min_temp)

            increment = settings.increment_percentage * proportion
            percent_open_val += increment
            calculated_percent_open[vent_id] = percent_open_val
            diff_percentage_sum -= increment
            if diff_percentage_sum <= 0:
                break

    return calculated_percent_open
