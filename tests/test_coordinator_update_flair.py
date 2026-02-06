import asyncio
from types import SimpleNamespace

from custom_components.hvac_vent_optimizer.coordinator import FlairCoordinator
from custom_components.hvac_vent_optimizer.const import (
    BRAND_FLAIR,
    CONF_DAB_ENABLED,
    CONF_STRUCTURE_ID,
    CONF_VENT_BRAND,
)


class _Api:
    async def async_get_vents(self, structure_id):
        return [{"id": "v1", "attributes": {"name": "Vent 1"}}]

    async def async_get_pucks(self, structure_id):
        return [{"id": "p1", "attributes": {"name": "Puck 1"}}]


def test_async_update_data_flair_path():
    coord = FlairCoordinator.__new__(FlairCoordinator)
    coord.entry = SimpleNamespace(
        data={CONF_STRUCTURE_ID: "struct1"},
        options={CONF_DAB_ENABLED: False, CONF_VENT_BRAND: BRAND_FLAIR},
    )
    coord.api = _Api()
    async def fake_enrich_vents(vents, cache):
        return vents

    async def fake_enrich_pucks(pucks, cache):
        return pucks

    coord._async_enrich_vents = fake_enrich_vents
    coord._async_enrich_pucks = fake_enrich_pucks
    coord._async_process_dab = lambda *_: None
    coord._async_notify_error = lambda *args, **kwargs: None

    data = asyncio.run(coord._async_update_data())
    assert "v1" in data["vents"]
    assert "p1" in data["pucks"]
