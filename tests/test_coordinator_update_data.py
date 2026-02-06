import asyncio
from types import SimpleNamespace

import pytest
from homeassistant.helpers.update_coordinator import UpdateFailed

from custom_components.hvac_vent_optimizer.coordinator import FlairCoordinator
from custom_components.hvac_vent_optimizer.const import BRAND_MANUAL, CONF_DAB_ENABLED, CONF_STRUCTURE_ID, CONF_VENT_BRAND


def test_async_update_data_manual_branch():
    coord = FlairCoordinator.__new__(FlairCoordinator)
    coord.entry = SimpleNamespace(
        options={CONF_VENT_BRAND: BRAND_MANUAL, CONF_DAB_ENABLED: False},
        data={},
    )

    async def fake_manual():
        return {"vents": {}}

    coord._async_update_manual_data = fake_manual
    result = asyncio.run(coord._async_update_data())
    assert result == {"vents": {}}


def test_async_update_data_requires_api():
    coord = FlairCoordinator.__new__(FlairCoordinator)
    coord.entry = SimpleNamespace(
        data={CONF_STRUCTURE_ID: "struct1"},
        options={CONF_DAB_ENABLED: False},
    )
    coord.api = None
    with pytest.raises(UpdateFailed):
        asyncio.run(coord._async_update_data())
