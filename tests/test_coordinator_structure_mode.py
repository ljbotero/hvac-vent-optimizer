import asyncio
from types import SimpleNamespace

from custom_components.hvac_vent_optimizer.coordinator import FlairCoordinator
from custom_components.hvac_vent_optimizer.const import (
    BRAND_FLAIR,
    CONF_DAB_ENABLED,
    CONF_DAB_FORCE_MANUAL,
    CONF_STRUCTURE_ID,
    CONF_VENT_BRAND,
)


class _Api:
    def __init__(self):
        self.calls = []

    async def async_set_structure_mode(self, structure_id, mode):
        self.calls.append((structure_id, mode))


def test_async_ensure_structure_mode_calls_api():
    coord = FlairCoordinator.__new__(FlairCoordinator)
    coord.api = _Api()
    coord.entry = SimpleNamespace(
        data={CONF_STRUCTURE_ID: "struct1"},
        options={
            CONF_VENT_BRAND: BRAND_FLAIR,
            CONF_DAB_ENABLED: True,
            CONF_DAB_FORCE_MANUAL: True,
        },
    )
    asyncio.run(coord.async_ensure_structure_mode())
    assert coord.api.calls == [("struct1", "manual")]
