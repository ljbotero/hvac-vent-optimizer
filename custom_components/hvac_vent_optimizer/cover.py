"""Cover platform for Flair vents."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from homeassistant.components.cover import CoverEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN


async def async_setup_entry(hass, entry, async_add_entities):
    coordinator = hass.data[DOMAIN][entry.entry_id]
    if coordinator.is_manual_brand():
        return
    vents = coordinator.data.get("vents", {}) if coordinator.data else {}
    entities = [
        FlairVentCover(coordinator, entry.entry_id, vent_id)
        for vent_id in vents.keys()
    ]
    async_add_entities(entities)


class FlairVentCover(CoordinatorEntity, CoverEntity):
    """Representation of a Flair vent as a cover."""

    def __init__(self, coordinator, entry_id: str, vent_id: str) -> None:
        super().__init__(coordinator)
        self._entry_id = entry_id
        self._vent_id = vent_id
        self._attr_unique_id = f"{entry_id}_vent_{vent_id}"
        self._attr_current_cover_position = None
        self._pending_position: int | None = None
        self._pending_until: datetime | None = None

    @property
    def name(self):
        vent = (self.coordinator.data or {}).get("vents", {}).get(self._vent_id, {})
        return vent.get("name") or f"Vent {self._vent_id}"

    @property
    def available(self) -> bool:
        if not self.coordinator.last_update_success:
            return False
        vent = (self.coordinator.data or {}).get("vents", {}).get(self._vent_id)
        if not vent:
            return False
        attrs = vent.get("attributes") or {}
        return attrs.get("percent-open") is not None

    @property
    def device_info(self):
        return self.coordinator.get_room_device_info_for_vent(self._vent_id)

    @property
    def current_cover_position(self):
        if self._pending_position is not None and self._pending_until:
            if datetime.now(timezone.utc) < self._pending_until:
                return self._pending_position
            self._pending_position = None
            self._pending_until = None

        if self._attr_current_cover_position is not None:
            return self._attr_current_cover_position

        vent = (self.coordinator.data or {}).get("vents", {}).get(self._vent_id, {})
        attrs = vent.get("attributes", {})
        percent = attrs.get("percent-open")
        return int(percent) if percent is not None else None

    @property
    def is_closed(self):
        position = self.current_cover_position
        if position is None:
            return None
        return position == 0

    async def async_set_cover_position(self, **kwargs):
        position = kwargs.get("position")
        if position is None:
            return
        position = int(position)
        self._pending_position = position
        self._pending_until = datetime.now(timezone.utc) + timedelta(seconds=30)
        self._attr_current_cover_position = position
        self.async_write_ha_state()
        await self.coordinator.api.async_set_vent_position(self._vent_id, position)
        await self.coordinator.async_request_refresh()

    async def async_open_cover(self, **kwargs):
        await self.async_set_cover_position(position=100)

    async def async_close_cover(self, **kwargs):
        await self.async_set_cover_position(position=0)

    def _handle_coordinator_update(self) -> None:
        vent = (self.coordinator.data or {}).get("vents", {}).get(self._vent_id, {})
        attrs = vent.get("attributes", {})
        percent = attrs.get("percent-open")
        now = datetime.now(timezone.utc)
        if self._pending_position is not None and self._pending_until:
            if now >= self._pending_until:
                self._pending_position = None
                self._pending_until = None
            elif percent is not None and int(percent) == self._pending_position:
                self._pending_position = None
                self._pending_until = None
            else:
                self.async_write_ha_state()
                return

        if percent is not None:
            self._attr_current_cover_position = int(percent)
        self.async_write_ha_state()
