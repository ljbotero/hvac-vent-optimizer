import asyncio
from types import SimpleNamespace

import custom_components.hvac_vent_optimizer as integration


class _FakeConfigEntries:
    def __init__(self):
        self.forward_called = False
        self.unload_called = False
        self.reload_called = False

    async def async_forward_entry_setups(self, entry, platforms):
        self.forward_called = True
        return True

    async def async_unload_platforms(self, entry, platforms):
        self.unload_called = True
        return True

    async def async_reload(self, entry_id):
        self.reload_called = True


class _FakeHass:
    def __init__(self):
        self.data = {}
        self.config_entries = _FakeConfigEntries()


class _FakeCoordinator:
    def __init__(self, hass, api, entry):
        self.hass = hass
        self.api = api
        self.entry = entry
        self.initialized = False
        self.refreshed = False
        self.listeners = False

    async def async_initialize(self):
        self.initialized = True

    async def async_ensure_structure_mode(self):
        return None

    async def async_config_entry_first_refresh(self):
        self.refreshed = True

    async def async_setup_thermostat_listeners(self):
        self.listeners = True

    def async_shutdown(self):
        self.shutdown = True


class _FakeEntry:
    def __init__(self):
        self.data = {"client_id": "id", "client_secret": "secret", "vent_brand": "flair"}
        self.options = {}
        self.entry_id = "entry1"
        self.title = "title"

    def add_update_listener(self, listener):
        self._listener = listener
        return lambda: None

    def async_on_unload(self, func):
        self._unload = func


def test_async_setup_and_unload_entry(monkeypatch):
    hass = _FakeHass()
    entry = _FakeEntry()

    monkeypatch.setattr(integration, "async_get_clientsession", lambda hass: object())
    monkeypatch.setattr(integration, "FlairApi", lambda *args, **kwargs: object())
    monkeypatch.setattr(integration, "FlairCoordinator", _FakeCoordinator)

    async def fake_register(_):
        return None

    async def fake_unregister(_):
        return None

    monkeypatch.setattr(integration, "async_register_services", fake_register)
    monkeypatch.setattr(integration, "async_unregister_services", fake_unregister)

    asyncio.run(integration.async_setup_entry(hass, entry))
    assert hass.config_entries.forward_called is True
    assert entry.entry_id in hass.data[integration.DOMAIN]

    asyncio.run(integration.async_unload_entry(hass, entry))
    assert hass.config_entries.unload_called is True


def test_update_listener_triggers_reload(monkeypatch):
    hass = _FakeHass()
    entry = _FakeEntry()

    asyncio.run(integration._async_update_listener(hass, entry))
    assert hass.config_entries.reload_called is True
