"""Binary sensor platform for Flair occupancy."""

from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN


async def async_setup_entry(hass, entry, async_add_entities):
    coordinator = hass.data[DOMAIN][entry.entry_id]
    pucks = coordinator.data.get("pucks", {}) if coordinator.data else {}
    vents = coordinator.data.get("vents", {}) if coordinator.data else {}
    entities: list[BinarySensorEntity] = [
        FlairPuckOccupancyBinarySensor(coordinator, entry.entry_id, puck_id) for puck_id in pucks
    ]

    # Per-room airflow-limited indicator (R5.4). One per room served by a vent.
    rooms: dict[str, dict] = {}
    for vent in vents.values():
        room = vent.get("room") or {}
        room_id = room.get("id")
        if room_id and room_id not in rooms:
            rooms[room_id] = room
    for room_id in rooms:
        entities.append(FlairRoomAirflowLimitedBinarySensor(coordinator, entry.entry_id, room_id))

    async_add_entities(entities)


class FlairPuckOccupancyBinarySensor(CoordinatorEntity, BinarySensorEntity):
    """Expose puck room occupancy as a binary sensor."""

    def __init__(self, coordinator, entry_id: str, puck_id: str) -> None:
        super().__init__(coordinator)
        self._entry_id = entry_id
        self._puck_id = puck_id
        self._attr_unique_id = f"{entry_id}_puck_{puck_id}_occupancy"
        self._attr_device_class = BinarySensorDeviceClass.OCCUPANCY

    @property
    def name(self):
        puck = (self.coordinator.data or {}).get("pucks", {}).get(self._puck_id, {})
        puck_name = puck.get("name") or f"Puck {self._puck_id}"
        return f"{puck_name} Occupancy"

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
        if "room-occupied" in attrs or "occupied" in attrs:
            return True
        room = puck.get("room") or {}
        room_attrs = room.get("attributes") or {}
        return "occupied" in room_attrs

    @property
    def is_on(self):
        puck = (self.coordinator.data or {}).get("pucks", {}).get(self._puck_id, {})
        attrs = puck.get("attributes", {})
        value = attrs.get("room-occupied")
        if value is None:
            value = attrs.get("occupied")
        if value is None:
            room = puck.get("room") or {}
            room_attrs = room.get("attributes") or {}
            value = room_attrs.get("occupied")
        if isinstance(value, str):
            return value.lower() in {"true", "occupied", "1"}
        return bool(value)


class FlairRoomAirflowLimitedBinarySensor(CoordinatorEntity, BinarySensorEntity):
    """Per-room airflow-limited indicator (R5.4).

    ``on`` when the room's vent is at/near full open yet still off-target, i.e.
    opening it further cannot improve it. Sourced from the coordinator's
    per-poll observability state (Task 24).
    """

    def __init__(self, coordinator, entry_id: str, room_id: str) -> None:
        super().__init__(coordinator)
        self._entry_id = entry_id
        self._room_id = room_id
        self._attr_unique_id = f"{entry_id}_room_{room_id}_airflow_limited"
        self._attr_device_class = BinarySensorDeviceClass.PROBLEM
        self._attr_icon = "mdi:fan-alert"

    @property
    def name(self):
        room = self.coordinator.get_room_by_id(self._room_id)
        room_name = (room.get("attributes") or {}).get("name") or f"Room {self._room_id}"
        return f"{room_name} Airflow Limited"

    @property
    def device_info(self):
        room = self.coordinator.get_room_by_id(self._room_id)
        return self.coordinator.get_room_device_info(room)

    @property
    def is_on(self):
        return self.coordinator.is_room_airflow_limited(self._room_id)
