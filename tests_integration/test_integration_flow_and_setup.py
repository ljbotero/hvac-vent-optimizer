from unittest.mock import patch

import pytest
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

try:
    from homeassistant import storage
except ImportError:
    pytest.skip("Home Assistant storage API not available", allow_module_level=True)
else:
    if not hasattr(storage.Store, "_async_load"):
        pytest.skip(
            "Home Assistant storage API not compatible with tests",
            allow_module_level=True,
        )


DOMAIN = "hvac_vent_optimizer"
CONF_CLIENT_ID = "client_id"
CONF_CLIENT_SECRET = "client_secret"
CONF_STRUCTURE_ID = "structure_id"
CONF_VENT_BRAND = "vent_brand"


@pytest.mark.asyncio
async def test_config_flow_creates_entry(hass, fake_api):
    with patch(
        "custom_components.hvac_vent_optimizer.config_flow.FlairApi",
        return_value=fake_api,
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": "user"}
        )
        assert result["type"] == "form"

        result2 = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_VENT_BRAND: "flair"},
        )
        assert result2["type"] == "form"

        result3 = await hass.config_entries.flow.async_configure(
            result2["flow_id"],
            {CONF_CLIENT_ID: "id", CONF_CLIENT_SECRET: "secret"},
        )
        assert result3["type"] == "create_entry"
        assert result3["data"][CONF_STRUCTURE_ID] == "structure1"


@pytest.mark.asyncio
async def test_setup_creates_room_device_and_entities(hass, fake_api):
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_VENT_BRAND: "flair",
            CONF_CLIENT_ID: "id",
            CONF_CLIENT_SECRET: "secret",
            CONF_STRUCTURE_ID: "structure1",
        },
    )
    entry.add_to_hass(hass)

    with patch("custom_components.hvac_vent_optimizer.FlairApi", return_value=fake_api):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    device_registry = dr.async_get(hass)
    entity_registry = er.async_get(hass)

    room_device = next(
        device
        for device in device_registry.devices.values()
        if (DOMAIN, "room_room1") in device.identifiers
    )

    room_entities = [
        entry
        for entry in entity_registry.entities.values()
        if entry.device_id == room_device.id
    ]

    assert any(entry.domain == "switch" for entry in room_entities)
    assert any(entry.domain == "climate" for entry in room_entities)
    assert any(entry.domain == "sensor" for entry in room_entities)
