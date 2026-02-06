from types import SimpleNamespace

import pytest

import asyncio

from custom_components.hvac_vent_optimizer.number import ManualVentApertureNumber
from custom_components.hvac_vent_optimizer import number as number_module
from custom_components.hvac_vent_optimizer.sensor import ManualSuggestedApertureSensor


class _FakeCoordinator:
    def __init__(self):
        self.aperture_updates = []
        self._targets = {"manual_1": 37}
        self._manual_vents = [{"id": "manual_1", "name": "Living Room"}]
        self.data = {"vents": {"manual_1": {"id": "manual_1", "name": "Living Room"}}}

    def set_manual_aperture(self, vent_id, value):
        self.aperture_updates.append((vent_id, value))

    def get_vent_target(self, vent_id):
        return self._targets.get(vent_id)

    def is_manual_brand(self):
        return True

    def get_manual_vents(self):
        return self._manual_vents


def test_manual_vent_number_updates_coordinator():
    coordinator = _FakeCoordinator()
    vent = {"id": "manual_1", "name": "Living Room"}
    entity = ManualVentApertureNumber(coordinator, "entry1", vent)
    assert entity.name == "Living Room Manual Aperture"

    asyncio.run(entity.async_set_native_value(25))
    assert coordinator.aperture_updates == [("manual_1", 25)]


def test_manual_suggested_sensor_reads_target():
    coordinator = _FakeCoordinator()
    entity = ManualSuggestedApertureSensor(
        coordinator,
        "entry1",
        "manual_1",
        SimpleNamespace(
            key="suggested_aperture",
            name="Suggested Aperture",
            icon=None,
            device_class=None,
            state_class=None,
            native_unit_of_measurement=None,
        ),
    )
    assert entity.native_value == 37


def test_manual_suggested_sensor_name():
    coordinator = _FakeCoordinator()
    entity = ManualSuggestedApertureSensor(
        coordinator,
        "entry1",
        "manual_1",
        SimpleNamespace(
            key="suggested_aperture",
            name="Suggested Aperture",
            icon=None,
            device_class=None,
            state_class=None,
            native_unit_of_measurement=None,
        ),
    )
    assert entity.name == "Living Room Suggested Aperture"


def test_number_setup_entry_adds_entities():
    coordinator = _FakeCoordinator()
    hass = SimpleNamespace(data={"hvac_vent_optimizer": {"entry1": coordinator}})
    entry = SimpleNamespace(entry_id="entry1")
    added = []

    def add_entities(entities):
        added.extend(entities)

    asyncio.run(number_module.async_setup_entry(hass, entry, add_entities))
    assert len(added) == 1


def test_manual_number_restores_value_when_missing_native():
    coordinator = _FakeCoordinator()
    vent = {"id": "manual_1", "name": "Living Room"}
    entity = ManualVentApertureNumber(coordinator, "entry1", vent)

    async def _restore():
        return SimpleNamespace(native_value=62)

    entity.async_get_last_number_data = _restore
    asyncio.run(entity.async_added_to_hass())
    assert entity.native_value == 62
    assert coordinator.aperture_updates[-1] == ("manual_1", 62)


def test_manual_number_defaults_to_50_when_no_restore():
    coordinator = _FakeCoordinator()
    vent = {"id": "manual_1", "name": "Living Room"}
    entity = ManualVentApertureNumber(coordinator, "entry1", vent)

    async def _restore():
        return None

    entity.async_get_last_number_data = _restore
    asyncio.run(entity.async_added_to_hass())
    assert entity.native_value == 50
    assert coordinator.aperture_updates[-1] == ("manual_1", 50)


def test_manual_number_uses_existing_native_value():
    coordinator = _FakeCoordinator()
    vent = {"id": "manual_1", "name": "Living Room"}
    entity = ManualVentApertureNumber(coordinator, "entry1", vent)
    entity._attr_native_value = 88

    async def _restore():
        raise AssertionError("restore should not be called when native_value is set")

    entity.async_get_last_number_data = _restore
    asyncio.run(entity.async_added_to_hass())
    assert coordinator.aperture_updates[-1] == ("manual_1", 88)
