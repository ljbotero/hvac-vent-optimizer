"""Sensor platform for Flair pucks and vents."""
from __future__ import annotations

from dataclasses import dataclass

from homeassistant.components.sensor import SensorEntity, SensorEntityDescription, SensorStateClass
from datetime import timezone
from homeassistant.const import (
    PERCENTAGE,
    SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
    UnitOfPressure,
    UnitOfTemperature,
)
from homeassistant.helpers.update_coordinator import CoordinatorEntity

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
class FlairRoomSensorDescription(SensorEntityDescription):
    """Describe a Flair room sensor."""

    room_field: str | None = None


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

    for puck_id in pucks.keys():
        for description in PUCK_SENSOR_DESCRIPTIONS:
            entities.append(FlairPuckSensor(coordinator, entry.entry_id, puck_id, description))

    for vent_id in vents.keys():
        for description in VENT_SENSOR_DESCRIPTIONS:
            entities.append(FlairVentSensor(coordinator, entry.entry_id, vent_id, description))

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

    for room_id in rooms.keys():
        for description in ROOM_SENSOR_DESCRIPTIONS:
            entities.append(FlairRoomSensor(coordinator, entry.entry_id, room_id, description))

    entities.append(FlairSystemSensor(coordinator, entry.entry_id))

    async_add_entities(entities)


class FlairPuckSensor(CoordinatorEntity, SensorEntity):
    """Representation of a Flair puck sensor."""

    entity_description: FlairPuckSensorDescription

    def __init__(self, coordinator, entry_id: str, puck_id: str, description: FlairPuckSensorDescription) -> None:
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
            return max(0, min(100, int(round(((voltage - 2.0) / 1.6) * 100))))

        return value


class FlairVentSensor(CoordinatorEntity, SensorEntity):
    """Representation of a Flair vent sensor."""

    entity_description: FlairVentSensorDescription

    def __init__(self, coordinator, entry_id: str, vent_id: str, description: FlairVentSensorDescription) -> None:
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
                return value.replace(tzinfo=timezone.utc)
            return value

        vent = (self.coordinator.data or {}).get("vents", {}).get(self._vent_id, {})
        attrs = vent.get("attributes", {})
        attribute = self.entity_description.attribute
        return attrs.get(attribute) if attribute else None


class FlairRoomSensor(CoordinatorEntity, SensorEntity):
    """Room-level sensor values (temperature, thermostat)."""

    entity_description: FlairRoomSensorDescription

    def __init__(self, coordinator, entry_id: str, room_id: str, description: FlairRoomSensorDescription) -> None:
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
