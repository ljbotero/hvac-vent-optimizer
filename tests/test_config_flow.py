import asyncio
from types import SimpleNamespace

from custom_components.hvac_vent_optimizer import config_flow
from custom_components.hvac_vent_optimizer.const import (
    BRAND_FLAIR,
    CONF_CLIENT_ID,
    CONF_CLIENT_SECRET,
    CONF_CLOSE_INACTIVE_ROOMS,
    CONF_CONVENTIONAL_VENTS_BY_THERMOSTAT,
    CONF_DAB_ENABLED,
    CONF_DAB_FORCE_MANUAL,
    CONF_INITIAL_EFFICIENCY_PERCENT,
    CONF_NOTIFY_EFFICIENCY_CHANGES,
    CONF_LOG_EFFICIENCY_CHANGES,
    CONF_CONTROL_STRATEGY,
    CONF_MIN_ADJUSTMENT_PERCENT,
    CONF_MIN_ADJUSTMENT_INTERVAL,
    CONF_TEMP_ERROR_OVERRIDE,
    CONF_POLL_INTERVAL_ACTIVE,
    CONF_POLL_INTERVAL_IDLE,
    CONF_STRUCTURE_ID,
    CONF_STRUCTURE_NAME,
    CONF_TEMP_SENSOR_ENTITY,
    CONF_THERMOSTAT_ENTITY,
    CONF_VENT_ASSIGNMENTS,
    CONF_VENT_BRAND,
    CONF_VENT_GRANULARITY,
    CONF_MANUAL_VENT_COUNT,
    CONF_MANUAL_VENTS,
    BRAND_MANUAL,
)


def _make_flow():
    flow = config_flow.HvacVentOptimizerConfigFlow()
    flow.hass = SimpleNamespace()
    return flow


def _make_options_flow(options=None, data=None):
    entry = SimpleNamespace(
        data=data
        or {
            CONF_VENT_BRAND: BRAND_FLAIR,
            CONF_CLIENT_ID: "id",
            CONF_CLIENT_SECRET: "secret",
            CONF_STRUCTURE_ID: "structure1",
            CONF_STRUCTURE_NAME: "House",
        },
        options=options or {},
    )
    entry.hass = SimpleNamespace()
    return config_flow.HvacVentOptimizerOptionsFlow(entry)


def test_async_step_user_shows_form_when_no_input(monkeypatch):
    flow = _make_flow()
    result = asyncio.run(flow.async_step_user())
    assert result["type"] == "form"
    assert result["step_id"] == "user"


def test_manual_flow_creates_entry():
    flow = _make_flow()
    result = asyncio.run(flow.async_step_user({CONF_VENT_BRAND: BRAND_MANUAL}))
    assert result["type"] == "form"
    assert result["step_id"] == "manual_count"

    result2 = asyncio.run(flow.async_step_manual_count({CONF_MANUAL_VENT_COUNT: 1}))
    assert result2["type"] == "form"
    assert result2["step_id"] == "manual_vents"

    result3 = asyncio.run(
        flow.async_step_manual_vents(
            {
                "manual_1_name": "Office",
                "manual_1_thermostat": "climate.main",
                "manual_1_temp_sensor": "sensor.office_temp",
            }
        )
    )
    assert result3["type"] == "create_entry"
    assert result3["data"][CONF_VENT_BRAND] == BRAND_MANUAL
    assert result3["data"][CONF_MANUAL_VENT_COUNT] == 1
    assert result3["data"][CONF_MANUAL_VENTS][0]["name"] == "Office"


def test_async_step_user_auth_error(monkeypatch):
    class _Api:
        def __init__(self, *_):
            pass

        async def async_authenticate(self):
            raise config_flow.FlairApiAuthError("bad")

        async def async_get_structures(self):
            return []

    monkeypatch.setattr(config_flow, "FlairApi", _Api)
    monkeypatch.setattr(config_flow.aiohttp_client, "async_get_clientsession", lambda hass: object())

    flow = _make_flow()
    result = asyncio.run(
        flow.async_step_flair_auth({CONF_CLIENT_ID: "id", CONF_CLIENT_SECRET: "secret"})
    )
    assert result["errors"]["base"] == "auth"


def test_async_step_user_cannot_connect(monkeypatch):
    class _Api:
        def __init__(self, *_):
            pass

        async def async_authenticate(self):
            raise config_flow.FlairApiError("down")

        async def async_get_structures(self):
            return []

    monkeypatch.setattr(config_flow, "FlairApi", _Api)
    monkeypatch.setattr(config_flow.aiohttp_client, "async_get_clientsession", lambda hass: object())

    flow = _make_flow()
    result = asyncio.run(
        flow.async_step_flair_auth({CONF_CLIENT_ID: "id", CONF_CLIENT_SECRET: "secret"})
    )
    assert result["errors"]["base"] == "cannot_connect"


def test_async_step_user_invalid_scope(monkeypatch):
    class _Api:
        def __init__(self, *_):
            pass

        async def async_authenticate(self):
            raise config_flow.FlairApiError(
                'Authentication failed: HTTP 400: {"error": "invalid_scope"}'
            )

        async def async_get_structures(self):
            return []

    monkeypatch.setattr(config_flow, "FlairApi", _Api)
    monkeypatch.setattr(config_flow.aiohttp_client, "async_get_clientsession", lambda hass: object())

    flow = _make_flow()
    result = asyncio.run(
        flow.async_step_flair_auth({CONF_CLIENT_ID: "id", CONF_CLIENT_SECRET: "secret"})
    )
    assert result["errors"]["base"] == "invalid_scope"


def test_async_step_user_invalid_client(monkeypatch):
    class _Api:
        def __init__(self, *_):
            pass

        async def async_authenticate(self):
            raise config_flow.FlairApiError(
                'Authentication failed: HTTP 400: {"error": "invalid_client"}'
            )

        async def async_get_structures(self):
            return []

    monkeypatch.setattr(config_flow, "FlairApi", _Api)
    monkeypatch.setattr(config_flow.aiohttp_client, "async_get_clientsession", lambda hass: object())

    flow = _make_flow()
    result = asyncio.run(
        flow.async_step_flair_auth({CONF_CLIENT_ID: "id", CONF_CLIENT_SECRET: "secret"})
    )
    assert result["errors"]["base"] == "invalid_client"


def test_async_step_user_invalid_grant(monkeypatch):
    class _Api:
        def __init__(self, *_):
            pass

        async def async_authenticate(self):
            raise config_flow.FlairApiError(
                'Authentication failed: HTTP 400: {"error": "invalid_grant"}'
            )

        async def async_get_structures(self):
            return []

    monkeypatch.setattr(config_flow, "FlairApi", _Api)
    monkeypatch.setattr(config_flow.aiohttp_client, "async_get_clientsession", lambda hass: object())

    flow = _make_flow()
    result = asyncio.run(
        flow.async_step_flair_auth({CONF_CLIENT_ID: "id", CONF_CLIENT_SECRET: "secret"})
    )
    assert result["errors"]["base"] == "invalid_grant"


def test_async_step_user_rate_limited(monkeypatch):
    class _Api:
        def __init__(self, *_):
            pass

        async def async_authenticate(self):
            raise config_flow.FlairApiError("Authentication failed: HTTP 429: rate_limited")

        async def async_get_structures(self):
            return []

    monkeypatch.setattr(config_flow, "FlairApi", _Api)
    monkeypatch.setattr(config_flow.aiohttp_client, "async_get_clientsession", lambda hass: object())

    flow = _make_flow()
    result = asyncio.run(
        flow.async_step_flair_auth({CONF_CLIENT_ID: "id", CONF_CLIENT_SECRET: "secret"})
    )
    assert result["errors"]["base"] == "rate_limited"


def test_options_flow_brand_manual_updates_options():
    options_flow = _make_options_flow(
        options={},
        data={
            CONF_VENT_BRAND: BRAND_FLAIR,
            CONF_CLIENT_ID: "id",
            CONF_CLIENT_SECRET: "secret",
            CONF_STRUCTURE_ID: "structure1",
            CONF_STRUCTURE_NAME: "House",
        },
    )
    result = asyncio.run(
        options_flow.async_step_brand_manual(
            {CONF_VENT_BRAND: BRAND_MANUAL, CONF_MANUAL_VENT_COUNT: 1}
        )
    )
    assert result["type"] == "form"
    result2 = asyncio.run(
        options_flow.async_step_manual_vents(
            {
                "manual_1_name": "Den",
                "manual_1_thermostat": "climate.main",
                "manual_1_temp_sensor": "sensor.den_temp",
            }
        )
    )
    assert result2["type"] == "create_entry"
    assert result2["data"][CONF_VENT_BRAND] == BRAND_MANUAL


def test_async_step_user_timeout(monkeypatch):
    class _Api:
        def __init__(self, *_):
            pass

        async def async_authenticate(self):
            raise config_flow.FlairApiError("Authentication request timed out")

        async def async_get_structures(self):
            return []

    monkeypatch.setattr(config_flow, "FlairApi", _Api)
    monkeypatch.setattr(config_flow.aiohttp_client, "async_get_clientsession", lambda hass: object())

    flow = _make_flow()
    result = asyncio.run(
        flow.async_step_flair_auth({CONF_CLIENT_ID: "id", CONF_CLIENT_SECRET: "secret"})
    )
    assert result["errors"]["base"] == "timeout"


def test_async_step_user_unknown_error(monkeypatch):
    class _Api:
        def __init__(self, *_):
            pass

        async def async_authenticate(self):
            raise RuntimeError("boom")

        async def async_get_structures(self):
            return []

    monkeypatch.setattr(config_flow, "FlairApi", _Api)
    monkeypatch.setattr(config_flow.aiohttp_client, "async_get_clientsession", lambda hass: object())

    flow = _make_flow()
    result = asyncio.run(
        flow.async_step_flair_auth({CONF_CLIENT_ID: "id", CONF_CLIENT_SECRET: "secret"})
    )
    assert result["errors"]["base"] == "unknown"


def test_async_step_user_no_structures(monkeypatch):
    class _Api:
        def __init__(self, *_):
            pass

        async def async_authenticate(self):
            return None

        async def async_get_structures(self):
            return []

    monkeypatch.setattr(config_flow, "FlairApi", _Api)
    monkeypatch.setattr(config_flow.aiohttp_client, "async_get_clientsession", lambda hass: object())

    flow = _make_flow()
    result = asyncio.run(
        flow.async_step_flair_auth({CONF_CLIENT_ID: "id", CONF_CLIENT_SECRET: "secret"})
    )
    assert result["errors"]["base"] == "no_structures"


def test_async_step_user_single_structure_creates_entry(monkeypatch):
    class _Api:
        def __init__(self, *_):
            pass

        async def async_authenticate(self):
            return None

        async def async_get_structures(self):
            return [{"id": "s1", "name": "Home"}]

    monkeypatch.setattr(config_flow, "FlairApi", _Api)
    monkeypatch.setattr(config_flow.aiohttp_client, "async_get_clientsession", lambda hass: object())

    flow = _make_flow()
    result = asyncio.run(
        flow.async_step_flair_auth({CONF_CLIENT_ID: "id", CONF_CLIENT_SECRET: "secret"})
    )
    assert result["type"] == "create_entry"
    assert result["data"][CONF_STRUCTURE_ID] == "s1"
    assert result["data"][CONF_STRUCTURE_NAME] == "Home"


def test_async_step_user_multi_structure_shows_structure_form(monkeypatch):
    class _Api:
        def __init__(self, *_):
            pass

        async def async_authenticate(self):
            return None

        async def async_get_structures(self):
            return [{"id": "s1", "name": "Home"}, {"id": "s2", "name": "Cabin"}]

    monkeypatch.setattr(config_flow, "FlairApi", _Api)
    monkeypatch.setattr(config_flow.aiohttp_client, "async_get_clientsession", lambda hass: object())

    flow = _make_flow()
    result = asyncio.run(
        flow.async_step_flair_auth({CONF_CLIENT_ID: "id", CONF_CLIENT_SECRET: "secret"})
    )
    assert result["type"] == "form"
    assert result["step_id"] == "structure"


def test_async_step_structure_selects_structure():
    flow = _make_flow()
    flow._structures = {"s1": "Home", "s2": "Cabin"}
    flow._client_id = "id"
    flow._client_secret = "secret"
    result = asyncio.run(flow.async_step_structure({CONF_STRUCTURE_ID: "s2"}))
    assert result["type"] == "create_entry"
    assert result["data"][CONF_STRUCTURE_ID] == "s2"


def test_async_step_structure_requires_structures():
    flow = _make_flow()
    result = asyncio.run(flow.async_step_structure())
    assert result["type"] == "form"
    assert result["errors"]["base"] == "no_structures"


def test_options_flow_factory_returns_options_flow():
    entry = SimpleNamespace(data={}, options={})
    flow = config_flow.HvacVentOptimizerConfigFlow()
    options_flow = flow.async_get_options_flow(entry)
    assert isinstance(options_flow, config_flow.HvacVentOptimizerOptionsFlow)


def test_options_flow_menu():
    options_flow = _make_options_flow()
    result = asyncio.run(options_flow.async_step_menu())
    assert result["type"] == "menu"
    assert "brand_manual" in result["menu_options"]
    assert "algorithm_settings" in result["menu_options"]


def test_options_flow_algorithm_settings_form():
    options_flow = _make_options_flow()
    result = asyncio.run(options_flow.async_step_algorithm_settings())
    assert result["type"] == "form"
    assert result["step_id"] == "algorithm_settings"


def test_options_flow_algorithm_settings_submit():
    options_flow = _make_options_flow()
    user_input = {
        CONF_DAB_ENABLED: False,
        CONF_DAB_FORCE_MANUAL: True,
        CONF_CLOSE_INACTIVE_ROOMS: False,
        CONF_VENT_GRANULARITY: "10",
        CONF_POLL_INTERVAL_ACTIVE: 5,
        CONF_POLL_INTERVAL_IDLE: 15,
        CONF_INITIAL_EFFICIENCY_PERCENT: 50,
        CONF_NOTIFY_EFFICIENCY_CHANGES: True,
        CONF_LOG_EFFICIENCY_CHANGES: False,
        CONF_CONTROL_STRATEGY: "hybrid",
        CONF_MIN_ADJUSTMENT_PERCENT: 10,
        CONF_MIN_ADJUSTMENT_INTERVAL: 30,
        CONF_TEMP_ERROR_OVERRIDE: 0.6,
    }
    result = asyncio.run(options_flow.async_step_algorithm_settings(user_input))
    assert result["type"] == "create_entry"
    assert result["data"][CONF_DAB_FORCE_MANUAL] is True
    assert result["data"][CONF_VENT_GRANULARITY] == 10
    assert result["data"][CONF_CONTROL_STRATEGY] == "hybrid"


def test_options_flow_vent_assignments_fetch_error(monkeypatch):
    options_flow = _make_options_flow()

    async def _raise():
        raise RuntimeError("boom")

    monkeypatch.setattr(options_flow, "_async_get_vents", _raise)
    result = asyncio.run(options_flow.async_step_vent_assignments())
    assert result["errors"]["base"] == "cannot_connect"


def test_options_flow_vent_assignments_fetches(monkeypatch):
    class _Api:
        def __init__(self, *_):
            pass

        async def async_authenticate(self):
            return None

        async def async_get_vents(self, structure_id):
            return [{"id": "v1", "name": "Office"}]

    monkeypatch.setattr(config_flow, "FlairApi", _Api)
    monkeypatch.setattr(config_flow.aiohttp_client, "async_get_clientsession", lambda hass: object())

    options_flow = _make_options_flow()
    result = asyncio.run(options_flow.async_step_vent_assignments())
    assert result["type"] == "form"
    assert options_flow._vents


def test_options_flow_vent_assignments_submit():
    options_flow = _make_options_flow(
        options={CONF_VENT_ASSIGNMENTS: {}},
    )
    options_flow._vents = [{"id": "v1", "name": "Office"}]
    result = asyncio.run(options_flow.async_step_vent_assignments())
    thermostat_key = next(iter(options_flow._vent_key_map))
    temp_sensor_key = next(iter(options_flow._temp_sensor_key_map))

    user_input = {
        thermostat_key: "climate.downstairs",
        temp_sensor_key: "sensor.office_temp",
    }
    result = asyncio.run(options_flow.async_step_vent_assignments(user_input))
    assert result["type"] == "create_entry"
    assignments = result["data"][CONF_VENT_ASSIGNMENTS]
    assert assignments["v1"][CONF_THERMOSTAT_ENTITY] == "climate.downstairs"
    assert assignments["v1"][CONF_TEMP_SENSOR_ENTITY] == "sensor.office_temp"


def test_options_flow_conventional_vents_no_assignments():
    options_flow = _make_options_flow(options={CONF_VENT_ASSIGNMENTS: {}})
    result = asyncio.run(options_flow.async_step_conventional_vents())
    assert result["errors"]["base"] == "no_assignments"


def test_options_flow_conventional_vents_submit():
    assignments = {
        "v1": {CONF_THERMOSTAT_ENTITY: "climate.one"},
        "v2": {CONF_THERMOSTAT_ENTITY: "climate.two"},
    }
    options_flow = _make_options_flow(options={CONF_VENT_ASSIGNMENTS: assignments})
    result = asyncio.run(options_flow.async_step_conventional_vents())
    user_input = {key: 2 for key in options_flow._thermostat_key_map}
    result = asyncio.run(options_flow.async_step_conventional_vents(user_input))
    assert result["type"] == "create_entry"
    mapping = result["data"][CONF_CONVENTIONAL_VENTS_BY_THERMOSTAT]
    assert mapping["climate.one"] == 2
    assert mapping["climate.two"] == 2


def test_safe_key_replaces_dots():
    assert config_flow._safe_key("conv", "climate.room.one") == "conv_climate_room_one"
