"""Switch platform for Flair room active state."""
from __future__ import annotations

from homeassistant.components.switch import SwitchEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN


async def async_setup_entry(hass, entry, async_add_entities):
    coordinator = hass.data[DOMAIN][entry.entry_id]
    if coordinator.is_manual_brand():
        return
    rooms: dict[str, dict] = {}

    for vent in (coordinator.data or {}).get("vents", {}).values():
        room = vent.get("room") or {}
        room_id = room.get("id")
        if room_id and room_id not in rooms:
            rooms[room_id] = room

    for puck in (coordinator.data or {}).get("pucks", {}).values():
        room = puck.get("room") or {}
        room_id = room.get("id")
        if room_id and room_id not in rooms:
            rooms[room_id] = room

    entities = [
        FlairRoomActiveSwitch(coordinator, entry.entry_id, room_id)
        for room_id in rooms.keys()
    ]
    async_add_entities(entities)


class FlairRoomActiveSwitch(CoordinatorEntity, SwitchEntity):
    """Switch to control room active state."""

    def __init__(self, coordinator, entry_id: str, room_id: str) -> None:
        super().__init__(coordinator)
        self._entry_id = entry_id
        self._room_id = room_id
        self._attr_unique_id = f"{entry_id}_room_{room_id}_active"

    @property
    def name(self):
        room = self._get_room()
        room_name = (room.get("attributes") or {}).get("name") or f"Room {self._room_id}"
        return f"{room_name} Active"

    @property
    def is_on(self):
        room = self._get_room()
        active = (room.get("attributes") or {}).get("active")
        if isinstance(active, str):
            return active.lower() in {"true", "active", "1"}
        if active is None:
            return True
        return bool(active)

    async def async_turn_on(self, **kwargs):
        await self.coordinator.async_set_room_active(self._room_id, True)

    async def async_turn_off(self, **kwargs):
        await self.coordinator.async_set_room_active(self._room_id, False)

    @property
    def device_info(self):
        room = self._get_room()
        return self.coordinator.get_room_device_info(room)

    def _get_room(self) -> dict:
        if not self.coordinator.data:
            return {}
        for vent in self.coordinator.data.get("vents", {}).values():
            room = vent.get("room") or {}
            if room.get("id") == self._room_id:
                return room
        for puck in self.coordinator.data.get("pucks", {}).values():
            room = puck.get("room") or {}
            if room.get("id") == self._room_id:
                return room
        return {}
