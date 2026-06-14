"""Entity-level tests for Task 24 observability surfaces (R13/R14/R5.4/R25.11).

These test the *real* ``sensor.py`` / ``binary_sensor.py`` entity classes wired
to a coordinator whose observability state is set directly, so the entities are
verified in isolation from the apply path.
"""

from __future__ import annotations

from hvac_vent_optimizer import const
from tests._fakes import FakeApi, FakeEntry, FakeHass


def _coord(options=None, data=None):
    from hvac_vent_optimizer.coordinator import FlairCoordinator

    opts = {const.CONF_VENT_BRAND: const.BRAND_FLAIR, const.CONF_DAB_ENABLED: True}
    if options:
        opts.update(options)
    entry = FakeEntry(
        data={
            const.CONF_STRUCTURE_ID: "s1",
            const.CONF_CLIENT_ID: "id",
            const.CONF_CLIENT_SECRET: "sec",
        },
        options=opts,
    )
    hass = FakeHass(unit="°C")
    coord = FlairCoordinator(hass, FakeApi(), entry)
    coord.data = data if data is not None else {"vents": {}, "pucks": {}}
    return coord


# ---------------------------------------------------------------------------
# New top-level DAB observability sensors (R13.2/R14.1)
# ---------------------------------------------------------------------------
def test_new_dab_sensors_report_coordinator_values():
    from hvac_vent_optimizer import sensor as sensor_mod

    coord = _coord()
    coord._last_active_spread = 3.09
    coord._last_max_active_error = 1.8
    coord._recalc_events = []
    coord._hold_events = []

    descriptions = {
        sensor_mod.ACTIVE_ROOM_SPREAD_DESCRIPTION: 3.09,
        sensor_mod.MAX_ACTIVE_ERROR_DESCRIPTION: 1.8,
        sensor_mod.RECALC_24H_DESCRIPTION: 0,
        sensor_mod.HOLDS_24H_DESCRIPTION: 0,
    }
    for desc, expected in descriptions.items():
        ent = sensor_mod.DabHoldStatusSensor(coord, "e1", desc)
        assert ent.native_value == expected, desc.key


def test_temperature_delta_sensors_convert_to_fahrenheit_on_us_system():
    # On a US (Fahrenheit) system the °C *delta* metrics report °F deltas
    # (x1.8, no offset) with a °F unit; non-temperature metrics are untouched.
    from homeassistant.const import UnitOfTemperature

    from hvac_vent_optimizer import sensor as sensor_mod

    coord = _coord()
    coord.hass.config.units.temperature_unit = UnitOfTemperature.FAHRENHEIT
    coord._last_active_spread = 2.0
    coord._last_max_active_error = 1.5

    spread = sensor_mod.DabHoldStatusSensor(coord, "e1", sensor_mod.ACTIVE_ROOM_SPREAD_DESCRIPTION)
    error = sensor_mod.DabHoldStatusSensor(coord, "e1", sensor_mod.MAX_ACTIVE_ERROR_DESCRIPTION)
    assert spread.native_value == 3.6  # 2.0 * 1.8
    assert spread.native_unit_of_measurement == UnitOfTemperature.FAHRENHEIT
    assert error.native_value == 2.7  # 1.5 * 1.8
    assert error.native_unit_of_measurement == UnitOfTemperature.FAHRENHEIT

    # A non-temperature metric on the same class is unaffected.
    ratio = sensor_mod.DabHoldStatusSensor(coord, "e1", sensor_mod.HOLD_RATIO_DESCRIPTION)
    assert ratio.native_unit_of_measurement == sensor_mod.HOLD_RATIO_DESCRIPTION.native_unit_of_measurement


def test_temperature_delta_sensors_stay_celsius_on_metric_system():
    from homeassistant.const import UnitOfTemperature

    from hvac_vent_optimizer import sensor as sensor_mod

    coord = _coord()  # FakeHass default unit is °C (metric)
    coord._last_active_spread = 2.0
    spread = sensor_mod.DabHoldStatusSensor(coord, "e1", sensor_mod.ACTIVE_ROOM_SPREAD_DESCRIPTION)
    assert spread.native_value == 2.0
    assert spread.native_unit_of_measurement == UnitOfTemperature.CELSIUS


def test_active_room_spread_sensor_has_celsius_unit():
    from homeassistant.const import UnitOfTemperature

    from hvac_vent_optimizer import sensor as sensor_mod

    desc = sensor_mod.ACTIVE_ROOM_SPREAD_DESCRIPTION
    assert desc.native_unit_of_measurement == UnitOfTemperature.CELSIUS
    assert desc.key == "dab_active_room_spread"


def test_recalc_and_holds_24h_sensor_keys():
    from hvac_vent_optimizer import sensor as sensor_mod

    assert sensor_mod.RECALC_24H_DESCRIPTION.key == "dab_recalculations_24h"
    assert sensor_mod.HOLDS_24H_DESCRIPTION.key == "dab_holds_24h"


# ---------------------------------------------------------------------------
# Per-strategy spread metric sensors (R13.4)
# ---------------------------------------------------------------------------
def test_strategy_spread_metric_sensors_exist():
    from hvac_vent_optimizer import sensor as sensor_mod

    keys = {d.key for d in sensor_mod.STRATEGY_METRIC_DESCRIPTIONS}
    assert "dab_avg_spread" in keys
    assert "dab_max_spread" in keys
    assert "dab_time_above_guardrail" in keys


def test_strategy_spread_metric_sensor_reads_value():
    from hvac_vent_optimizer import sensor as sensor_mod

    coord = _coord()
    coord._last_strategy = "balance"
    coord._strategy_metrics = {"balance": {"avg_spread": 1.23, "max_spread": 4.5}}
    desc = next(d for d in sensor_mod.STRATEGY_METRIC_DESCRIPTIONS if d.key == "dab_avg_spread")
    ent = sensor_mod.FlairStrategyMetricSensor(coord, "e1", desc)
    assert ent.native_value == 1.23


# ---------------------------------------------------------------------------
# Per-room signed-error + efficiency diagnostic attributes (R13.3/R25.11)
# ---------------------------------------------------------------------------
def test_room_sensor_exposes_signed_error_and_airflow_limited():
    from hvac_vent_optimizer import sensor as sensor_mod

    data = {
        "vents": {
            "v1": {
                "id": "v1",
                "name": "Bedroom 2 Vent",
                "attributes": {"percent-open": 100},
                "room": {"id": "room1", "attributes": {"name": "Bedroom 2"}},
            }
        },
        "pucks": {},
    }
    coord = _coord(data=data)
    coord._room_signed_errors = {"room1": 3.9}
    coord._airflow_limited_rooms = {"room1"}
    desc = next(d for d in sensor_mod.ROOM_SENSOR_DESCRIPTIONS if d.key == "room_temperature")
    ent = sensor_mod.FlairRoomSensor(coord, "e1", "room1", desc)
    attrs = ent.extra_state_attributes or {}
    assert attrs.get("signed_error_c") == 3.9
    assert attrs.get("airflow_limited") is True


# ---------------------------------------------------------------------------
# Per-vent leak diagnostic attribute (R25.11)
# ---------------------------------------------------------------------------
def test_vent_efficiency_sensor_exposes_leak_attribute():
    from hvac_vent_optimizer import sensor as sensor_mod

    data = {
        "vents": {"v1": {"id": "v1", "name": "V1", "attributes": {}, "room": {"id": "r1"}}},
        "pucks": {},
    }
    coord = _coord(data=data)
    desc = next(d for d in sensor_mod.VENT_SENSOR_DESCRIPTIONS if d.key == "cooling_efficiency")
    ent = sensor_mod.FlairVentSensor(coord, "e1", "v1", desc)
    attrs = ent.extra_state_attributes or {}
    assert "leak" in attrs
    assert isinstance(attrs["leak"], float)


# ---------------------------------------------------------------------------
# Per-room airflow-limited binary sensor (R5.4)
# ---------------------------------------------------------------------------
def test_room_airflow_limited_binary_sensor():
    from hvac_vent_optimizer import binary_sensor as bs_mod

    data = {
        "vents": {
            "v1": {
                "id": "v1",
                "name": "Bedroom 2 Vent",
                "attributes": {"percent-open": 100},
                "room": {"id": "room1", "attributes": {"name": "Bedroom 2"}},
            }
        },
        "pucks": {},
    }
    coord = _coord(data=data)
    coord._airflow_limited_rooms = {"room1"}
    ent = bs_mod.FlairRoomAirflowLimitedBinarySensor(coord, "e1", "room1")
    assert ent.is_on is True
    coord._airflow_limited_rooms = set()
    assert ent.is_on is False
