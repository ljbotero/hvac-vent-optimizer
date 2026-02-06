import asyncio
from datetime import timezone
from types import SimpleNamespace

from custom_components.hvac_vent_optimizer.coordinator import FlairCoordinator
from custom_components.hvac_vent_optimizer.const import (
    BRAND_MANUAL,
    CONF_DAB_ENABLED,
    CONF_MANUAL_VENTS,
    CONF_TEMP_SENSOR_ENTITY,
    CONF_VENT_BRAND,
)


class _State:
    def __init__(self, state, attributes):
        self.state = state
        self.attributes = attributes


class _States:
    def __init__(self, mapping):
        self._mapping = mapping

    def get(self, entity_id):
        return self._mapping.get(entity_id)


def _make_coord():
    coord = FlairCoordinator.__new__(FlairCoordinator)
    coord.entry = SimpleNamespace(
        data={},
        options={CONF_VENT_BRAND: BRAND_MANUAL, CONF_MANUAL_VENTS: [], CONF_DAB_ENABLED: False},
    )
    coord.hass = SimpleNamespace(
        states=_States({}),
        config=SimpleNamespace(units=SimpleNamespace(temperature_unit="C")),
    )
    coord._manual_apertures = {}
    coord._vent_last_reading = {}
    return coord


def test_async_update_manual_data_builds_vents():
    coord = _make_coord()
    coord.entry.options[CONF_MANUAL_VENTS] = [
        {"id": "manual_1", "name": "Office", CONF_TEMP_SENSOR_ENTITY: "sensor.office_temp"}
    ]
    coord.hass.states = _States(
        {"sensor.office_temp": _State("72", {"unit_of_measurement": "F"})}
    )

    data = asyncio.run(coord._async_update_manual_data())
    assert "manual_1" in data["vents"]
    vent = data["vents"]["manual_1"]
    assert vent["attributes"]["percent-open"] == 50
    assert coord._vent_last_reading["manual_1"].tzinfo == timezone.utc
