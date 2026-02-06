import asyncio
from types import SimpleNamespace

import pytest

from custom_components.hvac_vent_optimizer import services
from custom_components.hvac_vent_optimizer.const import DOMAIN


class _FakeApi:
    def __init__(self):
        self.calls = []

    async def async_set_room_setpoint(self, room_id, set_point_c, hold_until):
        self.calls.append(("setpoint", room_id, set_point_c, hold_until))

    async def async_set_structure_mode(self, structure_id, mode):
        self.calls.append(("mode", structure_id, mode))


class _FakeCoordinator:
    def __init__(self, entry_id="entry1", structure_id="struct1"):
        self.entry = SimpleNamespace(data={"structure_id": structure_id})
        self.entry.entry_id = entry_id
        self.api = _FakeApi()
        self.entry_id = entry_id
        self.last_room_active = None
        self.last_run_dab = None
        self.refresh_called = False
        self.export_called = False
        self.import_payload = None

    async def async_set_room_active(self, room_id, active):
        self.last_room_active = (room_id, active)

    async def async_run_dab(self, thermostat_entity=None):
        self.last_run_dab = thermostat_entity

    async def async_request_refresh(self):
        self.refresh_called = True

    def build_efficiency_export(self):
        self.export_called = True
        return {"efficiencyData": {"roomEfficiencies": []}}

    async def async_import_efficiency(self, payload):
        self.import_payload = payload
        return {"entries": 0, "applied": 0, "unmatched": 0}

    def resolve_room_id_from_vent(self, vent_id):
        return "room-from-vent"

    def is_manual_brand(self):
        return False


class _ManualCoordinator(_FakeCoordinator):
    def is_manual_brand(self):
        return True


class _FakeServices:
    def __init__(self):
        self.registry = {}

    def async_register(self, domain, service, handler, schema=None, supports_response=None):
        self.registry[(domain, service)] = handler

    def async_remove(self, domain, service):
        self.registry.pop((domain, service), None)


class _FakeHass:
    def __init__(self, coordinator):
        self.data = {DOMAIN: {"_services_registered": False, "entry1": coordinator}}
        self.services = _FakeServices()
        self._notifications = []
        self.config = SimpleNamespace(
            path=lambda *parts: "config/" + "/".join(parts),
            is_allowed_path=lambda path: True,
        )

    def async_create_task(self, coro):
        self._notifications.append(coro)

    async def async_add_executor_job(self, func, *args, **kwargs):
        return func(*args, **kwargs)


class _FakeConfig:
    def __init__(self, allowed=True):
        self._allowed = allowed

    def path(self, *parts):
        base = "C:\\config"
        if not parts:
            return base
        return base + "\\" + "\\".join(parts)

    def is_allowed_path(self, path):
        return self._allowed


class _ServiceCall:
    def __init__(self, data):
        self.data = data


def test_register_and_run_services():
    coordinator = _FakeCoordinator()
    hass = _FakeHass(coordinator)

    services.FlairCoordinator = _FakeCoordinator
    asyncio.run(services.async_register_services(hass))
    assert (DOMAIN, "set_room_active") in hass.services.registry
    assert (DOMAIN, "refresh_devices") in hass.services.registry
    assert (DOMAIN, "export_efficiency") in hass.services.registry
    assert (DOMAIN, "import_efficiency") in hass.services.registry

    call = _ServiceCall({"room_id": "room1", "active": True})
    asyncio.run(hass.services.registry[(DOMAIN, "set_room_active")](call))
    assert coordinator.last_room_active == ("room1", True)

    call = _ServiceCall({"vent_id": "vent1", "active": False})
    asyncio.run(hass.services.registry[(DOMAIN, "set_room_active")](call))
    assert coordinator.last_room_active == ("room-from-vent", False)

    call = _ServiceCall({"thermostat_entity": "climate.upstairs"})
    asyncio.run(hass.services.registry[(DOMAIN, "run_dab")](call))
    assert coordinator.last_run_dab == "climate.upstairs"

    call = _ServiceCall({"room_id": "room2", "set_point_c": 22.0})
    asyncio.run(hass.services.registry[(DOMAIN, "set_room_setpoint")](call))
    assert coordinator.api.calls[0] == ("setpoint", "room2", 22.0, None)
    assert coordinator.refresh_called is True

    call = _ServiceCall({"structure_mode": "manual"})
    asyncio.run(hass.services.registry[(DOMAIN, "set_structure_mode")](call))
    assert ("mode", "struct1", "manual") in coordinator.api.calls

    coordinator.refresh_called = False
    call = _ServiceCall({})
    asyncio.run(hass.services.registry[(DOMAIN, "refresh_devices")](call))
    assert coordinator.refresh_called is True

    services._save_json = lambda path, data: None
    call = _ServiceCall({"efficiency_path": "efficiency.json"})
    result = asyncio.run(hass.services.registry[(DOMAIN, "export_efficiency")](call))
    assert coordinator.export_called is True
    assert result["saved_to"].endswith("efficiency.json")

    coordinator.export_called = False
    call = _ServiceCall({})
    result = asyncio.run(hass.services.registry[(DOMAIN, "export_efficiency")](call))
    assert coordinator.export_called is True
    assert "efficiencyData" in result

    services.os.path.exists = lambda path: True
    services.json_util.load_json = lambda path: {"efficiencyData": {"roomEfficiencies": []}}
    call = _ServiceCall({"efficiency_path": "efficiency.json"})
    asyncio.run(hass.services.registry[(DOMAIN, "import_efficiency")](call))
    assert coordinator.import_payload == {"efficiencyData": {"roomEfficiencies": []}}

    coordinator.import_payload = None
    payload = {"efficiencyData": {"roomEfficiencies": []}}
    call = _ServiceCall({"efficiency_payload": payload})
    asyncio.run(hass.services.registry[(DOMAIN, "import_efficiency")](call))
    assert coordinator.import_payload == payload

    coordinator.import_payload = None
    call = _ServiceCall(
        {
            "exportMetadata": {"version": "0.22"},
            "efficiencyData": {"roomEfficiencies": []},
        }
    )
    asyncio.run(hass.services.registry[(DOMAIN, "import_efficiency")](call))
    assert coordinator.import_payload == {
        "exportMetadata": {"version": "0.22"},
        "efficiencyData": {"roomEfficiencies": []},
    }


def test_unregister_services():
    coordinator = _FakeCoordinator()
    hass = _FakeHass(coordinator)
    hass.data[DOMAIN]["_services_registered"] = True
    services.FlairCoordinator = _FakeCoordinator
    hass.data[DOMAIN]["entry1"] = coordinator
    asyncio.run(services.async_unregister_services(hass))
    # still has coordinator, so services remain
    assert hass.data[DOMAIN]["_services_registered"] is True

    hass.data[DOMAIN] = {"_services_registered": True}
    asyncio.run(services.async_unregister_services(hass))
    assert hass.data[DOMAIN].get("_services_registered") is None


def test_validate_room_or_vent():
    try:
        services._validate_room_or_vent({})
    except Exception as exc:
        assert "room_id or vent_id" in str(exc)
    assert services._validate_room_or_vent({"room_id": "r1"}) == {"room_id": "r1"}


def test_services_blocked_in_manual_mode():
    coordinator = _ManualCoordinator()
    hass = _FakeHass(coordinator)
    services.FlairCoordinator = _ManualCoordinator
    asyncio.run(services.async_register_services(hass))

    with pytest.raises(ValueError):
        asyncio.run(hass.services.registry[(DOMAIN, "set_room_active")](_ServiceCall({"room_id": "room1"})))
    with pytest.raises(ValueError):
        asyncio.run(hass.services.registry[(DOMAIN, "set_room_setpoint")](_ServiceCall({"room_id": "room1", "set_point_c": 22.0})))
    with pytest.raises(ValueError):
        asyncio.run(hass.services.registry[(DOMAIN, "set_structure_mode")](_ServiceCall({"structure_mode": "manual"})))
    with pytest.raises(ValueError):
        asyncio.run(hass.services.registry[(DOMAIN, "refresh_devices")](_ServiceCall({})))


def test_resolve_efficiency_path_allows_inside_config():
    hass = SimpleNamespace(config=_FakeConfig())
    path = services._resolve_efficiency_path(hass, "eff.json", "default.json")
    assert path.endswith("config\\eff.json")


def test_resolve_efficiency_path_blocks_external():
    hass = SimpleNamespace(config=_FakeConfig(allowed=False))
    with pytest.raises(ValueError):
        services._resolve_efficiency_path(hass, "C:/tmp/eff.json", "default.json")


def test_get_coordinator_lookup():
    services.FlairCoordinator = _FakeCoordinator
    c1 = _FakeCoordinator(entry_id="entry1")
    c2 = _FakeCoordinator(entry_id="entry2")
    hass = SimpleNamespace(data={DOMAIN: {"entry1": c1, "entry2": c2}})
    assert services._get_coordinator(hass, "entry1") == c1
    assert services._get_coordinator(hass, "missing") is None
    # multiple entries, no entry_id
    assert services._get_coordinator(hass, None) is None

    hass_single = SimpleNamespace(data={DOMAIN: {"entry1": c1}})
    assert services._get_coordinator(hass_single, None) == c1


def test_service_export_efficiency_handles_errors():
    coordinator = _FakeCoordinator()
    hass = _FakeHass(coordinator)
    services.FlairCoordinator = _FakeCoordinator
    asyncio.run(services.async_register_services(hass))

    notifications = []
    original_notify = services.persistent_notification.async_create
    original_resolve = services._resolve_efficiency_path
    services.persistent_notification.async_create = (
        lambda hass_obj, msg, title=None: notifications.append((msg, title))
    )
    services._resolve_efficiency_path = lambda *args, **kwargs: (_ for _ in ()).throw(ValueError("bad path"))

    call = _ServiceCall({"efficiency_path": "../bad.json"})
    result = asyncio.run(hass.services.registry[(DOMAIN, "export_efficiency")](call))
    assert "error" in result
    assert notifications
    services.persistent_notification.async_create = original_notify
    services._resolve_efficiency_path = original_resolve


def test_service_import_efficiency_missing_file_notifies():
    coordinator = _FakeCoordinator()
    hass = _FakeHass(coordinator)
    services.FlairCoordinator = _FakeCoordinator
    asyncio.run(services.async_register_services(hass))

    notifications = []
    original_notify = services.persistent_notification.async_create
    services.persistent_notification.async_create = (
        lambda hass_obj, msg, title=None: notifications.append((msg, title))
    )
    original_exists = services.os.path.exists
    services.os.path.exists = lambda path: False

    call = _ServiceCall({"efficiency_path": "missing.json"})
    asyncio.run(hass.services.registry[(DOMAIN, "import_efficiency")](call))
    assert notifications
    services.persistent_notification.async_create = original_notify
    services.os.path.exists = original_exists


def test_service_set_structure_mode_missing_structure_id():
    coordinator = _FakeCoordinator(structure_id=None)
    hass = _FakeHass(coordinator)
    services.FlairCoordinator = _FakeCoordinator
    asyncio.run(services.async_register_services(hass))

    call = _ServiceCall({"structure_mode": "manual"})
    asyncio.run(hass.services.registry[(DOMAIN, "set_structure_mode")](call))
    assert coordinator.api.calls == []
