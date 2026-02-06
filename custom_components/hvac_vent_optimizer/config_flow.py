"""Config flow for HVAC Vent Optimizer integration."""
from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers import aiohttp_client, selector

from .api import FlairApi, FlairApiAuthError, FlairApiError
from .const import (
    BRAND_FLAIR,
    BRAND_MANUAL,
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
    DEFAULT_CLOSE_INACTIVE_ROOMS,
    DEFAULT_CONVENTIONAL_VENTS,
    DEFAULT_DAB_ENABLED,
    DEFAULT_DAB_FORCE_MANUAL,
    DEFAULT_MANUAL_VENT_COUNT,
    DEFAULT_INITIAL_EFFICIENCY_PERCENT,
    DEFAULT_NOTIFY_EFFICIENCY_CHANGES,
    DEFAULT_LOG_EFFICIENCY_CHANGES,
    DEFAULT_CONTROL_STRATEGY,
    DEFAULT_MIN_ADJUSTMENT_PERCENT,
    DEFAULT_MIN_ADJUSTMENT_INTERVAL,
    DEFAULT_TEMP_ERROR_OVERRIDE,
    DEFAULT_POLL_INTERVAL_ACTIVE,
    DEFAULT_POLL_INTERVAL_IDLE,
    DEFAULT_VENT_GRANULARITY,
    DOMAIN,
    MAX_CONVENTIONAL_VENTS,
)

_LOGGER = logging.getLogger(__name__)


class HvacVentOptimizerConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for HVAC Vent Optimizer."""

    VERSION = 1

    def __init__(self) -> None:
        self._client_id: str | None = None
        self._client_secret: str | None = None
        self._structures: dict[str, str] = {}
        self._brand: str | None = None
        self._manual_vent_count: int | None = None
        self._manual_vent_defs: list[dict[str, Any]] = []

    async def async_step_user(self, user_input: dict[str, Any] | None = None):
        """Select vent brand."""
        errors: dict[str, str] = {}

        if user_input is not None:
            self._brand = user_input[CONF_VENT_BRAND]
            if self._brand == BRAND_FLAIR:
                return await self.async_step_flair_auth()
            return await self.async_step_manual_count()

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_VENT_BRAND, default=BRAND_FLAIR): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=[BRAND_FLAIR, BRAND_MANUAL],
                            mode=selector.SelectSelectorMode.DROPDOWN,
                        )
                    )
                }
            ),
            errors=errors,
        )

    async def async_step_flair_auth(self, user_input: dict[str, Any] | None = None):
        """Authenticate with Flair for device discovery."""
        errors: dict[str, str] = {}

        if user_input is not None:
            session = aiohttp_client.async_get_clientsession(self.hass)
            api = FlairApi(session, user_input[CONF_CLIENT_ID], user_input[CONF_CLIENT_SECRET])
            try:
                await api.async_authenticate()
                structures = await api.async_get_structures()
            except FlairApiAuthError:
                errors["base"] = "auth"
            except FlairApiError as err:
                message = str(err)
                message_lower = message.lower()
                _LOGGER.error("Flair API error during authentication: %s", message)
                if "invalid_scope" in message_lower:
                    errors["base"] = "invalid_scope"
                elif "invalid_client" in message_lower:
                    errors["base"] = "invalid_client"
                elif "invalid_grant" in message_lower:
                    errors["base"] = "invalid_grant"
                elif "429" in message_lower or "rate_limit" in message_lower:
                    errors["base"] = "rate_limited"
                elif "timed out" in message_lower or "timeout" in message_lower:
                    errors["base"] = "timeout"
                elif "http 5" in message_lower:
                    errors["base"] = "server_error"
                else:
                    errors["base"] = "cannot_connect"
            except Exception as err:  # noqa: BLE001 - surface unexpected errors
                _LOGGER.exception("Unexpected error during auth: %s", err)
                errors["base"] = "unknown"
            else:
                if not structures:
                    errors["base"] = "no_structures"
                else:
                    self._client_id = user_input[CONF_CLIENT_ID]
                    self._client_secret = user_input[CONF_CLIENT_SECRET]
                    self._structures = {s["id"]: s["name"] for s in structures if s.get("id")}

                    if len(self._structures) == 1:
                        structure_id = next(iter(self._structures))
                        return await self._create_entry_for_structure(structure_id)
                    return await self.async_step_structure()

        return self.async_show_form(
            step_id="flair_auth",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_CLIENT_ID): str,
                    vol.Required(CONF_CLIENT_SECRET): str,
                }
            ),
            errors=errors,
        )

    async def async_step_manual_count(self, user_input: dict[str, Any] | None = None):
        """Select number of manual vents."""
        errors: dict[str, str] = {}

        if user_input is not None:
            self._manual_vent_count = int(user_input[CONF_MANUAL_VENT_COUNT])
            return await self.async_step_manual_vents()

        return self.async_show_form(
            step_id="manual_count",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_MANUAL_VENT_COUNT,
                        default=DEFAULT_MANUAL_VENT_COUNT,
                    ): vol.All(vol.Coerce(int), vol.Range(min=1, max=50)),
                }
            ),
            errors=errors,
        )

    async def async_step_manual_vents(self, user_input: dict[str, Any] | None = None):
        """Configure manual vent definitions."""
        errors: dict[str, str] = {}
        count = self._manual_vent_count or DEFAULT_MANUAL_VENT_COUNT

        if user_input is not None:
            manual_vents: list[dict[str, Any]] = []
            vent_assignments: dict[str, dict[str, Any]] = {}
            for i in range(1, count + 1):
                vent_id = f"manual_{i}"
                name = user_input[f"manual_{i}_name"]
                thermostat = user_input[f"manual_{i}_thermostat"]
                temp_sensor = user_input[f"manual_{i}_temp_sensor"]
                manual_vents.append(
                    {
                        "id": vent_id,
                        "name": name,
                        CONF_THERMOSTAT_ENTITY: thermostat,
                        CONF_TEMP_SENSOR_ENTITY: temp_sensor,
                    }
                )
                vent_assignments[vent_id] = {
                    "vent_name": name,
                    CONF_THERMOSTAT_ENTITY: thermostat,
                    CONF_TEMP_SENSOR_ENTITY: temp_sensor,
                }

            return self.async_create_entry(
                title="HVAC Vent Optimizer (Manual)",
                data={
                    CONF_VENT_BRAND: BRAND_MANUAL,
                    CONF_MANUAL_VENT_COUNT: count,
                    CONF_MANUAL_VENTS: manual_vents,
                    CONF_VENT_ASSIGNMENTS: vent_assignments,
                },
            )

        data_schema: dict[Any, Any] = {}
        thermostat_selector = selector.EntitySelector(
            selector.EntitySelectorConfig(domain="climate")
        )
        temp_sensor_selector = selector.EntitySelector(
            selector.EntitySelectorConfig(domain="sensor", device_class="temperature")
        )
        for i in range(1, count + 1):
            data_schema[vol.Required(f"manual_{i}_name", default=f"Vent {i}")] = str
            data_schema[vol.Required(f"manual_{i}_thermostat")] = thermostat_selector
            data_schema[vol.Required(f"manual_{i}_temp_sensor")] = temp_sensor_selector

        return self.async_show_form(
            step_id="manual_vents",
            data_schema=vol.Schema(data_schema),
            errors=errors,
        )

    async def async_step_structure(self, user_input: dict[str, Any] | None = None):
        """Select which structure to add."""
        errors: dict[str, str] = {}

        if user_input is not None:
            structure_id = user_input[CONF_STRUCTURE_ID]
            return await self._create_entry_for_structure(structure_id)

        if not self._structures:
            errors["base"] = "no_structures"

        return self.async_show_form(
            step_id="structure",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_STRUCTURE_ID): vol.In(self._structures),
                }
            ),
            errors=errors,
        )

    async def _create_entry_for_structure(self, structure_id: str):
        await self.async_set_unique_id(structure_id)
        self._abort_if_unique_id_configured()

        return self.async_create_entry(
            title=self._structures.get(structure_id, structure_id),
            data={
                CONF_VENT_BRAND: BRAND_FLAIR,
                CONF_CLIENT_ID: self._client_id,
                CONF_CLIENT_SECRET: self._client_secret,
                CONF_STRUCTURE_ID: structure_id,
                CONF_STRUCTURE_NAME: self._structures.get(structure_id, structure_id),
            },
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: config_entries.ConfigEntry):
        """Get the options flow for this handler."""
        return HvacVentOptimizerOptionsFlow(config_entry)


class HvacVentOptimizerOptionsFlow(config_entries.OptionsFlowWithConfigEntry):
    """Handle options for HVAC Vent Optimizer."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        super().__init__(config_entry)
        self._vents: list[dict[str, Any]] = []
        self._thermostat_key_map: dict[str, str] = {}
        self._vent_key_map: dict[str, str] = {}
        self._temp_sensor_key_map: dict[str, str] = {}
        self._manual_vent_count: int | None = None

    async def async_step_init(self, user_input: dict[str, Any] | None = None):
        return await self.async_step_menu()

    async def async_step_menu(self, user_input: dict[str, Any] | None = None):
        brand = self._get_brand()
        menu_options: dict[str, str] = {
            "brand_manual": "Vent Brand & Manual Setup",
            "algorithm_settings": "Dynamic Airflow Balancing & Polling",
            "conventional_vents": "Conventional Vent Counts (Airflow Safety)",
        }
        if brand == BRAND_FLAIR:
            menu_options["vent_assignments"] = "Thermostat & Sensor Assignments"
        return self.async_show_menu(
            step_id="menu",
            menu_options=menu_options,
        )

    def _get_brand(self) -> str:
        return self.config_entry.options.get(
            CONF_VENT_BRAND, self.config_entry.data.get(CONF_VENT_BRAND, BRAND_FLAIR)
        )

    def _get_manual_vents(self) -> list[dict[str, Any]]:
        return self.config_entry.options.get(
            CONF_MANUAL_VENTS, self.config_entry.data.get(CONF_MANUAL_VENTS, [])
        )

    async def async_step_brand_manual(self, user_input: dict[str, Any] | None = None):
        errors: dict[str, str] = {}
        options = dict(self.config_entry.options)

        if user_input is not None:
            brand = user_input[CONF_VENT_BRAND]
            options[CONF_VENT_BRAND] = brand
            if brand == BRAND_MANUAL:
                return await self.async_step_manual_count()
            options.pop(CONF_MANUAL_VENT_COUNT, None)
            options.pop(CONF_MANUAL_VENTS, None)
            return self.async_create_entry(title="", data=options)

        return self.async_show_form(
            step_id="brand_manual",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_VENT_BRAND,
                        default=self._get_brand(),
                    ): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=[BRAND_FLAIR, BRAND_MANUAL],
                            mode=selector.SelectSelectorMode.DROPDOWN,
                        )
                    ),
                }
            ),
            errors=errors,
        )

    async def async_step_manual_count(self, user_input: dict[str, Any] | None = None):
        """Select number of manual vents (options flow)."""
        errors: dict[str, str] = {}

        if user_input is not None:
            self._manual_vent_count = int(user_input[CONF_MANUAL_VENT_COUNT])
            return await self.async_step_manual_vents()

        return self.async_show_form(
            step_id="manual_count",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_MANUAL_VENT_COUNT,
                        default=self.config_entry.options.get(
                            CONF_MANUAL_VENT_COUNT,
                            self.config_entry.data.get(
                                CONF_MANUAL_VENT_COUNT, DEFAULT_MANUAL_VENT_COUNT
                            ),
                        ),
                    ): vol.All(vol.Coerce(int), vol.Range(min=1, max=50)),
                }
            ),
            errors=errors,
        )

    async def async_step_manual_vents(self, user_input: dict[str, Any] | None = None):
        errors: dict[str, str] = {}
        options = dict(self.config_entry.options)

        count = self._manual_vent_count or self.config_entry.options.get(
            CONF_MANUAL_VENT_COUNT,
            self.config_entry.data.get(CONF_MANUAL_VENT_COUNT, DEFAULT_MANUAL_VENT_COUNT),
        )

        if user_input is not None:
            manual_vents: list[dict[str, Any]] = []
            vent_assignments: dict[str, dict[str, Any]] = {}
            for i in range(1, count + 1):
                vent_id = f"manual_{i}"
                name = user_input[f"manual_{i}_name"]
                thermostat = user_input[f"manual_{i}_thermostat"]
                temp_sensor = user_input[f"manual_{i}_temp_sensor"]
                manual_vents.append(
                    {
                        "id": vent_id,
                        "name": name,
                        CONF_THERMOSTAT_ENTITY: thermostat,
                        CONF_TEMP_SENSOR_ENTITY: temp_sensor,
                    }
                )
                vent_assignments[vent_id] = {
                    "vent_name": name,
                    CONF_THERMOSTAT_ENTITY: thermostat,
                    CONF_TEMP_SENSOR_ENTITY: temp_sensor,
                }

            options[CONF_VENT_BRAND] = BRAND_MANUAL
            options[CONF_MANUAL_VENT_COUNT] = count
            options[CONF_MANUAL_VENTS] = manual_vents
            options[CONF_VENT_ASSIGNMENTS] = vent_assignments
            return self.async_create_entry(title="", data=options)

        thermostat_selector = selector.EntitySelector(
            selector.EntitySelectorConfig(domain="climate")
        )
        temp_sensor_selector = selector.EntitySelector(
            selector.EntitySelectorConfig(domain="sensor", device_class="temperature")
        )
        existing = {v.get("id"): v for v in self._get_manual_vents()}
        data_schema: dict[Any, Any] = {}
        for i in range(1, count + 1):
            vent_id = f"manual_{i}"
            existing_vent = existing.get(vent_id, {})
            data_schema[vol.Required(f"manual_{i}_name", default=existing_vent.get("name", f"Vent {i}"))] = str
            data_schema[vol.Required(f"manual_{i}_thermostat", default=existing_vent.get(CONF_THERMOSTAT_ENTITY))] = thermostat_selector
            data_schema[vol.Required(f"manual_{i}_temp_sensor", default=existing_vent.get(CONF_TEMP_SENSOR_ENTITY))] = temp_sensor_selector

        return self.async_show_form(
            step_id="manual_vents",
            data_schema=vol.Schema(data_schema),
            errors=errors,
        )

    async def async_step_algorithm_settings(self, user_input: dict[str, Any] | None = None):
        errors: dict[str, str] = {}
        options = dict(self.config_entry.options)

        if user_input is not None:
            options.update(
                {
                    CONF_DAB_ENABLED: user_input[CONF_DAB_ENABLED],
                    CONF_DAB_FORCE_MANUAL: user_input[CONF_DAB_FORCE_MANUAL],
                    CONF_CLOSE_INACTIVE_ROOMS: user_input[CONF_CLOSE_INACTIVE_ROOMS],
                    CONF_VENT_GRANULARITY: int(user_input[CONF_VENT_GRANULARITY]),
                    CONF_POLL_INTERVAL_ACTIVE: user_input[CONF_POLL_INTERVAL_ACTIVE],
                    CONF_POLL_INTERVAL_IDLE: user_input[CONF_POLL_INTERVAL_IDLE],
                    CONF_INITIAL_EFFICIENCY_PERCENT: user_input[
                        CONF_INITIAL_EFFICIENCY_PERCENT
                    ],
                    CONF_NOTIFY_EFFICIENCY_CHANGES: user_input[
                        CONF_NOTIFY_EFFICIENCY_CHANGES
                    ],
                    CONF_LOG_EFFICIENCY_CHANGES: user_input[CONF_LOG_EFFICIENCY_CHANGES],
                    CONF_CONTROL_STRATEGY: user_input[CONF_CONTROL_STRATEGY],
                    CONF_MIN_ADJUSTMENT_PERCENT: user_input[CONF_MIN_ADJUSTMENT_PERCENT],
                    CONF_MIN_ADJUSTMENT_INTERVAL: user_input[CONF_MIN_ADJUSTMENT_INTERVAL],
                    CONF_TEMP_ERROR_OVERRIDE: user_input[CONF_TEMP_ERROR_OVERRIDE],
                }
            )
            return self.async_create_entry(title="", data=options)

        return self.async_show_form(
            step_id="algorithm_settings",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_DAB_ENABLED,
                        default=options.get(CONF_DAB_ENABLED, DEFAULT_DAB_ENABLED),
                    ): bool,
                    vol.Required(
                        CONF_DAB_FORCE_MANUAL,
                        default=options.get(CONF_DAB_FORCE_MANUAL, DEFAULT_DAB_FORCE_MANUAL),
                    ): bool,
                    vol.Required(
                        CONF_CLOSE_INACTIVE_ROOMS,
                        default=options.get(
                            CONF_CLOSE_INACTIVE_ROOMS, DEFAULT_CLOSE_INACTIVE_ROOMS
                        ),
                    ): bool,
                    vol.Required(
                        CONF_VENT_GRANULARITY,
                        default=str(
                            options.get(CONF_VENT_GRANULARITY, DEFAULT_VENT_GRANULARITY)
                        ),
                    ): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=["5", "10", "25", "50", "100"],
                            mode=selector.SelectSelectorMode.DROPDOWN,
                        )
                    ),
                    vol.Required(
                        CONF_POLL_INTERVAL_ACTIVE,
                        default=options.get(
                            CONF_POLL_INTERVAL_ACTIVE, DEFAULT_POLL_INTERVAL_ACTIVE
                        ),
                    ): vol.All(vol.Coerce(int), vol.Range(min=1)),
                    vol.Required(
                        CONF_POLL_INTERVAL_IDLE,
                        default=options.get(
                            CONF_POLL_INTERVAL_IDLE, DEFAULT_POLL_INTERVAL_IDLE
                        ),
                    ): vol.All(vol.Coerce(int), vol.Range(min=1)),
                    vol.Required(
                        CONF_INITIAL_EFFICIENCY_PERCENT,
                        default=options.get(
                            CONF_INITIAL_EFFICIENCY_PERCENT,
                            DEFAULT_INITIAL_EFFICIENCY_PERCENT,
                        ),
                    ): vol.All(vol.Coerce(int), vol.Range(min=0, max=100)),
                    vol.Required(
                        CONF_NOTIFY_EFFICIENCY_CHANGES,
                        default=options.get(
                            CONF_NOTIFY_EFFICIENCY_CHANGES,
                            DEFAULT_NOTIFY_EFFICIENCY_CHANGES,
                        ),
                    ): bool,
                    vol.Required(
                        CONF_LOG_EFFICIENCY_CHANGES,
                        default=options.get(
                            CONF_LOG_EFFICIENCY_CHANGES,
                            DEFAULT_LOG_EFFICIENCY_CHANGES,
                        ),
                    ): bool,
                    vol.Required(
                        CONF_CONTROL_STRATEGY,
                        default=options.get(
                            CONF_CONTROL_STRATEGY, DEFAULT_CONTROL_STRATEGY
                        ),
                    ): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=["dab", "cost", "stats", "hybrid"],
                            mode=selector.SelectSelectorMode.DROPDOWN,
                        )
                    ),
                    vol.Required(
                        CONF_MIN_ADJUSTMENT_PERCENT,
                        default=options.get(
                            CONF_MIN_ADJUSTMENT_PERCENT,
                            DEFAULT_MIN_ADJUSTMENT_PERCENT,
                        ),
                    ): vol.All(vol.Coerce(int), vol.Range(min=0, max=100)),
                    vol.Required(
                        CONF_MIN_ADJUSTMENT_INTERVAL,
                        default=options.get(
                            CONF_MIN_ADJUSTMENT_INTERVAL,
                            DEFAULT_MIN_ADJUSTMENT_INTERVAL,
                        ),
                    ): vol.All(vol.Coerce(int), vol.Range(min=0, max=240)),
                    vol.Required(
                        CONF_TEMP_ERROR_OVERRIDE,
                        default=options.get(
                            CONF_TEMP_ERROR_OVERRIDE,
                            DEFAULT_TEMP_ERROR_OVERRIDE,
                        ),
                    ): vol.All(vol.Coerce(float), vol.Range(min=0, max=5)),
                }
            ),
            errors=errors,
        )

    async def async_step_vent_assignments(self, user_input: dict[str, Any] | None = None):
        errors: dict[str, str] = {}
        options = dict(self.config_entry.options)

        if not self._vents:
            try:
                self._vents = await self._async_get_vents()
            except Exception as err:  # noqa: BLE001
                _LOGGER.exception("Failed to load vents: %s", err)
                errors["base"] = "cannot_connect"
                self._vents = []

        if user_input is not None:
            assignments: dict[str, dict[str, Any]] = {}
            for vent in self._vents:
                vent_id = vent["id"]
                thermostat_key = next(
                    (key for key, mapped_id in self._vent_key_map.items() if mapped_id == vent_id),
                    f"{vent_id}_thermostat",
                )
                temp_sensor_key = next(
                    (key for key, mapped_id in self._temp_sensor_key_map.items() if mapped_id == vent_id),
                    f"{vent_id}_temp_sensor",
                )
                assignments[vent_id] = {
                    "vent_name": vent["name"],
                    CONF_THERMOSTAT_ENTITY: user_input[thermostat_key],
                    CONF_TEMP_SENSOR_ENTITY: user_input.get(temp_sensor_key),
                }

            options[CONF_VENT_ASSIGNMENTS] = assignments
            return self.async_create_entry(title="", data=options)

        assignments = options.get(CONF_VENT_ASSIGNMENTS, {})
        data_schema: dict[Any, Any] = {}
        self._vent_key_map = {}
        self._temp_sensor_key_map = {}

        thermostat_selector = selector.EntitySelector(
            selector.EntitySelectorConfig(domain="climate")
        )
        temp_sensor_selector = selector.EntitySelector(
            selector.EntitySelectorConfig(domain="sensor", device_class="temperature")
        )

        for vent in self._vents:
            vent_id = vent["id"]
            vent_name = vent["name"]
            assignment = assignments.get(vent_id, {})

            thermostat_key = f"{vent_name} ({vent_id}) - Thermostat"
            temp_sensor_key = f"{vent_name} ({vent_id}) - Temperature Sensor (optional)"
            self._vent_key_map[thermostat_key] = vent_id
            self._temp_sensor_key_map[temp_sensor_key] = vent_id

            data_schema[
                vol.Required(
                    thermostat_key,
                    default=assignment.get(CONF_THERMOSTAT_ENTITY),
                )
            ] = thermostat_selector
            data_schema[
                vol.Optional(
                    temp_sensor_key,
                    default=assignment.get(CONF_TEMP_SENSOR_ENTITY),
                )
            ] = temp_sensor_selector

        return self.async_show_form(
            step_id="vent_assignments",
            data_schema=vol.Schema(data_schema),
            errors=errors,
            description_placeholders={"vent_count": str(len(self._vents))},
        )

    async def async_step_conventional_vents(self, user_input: dict[str, Any] | None = None):
        errors: dict[str, str] = {}
        options = dict(self.config_entry.options)
        assignments = options.get(CONF_VENT_ASSIGNMENTS, self.config_entry.data.get(CONF_VENT_ASSIGNMENTS, {}))

        if not assignments:
            errors["base"] = "no_assignments"
            return self.async_show_form(
                step_id="conventional_vents",
                data_schema=vol.Schema({}),
                errors=errors,
            )

        thermostats = sorted(
            {
                data.get(CONF_THERMOSTAT_ENTITY)
                for data in assignments.values()
                if data.get(CONF_THERMOSTAT_ENTITY)
            }
        )

        if user_input is not None:
            mapping: dict[str, int] = {}
            for key, thermostat_id in self._thermostat_key_map.items():
                mapping[thermostat_id] = user_input.get(key, DEFAULT_CONVENTIONAL_VENTS)
            options[CONF_CONVENTIONAL_VENTS_BY_THERMOSTAT] = mapping
            return self.async_create_entry(title="", data=options)

        existing = options.get(CONF_CONVENTIONAL_VENTS_BY_THERMOSTAT, {})
        data_schema: dict[Any, Any] = {}
        self._thermostat_key_map = {}

        for thermostat_id in thermostats:
            key = _safe_key("conv", thermostat_id)
            self._thermostat_key_map[key] = thermostat_id
            data_schema[vol.Required(key, default=existing.get(thermostat_id, 0))] = vol.All(
                vol.Coerce(int), vol.Range(min=0, max=MAX_CONVENTIONAL_VENTS)
            )

        return self.async_show_form(
            step_id="conventional_vents",
            data_schema=vol.Schema(data_schema),
            errors=errors,
        )

    async def _async_get_vents(self) -> list[dict[str, Any]]:
        session = aiohttp_client.async_get_clientsession(self.hass)
        api = FlairApi(
            session,
            self.config_entry.data[CONF_CLIENT_ID],
            self.config_entry.data[CONF_CLIENT_SECRET],
        )
        await api.async_authenticate()
        return await api.async_get_vents(self.config_entry.data[CONF_STRUCTURE_ID])


def _safe_key(prefix: str, entity_id: str) -> str:
    return f"{prefix}_{entity_id}".replace(".", "_")
