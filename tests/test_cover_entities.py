import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from custom_components.hvac_vent_optimizer.cover import FlairVentCover


class _FakeApi:
    def __init__(self):
        self.calls = []

    async def async_set_vent_position(self, vent_id, position):
        self.calls.append((vent_id, position))


class _FakeCoordinator:
    def __init__(self, data):
        self.data = data
        self.api = _FakeApi()
        self.refresh_called = False

    async def async_request_refresh(self):
        self.refresh_called = True

    def is_manual_brand(self):
        return False

    def get_room_device_info_for_vent(self, vent_id):
        vent = self.data.get("vents", {}).get(vent_id, {})
        room = vent.get("room") or {}
        room_id = room.get("id")
        if not room_id:
            return None
        name = (room.get("attributes") or {}).get("name") or f"Room {room_id}"
        return {"identifiers": {("hvac_vent_optimizer", f"room_{room_id}")}, "name": name}


def test_cover_name_and_position():
    coordinator = _FakeCoordinator(
        {
            "vents": {
                "v1": {
                    "id": "v1",
                    "name": "Office",
                    "attributes": {"percent-open": 25},
                    "room": {"id": "room1", "attributes": {"name": "Office"}},
                }
            }
        }
    )
    entity = FlairVentCover(coordinator, "entry1", "v1")
    assert entity.name == "Office"
    assert entity.current_cover_position == 25
    assert entity.device_info["identifiers"] == {("hvac_vent_optimizer", "room_room1")}


def test_cover_set_position_calls_api():
    coordinator = _FakeCoordinator(
        {"vents": {"v1": {"id": "v1", "name": "Office", "attributes": {"percent-open": 25}}}}
    )
    entity = FlairVentCover(coordinator, "entry1", "v1")
    asyncio.run(entity.async_set_cover_position(position=75))
    assert coordinator.api.calls == [("v1", 75)]
    assert coordinator.refresh_called is True
    assert entity.current_cover_position == 75


def test_cover_open_close():
    coordinator = _FakeCoordinator(
        {"vents": {"v1": {"id": "v1", "name": "Office", "attributes": {"percent-open": 25}}}}
    )
    entity = FlairVentCover(coordinator, "entry1", "v1")
    asyncio.run(entity.async_open_cover())
    asyncio.run(entity.async_close_cover())
    assert ("v1", 100) in coordinator.api.calls
    assert ("v1", 0) in coordinator.api.calls


def test_cover_pending_position_keeps_state_until_refresh():
    coordinator = _FakeCoordinator(
        {"vents": {"v1": {"id": "v1", "name": "Office", "attributes": {"percent-open": 20}}}}
    )
    entity = FlairVentCover(coordinator, "entry1", "v1")
    asyncio.run(entity.async_set_cover_position(position=57))
    assert entity.current_cover_position == 57

    entity._handle_coordinator_update()
    assert entity.current_cover_position == 57

    entity._pending_until = datetime.now(timezone.utc) - timedelta(seconds=1)
    entity._handle_coordinator_update()
    assert entity.current_cover_position == 20


def test_cover_available_and_pending_match_clears():
    coordinator = _FakeCoordinator(
        {"vents": {"v1": {"id": "v1", "name": "Office", "attributes": {"percent-open": 30}}}}
    )
    coordinator.last_update_success = True
    entity = FlairVentCover(coordinator, "entry1", "v1")
    assert entity.available is True

    asyncio.run(entity.async_set_cover_position(position=60))
    coordinator.data["vents"]["v1"]["attributes"]["percent-open"] = 60
    entity._handle_coordinator_update()
    assert entity.current_cover_position == 60
    assert entity._pending_position is None


def test_cover_unavailable_without_percent_open_or_update_failure():
    coordinator = _FakeCoordinator({"vents": {"v1": {"id": "v1", "name": "Office", "attributes": {}}}})
    coordinator.last_update_success = True
    entity = FlairVentCover(coordinator, "entry1", "v1")
    assert entity.available is False

    coordinator.last_update_success = False
    coordinator.data["vents"]["v1"]["attributes"]["percent-open"] = 25
    assert entity.available is False
def test_cover_async_setup_entry_adds_entities():
    from custom_components.hvac_vent_optimizer import cover as cover_module

    coordinator = _FakeCoordinator(
        {"vents": {"v1": {"id": "v1", "name": "Office", "attributes": {}}}}
    )
    hass = SimpleNamespace(data={"hvac_vent_optimizer": {"entry1": coordinator}})
    entry = SimpleNamespace(entry_id="entry1")
    added = []

    def add_entities(entities):
        added.extend(entities)

    asyncio.run(cover_module.async_setup_entry(hass, entry, add_entities))
    assert len(added) == 1


def test_cover_async_setup_entry_skips_manual():
    from custom_components.hvac_vent_optimizer import cover as cover_module

    coordinator = _FakeCoordinator({"vents": {"v1": {"id": "v1", "name": "Office", "attributes": {}}}})
    coordinator.is_manual_brand = lambda: True
    hass = SimpleNamespace(data={"hvac_vent_optimizer": {"entry1": coordinator}})
    entry = SimpleNamespace(entry_id="entry1")
    added = []

    def add_entities(entities):
        added.extend(entities)

    asyncio.run(cover_module.async_setup_entry(hass, entry, add_entities))
    assert added == []
