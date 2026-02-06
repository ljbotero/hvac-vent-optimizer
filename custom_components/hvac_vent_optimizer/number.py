"""Number entities for manual vent apertures."""
from __future__ import annotations

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.components.sensor import SensorStateClass
from homeassistant.const import PERCENTAGE
try:
    from homeassistant.helpers.restore_state import RestoreNumber
except ImportError:  # pragma: no cover - older HA versions
    from homeassistant.helpers.restore_state import RestoreEntity as RestoreNumber
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN


async def async_setup_entry(hass, entry, async_add_entities):
    coordinator = hass.data[DOMAIN][entry.entry_id]
    if not coordinator.is_manual_brand():
        return

    vents = coordinator.get_manual_vents()
    entities = [
        ManualVentApertureNumber(coordinator, entry.entry_id, vent)
        for vent in vents
    ]
    async_add_entities(entities)


class ManualVentApertureNumber(CoordinatorEntity, RestoreNumber, NumberEntity):
    """Manual vent aperture input."""

    _attr_mode = NumberMode.BOX
    _attr_native_min_value = 0
    _attr_native_max_value = 100
    _attr_native_step = 1
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator, entry_id: str, vent: dict) -> None:
        super().__init__(coordinator)
        self._entry_id = entry_id
        self._vent_id = vent.get("id")
        self._vent_name = vent.get("name") or f"Vent {self._vent_id}"
        self._attr_unique_id = f"{entry_id}_manual_{self._vent_id}_aperture"
        self._attr_name = f"{self._vent_name} Manual Aperture"

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if self.native_value is not None:
            self.coordinator.set_manual_aperture(self._vent_id, int(self.native_value))
            return
        restored = await self.async_get_last_number_data()
        if restored is None:
            self._attr_native_value = 50
        else:
            self._attr_native_value = restored.native_value
        self.coordinator.set_manual_aperture(self._vent_id, int(self._attr_native_value))

    async def async_set_native_value(self, value: float) -> None:
        value_int = max(0, min(100, int(round(value))))
        self._attr_native_value = value_int
        self.coordinator.set_manual_aperture(self._vent_id, value_int)
        self.async_write_ha_state()
