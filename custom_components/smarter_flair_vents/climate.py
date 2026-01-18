"""Climate platform for Flair room setpoint control."""
from __future__ import annotations

from homeassistant.components.climate import ClimateEntity
from homeassistant.components.climate.const import ClimateEntityFeature, HVACMode
from homeassistant.const import ATTR_TEMPERATURE, UnitOfTemperature
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN


async def async_setup_entry(hass, entry, async_add_entities):
    coordinator = hass.data[DOMAIN][entry.entry_id]
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
        FlairRoomClimate(coordinator, entry.entry_id, room_id)
        for room_id in rooms.keys()
    ]
    async_add_entities(entities)


class FlairRoomClimate(CoordinatorEntity, ClimateEntity):
    """Room setpoint control as a climate entity."""

    _attr_supported_features = ClimateEntityFeature.TARGET_TEMPERATURE
    _attr_hvac_modes = [HVACMode.AUTO]
    _attr_hvac_mode = HVACMode.AUTO
    _attr_temperature_unit = UnitOfTemperature.CELSIUS

    def __init__(self, coordinator, entry_id: str, room_id: str) -> None:
        super().__init__(coordinator)
        self._entry_id = entry_id
        self._room_id = room_id
        self._attr_unique_id = f"{entry_id}_room_{room_id}_climate"

    @property
    def name(self):
        room = self.coordinator.get_room_by_id(self._room_id)
        room_name = (room.get("attributes") or {}).get("name") or f"Room {self._room_id}"
        return f"{room_name} Climate"

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
        return self.coordinator.get_room_temperature(self._room_id) is not None

    @property
    def current_temperature(self):
        return self.coordinator.get_room_temperature(self._room_id)

    @property
    def target_temperature(self):
        room = self.coordinator.get_room_by_id(self._room_id)
        setpoint = (room.get("attributes") or {}).get("set-point-c")
        return float(setpoint) if setpoint is not None else None

    async def async_set_temperature(self, **kwargs):
        temperature = kwargs.get(ATTR_TEMPERATURE)
        if temperature is None:
            return
        temp_c = float(temperature)
        if self.hass.config.units.temperature_unit == UnitOfTemperature.FAHRENHEIT:
            temp_c = (temp_c - 32) * 5 / 9
        await self.coordinator.api.async_set_room_setpoint(self._room_id, temp_c)
        await self.coordinator.async_request_refresh()
