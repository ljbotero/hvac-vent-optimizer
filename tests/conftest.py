"""Test harness for hvac_vent_optimizer.

Home Assistant, aiohttp and voluptuous are not installed in the local test
environment (and the runtime targets a different Python version than the test
host). To unit-test the *real* component code we inject lightweight stub
modules into ``sys.modules`` before the package is imported. The stubs only
implement the surface the component actually touches.

These stubs live only in the test process; they are never shipped or imported
by Home Assistant at runtime.
"""
from __future__ import annotations

import asyncio
import sys
import types
from datetime import datetime
from pathlib import Path

# --- Make the component importable as the ``hvac_vent_optimizer`` package ----
# The integration ships under ``custom_components/hvac_vent_optimizer`` (HACS
# layout). Put that ``custom_components`` directory on ``sys.path`` so the tests
# can ``import hvac_vent_optimizer`` directly without pulling in Home Assistant.
_REPO_ROOT = Path(__file__).resolve().parent.parent        # repo root (contains custom_components/)
_PARENT = _REPO_ROOT / "custom_components"                 # dir that *contains* the package
if str(_PARENT) not in sys.path:
    sys.path.insert(0, str(_PARENT))
# Also expose the repo root so ``from tests._fakes import ...`` resolves.
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


def _module(name: str) -> types.ModuleType:
    mod = types.ModuleType(name)
    sys.modules[name] = mod
    return mod


def _ensure(name: str) -> types.ModuleType:
    if name in sys.modules:
        return sys.modules[name]
    return _module(name)


# ---------------------------------------------------------------------------
# aiohttp stub
# ---------------------------------------------------------------------------
def _install_aiohttp() -> None:
    if "aiohttp" in sys.modules:
        return
    aiohttp = _module("aiohttp")

    class ClientError(Exception):
        ...

    class ContentTypeError(ClientError):
        ...

    class ClientTimeout:
        def __init__(self, total=None):
            self.total = total

    class ClientResponse:  # only used in annotations (PEP 563 -> not evaluated)
        ...

    class ClientSession:  # placeholder; tests pass their own fakes
        ...

    aiohttp.ClientError = ClientError
    aiohttp.ContentTypeError = ContentTypeError
    aiohttp.ClientTimeout = ClientTimeout
    aiohttp.ClientResponse = ClientResponse
    aiohttp.ClientSession = ClientSession


# ---------------------------------------------------------------------------
# voluptuous stub
# ---------------------------------------------------------------------------
def _install_voluptuous() -> None:
    if "voluptuous" in sys.modules:
        return
    vol = _module("voluptuous")

    class Invalid(Exception):
        ...

    class _Marker:
        def __init__(self, schema, default=None, description=None):
            self.schema = schema
            self.default = default

        def __hash__(self):
            return hash(repr(self.schema))

        def __eq__(self, other):
            return isinstance(other, _Marker) and other.schema == self.schema

    class Required(_Marker):
        ...

    class Optional(_Marker):
        ...

    class Schema:
        def __init__(self, schema=None, **kwargs):
            self.schema = schema

        def __call__(self, data):
            return data

    class All:
        def __init__(self, *validators, **kwargs):
            self.validators = validators

        def __call__(self, data):
            return data

    class Any:
        def __init__(self, *validators, **kwargs):
            self.validators = validators

        def __call__(self, data):
            return data

    class Coerce:
        def __init__(self, type_, msg=None):
            self.type = type_

        def __call__(self, data):
            return self.type(data)

    class Range:
        def __init__(self, min=None, max=None, **kwargs):
            self.min = min
            self.max = max

        def __call__(self, data):
            return data

    class In:
        def __init__(self, container, msg=None):
            self.container = container

        def __call__(self, data):
            return data

    vol.Invalid = Invalid
    vol.Required = Required
    vol.Optional = Optional
    vol.Schema = Schema
    vol.All = All
    vol.Any = Any
    vol.Coerce = Coerce
    vol.Range = Range
    vol.In = In


# ---------------------------------------------------------------------------
# homeassistant stub tree
# ---------------------------------------------------------------------------
def _install_homeassistant() -> None:
    if "homeassistant" in sys.modules:
        return

    _ensure("homeassistant")

    # homeassistant.const
    const = _ensure("homeassistant.const")
    const.STATE_UNAVAILABLE = "unavailable"
    const.STATE_UNKNOWN = "unknown"
    const.ATTR_TEMPERATURE = "temperature"
    const.PERCENTAGE = "%"
    const.SIGNAL_STRENGTH_DECIBELS_MILLIWATT = "dBm"

    class UnitOfTemperature:
        CELSIUS = "°C"
        FAHRENHEIT = "°F"

    class UnitOfPressure:
        KPA = "kPa"

    const.UnitOfTemperature = UnitOfTemperature
    const.UnitOfPressure = UnitOfPressure

    # homeassistant.core
    core = _ensure("homeassistant.core")

    class HomeAssistant:
        ...

    class ServiceCall:
        def __init__(self, data=None):
            self.data = data or {}

    class SupportsResponse:
        ONLY = "only"
        OPTIONAL = "optional"

    def callback(func):
        return func

    core.HomeAssistant = HomeAssistant
    core.ServiceCall = ServiceCall
    core.SupportsResponse = SupportsResponse
    core.callback = callback

    # homeassistant.config_entries
    config_entries = _ensure("homeassistant.config_entries")

    class ConfigEntry:
        ...

    class _FlowResultMixin:
        """Minimal flow-result helpers mirroring HA's FlowHandler surface.

        The real Home Assistant base classes return ``FlowResult`` mappings from
        these helpers; the stub returns plain dicts so unit tests can inspect the
        ``type``/``data``/``step_id`` a flow step produced without a live HA.
        """

        def async_create_entry(self, *, title="", data=None, **kwargs):
            return {"type": "create_entry", "title": title, "data": data or {}}

        def async_show_form(self, *, step_id=None, data_schema=None, errors=None,
                            description_placeholders=None, **kwargs):
            return {
                "type": "form",
                "step_id": step_id,
                "data_schema": data_schema,
                "errors": errors or {},
            }

        def async_show_menu(self, *, step_id=None, menu_options=None, **kwargs):
            return {
                "type": "menu",
                "step_id": step_id,
                "menu_options": menu_options or {},
            }

    class _OptionsFlow(_FlowResultMixin):
        def __init__(self, config_entry=None):
            self.config_entry = config_entry

    config_entries.ConfigEntry = ConfigEntry
    config_entries.OptionsFlowWithConfigEntry = _OptionsFlow

    class _ConfigFlowMeta(type):
        def __init_subclass__(cls, **kwargs):  # pragma: no cover
            ...

    class ConfigFlow(_FlowResultMixin):
        def __init_subclass__(cls, **kwargs):
            ...

    config_entries.ConfigFlow = ConfigFlow

    # homeassistant.components (namespace) + submodules used directly
    components = _ensure("homeassistant.components")

    persistent_notification = _ensure("homeassistant.components.persistent_notification")
    persistent_notification.async_create = lambda *a, **k: None
    persistent_notification.async_dismiss = lambda *a, **k: None
    components.persistent_notification = persistent_notification

    logbook = _ensure("homeassistant.components.logbook")
    logbook.async_log_entry = lambda *a, **k: None
    components.logbook = logbook

    # homeassistant.components.climate + .const
    climate = _ensure("homeassistant.components.climate")

    class ClimateEntity:
        ...

    climate.ClimateEntity = ClimateEntity

    climate_const = _ensure("homeassistant.components.climate.const")

    class HVACAction:
        COOLING = "cooling"
        HEATING = "heating"
        IDLE = "idle"
        OFF = "off"
        FAN = "fan"

    class ClimateEntityFeature:
        TARGET_TEMPERATURE = 1
        TARGET_TEMPERATURE_RANGE = 2

    class HVACMode:
        AUTO = "auto"
        OFF = "off"
        HEAT = "heat"
        COOL = "cool"
        HEAT_COOL = "heat_cool"

    climate_const.HVACAction = HVACAction
    climate_const.ClimateEntityFeature = ClimateEntityFeature
    climate_const.HVACMode = HVACMode
    climate.const = climate_const

    # homeassistant.helpers.*
    _ensure("homeassistant.helpers")

    aiohttp_client = _ensure("homeassistant.helpers.aiohttp_client")
    aiohttp_client.async_get_clientsession = lambda hass: object()

    event = _ensure("homeassistant.helpers.event")
    event.async_track_state_change_event = lambda hass, entity, cb: (lambda: None)

    storage = _ensure("homeassistant.helpers.storage")

    class Store:
        def __init__(self, hass, version, key, **kwargs):
            self.hass = hass
            self.version = version
            self.key = key
            self.saved = None

        async def async_load(self):
            return None

        async def async_save(self, data):
            self.saved = data

    storage.Store = Store

    update_coordinator = _ensure("homeassistant.helpers.update_coordinator")

    class DataUpdateCoordinator:
        def __class_getitem__(cls, item):
            return cls

        def __init__(self, hass, logger, name=None, update_interval=None, **kwargs):
            self.hass = hass
            self.logger = logger
            self.name = name
            self.update_interval = update_interval
            self.data = None
            self.last_update_success = True

        async def async_request_refresh(self):
            return None

        async def async_config_entry_first_refresh(self):
            return None

        def async_set_updated_data(self, data):
            self.data = data

        def async_update_listeners(self):
            return None

    class CoordinatorEntity:
        def __init__(self, coordinator, context=None):
            self.coordinator = coordinator

        def async_write_ha_state(self):
            return None

        async def async_added_to_hass(self):
            return None

    class UpdateFailed(Exception):
        ...

    update_coordinator.DataUpdateCoordinator = DataUpdateCoordinator
    update_coordinator.CoordinatorEntity = CoordinatorEntity
    update_coordinator.UpdateFailed = UpdateFailed

    # homeassistant.util.dt + json
    _ensure("homeassistant.util")

    dt = _ensure("homeassistant.util.dt")
    dt.now = lambda: datetime.now()
    dt.utcnow = lambda: datetime.utcnow()

    def _as_utc(value):
        return value

    dt.as_utc = _as_utc

    json_util = _ensure("homeassistant.util.json")
    json_util.load_json = lambda path: {}
    json_util.save_json = lambda path, data: None

    # homeassistant.components.number / .sensor / restore_state (number.py)
    number = _ensure("homeassistant.components.number")

    class NumberEntity:
        ...

    class NumberMode:
        BOX = "box"
        SLIDER = "slider"
        AUTO = "auto"

    sensor = _ensure("homeassistant.components.sensor")

    class SensorEntity:
        ...

    import dataclasses as _dc

    @_dc.dataclass(frozen=True)
    class SensorEntityDescription:
        key: str | None = None
        name: str | None = None
        native_unit_of_measurement: str | None = None
        device_class: str | None = None
        state_class: str | None = None
        icon: str | None = None
        entity_category: str | None = None

    class SensorStateClass:
        MEASUREMENT = "measurement"
        TOTAL = "total"
        TOTAL_INCREASING = "total_increasing"

    sensor.SensorEntity = SensorEntity
    sensor.SensorEntityDescription = SensorEntityDescription
    sensor.SensorStateClass = SensorStateClass

    restore_state = _ensure("homeassistant.helpers.restore_state")

    class RestoreEntity:
        async def async_get_last_state(self):
            return None

        async def async_get_last_extra_data(self):
            return None

    restore_state.RestoreEntity = RestoreEntity

    # RestoreNumber lives in homeassistant.components.number (NOT restore_state).
    # Mirror the real layout so wrong-location imports fail in tests too.
    class RestoreNumber(NumberEntity, RestoreEntity):
        async def async_get_last_number_data(self):
            return None

    number.NumberEntity = NumberEntity
    number.NumberMode = NumberMode
    number.RestoreNumber = RestoreNumber

    # Remaining platform bases (so every platform module is importable).
    binary_sensor = _ensure("homeassistant.components.binary_sensor")

    class BinarySensorEntity:
        ...

    class BinarySensorDeviceClass:
        OCCUPANCY = "occupancy"
        PROBLEM = "problem"

    binary_sensor.BinarySensorEntity = BinarySensorEntity
    binary_sensor.BinarySensorDeviceClass = BinarySensorDeviceClass

    switch = _ensure("homeassistant.components.switch")

    class SwitchEntity:
        ...

    switch.SwitchEntity = SwitchEntity

    cover = _ensure("homeassistant.components.cover")

    class CoverEntity:
        ...

    cover.CoverEntity = CoverEntity

    # homeassistant.helpers.selector (config_flow.py)
    selector = _ensure("homeassistant.helpers.selector")

    class _SelectorBase:
        def __init__(self, config=None):
            self.config = config

    class EntitySelector(_SelectorBase):
        ...

    class EntitySelectorConfig(dict):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)

    class SelectSelector(_SelectorBase):
        ...

    class SelectSelectorConfig(dict):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)

    class SelectSelectorMode:
        DROPDOWN = "dropdown"
        LIST = "list"

    selector.EntitySelector = EntitySelector
    selector.EntitySelectorConfig = EntitySelectorConfig
    selector.SelectSelector = SelectSelector
    selector.SelectSelectorConfig = SelectSelectorConfig
    selector.SelectSelectorMode = SelectSelectorMode


_install_aiohttp()
_install_voluptuous()
_install_homeassistant()


# ---------------------------------------------------------------------------
# Pytest fixtures that build the real FlairCoordinator with fakes
# ---------------------------------------------------------------------------
import pytest  # noqa: E402

from tests._fakes import FakeApi, FakeEntry, FakeHass, FakeState  # noqa: E402


def _build_coordinator(*, options=None, data=None, unit="°C"):
    from hvac_vent_optimizer import const
    from hvac_vent_optimizer.coordinator import FlairCoordinator

    opts = {
        const.CONF_VENT_BRAND: const.BRAND_FLAIR,
        const.CONF_DAB_ENABLED: True,
    }
    if options:
        opts.update(options)
    entry = FakeEntry(
        data={const.CONF_STRUCTURE_ID: "s1", const.CONF_CLIENT_ID: "id",
              const.CONF_CLIENT_SECRET: "sec"},
        options=opts,
    )
    hass = FakeHass(unit=unit)
    api = FakeApi()
    coord = FlairCoordinator(hass, api, entry)
    coord.data = data if data is not None else {"vents": {}, "pucks": {}}
    return coord, hass, api, entry


@pytest.fixture
def make_coordinator():
    """Factory for a minimally-constructed coordinator."""
    return _build_coordinator


@pytest.fixture
def ready_coordinator():
    """A coordinator wired to run the DAB apply path for one vent/thermostat."""
    from hvac_vent_optimizer import const

    thermostat = "climate.t"
    vent_id = "v1"
    data = {
        "vents": {
            vent_id: {
                "id": vent_id,
                "name": "Vent 1",
                "attributes": {"percent-open": 0},
                "room": {
                    "id": "room1",
                    "attributes": {
                        "name": "Room1",
                        "active": True,
                        "current-temperature-c": 26.0,
                    },
                },
            }
        },
        "pucks": {},
    }
    options = {
        const.CONF_VENT_ASSIGNMENTS: {
            vent_id: {const.CONF_THERMOSTAT_ENTITY: thermostat,
                      const.CONF_TEMP_SENSOR_ENTITY: None},
        },
        const.CONF_CONTROL_STRATEGY: "hybrid",
    }
    coord, hass, api, entry = _build_coordinator(options=options, data=data)

    hass.states.set(
        thermostat,
        FakeState(
            "cool",
            {
                "hvac_action": "cooling",
                "current_temperature": 26.0,
                "temperature": 24.0,
                "temperature_unit": "°C",
            },
        ),
    )
    # Seed a learned rate so the apply path produces concrete targets.
    coord._vent_rates[vent_id] = {"cooling": 0.5, "heating": 0.5}

    return {
        "coord": coord,
        "hass": hass,
        "api": api,
        "entry": entry,
        "thermostat": thermostat,
        "vent_id": vent_id,
        "data": data,
    }
