"""Binary sensor platform for Flair occupancy."""
from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorEntity, BinarySensorDeviceClass
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN


async def async_setup_entry(hass, entry, async_add_entities):
    coordinator = hass.data[DOMAIN][entry.entry_id]
    pucks = coordinator.data.get("pucks", {}) if coordinator.data else {}
    entities = [
        FlairPuckOccupancyBinarySensor(coordinator, entry.entry_id, puck_id)
        for puck_id in pucks.keys()
    ]
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
