import asyncio
from types import SimpleNamespace


def test_climate_setup_skips_manual():
    from custom_components.hvac_vent_optimizer import climate as climate_module

    class _ManualCoordinator:
        def __init__(self):
            self.data = {"vents": {}, "pucks": {}}

        def is_manual_brand(self):
            return True

    coordinator = _ManualCoordinator()
    hass = SimpleNamespace(data={"hvac_vent_optimizer": {"entry1": coordinator}})
    entry = SimpleNamespace(entry_id="entry1")
    added = []

    def add_entities(entities):
        added.extend(entities)

    asyncio.run(climate_module.async_setup_entry(hass, entry, add_entities))
    assert added == []


def test_climate_setup_adds_unique_rooms():
    from custom_components.hvac_vent_optimizer import climate as climate_module

    class _Coordinator:
        def __init__(self):
            self.data = {
                "vents": {
                    "v1": {"room": {"id": "room1"}},
                    "v2": {"room": {"id": "room2"}},
                },
                "pucks": {
                    "p1": {"room": {"id": "room1"}},
                    "p2": {"room": {"id": "room3"}},
                },
            }

        def is_manual_brand(self):
            return False

    coordinator = _Coordinator()
    hass = SimpleNamespace(data={"hvac_vent_optimizer": {"entry1": coordinator}})
    entry = SimpleNamespace(entry_id="entry1")
    added = []

    def add_entities(entities):
        added.extend(entities)

    asyncio.run(climate_module.async_setup_entry(hass, entry, add_entities))
    assert len(added) == 3
