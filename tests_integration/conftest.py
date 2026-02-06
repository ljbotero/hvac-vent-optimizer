import os
import sys
import asyncio
from unittest.mock import MagicMock

import pytest
import pytest_socket

pytest_plugins = "pytest_homeassistant_custom_component"
pytestmark = pytest.mark.enable_socket

# Ensure sockets are enabled before the event loop fixture initializes on Windows.
pytest_socket.enable_socket()


def pytest_configure(config):
    # Override pytest-socket default disable behavior for integration tests.
    if hasattr(config, "option"):
        setattr(config.option, "disable_socket", False)
        setattr(config.option, "force_enable_socket", True)
    setattr(config, "__socket_disabled", False)
    setattr(config, "__socket_force_enabled", True)
    pytest_socket.enable_socket()


@pytest.hookimpl(trylast=True)
def pytest_runtest_setup(item):
    pytest_socket.enable_socket()


@pytest.fixture
def event_loop_policy():
    pytest_socket.enable_socket()
    return asyncio.get_event_loop_policy()

# Ensure project root is on sys.path so "config.custom_components" is importable.
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", ".."))
CONFIG_DIR = os.path.join(PROJECT_ROOT, "config")
CUSTOM_COMPONENTS_DIR = os.path.join(CONFIG_DIR, "custom_components")
# For Home Assistant custom integrations, "custom_components" must be importable
# from the config directory.
if CONFIG_DIR not in sys.path:
    sys.path.insert(0, CONFIG_DIR)
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)
# Some plugins may import a non-namespace "custom_components" module before we
# adjust sys.path. Ensure it can resolve our integration.
try:
    import custom_components  # type: ignore
except Exception:
    custom_components = None

if custom_components is not None:
    if not hasattr(custom_components, "__path__"):
        custom_components.__path__ = []  # type: ignore[attr-defined]
    if CUSTOM_COMPONENTS_DIR not in custom_components.__path__:
        custom_components.__path__.append(CUSTOM_COMPONENTS_DIR)  # type: ignore[attr-defined]


@pytest.fixture(autouse=True)
def _use_project_config_dir(hass):
    hass.config.config_dir = CONFIG_DIR
    try:
        from homeassistant.loader import DATA_CUSTOM_COMPONENTS, DATA_INTEGRATIONS

        # Clear cached integrations so the new config_dir is scanned.
        hass.data[DATA_INTEGRATIONS] = {}
        hass.data.pop(DATA_CUSTOM_COMPONENTS, None)
    except Exception:
        # If loader internals change, tests will surface it elsewhere.
        pass
    return None


@pytest.fixture(autouse=True)
def _patch_aiohttp_client(monkeypatch):
    """Avoid creating real aiohttp sessions (and aiodns) during tests."""
    session = MagicMock()
    monkeypatch.setattr(
        "homeassistant.helpers.aiohttp_client.async_get_clientsession",
        lambda hass: session,
    )
    return session


class FakeApi:
    def __init__(self):
        self.calls = []

    async def async_authenticate(self):
        return None

    async def async_get_structures(self):
        return [{"id": "structure1", "name": "Home"}]

    async def async_get_vents(self, structure_id):
        return [{"id": "vent1", "attributes": {"name": "Office Vent"}}]

    async def async_get_pucks(self, structure_id):
        return [{"id": "puck1", "attributes": {"name": "Office Puck"}}]

    async def async_get_vent_reading(self, vent_id):
        return {"percent-open": 25, "duct-temperature-c": 19.0}

    async def async_get_puck_reading(self, puck_id):
        return {"current-temperature-c": 22.0}

    async def async_get_vent_room(self, vent_id):
        return {
            "id": "room1",
            "attributes": {"name": "Office", "active": True, "current-temperature-c": 21.5},
            "relationships": {},
        }

    async def async_get_puck_room(self, puck_id):
        return {
            "id": "room1",
            "attributes": {"name": "Office", "active": True, "current-temperature-c": 21.5},
            "relationships": {},
        }

    async def async_get_remote_sensor_reading(self, sensor_id):
        return {}

    async def async_set_room_active(self, room_id, active):
        self.calls.append(("set_room_active", room_id, active))

    async def async_set_room_setpoint(self, room_id, set_point_c, hold_until=None):
        self.calls.append(("set_room_setpoint", room_id, set_point_c, hold_until))

    async def async_set_structure_mode(self, structure_id, mode):
        self.calls.append(("set_structure_mode", structure_id, mode))

    async def async_set_vent_position(self, vent_id, percent_open):
        self.calls.append(("set_vent_position", vent_id, percent_open))


@pytest.fixture
def fake_api():
    return FakeApi()
