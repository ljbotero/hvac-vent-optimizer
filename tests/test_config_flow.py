"""Config/options-flow tests for the balance-strategy configuration (Task 23).

Covers R18.1/R18.2 (new ``CONF_*``/``DEFAULT_*`` + validation/clamping), R5.1,
R6.4, R7.1/7.3/7.8, R12.2/12.3/12.5: the ``balance`` strategy is selectable, the
new tunables are present with documented defaults, every numeric is clamped to
its documented range on save, the outdoor-temp context source is reachable, and
the per-vent door sensor is reachable in the assignments step (closing the old
dead-config path).

The Home Assistant / voluptuous stubs installed by ``conftest`` make schema-level
``vol.Range`` a no-op, so out-of-range values flow through to the handler — which
is exactly why the handler must clamp explicitly. These tests drive the handler
directly and inspect the saved options dict.
"""

from __future__ import annotations

import asyncio
import json
import pathlib

import pytest

from hvac_vent_optimizer import config_flow, const
from tests._fakes import FakeEntry

_ROOT = pathlib.Path(__file__).resolve().parent.parent / "custom_components" / "hvac_vent_optimizer"


def _run(coro):
    return asyncio.run(coro)


def _baseline_algorithm_input() -> dict:
    """A complete, in-range ``algorithm_settings`` submission."""
    return {
        const.CONF_DAB_ENABLED: True,
        const.CONF_DAB_FORCE_MANUAL: True,
        const.CONF_OPEN_INACTIVE_ROOMS: True,
        const.CONF_VENT_GRANULARITY: "5",
        const.CONF_POLL_INTERVAL_ACTIVE: 3,
        const.CONF_POLL_INTERVAL_IDLE: 10,
        const.CONF_INITIAL_EFFICIENCY_PERCENT: 50,
        const.CONF_NOTIFY_EFFICIENCY_CHANGES: True,
        const.CONF_LOG_EFFICIENCY_CHANGES: True,
        const.CONF_CONTROL_STRATEGY: const.CONTROL_STRATEGY_BALANCE,
        const.CONF_MIN_ADJUSTMENT_PERCENT: 10,
        const.CONF_MIN_ADJUSTMENT_INTERVAL: 30,
        const.CONF_TEMP_ERROR_OVERRIDE: 1.0,
        const.CONF_DEADBAND_PERCENT: 15,
        const.CONF_DEVIATION_THRESHOLD: 1.0,
        const.CONF_MAX_RECALC_PER_CYCLE: 3,
        const.CONF_MAX_ADJUSTMENT_BATCHES_PER_CYCLE: 3,
        const.CONF_MAX_ADJUSTMENT_BATCHES_PER_WINDOW: 4,
        const.CONF_ADJUSTMENT_WINDOW_MINUTES: 120,
        # New balance tunables (Task 23):
        const.CONF_SAFETY_FLOOR_PCT: 40,
        const.CONF_SPREAD_GUARDRAIL_C: 1.0,
        const.CONF_SPREAD_IMPROVEMENT_DEADBAND_C: 0.3,
        const.CONF_CROSSCOUPLING_ENABLED: True,
        const.CONF_AIRFLOW_LIMITED_MARGIN_PCT: 5,
        const.CONF_AIRFLOW_LIMITED_ERROR_C: 0.5,
        const.CONF_SHORT_CYCLE_GAP_MIN: 10,
    }


def _options_flow(options=None, data=None):
    entry = FakeEntry(data=data or {}, options=options or {})
    return config_flow.HvacVentOptimizerOptionsFlow(entry)


# --------------------------------------------------------------------------- #
# const: new keys, defaults and documented ranges
# --------------------------------------------------------------------------- #
def test_balance_registered_in_strategies():
    assert const.CONTROL_STRATEGY_BALANCE == "balance"
    assert "balance" in const.CONTROL_STRATEGIES


@pytest.mark.parametrize(
    "conf, default, rng",
    [
        (
            "CONF_SAFETY_FLOOR_PCT",
            ("DEFAULT_SAFETY_FLOOR_PCT", 40),
            ("SAFETY_FLOOR_PCT_RANGE", (20, 90)),
        ),
        (
            "CONF_SPREAD_GUARDRAIL_C",
            ("DEFAULT_SPREAD_GUARDRAIL_C", 1.0),
            ("SPREAD_GUARDRAIL_C_RANGE", (0.2, 5.0)),
        ),
        (
            "CONF_SPREAD_IMPROVEMENT_DEADBAND_C",
            ("DEFAULT_SPREAD_IMPROVEMENT_DEADBAND_C", 0.3),
            ("SPREAD_IMPROVEMENT_DEADBAND_C_RANGE", (0.0, 2.0)),
        ),
        (
            "CONF_AIRFLOW_LIMITED_MARGIN_PCT",
            ("DEFAULT_AIRFLOW_LIMITED_MARGIN_PCT", 5),
            ("AIRFLOW_LIMITED_MARGIN_PCT_RANGE", (0, 20)),
        ),
        (
            "CONF_AIRFLOW_LIMITED_ERROR_C",
            ("DEFAULT_AIRFLOW_LIMITED_ERROR_C", 0.5),
            ("AIRFLOW_LIMITED_ERROR_C_RANGE", (0.1, 3.0)),
        ),
        (
            "CONF_SHORT_CYCLE_GAP_MIN",
            ("DEFAULT_SHORT_CYCLE_GAP_MIN", 10),
            ("SHORT_CYCLE_GAP_MIN_RANGE", (0, 60)),
        ),
    ],
)
def test_new_numeric_constants_defaults_and_ranges(conf, default, rng):
    assert hasattr(const, conf)
    default_name, default_value = default
    assert getattr(const, default_name) == default_value
    range_name, range_value = rng
    assert getattr(const, range_name) == range_value


def test_crosscoupling_constant_default_true():
    assert const.CONF_CROSSCOUPLING_ENABLED == "crosscoupling_enabled"
    assert const.DEFAULT_CROSSCOUPLING_ENABLED is True


def test_conf_values_match_alloc_settings_field_names():
    """The option keys must equal the balance.AllocSettings field names the
    coordinator reads, so config actually reaches the allocator."""
    assert const.CONF_SAFETY_FLOOR_PCT == "safety_floor_pct"
    assert const.CONF_SPREAD_GUARDRAIL_C == "spread_guardrail_c"
    assert const.CONF_SPREAD_IMPROVEMENT_DEADBAND_C == "spread_improvement_deadband_c"
    assert const.CONF_AIRFLOW_LIMITED_MARGIN_PCT == "airflow_limited_margin_pct"
    assert const.CONF_AIRFLOW_LIMITED_ERROR_C == "airflow_limited_error_c"


# --------------------------------------------------------------------------- #
# options flow: algorithm step accepts balance + persists new tunables
# --------------------------------------------------------------------------- #
def test_algorithm_step_persists_balance_and_new_tunables():
    flow = _options_flow()
    result = _run(flow.async_step_algorithm_settings(_baseline_algorithm_input()))
    assert result["type"] == "create_entry"
    data = result["data"]
    assert data[const.CONF_CONTROL_STRATEGY] == "balance"
    assert data[const.CONF_OPEN_INACTIVE_ROOMS] is True
    assert data[const.CONF_SAFETY_FLOOR_PCT] == 40
    assert data[const.CONF_SPREAD_GUARDRAIL_C] == 1.0
    assert data[const.CONF_SPREAD_IMPROVEMENT_DEADBAND_C] == 0.3
    assert data[const.CONF_CROSSCOUPLING_ENABLED] is True
    assert data[const.CONF_AIRFLOW_LIMITED_MARGIN_PCT] == 5
    assert data[const.CONF_AIRFLOW_LIMITED_ERROR_C] == 0.5
    assert data[const.CONF_SHORT_CYCLE_GAP_MIN] == 10


@pytest.mark.parametrize(
    "conf, too_low, too_high, lo, hi",
    [
        (const.CONF_SAFETY_FLOOR_PCT, 5, 99, 20, 90),
        (const.CONF_SPREAD_GUARDRAIL_C, 0.0, 99.0, 0.2, 5.0),
        (const.CONF_SPREAD_IMPROVEMENT_DEADBAND_C, -1.0, 9.0, 0.0, 2.0),
        (const.CONF_AIRFLOW_LIMITED_MARGIN_PCT, -3, 99, 0, 20),
        (const.CONF_AIRFLOW_LIMITED_ERROR_C, 0.0, 99.0, 0.1, 3.0),
        (const.CONF_SHORT_CYCLE_GAP_MIN, -5, 999, 0, 60),
    ],
)
def test_algorithm_step_clamps_numerics(conf, too_low, too_high, lo, hi):
    flow = _options_flow()
    low_in = _baseline_algorithm_input()
    low_in[conf] = too_low
    low_data = _run(flow.async_step_algorithm_settings(low_in))["data"]
    assert low_data[conf] == lo

    flow2 = _options_flow()
    high_in = _baseline_algorithm_input()
    high_in[conf] = too_high
    high_data = _run(flow2.async_step_algorithm_settings(high_in))["data"]
    assert high_data[conf] == hi


def test_algorithm_step_clamps_garbage_to_default():
    flow = _options_flow()
    bad = _baseline_algorithm_input()
    bad[const.CONF_SAFETY_FLOOR_PCT] = "not-a-number"
    data = _run(flow.async_step_algorithm_settings(bad))["data"]
    assert data[const.CONF_SAFETY_FLOOR_PCT] == const.DEFAULT_SAFETY_FLOOR_PCT


def test_algorithm_step_form_renders_with_defaults():
    flow = _options_flow()
    result = _run(flow.async_step_algorithm_settings(None))
    assert result["type"] == "form"
    assert result["step_id"] == "algorithm_settings"


# --------------------------------------------------------------------------- #
# options flow: context step (outdoor temp) reachable
# --------------------------------------------------------------------------- #
def test_menu_includes_context_step():
    flow = _options_flow(options={const.CONF_VENT_BRAND: const.BRAND_FLAIR})
    result = _run(flow.async_step_menu(None))
    assert result["type"] == "menu"
    assert "context_settings" in result["menu_options"]


def test_context_step_persists_outdoor_temp_entity():
    flow = _options_flow()
    result = _run(flow.async_step_context_settings({const.CONF_OUTDOOR_TEMP_ENTITY: "sensor.outdoor"}))
    assert result["type"] == "create_entry"
    assert result["data"][const.CONF_OUTDOOR_TEMP_ENTITY] == "sensor.outdoor"


def test_context_step_allows_empty_outdoor_temp_entity():
    flow = _options_flow()
    result = _run(flow.async_step_context_settings({}))
    assert result["type"] == "create_entry"
    # Unset is allowed (degrades to the mild band); nothing stored or None.
    assert not result["data"].get(const.CONF_OUTDOOR_TEMP_ENTITY)


# --------------------------------------------------------------------------- #
# options flow: per-vent door sensor reachable in assignments (R12.2)
# --------------------------------------------------------------------------- #
def test_vent_assignments_persists_door_sensor():
    flow = _options_flow(options={const.CONF_VENT_BRAND: const.BRAND_FLAIR})
    flow._vents = [{"id": "v1", "name": "Office"}]

    # First render builds the dynamic key maps.
    _run(flow.async_step_vent_assignments(None))
    therm_key = next(k for k, v in flow._vent_key_map.items() if v == "v1")
    temp_key = next(k for k, v in flow._temp_sensor_key_map.items() if v == "v1")
    door_key = next(k for k, v in flow._door_sensor_key_map.items() if v == "v1")

    user_input = {
        therm_key: "climate.house",
        temp_key: "sensor.office_temp",
        door_key: "binary_sensor.office_door",
    }
    result = _run(flow.async_step_vent_assignments(user_input))
    assert result["type"] == "create_entry"
    assignment = result["data"][const.CONF_VENT_ASSIGNMENTS]["v1"]
    assert assignment[const.CONF_DOOR_SENSOR_ENTITY] == "binary_sensor.office_door"
    assert assignment[const.CONF_THERMOSTAT_ENTITY] == "climate.house"


# --------------------------------------------------------------------------- #
# translations cover the new fields/steps
# --------------------------------------------------------------------------- #
def _translations():
    return json.loads((_ROOT / "translations/en.json").read_text(encoding="utf-8"))


def test_translations_have_new_algorithm_fields():
    strings = _translations()
    fields = strings["options"]["step"]["algorithm_settings"]["data"]
    for key in (
        const.CONF_SAFETY_FLOOR_PCT,
        const.CONF_SPREAD_GUARDRAIL_C,
        const.CONF_SPREAD_IMPROVEMENT_DEADBAND_C,
        const.CONF_CROSSCOUPLING_ENABLED,
        const.CONF_AIRFLOW_LIMITED_MARGIN_PCT,
        const.CONF_AIRFLOW_LIMITED_ERROR_C,
        const.CONF_SHORT_CYCLE_GAP_MIN,
    ):
        assert key in fields, f"missing translation for {key}"


def test_translations_have_context_step():
    strings = _translations()
    steps = strings["options"]["step"]
    assert "context_settings" in steps
    assert const.CONF_OUTDOOR_TEMP_ENTITY in steps["context_settings"]["data"]
