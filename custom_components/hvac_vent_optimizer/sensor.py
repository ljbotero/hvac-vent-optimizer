"""Sensor platform for Flair pucks and vents."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC

from homeassistant.components.sensor import SensorEntity, SensorEntityDescription, SensorStateClass
from homeassistant.const import (
    PERCENTAGE,
    SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
    UnitOfPressure,
    UnitOfTemperature,
)
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .const import DOMAIN


@dataclass(frozen=True)
class FlairPuckSensorDescription(SensorEntityDescription):
    """Describe a Flair puck sensor."""

    attribute: str | None = None


@dataclass(frozen=True)
class FlairVentSensorDescription(SensorEntityDescription):
    """Describe a Flair vent sensor."""

    attribute: str | None = None
    efficiency_mode: str | None = None


@dataclass(frozen=True)
class FlairVentMetricSensorDescription(SensorEntityDescription):
    """Describe a computed vent metric sensor."""

    metric_key: str | None = None


@dataclass(frozen=True)
class FlairRoomSensorDescription(SensorEntityDescription):
    """Describe a Flair room sensor."""

    room_field: str | None = None


@dataclass(frozen=True)
class StrategyMetricSensorDescription(SensorEntityDescription):
    """Describe a strategy effectiveness metric sensor."""

    metric_key: str | None = None


PUCK_SENSOR_DESCRIPTIONS: tuple[FlairPuckSensorDescription, ...] = (
    FlairPuckSensorDescription(
        key="temperature",
        name="Temperature",
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        attribute="current-temperature-c",
        device_class="temperature",
        state_class=SensorStateClass.MEASUREMENT,
    ),
    FlairPuckSensorDescription(
        key="humidity",
        name="Humidity",
        native_unit_of_measurement="%",
        attribute="current-humidity",
        device_class="humidity",
        state_class=SensorStateClass.MEASUREMENT,
    ),
    FlairPuckSensorDescription(
        key="voltage",
        name="Voltage",
        native_unit_of_measurement="V",
        attribute="system-voltage",
    ),
    FlairPuckSensorDescription(
        key="battery",
        name="Battery",
        native_unit_of_measurement="%",
        attribute="battery",
        device_class="battery",
    ),
    FlairPuckSensorDescription(
        key="pressure",
        name="Pressure",
        native_unit_of_measurement=UnitOfPressure.KPA,
        attribute="room-pressure",
        device_class="pressure",
        state_class=SensorStateClass.MEASUREMENT,
    ),
    FlairPuckSensorDescription(
        key="rssi",
        name="Signal",
        native_unit_of_measurement=SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
        attribute="rssi",
        device_class="signal_strength",
    ),
)

SYSTEM_SENSOR_DESCRIPTION = SensorEntityDescription(
    key="strategy_effectiveness",
    name="DAB Strategy Effectiveness",
)
HOLD_STATUS_DESCRIPTION = SensorEntityDescription(
    key="dab_hold_status",
    name="DAB Hold Status",
    icon="mdi:pause-circle-outline",
)
DEVIATION_MAX_DESCRIPTION = SensorEntityDescription(
    key="dab_deviation_max",
    name="DAB Max Deviation",
    native_unit_of_measurement=UnitOfTemperature.CELSIUS,
    state_class=SensorStateClass.MEASUREMENT,
    icon="mdi:thermometer-chevron-up",
)
HOLDS_COUNT_DESCRIPTION = SensorEntityDescription(
    key="dab_holds_count",
    name="DAB Holds (Total)",
    state_class=SensorStateClass.TOTAL_INCREASING,
    icon="mdi:counter",
)
RECALC_COUNT_DESCRIPTION = SensorEntityDescription(
    key="dab_recalculations_count",
    name="DAB Recalculations (Total)",
    state_class=SensorStateClass.TOTAL_INCREASING,
    icon="mdi:calculator-variant",
)
HOLD_RATIO_DESCRIPTION = SensorEntityDescription(
    key="dab_hold_ratio",
    name="DAB Hold Ratio",
    native_unit_of_measurement=PERCENTAGE,
    state_class=SensorStateClass.MEASUREMENT,
    icon="mdi:percent-circle-outline",
)
ACTIVE_ROOM_SPREAD_DESCRIPTION = SensorEntityDescription(
    key="dab_active_room_spread",
    name="DAB Active Room Spread",
    native_unit_of_measurement=UnitOfTemperature.CELSIUS,
    state_class=SensorStateClass.MEASUREMENT,
    icon="mdi:arrow-expand-horizontal",
)
MAX_ACTIVE_ERROR_DESCRIPTION = SensorEntityDescription(
    key="dab_max_active_error",
    name="DAB Max Active Error",
    native_unit_of_measurement=UnitOfTemperature.CELSIUS,
    state_class=SensorStateClass.MEASUREMENT,
    icon="mdi:thermometer-alert",
)
RECALC_24H_DESCRIPTION = SensorEntityDescription(
    key="dab_recalculations_24h",
    name="DAB Recalculations (24h)",
    state_class=SensorStateClass.MEASUREMENT,
    icon="mdi:calculator-variant-outline",
)
HOLDS_24H_DESCRIPTION = SensorEntityDescription(
    key="dab_holds_24h",
    name="DAB Holds (24h)",
    state_class=SensorStateClass.MEASUREMENT,
    icon="mdi:counter",
)
STRATEGY_METRIC_DESCRIPTIONS: tuple[StrategyMetricSensorDescription, ...] = (
    StrategyMetricSensorDescription(
        key="dab_avg_temp_error",
        name="DAB Avg Temp Error",
        metric_key="avg_temp_error",
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:thermometer",
    ),
    StrategyMetricSensorDescription(
        key="dab_last_temp_error",
        name="DAB Last Temp Error",
        metric_key="last_temp_error",
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:thermometer-alert",
    ),
    StrategyMetricSensorDescription(
        key="dab_avg_active_temp_error",
        name="DAB Avg Active Temp Error",
        metric_key="avg_active_temp_error",
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:thermometer-check",
    ),
    StrategyMetricSensorDescription(
        key="dab_last_active_temp_error",
        name="DAB Last Active Temp Error",
        metric_key="last_active_temp_error",
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:thermometer-check",
    ),
    StrategyMetricSensorDescription(
        key="dab_avg_adjustments",
        name="DAB Avg Adjustments",
        metric_key="avg_adjustments",
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:tune-variant",
    ),
    StrategyMetricSensorDescription(
        key="dab_last_adjustments",
        name="DAB Last Adjustments",
        metric_key="last_adjustments",
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:tune-variant",
    ),
    StrategyMetricSensorDescription(
        key="dab_avg_movement",
        name="DAB Avg Movement",
        metric_key="avg_movement",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:percent",
    ),
    StrategyMetricSensorDescription(
        key="dab_last_movement",
        name="DAB Last Movement",
        metric_key="last_movement",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:percent",
    ),
    StrategyMetricSensorDescription(
        key="dab_last_active_rooms",
        name="DAB Active Rooms (Last Cycle)",
        metric_key="last_active_rooms",
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:home-group",
    ),
    StrategyMetricSensorDescription(
        key="dab_avg_spread",
        name="DAB Avg Spread",
        metric_key="avg_spread",
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:arrow-expand-horizontal",
    ),
    StrategyMetricSensorDescription(
        key="dab_max_spread",
        name="DAB Max Spread",
        metric_key="max_spread",
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:arrow-expand-horizontal",
    ),
    StrategyMetricSensorDescription(
        key="dab_time_above_guardrail",
        name="DAB Time Above Guardrail",
        metric_key="time_above_guardrail_min",
        native_unit_of_measurement="min",
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:timer-alert-outline",
    ),
)
MANUAL_SUGGESTED_DESCRIPTION = SensorEntityDescription(
    key="suggested_aperture",
    name="Suggested Aperture",
    native_unit_of_measurement=PERCENTAGE,
    state_class=SensorStateClass.MEASUREMENT,
)
VENT_SENSOR_DESCRIPTIONS: tuple[FlairVentSensorDescription, ...] = (
    FlairVentSensorDescription(
        key="aperture",
        name="Aperture",
        native_unit_of_measurement=PERCENTAGE,
        attribute="percent-open",
        state_class=SensorStateClass.MEASUREMENT,
    ),
    FlairVentSensorDescription(
        key="duct_temperature",
        name="Duct Temperature",
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        attribute="duct-temperature-c",
        device_class="temperature",
        state_class=SensorStateClass.MEASUREMENT,
    ),
    FlairVentSensorDescription(
        key="voltage",
        name="Voltage",
        native_unit_of_measurement="V",
        attribute="system-voltage",
    ),
    FlairVentSensorDescription(
        key="rssi",
        name="Signal",
        native_unit_of_measurement=SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
        attribute="rssi",
        device_class="signal_strength",
    ),
    FlairVentSensorDescription(
        key="cooling_efficiency",
        name="Cooling Efficiency",
        native_unit_of_measurement=PERCENTAGE,
        efficiency_mode="cooling",
    ),
    FlairVentSensorDescription(
        key="heating_efficiency",
        name="Heating Efficiency",
        native_unit_of_measurement=PERCENTAGE,
        efficiency_mode="heating",
    ),
    FlairVentSensorDescription(
        key="last_reading",
        name="Last Reading",
        device_class="timestamp",
    ),
)
VENT_METRIC_SENSOR_DESCRIPTIONS: tuple[FlairVentMetricSensorDescription, ...] = (
    FlairVentMetricSensorDescription(
        key="adjustments_24h",
        name="Adjustments (24h)",
        metric_key="count",
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:counter",
    ),
    FlairVentMetricSensorDescription(
        key="movement_24h",
        name="Movement (24h)",
        metric_key="movement",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:arrow-split-horizontal",
    ),
)

ROOM_SENSOR_DESCRIPTIONS: tuple[FlairRoomSensorDescription, ...] = (
    FlairRoomSensorDescription(
        key="room_temperature",
        name="Room Temperature",
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        device_class="temperature",
        room_field="temperature",
        state_class=SensorStateClass.MEASUREMENT,
    ),
    FlairRoomSensorDescription(
        key="room_thermostat",
        name="Room Thermostat",
        icon="mdi:thermostat",
        room_field="thermostat",
    ),
)


async def async_setup_entry(hass, entry, async_add_entities):
    coordinator = hass.data[DOMAIN][entry.entry_id]
    pucks = coordinator.data.get("pucks", {}) if coordinator.data else {}
    vents = coordinator.data.get("vents", {}) if coordinator.data else {}
    entities: list[FlairPuckSensor] = []

    for puck_id in pucks:
        for description in PUCK_SENSOR_DESCRIPTIONS:
            entities.append(FlairPuckSensor(coordinator, entry.entry_id, puck_id, description))

    for vent_id in vents:
        for description in VENT_SENSOR_DESCRIPTIONS:
            entities.append(FlairVentSensor(coordinator, entry.entry_id, vent_id, description))
        for description in VENT_METRIC_SENSOR_DESCRIPTIONS:
            entities.append(FlairVentMetricSensor(coordinator, entry.entry_id, vent_id, description))
        if coordinator.is_manual_brand():
            entities.append(
                ManualSuggestedApertureSensor(
                    coordinator, entry.entry_id, vent_id, MANUAL_SUGGESTED_DESCRIPTION
                )
            )

    rooms: dict[str, dict] = {}
    for vent in vents.values():
        room = vent.get("room") or {}
        room_id = room.get("id")
        if room_id and room_id not in rooms:
            rooms[room_id] = room
    for puck in pucks.values():
        room = puck.get("room") or {}
        room_id = room.get("id")
        if room_id and room_id not in rooms:
            rooms[room_id] = room

    for room_id in rooms:
        for description in ROOM_SENSOR_DESCRIPTIONS:
            entities.append(FlairRoomSensor(coordinator, entry.entry_id, room_id, description))

    entities.append(FlairSystemSensor(coordinator, entry.entry_id))
    for description in STRATEGY_METRIC_DESCRIPTIONS:
        entities.append(FlairStrategyMetricSensor(coordinator, entry.entry_id, description))

    # Hold/deviation observability sensors
    entities.append(DabHoldStatusSensor(coordinator, entry.entry_id, HOLD_STATUS_DESCRIPTION))
    entities.append(DabHoldStatusSensor(coordinator, entry.entry_id, DEVIATION_MAX_DESCRIPTION))
    entities.append(DabHoldStatusSensor(coordinator, entry.entry_id, HOLDS_COUNT_DESCRIPTION))
    entities.append(DabHoldStatusSensor(coordinator, entry.entry_id, RECALC_COUNT_DESCRIPTION))
    entities.append(DabHoldStatusSensor(coordinator, entry.entry_id, HOLD_RATIO_DESCRIPTION))
    # Task 24 spread/error observability sensors (R13/R14)
    entities.append(DabHoldStatusSensor(coordinator, entry.entry_id, ACTIVE_ROOM_SPREAD_DESCRIPTION))
    entities.append(DabHoldStatusSensor(coordinator, entry.entry_id, MAX_ACTIVE_ERROR_DESCRIPTION))
    entities.append(DabHoldStatusSensor(coordinator, entry.entry_id, RECALC_24H_DESCRIPTION))
    entities.append(DabHoldStatusSensor(coordinator, entry.entry_id, HOLDS_24H_DESCRIPTION))

    async_add_entities(entities)


class FlairPuckSensor(CoordinatorEntity, SensorEntity):
    """Representation of a Flair puck sensor."""

    entity_description: FlairPuckSensorDescription

    def __init__(
        self, coordinator, entry_id: str, puck_id: str, description: FlairPuckSensorDescription
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._entry_id = entry_id
        self._puck_id = puck_id
        self._attr_unique_id = f"{entry_id}_puck_{puck_id}_{description.key}"

    @property
    def name(self):
        puck = (self.coordinator.data or {}).get("pucks", {}).get(self._puck_id, {})
        puck_name = puck.get("name") or f"Puck {self._puck_id}"
        return f"{puck_name} {self.entity_description.name}"

    @property
    def device_info(self):
        return self.coordinator.get_room_device_info_for_puck(self._puck_id)

    @property
    def available(self) -> bool:
        if not self.coordinator.last_update_success:
            return False
        puck = (self.coordinator.data or {}).get("pucks", {}).get(self._puck_id)
        if not puck:
            return False
        attrs = puck.get("attributes") or {}
        if self.entity_description.attribute:
            return self.entity_description.attribute in attrs
        return True

    @property
    def native_value(self):
        puck = (self.coordinator.data or {}).get("pucks", {}).get(self._puck_id, {})
        attrs = puck.get("attributes", {})
        attribute = self.entity_description.attribute
        value = attrs.get(attribute) if attribute else None

        if self.entity_description.key == "battery" and value is None:
            voltage = attrs.get("system-voltage")
            if voltage is None:
                return None
            return max(0, min(100, round(((voltage - 2.0) / 1.6) * 100)))

        return value


class FlairVentSensor(CoordinatorEntity, SensorEntity):
    """Representation of a Flair vent sensor."""

    entity_description: FlairVentSensorDescription

    def __init__(
        self, coordinator, entry_id: str, vent_id: str, description: FlairVentSensorDescription
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._entry_id = entry_id
        self._vent_id = vent_id
        self._attr_unique_id = f"{entry_id}_vent_{vent_id}_{description.key}"

    @property
    def name(self):
        vent = (self.coordinator.data or {}).get("vents", {}).get(self._vent_id, {})
        vent_name = vent.get("name") or f"Vent {self._vent_id}"
        return f"{vent_name} {self.entity_description.name}"

    @property
    def device_info(self):
        return self.coordinator.get_room_device_info_for_vent(self._vent_id)

    @property
    def available(self) -> bool:
        if not self.coordinator.last_update_success:
            return False
        vent = (self.coordinator.data or {}).get("vents", {}).get(self._vent_id)
        if not vent:
            return False
        if self.entity_description.efficiency_mode:
            return True
        if self.entity_description.key == "last_reading":
            return self.coordinator.get_vent_last_reading(self._vent_id) is not None
        attrs = vent.get("attributes") or {}
        if self.entity_description.attribute:
            return self.entity_description.attribute in attrs
        return True

    @property
    def native_value(self):
        if self.entity_description.efficiency_mode:
            return self.coordinator.get_vent_efficiency_percent(
                self._vent_id, self.entity_description.efficiency_mode
            )
        if self.entity_description.key == "last_reading":
            value = self.coordinator.get_vent_last_reading(self._vent_id)
            if value is None:
                return None
            if value.tzinfo is None:
                return value.replace(tzinfo=UTC)
            return dt_util.as_utc(value)

        vent = (self.coordinator.data or {}).get("vents", {}).get(self._vent_id, {})
        attrs = vent.get("attributes", {})
        attribute = self.entity_description.attribute
        return attrs.get(attribute) if attribute else None

    @property
    def extra_state_attributes(self):
        # Per-vent learned leakage diagnostic on the efficiency sensors (R25.11).
        mode = self.entity_description.efficiency_mode
        if not mode:
            return None
        return {"leak": self.coordinator.get_vent_leak(self._vent_id, mode)}


class FlairVentMetricSensor(CoordinatorEntity, SensorEntity):
    """Computed 24h vent metrics (adjustments/movement)."""

    entity_description: FlairVentMetricSensorDescription

    def __init__(
        self,
        coordinator,
        entry_id: str,
        vent_id: str,
        description: FlairVentMetricSensorDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._entry_id = entry_id
        self._vent_id = vent_id
        self._attr_unique_id = f"{entry_id}_vent_{vent_id}_{description.key}"

    @property
    def name(self):
        vent = (self.coordinator.data or {}).get("vents", {}).get(self._vent_id, {})
        vent_name = vent.get("name") or f"Vent {self._vent_id}"
        return f"{vent_name} {self.entity_description.name}"

    @property
    def device_info(self):
        return self.coordinator.get_room_device_info_for_vent(self._vent_id)

    @property
    def available(self) -> bool:
        if not self.coordinator.last_update_success:
            return False
        vent = (self.coordinator.data or {}).get("vents", {}).get(self._vent_id)
        return vent is not None

    @property
    def native_value(self):
        stats = self.coordinator.get_vent_adjustment_stats(self._vent_id)
        value = stats.get(self.entity_description.metric_key)
        if value is None:
            return None
        if isinstance(value, (int, float)):
            return value
        try:
            return float(value)
        except (TypeError, ValueError):
            return None


class FlairRoomSensor(CoordinatorEntity, SensorEntity):
    """Room-level sensor values (temperature, thermostat)."""

    entity_description: FlairRoomSensorDescription

    def __init__(
        self, coordinator, entry_id: str, room_id: str, description: FlairRoomSensorDescription
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._entry_id = entry_id
        self._room_id = room_id
        self._attr_unique_id = f"{entry_id}_room_{room_id}_{description.key}"

    @property
    def name(self):
        room = self.coordinator.get_room_by_id(self._room_id)
        room_name = (room.get("attributes") or {}).get("name") or f"Room {self._room_id}"
        return f"{room_name} {self.entity_description.name}"

    @property
    def device_info(self):
        room = self.coordinator.get_room_by_id(self._room_id)
        return self.coordinator.get_room_device_info(room)

    @property
    def available(self) -> bool:
        if not self.coordinator.last_update_success:
            return False
        room = self.coordinator.get_room_by_id(self._room_id)
        if not room:
            return False
        if self.entity_description.room_field == "temperature":
            return self.coordinator.get_room_temperature(self._room_id) is not None
        return True

    @property
    def native_value(self):
        if self.entity_description.room_field == "temperature":
            return self.coordinator.get_room_temperature(self._room_id)
        if self.entity_description.room_field == "thermostat":
            return self.coordinator.get_room_thermostat(self._room_id)
        return None

    @property
    def extra_state_attributes(self):
        # Per-room observability diagnostics (R13.3/R5.4/R25.11). Only attached
        # to the room temperature sensor; thermostat sensor has none.
        if self.entity_description.room_field != "temperature":
            return None
        attrs: dict[str, object] = {
            "signed_error_c": self.coordinator.get_room_signed_error(self._room_id),
            "airflow_limited": self.coordinator.is_room_airflow_limited(self._room_id),
            "cooling_efficiency": self.coordinator.get_room_efficiency_percent(self._room_id, "cooling"),
            "heating_efficiency": self.coordinator.get_room_efficiency_percent(self._room_id, "heating"),
        }
        # Learned door-leakage multiplier (R30): surfaced ONLY for a room with a
        # door sensor configured, so a sensorless room shows no misleading factor.
        door_factor = self.coordinator.get_room_door_factor(self._room_id)
        if door_factor is not None:
            attrs["door_factor"] = door_factor
            attrs["door_factor_trusted"] = self.coordinator.get_room_door_factor_trusted(self._room_id)
            attrs["door_open"] = self.coordinator.get_room_door_open(self._room_id)
        return attrs


class _CelsiusDeltaMixin:
    """Render °C temperature-*delta* metrics in the system unit (°F on US).

    Spread/error metrics are temperature DIFFERENCES, so they are intentionally
    not ``device_class=temperature`` (HA would convert them as absolute temps,
    e.g. a 2 °C spread → 35.6 °F). We convert the delta ourselves: a °F delta is
    the °C delta x 1.8 with no +32 offset. Non-temperature descriptions (counts,
    percentages, strings) and non-US systems pass through unchanged.
    """

    entity_description: SensorEntityDescription

    def _delta_is_temperature(self) -> bool:
        return self.entity_description.native_unit_of_measurement == UnitOfTemperature.CELSIUS

    def _system_is_fahrenheit(self) -> bool:
        coordinator = getattr(self, "coordinator", None)
        hass = getattr(coordinator, "hass", None) or getattr(self, "hass", None)
        config = getattr(hass, "config", None)
        units = getattr(config, "units", None)
        return getattr(units, "temperature_unit", None) == UnitOfTemperature.FAHRENHEIT

    @property
    def native_unit_of_measurement(self):
        base = self.entity_description.native_unit_of_measurement
        if self._delta_is_temperature() and self._system_is_fahrenheit():
            return UnitOfTemperature.FAHRENHEIT
        return base

    def _convert_delta(self, value):
        if (
            value is not None
            and not isinstance(value, str)
            and self._delta_is_temperature()
            and self._system_is_fahrenheit()
        ):
            try:
                return round(float(value) * 1.8, 2)
            except (TypeError, ValueError):
                return value
        return value


class FlairSystemSensor(CoordinatorEntity, SensorEntity):
    """System-level diagnostic sensor for strategy effectiveness."""

    def __init__(self, coordinator, entry_id: str) -> None:
        super().__init__(coordinator)
        self._entry_id = entry_id
        self.entity_description = SYSTEM_SENSOR_DESCRIPTION
        self._attr_unique_id = f"{entry_id}_strategy_effectiveness"

    @property
    def name(self):
        return self.entity_description.name

    @property
    def native_value(self):
        metrics = self.coordinator.get_strategy_metrics()
        return metrics.get("last_strategy") or "unknown"

    @property
    def extra_state_attributes(self):
        return self.coordinator.get_strategy_metrics()


class FlairStrategyMetricSensor(_CelsiusDeltaMixin, CoordinatorEntity, SensorEntity):
    """Expose selected DAB strategy effectiveness metrics."""

    entity_description: StrategyMetricSensorDescription

    def __init__(
        self,
        coordinator,
        entry_id: str,
        description: StrategyMetricSensorDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._entry_id = entry_id
        self._attr_unique_id = f"{entry_id}_{description.key}"

    @property
    def name(self):
        return self.entity_description.name

    @property
    def native_value(self):
        metrics = self.coordinator.get_strategy_metrics()
        strategies = metrics.get("strategies") or {}
        strategy = metrics.get("last_strategy")
        if not strategy and strategies:
            strategy = sorted(strategies.keys())[0]
        if not strategy:
            return None
        data = strategies.get(strategy, {})
        value = data.get(self.entity_description.metric_key)
        if value is None:
            return None
        if isinstance(value, (int, float)):
            return self._convert_delta(value)
        try:
            return self._convert_delta(float(value))
        except (TypeError, ValueError):
            return None


class DabHoldStatusSensor(_CelsiusDeltaMixin, CoordinatorEntity, SensorEntity):
    """Expose DAB hold/deviation observability metrics."""

    def __init__(self, coordinator, entry_id: str, description: SensorEntityDescription) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._entry_id = entry_id
        self._attr_unique_id = f"{entry_id}_{description.key}"

    @property
    def name(self):
        return self.entity_description.name

    @property
    def native_value(self):
        key = self.entity_description.key
        if key == "dab_hold_status":
            return self.coordinator.get_hold_status()
        if key == "dab_deviation_max":
            return self._convert_delta(self.coordinator.get_max_deviation())
        if key == "dab_holds_count":
            return self.coordinator.get_hold_count()
        if key == "dab_recalculations_count":
            return self.coordinator.get_recalc_count()
        if key == "dab_hold_ratio":
            return self.coordinator.get_hold_ratio()
        if key == "dab_active_room_spread":
            return self._convert_delta(self.coordinator.get_active_room_spread())
        if key == "dab_max_active_error":
            return self._convert_delta(self.coordinator.get_max_active_error())
        if key == "dab_recalculations_24h":
            return self.coordinator.get_recalculations_24h()
        if key == "dab_holds_24h":
            return self.coordinator.get_holds_24h()
        return None


class ManualSuggestedApertureSensor(CoordinatorEntity, SensorEntity):
    """Suggested aperture sensor for manual vents."""

    def __init__(
        self, coordinator, entry_id: str, vent_id: str, description: SensorEntityDescription
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._entry_id = entry_id
        self._vent_id = vent_id
        self._attr_unique_id = f"{entry_id}_manual_{vent_id}_suggested"

    @property
    def name(self):
        vent = (self.coordinator.data or {}).get("vents", {}).get(self._vent_id, {})
        vent_name = vent.get("name") or f"Vent {self._vent_id}"
        return f"{vent_name} {self.entity_description.name}"

    @property
    def device_info(self):
        return self.coordinator.get_room_device_info_for_vent(self._vent_id)

    @property
    def native_value(self):
        return self.coordinator.get_vent_target(self._vent_id)
