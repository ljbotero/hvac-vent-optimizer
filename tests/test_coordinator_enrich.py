import asyncio
from types import SimpleNamespace

from custom_components.hvac_vent_optimizer.coordinator import FlairCoordinator


class _Api:
    def __init__(self):
        self.remote_calls = 0

    async def async_get_remote_sensor_reading(self, remote_id):
        self.remote_calls += 1
        return {"occupied": True}

    async def async_get_vent_reading(self, vent_id):
        return {"percent-open": 10}

    async def async_get_vent_room(self, vent_id):
        return {
            "id": "room1",
            "attributes": {},
            "relationships": {"remote-sensors": {"data": {"id": "rs1"}}},
        }

    async def async_get_puck_reading(self, puck_id):
        return {"current-temperature-c": 22.0}

    async def async_get_puck_room(self, puck_id):
        return {
            "id": "room2",
            "attributes": {},
            "relationships": {"remote-sensors": {"data": {"id": "rs1"}}},
        }


class _Hass:
    def __init__(self):
        self.config = SimpleNamespace(units=SimpleNamespace(temperature_unit="C"))

    def async_create_task(self, coro):
        return asyncio.create_task(coro)


def _make_coord():
    coord = FlairCoordinator.__new__(FlairCoordinator)
    coord.hass = _Hass()
    coord.api = _Api()
    coord._vent_last_reading = {}
    return coord


def test_async_enrich_room_uses_cache():
    coord = _make_coord()
    room = {
        "id": "room1",
        "attributes": {},
        "relationships": {"remote-sensors": {"data": {"id": "rs1"}}},
    }
    cache = {}
    asyncio.run(coord._async_enrich_room(room, cache))
    asyncio.run(coord._async_enrich_room(room, cache))
    assert coord.api.remote_calls == 1
    assert room["attributes"]["occupied"] is True


def test_async_enrich_vents_and_pucks():
    coord = _make_coord()
    vents = [{"id": "v1", "attributes": {}, "room": {}}]
    pucks = [{"id": "p1", "attributes": {}, "room": {}}]
    cache = {}
    vents_out = asyncio.run(coord._async_enrich_vents(vents, cache))
    pucks_out = asyncio.run(coord._async_enrich_pucks(pucks, cache))
    assert vents_out[0]["room"]["id"] == "room1"
    assert pucks_out[0]["room"]["id"] == "room2"
