"""Service handlers for HVAC Vent Optimizer."""
from __future__ import annotations

import json
import logging
import os
from typing import Any

import voluptuous as vol
from homeassistant.core import HomeAssistant, ServiceCall
try:
    from homeassistant.core import SupportsResponse  # type: ignore
except Exception:  # pragma: no cover - older HA versions
    SupportsResponse = None
from homeassistant.components import persistent_notification
from homeassistant.util import json as json_util

from .const import (
    CONF_ACTIVE,
    CONF_ENTRY_ID,
    CONF_EFFICIENCY_PATH,
    CONF_EFFICIENCY_PAYLOAD,
    CONF_HOLD_UNTIL,
    CONF_ROOM_ID,
    CONF_SET_POINT_C,
    CONF_STRUCTURE_ID,
    CONF_STRUCTURE_MODE,
    CONF_THERMOSTAT_ENTITY,
    CONF_VENT_ID,
    DOMAIN,
    SERVICE_EXPORT_EFFICIENCY,
    SERVICE_IMPORT_EFFICIENCY,
    SERVICE_RUN_DAB,
    SERVICE_REFRESH_DEVICES,
    SERVICE_SET_ROOM_ACTIVE,
    SERVICE_SET_ROOM_SETPOINT,
    SERVICE_SET_STRUCTURE_MODE,
)
from .coordinator import FlairCoordinator

_LOGGER = logging.getLogger(__name__)


def _validate_room_or_vent(data: dict) -> dict:
    if not data.get(CONF_ROOM_ID) and not data.get(CONF_VENT_ID):
        raise vol.Invalid("room_id or vent_id must be provided")
    return data


SET_ROOM_ACTIVE_SCHEMA = vol.All(
    vol.Schema(
        {
            vol.Optional(CONF_ENTRY_ID): str,
            vol.Optional(CONF_ROOM_ID): str,
            vol.Optional(CONF_VENT_ID): str,
            vol.Required(CONF_ACTIVE): bool,
        }
    ),
    _validate_room_or_vent,
)

SET_ROOM_SETPOINT_SCHEMA = vol.All(
    vol.Schema(
        {
            vol.Optional(CONF_ENTRY_ID): str,
            vol.Optional(CONF_ROOM_ID): str,
            vol.Optional(CONF_VENT_ID): str,
            vol.Required(CONF_SET_POINT_C): vol.Coerce(float),
            vol.Optional(CONF_HOLD_UNTIL): str,
        }
    ),
    _validate_room_or_vent,
)

RUN_DAB_SCHEMA = vol.Schema(
    {
        vol.Optional(CONF_ENTRY_ID): str,
        vol.Optional(CONF_THERMOSTAT_ENTITY): str,
    }
)

REFRESH_DEVICES_SCHEMA = vol.Schema({vol.Optional(CONF_ENTRY_ID): str})

SET_STRUCTURE_MODE_SCHEMA = vol.Schema(
    {
        vol.Optional(CONF_ENTRY_ID): str,
        vol.Required(CONF_STRUCTURE_MODE): vol.In(["auto", "manual"]),
    }
)

EXPORT_EFFICIENCY_SCHEMA = vol.Schema(
    {
        vol.Optional(CONF_ENTRY_ID): str,
        vol.Optional(CONF_EFFICIENCY_PATH): str,
    }
)

IMPORT_EFFICIENCY_SCHEMA = vol.Any(
    vol.Schema(
        {
            vol.Optional(CONF_ENTRY_ID): str,
            vol.Required(CONF_EFFICIENCY_PATH): str,
        }
    ),
    vol.Schema(
        {
            vol.Optional(CONF_ENTRY_ID): str,
            vol.Required(CONF_EFFICIENCY_PAYLOAD): dict,
        }
    ),
    vol.Schema(
        {
            vol.Optional(CONF_ENTRY_ID): str,
            vol.Optional("exportMetadata"): dict,
            vol.Required("efficiencyData"): dict,
        }
    ),
)


async def async_register_services(hass: HomeAssistant) -> None:
    """Register integration services."""
    domain_data = hass.data.setdefault(DOMAIN, {})
    if domain_data.get("_services_registered"):
        return

    async def handle_set_room_active(call: ServiceCall) -> None:
        coordinator = _get_coordinator(hass, call.data.get(CONF_ENTRY_ID))
        if not coordinator:
            return
        if coordinator.is_manual_brand():
            raise ValueError("set_room_active is not available in Manual mode")

        room_id = call.data.get(CONF_ROOM_ID)
        vent_id = call.data.get(CONF_VENT_ID)
        if not room_id and vent_id:
            room_id = coordinator.resolve_room_id_from_vent(vent_id)

        if not room_id:
            _LOGGER.error("Could not resolve room_id for service call")
            return

        try:
            await coordinator.async_set_room_active(room_id, call.data[CONF_ACTIVE])
        except Exception as err:  # noqa: BLE001
            _LOGGER.exception("Failed to set room active: %s", err)
            persistent_notification.async_create(
                hass,
                f"Failed to set room active for {room_id}: {err}",
                title="HVAC Vent Optimizer error",
            )

    async def handle_run_dab(call: ServiceCall) -> None:
        coordinator = _get_coordinator(hass, call.data.get(CONF_ENTRY_ID))
        if not coordinator:
            return
        try:
            await coordinator.async_run_dab(call.data.get(CONF_THERMOSTAT_ENTITY))
        except Exception as err:  # noqa: BLE001
            _LOGGER.exception("Failed to run DAB: %s", err)
            persistent_notification.async_create(
                hass,
                f"Failed to run DAB: {err}",
                title="HVAC Vent Optimizer error",
            )

    async def handle_set_structure_mode(call: ServiceCall) -> None:
        coordinator = _get_coordinator(hass, call.data.get(CONF_ENTRY_ID))
        if not coordinator:
            return
        if coordinator.is_manual_brand():
            raise ValueError("set_structure_mode is not available in Manual mode")
        structure_id = coordinator.entry.data.get(CONF_STRUCTURE_ID)
        if not structure_id:
            _LOGGER.error("Missing structure_id in config entry")
            return
        try:
            await coordinator.api.async_set_structure_mode(
                structure_id, call.data[CONF_STRUCTURE_MODE]
            )
            await coordinator.async_request_refresh()
        except Exception as err:  # noqa: BLE001
            _LOGGER.exception("Failed to set structure mode: %s", err)
            persistent_notification.async_create(
                hass,
                f"Failed to set structure mode: {err}",
                title="HVAC Vent Optimizer error",
            )

    async def handle_set_room_setpoint(call: ServiceCall) -> None:
        coordinator = _get_coordinator(hass, call.data.get(CONF_ENTRY_ID))
        if not coordinator:
            return
        if coordinator.is_manual_brand():
            raise ValueError("set_room_setpoint is not available in Manual mode")

        room_id = call.data.get(CONF_ROOM_ID)
        vent_id = call.data.get(CONF_VENT_ID)
        if not room_id and vent_id:
            room_id = coordinator.resolve_room_id_from_vent(vent_id)

        if not room_id:
            _LOGGER.error("Could not resolve room_id for setpoint service call")
            return

        try:
            await coordinator.api.async_set_room_setpoint(
                room_id,
                call.data[CONF_SET_POINT_C],
                call.data.get(CONF_HOLD_UNTIL),
            )
            await coordinator.async_request_refresh()
        except Exception as err:  # noqa: BLE001
            _LOGGER.exception("Failed to set room setpoint: %s", err)
            persistent_notification.async_create(
                hass,
                f"Failed to set room setpoint for {room_id}: {err}",
                title="HVAC Vent Optimizer error",
            )

    async def handle_refresh_devices(call: ServiceCall) -> None:
        coordinator = _get_coordinator(hass, call.data.get(CONF_ENTRY_ID))
        if not coordinator:
            return
        if coordinator.is_manual_brand():
            raise ValueError("refresh_devices is not available in Manual mode")
        try:
            await coordinator.async_request_refresh()
        except Exception as err:  # noqa: BLE001
            _LOGGER.exception("Failed to refresh devices: %s", err)
            persistent_notification.async_create(
                hass,
                f"Failed to refresh devices: {err}",
                title="HVAC Vent Optimizer error",
            )

    async def handle_export_efficiency(call: ServiceCall) -> dict[str, Any]:
        coordinator = _get_coordinator(hass, call.data.get(CONF_ENTRY_ID))
        if not coordinator:
            return {"error": "No coordinator found"}

        try:
            await coordinator.async_request_refresh()
            payload = coordinator.build_efficiency_export()
            path_input = call.data.get(CONF_EFFICIENCY_PATH)
            if path_input:
                path = _resolve_efficiency_path(
                    hass,
                    path_input,
                    f"{DOMAIN}_efficiency_export_{coordinator.entry.entry_id}.json",
                )
                await hass.async_add_executor_job(_save_json, path, payload)
                _LOGGER.info("Exported efficiency data to %s", path)
                return {"saved_to": path}
            return payload
        except Exception as err:  # noqa: BLE001
            _LOGGER.exception("Failed to export efficiency data: %s", err)
            persistent_notification.async_create(
                hass,
                f"Failed to export efficiency data: {err}",
                title="HVAC Vent Optimizer error",
            )
            return {"error": str(err)}

    async def handle_import_efficiency(call: ServiceCall) -> None:
        coordinator = _get_coordinator(hass, call.data.get(CONF_ENTRY_ID))
        if not coordinator:
            return

        try:
            payload = call.data.get(CONF_EFFICIENCY_PAYLOAD)
            if payload is None and "efficiencyData" in call.data:
                payload = {
                    "exportMetadata": call.data.get("exportMetadata"),
                    "efficiencyData": call.data.get("efficiencyData"),
                }
            if payload is None:
                path = _resolve_efficiency_path(
                    hass,
                    call.data.get(CONF_EFFICIENCY_PATH),
                    "",
                )
                if not os.path.exists(path):
                    raise FileNotFoundError(path)
                payload = await hass.async_add_executor_job(json_util.load_json, path)
            result = await coordinator.async_import_efficiency(payload)
            _LOGGER.info(
                "Imported efficiency data: %s entries (%s applied, %s unmatched)",
                result["entries"],
                result["applied"],
                result["unmatched"],
            )
        except Exception as err:  # noqa: BLE001
            _LOGGER.exception("Failed to import efficiency data: %s", err)
            persistent_notification.async_create(
                hass,
                f"Failed to import efficiency data: {err}",
                title="HVAC Vent Optimizer error",
            )

    hass.services.async_register(
        DOMAIN,
        SERVICE_SET_ROOM_ACTIVE,
        handle_set_room_active,
        schema=SET_ROOM_ACTIVE_SCHEMA,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_SET_ROOM_SETPOINT,
        handle_set_room_setpoint,
        schema=SET_ROOM_SETPOINT_SCHEMA,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_SET_STRUCTURE_MODE,
        handle_set_structure_mode,
        schema=SET_STRUCTURE_MODE_SCHEMA,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_RUN_DAB,
        handle_run_dab,
        schema=RUN_DAB_SCHEMA,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_REFRESH_DEVICES,
        handle_refresh_devices,
        schema=REFRESH_DEVICES_SCHEMA,
    )
    export_kwargs = {}
    if SupportsResponse is not None:
        export_kwargs["supports_response"] = SupportsResponse.ONLY
    hass.services.async_register(
        DOMAIN,
        SERVICE_EXPORT_EFFICIENCY,
        handle_export_efficiency,
        schema=EXPORT_EFFICIENCY_SCHEMA,
        **export_kwargs,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_IMPORT_EFFICIENCY,
        handle_import_efficiency,
        schema=IMPORT_EFFICIENCY_SCHEMA,
    )

    domain_data["_services_registered"] = True


async def async_unregister_services(hass: HomeAssistant) -> None:
    """Unregister services if no entries remain."""
    domain_data = hass.data.get(DOMAIN, {})
    remaining = [
        value for key, value in domain_data.items() if isinstance(value, FlairCoordinator)
    ]
    if remaining:
        return

    if domain_data.pop("_services_registered", None):
        hass.services.async_remove(DOMAIN, SERVICE_SET_ROOM_ACTIVE)
        hass.services.async_remove(DOMAIN, SERVICE_SET_ROOM_SETPOINT)
        hass.services.async_remove(DOMAIN, SERVICE_SET_STRUCTURE_MODE)
        hass.services.async_remove(DOMAIN, SERVICE_RUN_DAB)
        hass.services.async_remove(DOMAIN, SERVICE_REFRESH_DEVICES)
        hass.services.async_remove(DOMAIN, SERVICE_EXPORT_EFFICIENCY)
        hass.services.async_remove(DOMAIN, SERVICE_IMPORT_EFFICIENCY)


def _get_coordinator(hass: HomeAssistant, entry_id: str | None) -> FlairCoordinator | None:
    domain_data = hass.data.get(DOMAIN, {})
    if entry_id:
        coordinator = domain_data.get(entry_id)
        if isinstance(coordinator, FlairCoordinator):
            return coordinator
        _LOGGER.error("No coordinator found for entry_id=%s", entry_id)
        return None

    coordinators = [
        value for value in domain_data.values() if isinstance(value, FlairCoordinator)
    ]
    if len(coordinators) == 1:
        return coordinators[0]

    _LOGGER.error("Multiple entries found; specify entry_id")
    return None


def _resolve_efficiency_path(hass: HomeAssistant, path: str | None, default_name: str) -> str:
    base_path = hass.config.path("")
    if not path:
        path = hass.config.path(default_name)
    elif not os.path.isabs(path):
        path = hass.config.path(path)

    base_real = os.path.realpath(base_path)
    path_real = os.path.realpath(path)

    if os.path.commonpath([base_real, path_real]) != base_real:
        if not hass.config.is_allowed_path(path_real):
            raise ValueError("Path is not allowed by Home Assistant")

    return path_real


def _save_json(path: str, payload: dict[str, Any]) -> None:
    with open(path, "w", encoding="utf-8") as file:
        json.dump(payload, file, indent=2, sort_keys=True)
