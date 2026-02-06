"""Coordinator for HVAC Vent Optimizer."""
from __future__ import annotations

import logging
import math
from datetime import datetime, timedelta, timezone
import asyncio
from typing import Any

import aiohttp
from homeassistant.components.climate.const import HVACAction
from homeassistant.components import persistent_notification, logbook
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import FlairApi, FlairApiError
from .const import (
    BRAND_FLAIR,
    BRAND_MANUAL,
    CONF_MANUAL_VENTS,
    CONF_VENT_BRAND,
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
    CONF_VENT_ASSIGNMENTS,
    CONF_THERMOSTAT_ENTITY,
    CONF_TEMP_SENSOR_ENTITY,
    CONF_VENT_GRANULARITY,
    DEFAULT_POLL_INTERVAL_ACTIVE,
    DEFAULT_POLL_INTERVAL_IDLE,
    DEFAULT_DAB_FORCE_MANUAL,
    DEFAULT_INITIAL_EFFICIENCY_PERCENT,
    DEFAULT_NOTIFY_EFFICIENCY_CHANGES,
    DEFAULT_LOG_EFFICIENCY_CHANGES,
    DEFAULT_CONTROL_STRATEGY,
    DEFAULT_MIN_ADJUSTMENT_PERCENT,
    DEFAULT_MIN_ADJUSTMENT_INTERVAL,
    DEFAULT_TEMP_ERROR_OVERRIDE,
    DOMAIN,
)
from .dab import (
    DEFAULT_SETTINGS,
    adjust_for_minimum_airflow,
    calculate_longest_minutes_to_target,
    calculate_open_percentage_for_all_vents,
    calculate_hvac_mode,
    calculate_room_change_rate,
    calculate_vent_open_percentage,
    has_room_reached_setpoint,
    rolling_average,
    round_big_decimal,
    round_to_nearest_multiple,
    should_pre_adjust,
)
from .utils import get_remote_sensor_id, is_fahrenheit_unit

_LOGGER = logging.getLogger(__name__)

EFF_ACTION_STABLE_MIN = 1.0
EFF_WARMUP_MIN = 2.0
EFF_MIN_WINDOW_MIN = 5.0
EFF_MAX_WINDOW_MIN = 30.0
EFF_MIN_DELTA_C = 0.2
EFF_MIN_APERTURE_PCT = 5.0
EFF_MIN_DUCT_DELTA_C = 2.0
EFF_DUCT_STABILITY_C = 1.0
EFF_APERTURE_JITTER_PCT = 15.0
EFF_ALPHA0 = 0.10
EFF_ALPHA_MIN = 0.01
EFF_BETA = 0.20
EFF_SHRINKAGE = 0.01
EFF_REGIME_CONFIDENCE = 0.60
EFF_SIGMA_MIN = 0.05
EFF_SIGMA_REL = 0.25
EFF_REGIME_COUNT = 2


class FlairCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Coordinates API access and polling for vent devices."""

    def __init__(self, hass: HomeAssistant, api: FlairApi | None, entry: ConfigEntry) -> None:
        self.api = api
        self.entry = entry
        self._unsub_thermostat_listeners: list[callable] = []
        self._dab_state: dict[str, dict[str, Any]] = {}
        self._last_hvac_action: dict[str, str] = {}
        self._vent_rates: dict[str, dict[str, float]] = {}
        self._vent_last_reading: dict[str, datetime] = {}
        self._vent_last_commanded: dict[str, datetime] = {}
        self._vent_last_target: dict[str, int] = {}
        self._manual_apertures: dict[str, int] = {}
        self._vent_models: dict[str, dict[str, dict[str, float]]] = {}
        self._efficiency_models: dict[str, dict[str, dict[str, Any]]] = {}
        self._vent_adjustments: dict[str, list[dict[str, Any]]] = {}
        self._strategy_metrics: dict[str, dict[str, Any]] = {}
        self._cycle_stats: dict[str, dict[str, Any]] = {}
        self._last_strategy: str | None = None
        self._max_rates: dict[str, float] = {"cooling": 0.0, "heating": 0.0}
        self._max_running_minutes: dict[str, float] = {}
        self._vent_starting_temps: dict[str, float] = {}
        self._vent_starting_open: dict[str, int] = {}
        self._pre_adjust_flags: dict[str, bool] = {}
        self._store = Store(hass, 1, f"{DOMAIN}_{entry.entry_id}_dab.json")
        self._save_lock = asyncio.Lock()
        self._pending_finalize: dict[str, asyncio.Task] = {}
        self._error_counter = 0

        poll_active = entry.options.get(
            CONF_POLL_INTERVAL_ACTIVE, DEFAULT_POLL_INTERVAL_ACTIVE
        )
        poll_idle = entry.options.get(
            CONF_POLL_INTERVAL_IDLE, DEFAULT_POLL_INTERVAL_IDLE
        )
        self._poll_interval_active = timedelta(minutes=poll_active)
        self._poll_interval_idle = timedelta(minutes=poll_idle)
        self._initial_efficiency_percent = float(
            entry.options.get(
                CONF_INITIAL_EFFICIENCY_PERCENT, DEFAULT_INITIAL_EFFICIENCY_PERCENT
            )
        )
        self._notify_efficiency_changes = bool(
            entry.options.get(
                CONF_NOTIFY_EFFICIENCY_CHANGES, DEFAULT_NOTIFY_EFFICIENCY_CHANGES
            )
        )
        self._log_efficiency_changes = bool(
            entry.options.get(
                CONF_LOG_EFFICIENCY_CHANGES, DEFAULT_LOG_EFFICIENCY_CHANGES
            )
        )

        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}-{entry.title}",
            update_interval=self._poll_interval_idle,
        )

    async def async_initialize(self) -> None:
        """Load persisted DAB state."""
        stored = await self._store.async_load()
        if not stored:
            return
        self._vent_rates = stored.get("vent_rates", {})
        self._max_rates = stored.get("max_rates", self._max_rates)
        self._max_running_minutes = stored.get("max_running_minutes", {})
        self._vent_models = stored.get("vent_models", {})
        self._efficiency_models = stored.get("efficiency_models", {})
        self._vent_adjustments = stored.get("vent_adjustments", {})
        self._strategy_metrics = stored.get("strategy_metrics", {})

    def _get_brand(self) -> str:
        return self.entry.options.get(CONF_VENT_BRAND, self.entry.data.get(CONF_VENT_BRAND, BRAND_FLAIR))

    def _is_manual(self) -> bool:
        return self._get_brand() == BRAND_MANUAL

    def is_manual_brand(self) -> bool:
        return self._is_manual()

    def _get_vent_assignments(self) -> dict[str, dict[str, Any]]:
        if self._is_manual():
            assignments: dict[str, dict[str, Any]] = {}
            for vent in self._get_manual_vents():
                vent_id = vent.get("id")
                if not vent_id:
                    continue
                assignment: dict[str, Any] = {}
                thermostat = vent.get(CONF_THERMOSTAT_ENTITY)
                temp_sensor = vent.get(CONF_TEMP_SENSOR_ENTITY)
                if thermostat:
                    assignment[CONF_THERMOSTAT_ENTITY] = thermostat
                if temp_sensor:
                    assignment[CONF_TEMP_SENSOR_ENTITY] = temp_sensor
                assignments[vent_id] = assignment
            return assignments
        return self.entry.options.get(CONF_VENT_ASSIGNMENTS, self.entry.data.get(CONF_VENT_ASSIGNMENTS, {}))

    def _get_manual_vents(self) -> list[dict[str, Any]]:
        return self.entry.options.get(CONF_MANUAL_VENTS, self.entry.data.get(CONF_MANUAL_VENTS, []))

    def get_manual_vents(self) -> list[dict[str, Any]]:
        return self._get_manual_vents()

    def set_manual_aperture(self, vent_id: str, value: int) -> None:
        self._manual_apertures[vent_id] = max(0, min(100, int(value)))

    async def async_ensure_structure_mode(self) -> None:
        """Ensure structure mode is manual when DAB is enabled (optional)."""
        if self._is_manual():
            return
        if not self.entry.options.get(CONF_DAB_ENABLED, False):
            return
        if not self.entry.options.get(CONF_DAB_FORCE_MANUAL, DEFAULT_DAB_FORCE_MANUAL):
            return
        structure_id = self.entry.data.get(CONF_STRUCTURE_ID)
        if not structure_id:
            return
        try:
            if self.api:
                await self.api.async_set_structure_mode(structure_id, "manual")
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Failed to set structure mode to manual: %s", err)

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch data from Flair API."""
        if self._is_manual():
            return await self._async_update_manual_data()

        structure_id = self.entry.data[CONF_STRUCTURE_ID]
        if self.entry.options.get(CONF_DAB_ENABLED, False):
            await self.async_ensure_structure_mode()
        try:
            if not self.api:
                raise UpdateFailed("Flair API client not initialized")
            vents = await self.api.async_get_vents(structure_id)
            pucks = await self.api.async_get_pucks(structure_id)
        except Exception as err:  # noqa: BLE001 - surface errors to HA
            self._async_notify_error("Flair update failed", str(err))
            raise UpdateFailed(f"Error fetching Flair data: {err}") from err

        remote_cache: dict[str, asyncio.Task | Any] = {}
        vents = await self._async_enrich_vents(vents, remote_cache)
        pucks = await self._async_enrich_pucks(pucks, remote_cache)

        data = {
            "vents": {vent["id"]: vent for vent in vents},
            "pucks": {puck["id"]: puck for puck in pucks},
        }

        if self.entry.options.get(CONF_DAB_ENABLED, False):
            try:
                await self._async_process_dab(data)
            except Exception as err:  # noqa: BLE001
                _LOGGER.exception("DAB processing failed: %s", err)
                self._async_notify_error("DAB processing failed", str(err))

        return data

    async def _async_update_manual_data(self) -> dict[str, Any]:
        manual_vents = self._get_manual_vents()
        vents: list[dict[str, Any]] = []
        now = datetime.now(timezone.utc)

        for vent in manual_vents:
            vent_id = vent.get("id")
            name = vent.get("name") or f"Vent {vent_id}"
            temp_sensor = vent.get(CONF_TEMP_SENSOR_ENTITY)
            temp = None
            if temp_sensor:
                state = self.hass.states.get(temp_sensor)
                if state and state.state not in {STATE_UNKNOWN, STATE_UNAVAILABLE}:
                    try:
                        temp = float(state.state)
                    except ValueError:
                        temp = None
                    if temp is not None:
                        unit = state.attributes.get("unit_of_measurement")
                        if is_fahrenheit_unit(unit):
                            temp = (temp - 32) * 5 / 9

            if vent_id is None:
                continue

            aperture = self._manual_apertures.get(vent_id)
            if aperture is None:
                aperture = 50
            self._manual_apertures[vent_id] = int(aperture)
            self._vent_last_reading[vent_id] = now

            room = {
                "id": vent_id,
                "attributes": {
                    "name": name,
                    "current-temperature-c": temp,
                    "active": True,
                },
            }
            vents.append(
                {
                    "id": vent_id,
                    "name": name,
                    "attributes": {"percent-open": aperture},
                    "room": room,
                }
            )

        data = {"vents": {vent["id"]: vent for vent in vents}, "pucks": {}}

        if self.entry.options.get(CONF_DAB_ENABLED, False):
            try:
                await self._async_process_dab(data)
            except Exception as err:  # noqa: BLE001
                _LOGGER.exception("DAB processing failed: %s", err)
                self._async_notify_error("DAB processing failed", str(err))

        return data

    def async_shutdown(self) -> None:
        """Clean up listeners when unloading."""
        for unsub in self._unsub_thermostat_listeners:
            unsub()
        self._unsub_thermostat_listeners.clear()

    async def async_setup_thermostat_listeners(self) -> None:
        """Track thermostat HVAC action changes to adjust polling interval."""
        self.async_shutdown()

        thermostat_entities = self._get_thermostat_entities()
        if not thermostat_entities:
            _LOGGER.debug("No thermostat entities configured for polling control")
            return

        for entity_id in thermostat_entities:
            unsub = async_track_state_change_event(
                self.hass, entity_id, self._handle_thermostat_event
            )
            self._unsub_thermostat_listeners.append(unsub)

        await self._recompute_polling_interval()

    async def _async_enrich_vents(
        self,
        vents: list[dict[str, Any]],
        remote_cache: dict[str, asyncio.Task | Any],
    ) -> list[dict[str, Any]]:
        semaphore = asyncio.Semaphore(6)

        async def enrich(vent: dict[str, Any]) -> dict[str, Any]:
            async with semaphore:
                vent_id = vent["id"]
                try:
                    reading = await self.api.async_get_vent_reading(vent_id)
                    self._vent_last_reading[vent_id] = datetime.now(timezone.utc)
                except Exception as err:  # noqa: BLE001
                    _LOGGER.warning("Failed to fetch vent reading for %s: %s", vent_id, err)
                    reading = {}
                try:
                    room = await self.api.async_get_vent_room(vent_id)
                except Exception as err:  # noqa: BLE001
                    _LOGGER.warning("Failed to fetch vent room for %s: %s", vent_id, err)
                    room = {}
                if room:
                    room = await self._async_enrich_room(room, remote_cache)
                attributes = dict(vent.get("attributes") or {})
                attributes.update(reading)
                vent["attributes"] = attributes
                vent["room"] = room
                return vent

        return await asyncio.gather(*(enrich(vent) for vent in vents))

    async def _async_enrich_pucks(
        self,
        pucks: list[dict[str, Any]],
        remote_cache: dict[str, asyncio.Task | Any],
    ) -> list[dict[str, Any]]:
        semaphore = asyncio.Semaphore(6)

        async def enrich(puck: dict[str, Any]) -> dict[str, Any]:
            async with semaphore:
                puck_id = puck["id"]
                try:
                    reading = await self.api.async_get_puck_reading(puck_id)
                except Exception as err:  # noqa: BLE001
                    _LOGGER.warning("Failed to fetch puck reading for %s: %s", puck_id, err)
                    reading = {}
                try:
                    room = await self.api.async_get_puck_room(puck_id)
                except Exception as err:  # noqa: BLE001
                    _LOGGER.warning("Failed to fetch puck room for %s: %s", puck_id, err)
                    room = {}
                if room:
                    room = await self._async_enrich_room(room, remote_cache)
                attributes = dict(puck.get("attributes") or {})
                attributes.update(reading)
                puck["attributes"] = attributes
                puck["room"] = room
                return puck

        return await asyncio.gather(*(enrich(puck) for puck in pucks))

    async def _async_enrich_room(
        self, room: dict[str, Any], remote_cache: dict[str, asyncio.Task | Any]
    ) -> dict[str, Any]:
        if not room:
            return room
        remote_id = get_remote_sensor_id(room)
        if not remote_id:
            return room

        task = remote_cache.get(remote_id)
        if task is None:
            task = self.hass.async_create_task(self._async_get_remote_occupied(remote_id))
            remote_cache[remote_id] = task

        try:
            occupied = await task
        except Exception:  # noqa: BLE001
            occupied = None

        if occupied is not None:
            room.setdefault("attributes", {})["occupied"] = occupied
            room["remote_sensor_id"] = remote_id
        return room

    async def _async_get_remote_occupied(self, remote_id: str) -> Any:
        try:
            reading = await self.api.async_get_remote_sensor_reading(remote_id)
        except Exception:  # noqa: BLE001
            return None
        return reading.get("occupied")


    def _get_thermostat_entities(self) -> list[str]:
        assignments = self._get_vent_assignments()
        entities = {
            data.get(CONF_THERMOSTAT_ENTITY)
            for data in assignments.values()
            if data.get(CONF_THERMOSTAT_ENTITY)
        }
        return sorted(entities)

    def _resolve_temperature_unit(self, unit: str | None) -> str | None:
        if unit:
            return unit
        return self.hass.config.units.temperature_unit

    def _coerce_temperature(self, value: Any, unit: str | None) -> float | None:
        if value is None:
            return None
        try:
            temp = float(value)
        except (TypeError, ValueError):
            return None
        if is_fahrenheit_unit(self._resolve_temperature_unit(unit)):
            return (temp - 32) * 5 / 9
        return temp

    def _resolve_hvac_action(self, state) -> str | None:
        if not state or state.state in {STATE_UNKNOWN, STATE_UNAVAILABLE}:
            return None

        hvac_action = state.attributes.get("hvac_action")
        if hvac_action in {HVACAction.COOLING, HVACAction.HEATING}:
            return hvac_action

        hvac_mode = state.state or state.attributes.get("hvac_mode")
        unit = self._resolve_temperature_unit(state.attributes.get("temperature_unit"))
        current_temp = self._coerce_temperature(
            state.attributes.get("current_temperature"), unit
        )
        if current_temp is None:
            return None

        target = self._coerce_temperature(state.attributes.get("temperature"), unit)
        target_low = self._coerce_temperature(
            state.attributes.get("target_temp_low")
            or state.attributes.get("heating_setpoint"),
            unit,
        )
        target_high = self._coerce_temperature(
            state.attributes.get("target_temp_high")
            or state.attributes.get("cooling_setpoint"),
            unit,
        )

        hysteresis = DEFAULT_SETTINGS.thermostat_hysteresis

        if hvac_mode == "heat":
            if target is None:
                target = target_low
            if target is None:
                return None
            return HVACAction.HEATING if current_temp <= target - hysteresis else None
        if hvac_mode == "cool":
            if target is None:
                target = target_high
            if target is None:
                return None
            return HVACAction.COOLING if current_temp >= target + hysteresis else None
        if hvac_mode in {"heat_cool", "auto"}:
            if target_low is None or target_high is None:
                return None
            if current_temp <= target_low - hysteresis:
                return HVACAction.HEATING
            if current_temp >= target_high + hysteresis:
                return HVACAction.COOLING
        return None

    def _calculate_temp_error(self, hvac_action: str, setpoint: float, temp: float | None) -> float | None:
        if temp is None:
            return None
        if hvac_action == HVACAction.HEATING:
            return max(0.0, setpoint - temp)
        if hvac_action == HVACAction.COOLING:
            return max(0.0, temp - setpoint)
        return None

    def _calculate_linear_target_percent(
        self, temp: float, setpoint: float, rate: float, target_minutes: float
    ) -> float:
        if rate <= 0 or target_minutes <= 0:
            return 100.0
        diff = abs(setpoint - temp)
        if diff <= 0:
            return 0.0
        target_rate = diff / target_minutes
        percent = (target_rate / rate) * 100
        return max(0.0, min(100.0, percent))

    def _cost_for_target(
        self,
        temp: float,
        setpoint: float,
        rate: float,
        target_minutes: float,
        candidate: float,
        current: float,
    ) -> float:
        if candidate <= 0 or rate <= 0 or target_minutes <= 0:
            time_to_target = float("inf") if abs(setpoint - temp) > 0 else 0.0
        else:
            time_to_target = abs(setpoint - temp) / (rate * (candidate / 100))

        temp_cost = abs(time_to_target - target_minutes) if target_minutes > 0 else 0.0
        move_cost = abs(candidate - current) / 100.0
        open_cost = candidate / 100.0

        temp_weight = 1.0
        open_weight = 0.25
        move_weight = 0.3
        return (temp_weight * temp_cost) + (open_weight * open_cost) + (move_weight * move_cost)

    def _get_model_params(self, vent_id: str, mode: str) -> tuple[float, float] | None:
        stats = (self._vent_models.get(vent_id) or {}).get(mode)
        if not stats:
            return None
        n = stats.get("n", 0)
        if n < 2:
            return None
        sum_x = stats.get("sum_x", 0.0)
        sum_y = stats.get("sum_y", 0.0)
        sum_xx = stats.get("sum_xx", 0.0)
        sum_xy = stats.get("sum_xy", 0.0)
        denom = (n * sum_xx) - (sum_x * sum_x)
        if denom == 0:
            return None
        slope = ((n * sum_xy) - (sum_x * sum_y)) / denom
        intercept = (sum_y - (slope * sum_x)) / n
        return slope, intercept

    def _update_strategy_metrics(
        self,
        strategy: str,
        temp_error: float,
        adjustments: int,
        movement: float,
        active_temp_error: float | None = None,
        active_rooms: int = 0,
    ) -> None:
        metrics = self._strategy_metrics.setdefault(
            strategy,
            {
                "cycles": 0,
                "avg_temp_error": 0.0,
                "avg_adjustments": 0.0,
                "avg_movement": 0.0,
                "last_temp_error": None,
                "last_adjustments": 0,
                "last_movement": 0.0,
                "last_updated": None,
                "active_cycles": 0,
                "avg_active_temp_error": 0.0,
                "last_active_temp_error": None,
                "last_active_rooms": 0,
            },
        )
        cycles = metrics["cycles"] + 1
        metrics["cycles"] = cycles
        metrics["avg_temp_error"] = (
            (metrics["avg_temp_error"] * (cycles - 1) + temp_error) / cycles
        )
        metrics["avg_adjustments"] = (
            (metrics["avg_adjustments"] * (cycles - 1) + adjustments) / cycles
        )
        metrics["avg_movement"] = (
            (metrics["avg_movement"] * (cycles - 1) + movement) / cycles
        )
        metrics["last_temp_error"] = temp_error
        metrics["last_adjustments"] = adjustments
        metrics["last_movement"] = movement
        metrics["last_updated"] = datetime.now(timezone.utc).isoformat()
        if active_temp_error is not None:
            active_cycles = metrics.get("active_cycles", 0) + 1
            metrics["active_cycles"] = active_cycles
            metrics["avg_active_temp_error"] = (
                (metrics.get("avg_active_temp_error", 0.0) * (active_cycles - 1) + active_temp_error)
                / active_cycles
            )
            metrics["last_active_temp_error"] = active_temp_error
            metrics["last_active_rooms"] = active_rooms

    def get_strategy_metrics(self) -> dict[str, Any]:
        return {
            "last_strategy": self._last_strategy,
            "strategies": self._strategy_metrics,
            "vent_brand": self._get_brand(),
            "dab_enabled": self.entry.options.get(CONF_DAB_ENABLED, False),
            "close_inactive_rooms": self.entry.options.get(CONF_CLOSE_INACTIVE_ROOMS, True),
            "min_adjustment_percent": self.entry.options.get(
                CONF_MIN_ADJUSTMENT_PERCENT, DEFAULT_MIN_ADJUSTMENT_PERCENT
            ),
            "min_adjustment_interval": self.entry.options.get(
                CONF_MIN_ADJUSTMENT_INTERVAL, DEFAULT_MIN_ADJUSTMENT_INTERVAL
            ),
            "min_combined_airflow_percent": DEFAULT_SETTINGS.min_combined_vent_flow,
        }

    @callback
    def _handle_thermostat_event(self, event) -> None:
        """Handle thermostat state changes and adjust polling."""
        self.hass.async_create_task(self._recompute_polling_interval())
        self.hass.async_create_task(self._async_handle_pre_adjust(event))

    async def _recompute_polling_interval(self) -> None:
        thermostat_entities = self._get_thermostat_entities()
        if not thermostat_entities:
            self.update_interval = self._poll_interval_idle
            return

        active = False
        for entity_id in thermostat_entities:
            state = self.hass.states.get(entity_id)
            if not state or state.state in {STATE_UNKNOWN, STATE_UNAVAILABLE}:
                continue
            hvac_action = self._resolve_hvac_action(state)
            if hvac_action in {HVACAction.COOLING, HVACAction.HEATING}:
                active = True
                break

        self.update_interval = self._poll_interval_active if active else self._poll_interval_idle

    async def _async_handle_pre_adjust(self, event) -> None:
        if not self.entry.options.get(CONF_DAB_ENABLED, False):
            return
        new_state = event.data.get("new_state")
        if not new_state:
            return
        entity_id = new_state.entity_id

        hvac_action = new_state.attributes.get("hvac_action")
        if hvac_action in {HVACAction.COOLING, HVACAction.HEATING}:
            self._pre_adjust_flags[entity_id] = False
            return

        current_temp = new_state.attributes.get("current_temperature")
        if current_temp is None:
            return
        try:
            current_temp = float(current_temp)
        except (TypeError, ValueError):
            return
        unit = self._resolve_temperature_unit(new_state.attributes.get("temperature_unit"))
        if is_fahrenheit_unit(unit):
            current_temp = (current_temp - 32) * 5 / 9

        hvac_mode = new_state.state
        predicted: str | None = None
        if hvac_mode == "cool":
            predicted = HVACAction.COOLING
        elif hvac_mode == "heat":
            predicted = HVACAction.HEATING
        elif hvac_mode in {"heat_cool", "auto"}:
            cooling = (
                new_state.attributes.get("target_temp_high")
                or new_state.attributes.get("cooling_setpoint")
            )
            heating = (
                new_state.attributes.get("target_temp_low")
                or new_state.attributes.get("heating_setpoint")
            )
            if cooling is None or heating is None:
                _LOGGER.debug(
                    "Skipping pre-adjust for %s; missing target temps in auto/heat_cool",
                    entity_id,
                )
                return
            try:
                cooling = float(cooling)
                heating = float(heating)
            except (TypeError, ValueError):
                _LOGGER.debug(
                    "Skipping pre-adjust for %s; invalid target temps", entity_id
                )
                return
            if is_fahrenheit_unit(unit):
                cooling = (cooling - 32) * 5 / 9
                heating = (heating - 32) * 5 / 9
            predicted = calculate_hvac_mode(current_temp, cooling, heating)

        if predicted is None:
            _LOGGER.debug("Skipping pre-adjust for %s; no predicted HVAC action", entity_id)
            return

        setpoint = self._get_thermostat_setpoint(entity_id, predicted)
        if setpoint is None:
            _LOGGER.debug("Skipping pre-adjust for %s; no setpoint found", entity_id)
            return

        if should_pre_adjust(predicted, setpoint, current_temp):
            if self._pre_adjust_flags.get(entity_id):
                return
            self._pre_adjust_flags[entity_id] = True
            await self._async_pre_adjust(entity_id, predicted)
        else:
            self._pre_adjust_flags[entity_id] = False

    async def _async_pre_adjust(self, thermostat_entity: str, hvac_action: str) -> None:
        if not self.data:
            await self.async_request_refresh()
        if not self.data:
            return

        assignments = self._get_vent_assignments()
        vent_ids = [
            vent_id
            for vent_id, assignment in assignments.items()
            if assignment.get(CONF_THERMOSTAT_ENTITY) == thermostat_entity
            and vent_id in (self.data.get("vents") or {})
        ]
        if not vent_ids:
            return

        await self._async_apply_dab_adjustments(thermostat_entity, hvac_action, vent_ids, self.data)

    async def _async_process_dab(self, data: dict[str, Any]) -> None:
        assignments = self._get_vent_assignments()
        if not assignments:
            return

        vents = data.get("vents", {})
        grouped: dict[str, list[str]] = {}
        for vent_id, assignment in assignments.items():
            thermostat = assignment.get(CONF_THERMOSTAT_ENTITY)
            if thermostat and vent_id in vents:
                grouped.setdefault(thermostat, []).append(vent_id)

        for thermostat_entity, vent_ids in grouped.items():
            await self._async_process_thermostat_group(thermostat_entity, vent_ids, data)

    async def async_run_dab(self, thermostat_entity: str | None = None) -> None:
        """Manually trigger DAB adjustments."""
        if not self.entry.options.get(CONF_DAB_ENABLED, False):
            _LOGGER.info("DAB is disabled; ignoring manual run request")
            return

        if not self.data:
            await self.async_request_refresh()

        assignments = self._get_vent_assignments()
        if not assignments:
            return

        grouped: dict[str, list[str]] = {}
        for vent_id, assignment in assignments.items():
            thermo = assignment.get(CONF_THERMOSTAT_ENTITY)
            if thermo and vent_id in (self.data.get("vents") or {}):
                grouped.setdefault(thermo, []).append(vent_id)

        for thermo, vent_ids in grouped.items():
            if thermostat_entity and thermo != thermostat_entity:
                continue

            state = self.hass.states.get(thermo)
            if not state:
                continue
            hvac_action = self._resolve_hvac_action(state)
            if hvac_action not in {HVACAction.COOLING, HVACAction.HEATING}:
                _LOGGER.debug("Thermostat %s not actively heating/cooling", thermo)
                continue

            await self._async_apply_dab_adjustments(thermo, hvac_action, vent_ids, self.data)

    async def async_set_room_active(self, room_id: str, active: bool) -> None:
        """Set room active state via API and refresh."""
        await self.api.async_set_room_active(room_id, active)
        await self.async_request_refresh()

    def resolve_room_id_from_vent(self, vent_id: str) -> str | None:
        """Resolve a room id for a given vent id."""
        if not self.data:
            return None
        room = self._get_room_data(vent_id, self.data)
        return room.get("id")

    async def _async_process_thermostat_group(
        self, thermostat_entity: str, vent_ids: list[str], data: dict[str, Any]
    ) -> None:
        climate_state = self.hass.states.get(thermostat_entity)
        if not climate_state or climate_state.state in {STATE_UNKNOWN, STATE_UNAVAILABLE}:
            return

        hvac_action = self._resolve_hvac_action(climate_state)
        prev_action = self._last_hvac_action.get(thermostat_entity)

        if hvac_action in {HVACAction.COOLING, HVACAction.HEATING} and prev_action not in {
            HVACAction.COOLING,
            HVACAction.HEATING,
        }:
            self._start_hvac_cycle(thermostat_entity, hvac_action, vent_ids, data)

        if hvac_action not in {HVACAction.COOLING, HVACAction.HEATING} and prev_action in {
            HVACAction.COOLING,
            HVACAction.HEATING,
        }:
            await self._schedule_finalize(thermostat_entity, prev_action, vent_ids)

        self._last_hvac_action[thermostat_entity] = hvac_action

        if hvac_action in {HVACAction.COOLING, HVACAction.HEATING}:
            await self._async_apply_dab_adjustments(
                thermostat_entity, hvac_action, vent_ids, data
            )

    def _start_hvac_cycle(
        self, thermostat_entity: str, hvac_action: str, vent_ids: list[str], data: dict[str, Any]
    ) -> None:
        now = datetime.now(timezone.utc)
        self._dab_state[thermostat_entity] = {
            "mode": hvac_action,
            "started_cycle": now,
            "started_running": now + timedelta(minutes=EFF_ACTION_STABLE_MIN),
            "samples": {},
        }
        self._cycle_stats[thermostat_entity] = {
            "adjustments": 0,
            "movement": 0.0,
            "strategy": self.entry.options.get(
                CONF_CONTROL_STRATEGY, DEFAULT_CONTROL_STRATEGY
            ),
            "vent_movement": {},
        }

        for vent_id in vent_ids:
            temp = self._get_room_temp(vent_id, data)
            if temp is not None:
                self._vent_starting_temps[vent_id] = temp
            self._vent_starting_open[vent_id] = int(
                self._get_vent_attribute(vent_id, data, "percent-open") or 0
            )

    async def _schedule_finalize(
        self, thermostat_entity: str, hvac_action: str, vent_ids: list[str]
    ) -> None:
        if thermostat_entity in self._pending_finalize:
            return

        async def finalize_task() -> None:
            await asyncio.sleep(30)
            await self.async_request_refresh()
            await self._async_finalize_cycle(thermostat_entity, hvac_action, vent_ids)

        task = self.hass.async_create_task(finalize_task())
        self._pending_finalize[thermostat_entity] = task

    async def _async_finalize_cycle(
        self, thermostat_entity: str, hvac_action: str, vent_ids: list[str]
    ) -> None:
        self._pending_finalize.pop(thermostat_entity, None)
        state = self._dab_state.pop(thermostat_entity, None)
        if not state:
            return
        cycle_stats = self._cycle_stats.pop(thermostat_entity, None) or {}

        started_cycle = state.get("started_cycle")
        started_running = state.get("started_running")
        if not started_running:
            return

        finished_running = datetime.now(timezone.utc)
        total_running_minutes = max(
            0.0, (finished_running - started_running).total_seconds() / 60.0
        )
        total_cycle_minutes = max(
            0.0, (finished_running - started_cycle).total_seconds() / 60.0
        )

        prev_max = self._max_running_minutes.get(
            thermostat_entity, DEFAULT_SETTINGS.max_minutes_to_setpoint
        )
        self._max_running_minutes[thermostat_entity] = rolling_average(
            prev_max, total_running_minutes, 1, 6
        )

        rate_prop = "cooling" if hvac_action == HVACAction.COOLING else "heating"
        setpoint_target = self._get_thermostat_target_raw(thermostat_entity, hvac_action)
        room_rates: dict[str, float] = {}
        samples_by_vent: dict[str, list[dict[str, Any]]] = state.get("samples", {})

        for vent_id in vent_ids:
            room_name = self._get_room_name(vent_id, self.data)
            if room_name and room_name in room_rates:
                self._set_vent_rate(vent_id, rate_prop, room_rates[room_name])
                continue

            samples = samples_by_vent.get(vent_id, [])
            efficiency_sample, observed_rate, mean_aperture = self._compute_efficiency_sample(
                hvac_action, started_running, samples, setpoint_target
            )
            if efficiency_sample is None:
                continue

            current_rate = self._vent_rates.get(vent_id, {}).get(rate_prop, 0.0)
            baseline_rate, effective_rate, confidence = self._update_efficiency_model(
                vent_id, rate_prop, efficiency_sample
            )
            cleaned = round_big_decimal(baseline_rate, 6)
            self._set_vent_rate(vent_id, rate_prop, cleaned)
            self._maybe_log_efficiency_change(vent_id, rate_prop, current_rate, cleaned)

            if room_name:
                room_rates[room_name] = cleaned

            if effective_rate > self._max_rates.get(rate_prop, 0):
                self._max_rates[rate_prop] = effective_rate

            if observed_rate is not None and observed_rate > 0 and mean_aperture is not None:
                model = self._vent_models.setdefault(vent_id, {})
                stats = model.setdefault(
                    rate_prop,
                    {"n": 0, "sum_x": 0.0, "sum_y": 0.0, "sum_xx": 0.0, "sum_xy": 0.0},
                )
                stats["n"] += 1
                stats["sum_x"] += mean_aperture
                stats["sum_y"] += observed_rate
                stats["sum_xx"] += mean_aperture * mean_aperture
                stats["sum_xy"] += mean_aperture * observed_rate

        setpoint = setpoint_target or self._get_thermostat_setpoint(
            thermostat_entity, hvac_action
        )
        if setpoint is not None:
            errors: list[float] = []
            active_errors: list[float] = []
            for vent_id in vent_ids:
                temp = self._get_room_temp(vent_id, self.data)
                error = self._calculate_temp_error(hvac_action, setpoint, temp)
                if error is not None:
                    errors.append(error)
                    if self._get_room_active(vent_id, self.data):
                        active_errors.append(error)
            if errors:
                strategy = cycle_stats.get(
                    "strategy",
                    self.entry.options.get(CONF_CONTROL_STRATEGY, DEFAULT_CONTROL_STRATEGY),
                )
                adjustments = int(cycle_stats.get("adjustments", 0) or 0)
                movement = float(cycle_stats.get("movement", 0.0) or 0.0)
                mean_error = sum(errors) / len(errors)
                active_mean = sum(active_errors) / len(active_errors) if active_errors else None
                self._update_strategy_metrics(
                    strategy,
                    mean_error,
                    adjustments,
                    movement,
                    active_mean,
                    len(active_errors),
                )

        await self._async_save_state()

    async def _async_apply_dab_adjustments(
        self, thermostat_entity: str, hvac_action: str, vent_ids: list[str], data: dict[str, Any]
    ) -> None:
        setpoint = self._get_thermostat_setpoint(thermostat_entity, hvac_action)
        if setpoint is None:
            _LOGGER.debug(
                "Skipping DAB for %s; missing setpoint for %s",
                thermostat_entity,
                hvac_action,
            )
            return

        close_inactive = self.entry.options.get(CONF_CLOSE_INACTIVE_ROOMS, True)
        granularity = int(self.entry.options.get(CONF_VENT_GRANULARITY, 5))
        control_strategy = self.entry.options.get(
            CONF_CONTROL_STRATEGY, DEFAULT_CONTROL_STRATEGY
        )
        min_adjust_percent = int(
            self.entry.options.get(CONF_MIN_ADJUSTMENT_PERCENT, DEFAULT_MIN_ADJUSTMENT_PERCENT)
        )
        min_adjust_interval = int(
            self.entry.options.get(CONF_MIN_ADJUSTMENT_INTERVAL, DEFAULT_MIN_ADJUSTMENT_INTERVAL)
        )
        temp_error_override = float(
            self.entry.options.get(CONF_TEMP_ERROR_OVERRIDE, DEFAULT_TEMP_ERROR_OVERRIDE)
        )
        max_running_time = self._max_running_minutes.get(
            thermostat_entity, DEFAULT_SETTINGS.max_minutes_to_setpoint
        )

        rate_and_temp: dict[str, dict[str, Any]] = {}
        missing_temp_vents: set[str] = set()
        for vent_id in vent_ids:
            rate = self._get_effective_efficiency_rate(vent_id, hvac_action)
            temp = self._get_room_temp(vent_id, data)
            if temp is None:
                missing_temp_vents.add(vent_id)
                temp = setpoint
            if temp is None:
                continue
            rate_and_temp[vent_id] = {
                "rate": rate,
                "temp": temp,
                "active": self._get_room_active(vent_id, data),
                "name": self._get_room_name(vent_id, data) or "",
            }

        if not rate_and_temp:
            return

        longest_time = calculate_longest_minutes_to_target(
            rate_and_temp,
            hvac_action,
            setpoint,
            max_running_time,
            True,
            DEFAULT_SETTINGS,
        )
        if longest_time < 0:
            longest_time = max_running_time
        rate_prop = "cooling" if hvac_action == HVACAction.COOLING else "heating"
        dab_targets: dict[str, float] = {}
        cost_targets: dict[str, float] = {}
        stats_targets: dict[str, float] = {}
        if longest_time == 0:
            dab_targets = {vent_id: 100.0 for vent_id in rate_and_temp}
        else:
            dab_targets = calculate_open_percentage_for_all_vents(
                rate_and_temp, hvac_action, setpoint, longest_time, True, DEFAULT_SETTINGS
            )

        for vent_id, state_val in rate_and_temp.items():
            if close_inactive and not state_val.get("active", True):
                cost_targets[vent_id] = 0.0
                continue
            rate = float(state_val.get("rate", 0) or 0)
            temp = float(state_val.get("temp", 0) or 0)
            if rate < DEFAULT_SETTINGS.min_temp_change_rate:
                cost_targets[vent_id] = 100.0
                continue
            cost_targets[vent_id] = self._calculate_linear_target_percent(
                temp, setpoint, rate, longest_time
            )

        for vent_id, state_val in rate_and_temp.items():
            if close_inactive and not state_val.get("active", True):
                stats_targets[vent_id] = 0.0
                continue
            temp = float(state_val.get("temp", 0) or 0)
            diff = abs(setpoint - temp)
            if longest_time <= 0:
                stats_targets[vent_id] = 100.0
                continue
            target_rate = diff / longest_time if longest_time > 0 else 0.0
            params = self._get_model_params(vent_id, rate_prop)
            if params is None:
                stats_targets[vent_id] = cost_targets.get(vent_id, 100.0)
                continue
            slope, intercept = params
            if slope <= 0:
                stats_targets[vent_id] = cost_targets.get(vent_id, 100.0)
                continue
            percent = (target_rate - intercept) / slope
            stats_targets[vent_id] = max(0.0, min(100.0, percent))

        targets: dict[str, float] = {}
        if control_strategy == "dab":
            targets = dict(dab_targets)
        elif control_strategy == "cost":
            targets = dict(cost_targets)
        elif control_strategy == "stats":
            targets = dict(stats_targets)
        else:
            for vent_id, state_val in rate_and_temp.items():
                if close_inactive and not state_val.get("active", True):
                    targets[vent_id] = 0.0
                    continue
                dab_target = dab_targets.get(vent_id, 100.0)
                cost_target = cost_targets.get(vent_id, dab_target)
                stats_target = stats_targets.get(vent_id, cost_target)
                current = self._get_vent_attribute(vent_id, data, "percent-open")
                current = float(current) if current is not None else dab_target
                temp = float(state_val.get("temp", 0) or 0)
                rate = float(state_val.get("rate", 0) or 0)
                dab_cost = self._cost_for_target(
                    temp, setpoint, rate, longest_time, dab_target, current
                )
                cost_cost = self._cost_for_target(
                    temp, setpoint, rate, longest_time, cost_target, current
                )
                stats_cost = self._cost_for_target(
                    temp, setpoint, rate, longest_time, stats_target, current
                )
                best_target = dab_target
                best_cost = dab_cost
                if cost_cost < best_cost:
                    best_cost = cost_cost
                    best_target = cost_target
                if stats_cost < best_cost:
                    best_target = stats_target
                targets[vent_id] = best_target

        for vent_id in missing_temp_vents:
            if rate_and_temp.get(vent_id, {}).get("active", True):
                targets[vent_id] = 100.0

        conventional = self.entry.options.get(CONF_CONVENTIONAL_VENTS_BY_THERMOSTAT, {}).get(
            thermostat_entity, 0
        )
        pre_safety_targets = dict(targets)
        targets = adjust_for_minimum_airflow(
            rate_and_temp,
            hvac_action,
            targets,
            conventional,
            DEFAULT_SETTINGS,
            active_only=True,
            allow_inactive_if_needed=True,
        )

        safety_opened = {
            vent_id
            for vent_id, target in targets.items()
            if target > pre_safety_targets.get(vent_id, 0)
        }
        now = datetime.now(timezone.utc)
        changed = 0
        movement_total = 0.0
        for vent_id, target in targets.items():
            active = rate_and_temp.get(vent_id, {}).get("active", True)
            if not close_inactive and not active:
                current = self._get_vent_attribute(vent_id, data, "percent-open")
                if current is None:
                    continue
                if vent_id in safety_opened:
                    target = max(float(current), target)
                else:
                    target = float(current)
            target_rounded = round_to_nearest_multiple(target, granularity)
            current = self._get_vent_attribute(vent_id, data, "percent-open")
            if current is None:
                continue
            current_int = int(current)
            if current_int == target_rounded:
                continue
            temp = float(rate_and_temp.get(vent_id, {}).get("temp", 0) or 0)
            error = self._calculate_temp_error(hvac_action, setpoint, temp)
            override = error is not None and error >= temp_error_override
            safety_override = vent_id in safety_opened
            if not override and not safety_override:
                if min_adjust_percent > 0 and abs(target_rounded - current_int) < min_adjust_percent:
                    continue
                last_change = self._vent_last_commanded.get(vent_id)
                if last_change and (now - last_change) < timedelta(minutes=min_adjust_interval):
                    continue
            movement_value = abs(target_rounded - current_int)
            if self._is_manual():
                self._vent_last_target[vent_id] = target_rounded
                self._vent_last_commanded[vent_id] = now
            else:
                if self.api:
                    try:
                        await self.api.async_set_vent_position(vent_id, target_rounded)
                    except (asyncio.TimeoutError, aiohttp.ClientError, FlairApiError) as err:
                        _LOGGER.warning(
                            "Failed to set vent %s to %s%%: %s",
                            vent_id,
                            target_rounded,
                            err,
                        )
                        continue
                self._vent_last_commanded[vent_id] = now
                self._vent_last_target[vent_id] = target_rounded

            changed += 1
            movement_total += movement_value
            self._record_vent_adjustment(vent_id, current_int, target_rounded, now)
            vent_movement = self._cycle_stats.get(thermostat_entity, {}).setdefault(
                "vent_movement", {}
            )
            vent_movement[vent_id] = vent_movement.get(vent_id, 0.0) + movement_value

        for vent_id in vent_ids:
            self._record_cycle_sample(thermostat_entity, vent_id, data)

        if thermostat_entity:
            cycle_stats = self._cycle_stats.setdefault(
                thermostat_entity,
                {
                    "adjustments": 0,
                    "movement": 0.0,
                    "strategy": control_strategy,
                    "vent_movement": {},
                },
            )
            cycle_stats["adjustments"] += changed
            cycle_stats["movement"] += movement_total
            cycle_stats["strategy"] = control_strategy
            self._last_strategy = control_strategy

        if changed == 0:
            _LOGGER.debug(
                "DAB targets match current positions for %s; no vent changes applied",
                thermostat_entity,
            )

    def _get_vent_attribute(self, vent_id: str, data: dict[str, Any], attr: str) -> Any:
        vent = (data.get("vents") or {}).get(vent_id, {})
        return (vent.get("attributes") or {}).get(attr)

    def _get_room_data(self, vent_id: str, data: dict[str, Any]) -> dict[str, Any]:
        vent = (data.get("vents") or {}).get(vent_id, {})
        return vent.get("room") or {}

    def _get_room_name(self, vent_id: str, data: dict[str, Any]) -> str | None:
        room = self._get_room_data(vent_id, data)
        return (room.get("attributes") or {}).get("name")

    def _get_room_active(self, vent_id: str, data: dict[str, Any]) -> bool:
        room = self._get_room_data(vent_id, data)
        active = (room.get("attributes") or {}).get("active")
        if isinstance(active, str):
            return active.lower() == "true"
        return bool(active) if active is not None else True

    def _get_room_temp(self, vent_id: str, data: dict[str, Any]) -> float | None:
        assignment = self._get_vent_assignments().get(vent_id, {})
        temp_sensor = assignment.get(CONF_TEMP_SENSOR_ENTITY)
        if temp_sensor:
            sensor_state = self.hass.states.get(temp_sensor)
            if sensor_state and sensor_state.state not in {STATE_UNKNOWN, STATE_UNAVAILABLE}:
                try:
                    temp = float(sensor_state.state)
                except ValueError:
                    temp = None
                if temp is not None:
                    unit = sensor_state.attributes.get("unit_of_measurement")
                    if is_fahrenheit_unit(unit):
                        return (temp - 32) * 5 / 9
                    return temp

        room = self._get_room_data(vent_id, data)
        temp = (room.get("attributes") or {}).get("current-temperature-c")
        return float(temp) if temp is not None else None

    def _get_vent_duct_temp(self, vent_id: str, data: dict[str, Any]) -> float | None:
        vent = (data.get("vents") or {}).get(vent_id, {})
        attrs = vent.get("attributes") or {}
        temp = attrs.get("duct-temperature-c")
        if temp is not None:
            return self._coerce_temperature(temp, "C")
        temp = attrs.get("duct-temperature-f")
        if temp is not None:
            return self._coerce_temperature(temp, "F")
        return None

    def _record_cycle_sample(self, thermostat_entity: str, vent_id: str, data: dict[str, Any]) -> None:
        dab_state = getattr(self, "_dab_state", {})
        state = dab_state.get(thermostat_entity)
        if not state:
            return
        samples_by_vent = state.setdefault("samples", {})
        samples = samples_by_vent.setdefault(vent_id, [])
        temp = self._get_room_temp(vent_id, data)
        aperture = self._get_vent_attribute(vent_id, data, "percent-open")
        if temp is None or aperture is None:
            return
        duct_temp = self._get_vent_duct_temp(vent_id, data)
        samples.append(
            {
                "t": datetime.now(timezone.utc),
                "temp": float(temp),
                "aperture": float(aperture),
                "duct": duct_temp,
            }
        )
        if len(samples) > 240:
            del samples[: len(samples) - 240]

    def _filter_samples_window(
        self, started_running: datetime, samples: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        if not samples:
            return []
        window_start = started_running + timedelta(minutes=EFF_WARMUP_MIN)
        window_end = started_running + timedelta(minutes=EFF_MAX_WINDOW_MIN)
        windowed = [
            sample
            for sample in samples
            if sample["t"] >= window_start and sample["t"] <= window_end
        ]
        return windowed

    def _truncate_samples_to_setpoint(
        self,
        hvac_action: str,
        samples: list[dict[str, Any]],
        setpoint_target: float | None,
    ) -> list[dict[str, Any]]:
        if not samples or setpoint_target is None:
            return samples
        if hvac_action == HVACAction.HEATING:
            for idx, sample in enumerate(samples):
                if float(sample["temp"]) >= setpoint_target:
                    return samples[: idx + 1]
        elif hvac_action == HVACAction.COOLING:
            for idx, sample in enumerate(samples):
                if float(sample["temp"]) <= setpoint_target:
                    return samples[: idx + 1]
        return samples

    def _robust_slope(self, samples: list[dict[str, Any]]) -> float | None:
        if len(samples) < 2:
            return None
        temps = [float(sample["temp"]) for sample in samples]
        temps_sorted = sorted(temps)
        mid = len(temps_sorted) // 2
        median = temps_sorted[mid] if len(temps_sorted) % 2 else (temps_sorted[mid - 1] + temps_sorted[mid]) / 2
        deviations = [abs(temp - median) for temp in temps]
        deviations_sorted = sorted(deviations)
        mad_mid = len(deviations_sorted) // 2
        mad = deviations_sorted[mad_mid] if len(deviations_sorted) % 2 else (
            deviations_sorted[mad_mid - 1] + deviations_sorted[mad_mid]
        ) / 2
        filtered = samples
        if mad > 0:
            threshold = 3 * mad
            filtered = [s for s in samples if abs(float(s["temp"]) - median) <= threshold]
        if len(filtered) < 2:
            return None
        t0 = filtered[0]["t"]
        xs = [(s["t"] - t0).total_seconds() / 60.0 for s in filtered]
        ys = [float(s["temp"]) for s in filtered]
        n = len(xs)
        sum_x = sum(xs)
        sum_y = sum(ys)
        sum_xx = sum(x * x for x in xs)
        sum_xy = sum(x * y for x, y in zip(xs, ys))
        denom = (n * sum_xx) - (sum_x * sum_x)
        if denom == 0:
            return None
        return ((n * sum_xy) - (sum_x * sum_y)) / denom

    def _mean_aperture_from_samples(self, samples: list[dict[str, Any]]) -> float | None:
        if not samples:
            return None
        apertures = [float(sample["aperture"]) for sample in samples]
        return sum(apertures) / len(apertures) if apertures else None

    def _compute_efficiency_sample(
        self,
        hvac_action: str,
        started_running: datetime,
        samples: list[dict[str, Any]],
        setpoint_target: float | None = None,
    ) -> tuple[float | None, float | None, float | None]:
        windowed = self._filter_samples_window(started_running, samples)
        windowed = self._truncate_samples_to_setpoint(hvac_action, windowed, setpoint_target)
        if len(windowed) < 2:
            return None, None, None
        duration = (windowed[-1]["t"] - windowed[0]["t"]).total_seconds() / 60.0
        if duration < EFF_MIN_WINDOW_MIN:
            return None, None, None
        delta_temp = abs(float(windowed[-1]["temp"]) - float(windowed[0]["temp"]))
        if delta_temp < EFF_MIN_DELTA_C:
            return None, None, None
        apertures = [float(sample["aperture"]) for sample in windowed]
        if not apertures:
            return None, None, None
        mean_aperture = sum(apertures) / len(apertures)
        if mean_aperture < EFF_MIN_APERTURE_PCT:
            return None, None, None
        if max(apertures) - min(apertures) > EFF_APERTURE_JITTER_PCT:
            return None, None, None

        rate_room = self._robust_slope(windowed)
        if rate_room is None:
            return None, None, None

        duct_values = [sample.get("duct") for sample in windowed if sample.get("duct") is not None]
        rate_norm = rate_room
        if duct_values:
            mean_duct = sum(float(v) for v in duct_values) / len(duct_values)
            variance = sum((float(v) - mean_duct) ** 2 for v in duct_values) / len(duct_values)
            stability = variance ** 0.5
            if stability <= EFF_DUCT_STABILITY_C:
                deltas = [
                    abs(float(sample["duct"]) - float(sample["temp"]))
                    for sample in windowed
                    if sample.get("duct") is not None
                ]
                mean_room_delta = sum(deltas) / len(deltas) if deltas else 0.0
                if mean_room_delta >= EFF_MIN_DUCT_DELTA_C:
                    rate_norm = rate_room / mean_room_delta

        rate_eff = rate_norm / (mean_aperture / 100.0)
        if hvac_action == HVACAction.HEATING:
            efficiency = max(0.0, rate_eff)
        else:
            efficiency = max(0.0, -rate_eff)
        if efficiency <= 0:
            return None, None, None
        observed_rate = abs(rate_room)
        return efficiency, observed_rate, mean_aperture

    def _update_efficiency_model(
        self, vent_id: str, mode: str, sample: float
    ) -> tuple[float, float, float]:
        vent_model = self._efficiency_models.setdefault(vent_id, {})
        model = vent_model.setdefault(
            mode,
            {
                "baseline": None,
                "offsets": [0.0 for _ in range(EFF_REGIME_COUNT)],
                "n": 0,
                "last_sample": None,
                "confidence": 0.0,
                "effective": None,
            },
        )
        baseline = model.get("baseline")
        if baseline is None:
            baseline = sample
        n = int(model.get("n", 0)) + 1
        alpha = max(EFF_ALPHA_MIN, EFF_ALPHA0 / (n ** 0.5))
        baseline = baseline + alpha * (sample - baseline)

        offsets = list(model.get("offsets") or [0.0 for _ in range(EFF_REGIME_COUNT)])
        sigma = max(EFF_SIGMA_MIN, EFF_SIGMA_REL * max(baseline, 0.001))
        weights: list[float] = []
        for offset in offsets:
            predict = baseline + offset
            error = abs(sample - predict)
            weights.append(math.exp(-error / sigma))
        total = sum(weights) or 1.0
        weights = [w / total for w in weights]

        for idx, offset in enumerate(offsets):
            predict = baseline + offset
            offsets[idx] = offset + (EFF_BETA * weights[idx] * (sample - predict))
            offsets[idx] *= (1.0 - EFF_SHRINKAGE)

        best_idx = max(range(len(weights)), key=lambda i: weights[i])
        confidence = weights[best_idx] if weights else 0.0
        effective = baseline + offsets[best_idx] if confidence >= EFF_REGIME_CONFIDENCE else baseline

        model.update(
            {
                "baseline": baseline,
                "offsets": offsets,
                "n": n,
                "last_sample": sample,
                "confidence": confidence,
                "effective": effective,
            }
        )
        vent_model[mode] = model
        return float(baseline), float(effective), float(confidence)

    def _get_effective_efficiency_rate(self, vent_id: str, hvac_action: str) -> float:
        mode = "cooling" if hvac_action == HVACAction.COOLING else "heating"
        efficiency_models = getattr(self, "_efficiency_models", {})
        model = (efficiency_models.get(vent_id) or {}).get(mode)
        if model:
            effective = model.get("effective")
            if isinstance(effective, (int, float)) and effective > 0:
                return float(effective)
            baseline = model.get("baseline")
            if isinstance(baseline, (int, float)) and baseline > 0:
                return float(baseline)
        rate = self._vent_rates.get(vent_id, {}).get(mode, 0.0)
        if rate <= 0:
            rate = self._ensure_initial_rate(vent_id, hvac_action)
        return rate

    def _get_thermostat_setpoint(self, thermostat_entity: str, hvac_action: str) -> float | None:
        state = self.hass.states.get(thermostat_entity)
        if not state:
            return None
        attrs = state.attributes
        cool = attrs.get("target_temp_high") or attrs.get("cooling_setpoint")
        heat = attrs.get("target_temp_low") or attrs.get("heating_setpoint")
        target = attrs.get("temperature")

        if hvac_action == HVACAction.COOLING:
            setpoint = cool if cool is not None else target
            offset = -DEFAULT_SETTINGS.setpoint_offset
        else:
            setpoint = heat if heat is not None else target
            offset = DEFAULT_SETTINGS.setpoint_offset

        if setpoint is None:
            return None

        unit = self._resolve_temperature_unit(attrs.get("temperature_unit"))
        try:
            setpoint = float(setpoint)
        except ValueError:
            return None
        if is_fahrenheit_unit(unit):
            setpoint = (setpoint - 32) * 5 / 9

        return setpoint + offset

    def _get_thermostat_target_raw(self, thermostat_entity: str, hvac_action: str) -> float | None:
        state = self.hass.states.get(thermostat_entity)
        if not state:
            return None
        attrs = state.attributes
        if hvac_action == HVACAction.COOLING:
            target = attrs.get("target_temp_high") or attrs.get("cooling_setpoint") or attrs.get("temperature")
        else:
            target = attrs.get("target_temp_low") or attrs.get("heating_setpoint") or attrs.get("temperature")
        if target is None:
            return None
        unit = self._resolve_temperature_unit(attrs.get("temperature_unit"))
        try:
            target = float(target)
        except (TypeError, ValueError):
            return None
        if is_fahrenheit_unit(unit):
            target = (target - 32) * 5 / 9
        return target

    def _set_vent_rate(self, vent_id: str, rate_type: str, value: float) -> None:
        self._vent_rates.setdefault(vent_id, {})[rate_type] = value

    def get_vent_efficiency_percent(self, vent_id: str, mode: str) -> float | None:
        efficiency_models = getattr(self, "_efficiency_models", {})
        model = (efficiency_models.get(vent_id) or {}).get(mode)
        if model and isinstance(model.get("baseline"), (int, float)):
            rate = float(model.get("baseline") or 0.0)
        else:
            rate = self._vent_rates.get(vent_id, {}).get(mode, 0.0)
        if rate <= 0:
            return round(self._clamp_efficiency_percent(self._initial_efficiency_percent), 1)
        percent = max(0.0, min(100.0, rate * 100))
        return round(percent, 1)

    def build_efficiency_export(self) -> dict[str, Any]:
        """Build a Hubitat-compatible efficiency export payload."""
        export_date = (
            datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        )
        structure_id = self.entry.data.get(CONF_STRUCTURE_ID)
        room_efficiencies: list[dict[str, Any]] = []

        for vent_id, rates in self._vent_rates.items():
            room = self.get_room_for_vent(vent_id) if self.data else {}
            room_id = room.get("id")
            room_name = (room.get("attributes") or {}).get("name")
            room_efficiencies.append(
                {
                    "roomId": room_id,
                    "roomName": room_name,
                    "ventId": vent_id,
                    "coolingRate": float(rates.get("cooling", 0.0)),
                    "heatingRate": float(rates.get("heating", 0.0)),
                }
            )

        return {
            "exportMetadata": {
                "version": "ha-1",
                "exportDate": export_date,
                "structureId": structure_id,
            },
            "efficiencyData": {
                "globalRates": {
                    "maxCoolingRate": float(self._max_rates.get("cooling", 0.0)),
                    "maxHeatingRate": float(self._max_rates.get("heating", 0.0)),
                },
                "roomEfficiencies": room_efficiencies,
            },
            "efficiencyModels": getattr(self, "_efficiency_models", {}),
        }

    async def async_import_efficiency(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Import Hubitat efficiency export data."""
        if not isinstance(payload, dict):
            raise ValueError("Efficiency payload must be a JSON object")

        data = payload.get("efficiencyData") or payload
        if not isinstance(data, dict):
            raise ValueError("Missing efficiencyData section")

        models = payload.get("efficiencyModels")
        if isinstance(models, dict):
            self._efficiency_models = models

        entries = data.get("roomEfficiencies") or []
        if not isinstance(entries, list):
            raise ValueError("roomEfficiencies must be a list")

        global_rates = data.get("globalRates") or {}
        if isinstance(global_rates, dict):
            cooling_rate = _coerce_rate(global_rates.get("maxCoolingRate"))
            heating_rate = _coerce_rate(global_rates.get("maxHeatingRate"))
            if cooling_rate is not None:
                self._max_rates["cooling"] = cooling_rate
            if heating_rate is not None:
                self._max_rates["heating"] = heating_rate

        if not self.data:
            await self.async_request_refresh()

        vents = (self.data or {}).get("vents", {})
        room_by_id: dict[str, list[str]] = {}
        room_by_name: dict[str, list[str]] = {}
        for vent_id, vent in vents.items():
            room = vent.get("room") or {}
            room_id = room.get("id")
            room_name = (room.get("attributes") or {}).get("name")
            if room_id:
                room_by_id.setdefault(str(room_id), []).append(vent_id)
            if room_name:
                room_by_name.setdefault(str(room_name).lower(), []).append(vent_id)

        applied = 0
        unmatched = 0
        used_vents: set[str] = set()

        for entry in entries:
            if not isinstance(entry, dict):
                unmatched += 1
                continue

            vent_id = entry.get("ventId")
            room_id = entry.get("roomId")
            room_name = entry.get("roomName")

            target_vent: str | None = None
            if vent_id and str(vent_id) in vents:
                target_vent = str(vent_id)
            else:
                candidates = []
                if room_id is not None:
                    candidates = room_by_id.get(str(room_id), [])
                if not candidates and room_name:
                    candidates = room_by_name.get(str(room_name).lower(), [])

                for candidate in candidates:
                    if candidate not in used_vents:
                        target_vent = candidate
                        break
                if target_vent is None and candidates:
                    target_vent = candidates[0]

            if not target_vent:
                unmatched += 1
                continue

            cooling_rate = _coerce_rate(entry.get("coolingRate"))
            heating_rate = _coerce_rate(entry.get("heatingRate"))
            if cooling_rate is None and heating_rate is None:
                unmatched += 1
                continue

            rates = self._vent_rates.setdefault(target_vent, {})
            if cooling_rate is not None:
                rates["cooling"] = cooling_rate
            if heating_rate is not None:
                rates["heating"] = heating_rate
            used_vents.add(target_vent)
            applied += 1

        await self._async_save_state()
        if self.data is not None:
            self.async_set_updated_data(self.data)

        return {"entries": len(entries), "applied": applied, "unmatched": unmatched}


    def get_room_for_vent(self, vent_id: str) -> dict[str, Any]:
        vent = (self.data or {}).get("vents", {}).get(vent_id, {})
        return vent.get("room") or {}

    def get_room_for_puck(self, puck_id: str) -> dict[str, Any]:
        puck = (self.data or {}).get("pucks", {}).get(puck_id, {})
        return puck.get("room") or {}

    def get_vent_last_reading(self, vent_id: str) -> datetime | None:
        return self._vent_last_reading.get(vent_id)

    def get_vent_target(self, vent_id: str) -> int | None:
        return self._vent_last_target.get(vent_id)

    def _prune_adjustments(
        self, vent_id: str, now: datetime | None = None, window_hours: float = 48.0
    ) -> None:
        events = self._vent_adjustments.get(vent_id)
        if not events:
            return
        if now is None:
            now = datetime.now(timezone.utc)
        cutoff = now - timedelta(hours=window_hours)
        pruned: list[dict[str, Any]] = []
        for event in events:
            ts = event.get("t")
            if isinstance(ts, datetime):
                ts_dt = ts
            elif isinstance(ts, str):
                try:
                    ts_dt = datetime.fromisoformat(ts)
                except ValueError:
                    continue
            else:
                continue
            if ts_dt >= cutoff:
                pruned.append(event)
        self._vent_adjustments[vent_id] = pruned

    def _record_vent_adjustment(
        self, vent_id: str, previous: int, target: int, when: datetime
    ) -> None:
        if not hasattr(self, "_vent_adjustments") or self._vent_adjustments is None:
            self._vent_adjustments = {}
        delta = abs(int(target) - int(previous))
        if delta <= 0:
            return
        events = self._vent_adjustments.setdefault(vent_id, [])
        events.append(
            {
                "t": when.isoformat(),
                "from": int(previous),
                "to": int(target),
                "delta": float(delta),
            }
        )
        self._prune_adjustments(vent_id, now=when)

    def get_vent_adjustment_stats(
        self, vent_id: str, window_hours: float = 24.0
    ) -> dict[str, float]:
        if not hasattr(self, "_vent_adjustments") or self._vent_adjustments is None:
            self._vent_adjustments = {}
        now = datetime.now(timezone.utc)
        self._prune_adjustments(vent_id, now=now, window_hours=max(window_hours, 1.0))
        events = self._vent_adjustments.get(vent_id, [])
        if not events:
            return {"count": 0.0, "movement": 0.0}
        cutoff = now - timedelta(hours=window_hours)
        count = 0
        movement = 0.0
        for event in events:
            ts = event.get("t")
            if isinstance(ts, datetime):
                ts_dt = ts
            elif isinstance(ts, str):
                try:
                    ts_dt = datetime.fromisoformat(ts)
                except ValueError:
                    continue
            else:
                continue
            if ts_dt < cutoff:
                continue
            count += 1
            movement += float(event.get("delta", 0.0) or 0.0)
        return {"count": float(count), "movement": movement}

    def get_room_device_info(self, room: dict[str, Any]) -> dict[str, Any] | None:
        room_id = room.get("id")
        if not room_id:
            return None
        attrs = room.get("attributes") or {}
        name = attrs.get("name") or f"Room {room_id}"
        manufacturer = "Manual" if self._is_manual() else "Flair"
        return {
            "identifiers": {(DOMAIN, f"room_{room_id}")},
            "name": name,
            "manufacturer": manufacturer,
            "model": "Room",
        }

    def get_room_device_info_for_vent(self, vent_id: str) -> dict[str, Any] | None:
        return self.get_room_device_info(self.get_room_for_vent(vent_id))

    def get_room_device_info_for_puck(self, puck_id: str) -> dict[str, Any] | None:
        return self.get_room_device_info(self.get_room_for_puck(puck_id))

    def get_room_by_id(self, room_id: str) -> dict[str, Any]:
        if not self.data:
            return {}
        for vent in self.data.get("vents", {}).values():
            room = vent.get("room") or {}
            if room.get("id") == room_id:
                return room
        for puck in self.data.get("pucks", {}).values():
            room = puck.get("room") or {}
            if room.get("id") == room_id:
                return room
        return {}

    def get_room_temperature(self, room_id: str) -> float | None:
        room = self.get_room_by_id(room_id)
        if not room:
            return None

        # Prefer assigned temp sensor for any vent in this room.
        assignments = self._get_vent_assignments()
        for vent_id, vent in (self.data or {}).get("vents", {}).items():
            if (vent.get("room") or {}).get("id") != room_id:
                continue
            assignment = assignments.get(vent_id, {})
            temp_sensor = assignment.get(CONF_TEMP_SENSOR_ENTITY)
            if temp_sensor:
                state = self.hass.states.get(temp_sensor)
                if state and state.state not in {STATE_UNKNOWN, STATE_UNAVAILABLE}:
                    try:
                        temp = float(state.state)
                    except ValueError:
                        temp = None
                    if temp is not None:
                        unit = state.attributes.get("unit_of_measurement")
                        if is_fahrenheit_unit(unit):
                            return (temp - 32) * 5 / 9
                        return temp

        temp = (room.get("attributes") or {}).get("current-temperature-c")
        return float(temp) if temp is not None else None

    def get_room_thermostat(self, room_id: str) -> str | None:
        assignments = self._get_vent_assignments()
        thermostats: set[str] = set()
        for vent_id, vent in (self.data or {}).get("vents", {}).items():
            if (vent.get("room") or {}).get("id") != room_id:
                continue
            thermostat = assignments.get(vent_id, {}).get(CONF_THERMOSTAT_ENTITY)
            if thermostat:
                thermostats.add(thermostat)
        if not thermostats:
            return None
        return sorted(thermostats)[0]

    def _async_notify_error(self, title: str, message: str) -> None:
        self._error_counter += 1
        notification_id = f"{DOMAIN}_{self.entry.entry_id}_error_{self._error_counter}"
        persistent_notification.async_create(
            self.hass,
            message,
            title=title,
            notification_id=notification_id,
        )

    def _ensure_initial_rate(self, vent_id: str, hvac_action: str) -> float:
        rate_prop = "cooling" if hvac_action == HVACAction.COOLING else "heating"
        initial_rate = self._initial_rate()
        if initial_rate <= 0:
            return 0.0
        self._vent_rates.setdefault(vent_id, {})[rate_prop] = initial_rate
        return initial_rate

    def _initial_rate(self) -> float:
        percent = self._clamp_efficiency_percent(self._initial_efficiency_percent)
        return percent / 100.0

    @staticmethod
    def _clamp_efficiency_percent(value: float) -> float:
        try:
            value = float(value)
        except (TypeError, ValueError):
            return 0.0
        return max(0.0, min(100.0, value))

    def _maybe_log_efficiency_change(
        self, vent_id: str, rate_prop: str, old_rate: float, new_rate: float
    ) -> None:
        if not (self._notify_efficiency_changes or self._log_efficiency_changes):
            return

        old_percent = old_rate * 100
        new_percent = new_rate * 100
        if abs(new_percent - old_percent) < 1.0:
            return

        room_name = self._get_room_name(vent_id, self.data) or "Unknown Room"
        message = (
            f"{room_name} {rate_prop} efficiency adjusted: "
            f"{old_percent:.1f}% → {new_percent:.1f}%"
        )

        if self._notify_efficiency_changes:
            persistent_notification.async_create(
                self.hass,
                message,
                title="HVAC Vent Optimizer",
            )

        if self._log_efficiency_changes:
            logbook.async_log_entry(
                self.hass,
                "HVAC Vent Optimizer",
                message,
                domain=DOMAIN,
            )

    async def _async_save_state(self) -> None:
        async with self._save_lock:
            await self._store.async_save(
                {
                    "vent_rates": self._vent_rates,
                    "max_rates": self._max_rates,
                    "max_running_minutes": self._max_running_minutes,
                    "efficiency_models": self._efficiency_models,
                    "vent_adjustments": self._vent_adjustments,
                    "strategy_metrics": self._strategy_metrics,
                }
            )


def _coerce_rate(value: Any) -> float | None:
    if value is None:
        return None
    try:
        rate = float(value)
    except (TypeError, ValueError):
        return None
    if rate < 0:
        return None
    return rate
