"""Test configuration for HVAC Vent Optimizer."""
import os
import sys
from dataclasses import dataclass
from types import SimpleNamespace, ModuleType
from unittest.mock import MagicMock

import pytest
import pytest_socket

# Ensure sockets are enabled for event loop creation on Windows.
pytestmark = pytest.mark.enable_socket
pytest_socket.enable_socket()


@pytest.hookimpl(trylast=True)
def pytest_configure(config):
    if hasattr(config, "option"):
        setattr(config.option, "disable_socket", False)
        setattr(config.option, "force_enable_socket", True)
    setattr(config, "__socket_disabled", False)
    setattr(config, "__socket_force_enabled", True)
    pytest_socket.enable_socket()


@pytest.hookimpl(trylast=True)
def pytest_runtest_setup(item):
    pytest_socket.enable_socket()


@pytest.hookimpl(tryfirst=True)
def pytest_fixture_setup(fixturedef, request):
    if fixturedef.argname == "event_loop":
        pytest_socket.enable_socket()

# Ensure we can import local modules like dab.py without importing Home Assistant.
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
# Allow package-style imports (hvac_vent_optimizer.*) for modules that use relative imports.
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

# Provide minimal Home Assistant mocks so package imports don't fail.
homeassistant = sys.modules.get("homeassistant")
if homeassistant is None:
    homeassistant = MagicMock()
    sys.modules["homeassistant"] = homeassistant

homeassistant.helpers = getattr(homeassistant, "helpers", MagicMock())
homeassistant.helpers.aiohttp_client = getattr(homeassistant.helpers, "aiohttp_client", MagicMock())
homeassistant.helpers.update_coordinator = getattr(
    homeassistant.helpers, "update_coordinator", MagicMock()
)
homeassistant.helpers.event = getattr(homeassistant.helpers, "event", MagicMock())
homeassistant.helpers.storage = getattr(homeassistant.helpers, "storage", MagicMock())
homeassistant.components = getattr(homeassistant, "components", MagicMock())
homeassistant.components.cover = getattr(homeassistant.components, "cover", MagicMock())
homeassistant.components.sensor = getattr(homeassistant.components, "sensor", MagicMock())
homeassistant.components.binary_sensor = getattr(
    homeassistant.components, "binary_sensor", MagicMock()
)
homeassistant.components.switch = getattr(homeassistant.components, "switch", MagicMock())
homeassistant.components.persistent_notification = getattr(
    homeassistant.components, "persistent_notification", MagicMock()
)
homeassistant.components.logbook = getattr(homeassistant.components, "logbook", MagicMock())
homeassistant.components.climate = getattr(homeassistant.components, "climate", MagicMock())
homeassistant.components.climate.const = getattr(
    homeassistant.components.climate, "const", MagicMock()
)
homeassistant.const = getattr(homeassistant, "const", MagicMock())

config_entries_module = ModuleType("homeassistant.config_entries")


class _ConfigFlow:
    def __init_subclass__(cls, domain=None, **kwargs):
        super().__init_subclass__(**kwargs)
        cls.domain = domain

    async def async_set_unique_id(self, unique_id):
        self._unique_id = unique_id

    def _abort_if_unique_id_configured(self):
        return None

    def async_show_form(self, step_id, data_schema, errors=None, description_placeholders=None):
        return {
            "type": "form",
            "step_id": step_id,
            "data_schema": data_schema,
            "errors": errors or {},
            "description_placeholders": description_placeholders,
        }

    def async_show_menu(self, step_id, menu_options):
        return {"type": "menu", "step_id": step_id, "menu_options": menu_options}

    def async_create_entry(self, title, data):
        return {"type": "create_entry", "title": title, "data": data}


class _OptionsFlowWithConfigEntry(_ConfigFlow):
    def __init__(self, config_entry):
        self.config_entry = config_entry
        self.hass = getattr(config_entry, "hass", None)


class _ConfigEntry:
    def __init__(self, data=None, options=None):
        self.data = data or {}
        self.options = options or {}


config_entries_module.ConfigFlow = _ConfigFlow
config_entries_module.OptionsFlowWithConfigEntry = _OptionsFlowWithConfigEntry
config_entries_module.ConfigEntry = _ConfigEntry
sys.modules["homeassistant.config_entries"] = config_entries_module
homeassistant.config_entries = config_entries_module

core_module = ModuleType("homeassistant.core")


def _callback(func):
    return func


core_module.callback = _callback
class _HomeAssistant:
    pass


class _ServiceCall:
    def __init__(self, data=None):
        self.data = data or {}


core_module.HomeAssistant = _HomeAssistant
core_module.ServiceCall = _ServiceCall
sys.modules["homeassistant.core"] = core_module
homeassistant.core = core_module

selector_module = ModuleType("homeassistant.helpers.selector")


class _SelectorBase:
    def __init__(self, config):
        self.config = config

    def __call__(self, value):
        return value


class _EntitySelector(_SelectorBase):
    pass


class _EntitySelectorConfig:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class _SelectSelector(_SelectorBase):
    pass


class _SelectSelectorConfig:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class _SelectSelectorMode:
    DROPDOWN = "dropdown"


selector_module.EntitySelector = _EntitySelector
selector_module.EntitySelectorConfig = _EntitySelectorConfig
selector_module.SelectSelector = _SelectSelector
selector_module.SelectSelectorConfig = _SelectSelectorConfig
selector_module.SelectSelectorMode = _SelectSelectorMode
sys.modules["homeassistant.helpers.selector"] = selector_module
homeassistant.helpers.selector = selector_module
sys.modules.setdefault("homeassistant.helpers", homeassistant.helpers)
sys.modules.setdefault("homeassistant.helpers.aiohttp_client", homeassistant.helpers.aiohttp_client)
sys.modules.setdefault("homeassistant.helpers.update_coordinator", homeassistant.helpers.update_coordinator)
sys.modules.setdefault("homeassistant.helpers.event", homeassistant.helpers.event)
sys.modules.setdefault("homeassistant.helpers.storage", homeassistant.helpers.storage)
sys.modules.setdefault("homeassistant.helpers.selector", selector_module)
sys.modules.setdefault("homeassistant.components", homeassistant.components)
sys.modules.setdefault("homeassistant.components.cover", homeassistant.components.cover)
sys.modules.setdefault("homeassistant.components.sensor", homeassistant.components.sensor)
sys.modules.setdefault("homeassistant.components.binary_sensor", homeassistant.components.binary_sensor)
sys.modules.setdefault("homeassistant.components.switch", homeassistant.components.switch)
sys.modules.setdefault(
    "homeassistant.components.persistent_notification",
    homeassistant.components.persistent_notification,
)
sys.modules.setdefault("homeassistant.components.logbook", homeassistant.components.logbook)
sys.modules.setdefault("homeassistant.components.climate", homeassistant.components.climate)
sys.modules.setdefault("homeassistant.components.climate.const", homeassistant.components.climate.const)
sys.modules.setdefault("homeassistant.const", homeassistant.const)


class _DummyCoordinator:
    def __init__(self, *args, **kwargs):
        self.hass = args[0] if args else None
        self.update_interval = kwargs.get("update_interval")

    @classmethod
    def __class_getitem__(cls, item):
        return cls

    def async_set_updated_data(self, data):
        self.data = data


homeassistant.helpers.update_coordinator.DataUpdateCoordinator = _DummyCoordinator
homeassistant.helpers.update_coordinator.UpdateFailed = Exception


class _CoordinatorEntity:
    def __init__(self, coordinator):
        self.coordinator = coordinator

    def async_write_ha_state(self):
        return None


class _CoverEntity:
    def async_write_ha_state(self):
        return None


class _SensorEntity:
    pass


class _BinarySensorEntity:
    pass


class _SwitchEntity:
    pass


class _ClimateEntity:
    pass


@dataclass(frozen=True)
class _SensorEntityDescription:
    key: str
    name: str | None = None
    native_unit_of_measurement: str | None = None
    device_class: str | None = None
    state_class: str | None = None
    attribute: str | None = None
    efficiency_mode: str | None = None
    icon: str | None = None


homeassistant.helpers.update_coordinator.CoordinatorEntity = _CoordinatorEntity
homeassistant.components.cover.CoverEntity = _CoverEntity
homeassistant.components.sensor.SensorEntity = _SensorEntity
homeassistant.components.sensor.SensorEntityDescription = _SensorEntityDescription
homeassistant.components.binary_sensor.BinarySensorEntity = _BinarySensorEntity
homeassistant.components.binary_sensor.BinarySensorDeviceClass = SimpleNamespace(
    OCCUPANCY="occupancy"
)
homeassistant.components.switch.SwitchEntity = _SwitchEntity
homeassistant.components.climate.ClimateEntity = _ClimateEntity

homeassistant.components.climate.const.HVACAction = SimpleNamespace(
    COOLING="cooling", HEATING="heating"
)
homeassistant.components.climate.const.HVACMode = SimpleNamespace(AUTO="auto")
homeassistant.components.climate.const.ClimateEntityFeature = SimpleNamespace(
    TARGET_TEMPERATURE=1
)
homeassistant.components.climate.const.ATTR_TEMPERATURE = "temperature"
homeassistant.const.CONF_ENTRY_ID = "entry_id"
homeassistant.const.STATE_UNKNOWN = "unknown"
homeassistant.const.STATE_UNAVAILABLE = "unavailable"
homeassistant.const.UnitOfTemperature = SimpleNamespace(CELSIUS="C", FAHRENHEIT="F")
homeassistant.const.UnitOfPressure = SimpleNamespace(KPA="kPa")
homeassistant.const.SIGNAL_STRENGTH_DECIBELS_MILLIWATT = "dBm"
homeassistant.const.PERCENTAGE = "%"


class _DummyStore:
    def __init__(self, *args, **kwargs):
        self.data = None

    async def async_load(self):
        return self.data

    async def async_save(self, data):
        self.data = data


homeassistant.helpers.storage.Store = _DummyStore


def _track_state_change_event(hass, entity_id, callback):
    return lambda: None


homeassistant.helpers.event.async_track_state_change_event = _track_state_change_event
