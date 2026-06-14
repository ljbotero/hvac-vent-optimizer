"""Coordinator for HVAC Vent Optimizer."""

from __future__ import annotations

import asyncio
import logging
import math
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import aiohttp
from homeassistant.components import logbook, persistent_notification
from homeassistant.components.climate.const import HVACAction
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import FlairApi, FlairApiError
from .balance import (
    MODE_COOLING,
    MODE_HEATING,
    AllocSettings,
    RoomAllocInput,
    allocate,
    apply_safety_floor,
    predicted_spread,
)
from .const import (
    BRAND_FLAIR,
    BRAND_MANUAL,
    CONF_ADJUSTMENT_WINDOW_MINUTES,
    CONF_AIRFLOW_LIMITED_ERROR_C,
    CONF_AIRFLOW_LIMITED_MARGIN_PCT,
    CONF_CLOSE_INACTIVE_ROOMS,
    CONF_CONTROL_STRATEGY,
    CONF_CONVENTIONAL_VENTS_BY_THERMOSTAT,
    CONF_CROSSCOUPLING_ENABLED,
    CONF_DAB_ENABLED,
    CONF_DAB_FORCE_MANUAL,
    CONF_DEADBAND_PERCENT,
    CONF_DEVIATION_THRESHOLD,
    CONF_DOOR_SENSOR_ENTITY,
    CONF_INITIAL_EFFICIENCY_PERCENT,
    CONF_LOG_EFFICIENCY_CHANGES,
    CONF_MANUAL_VENTS,
    CONF_MAX_ADJUSTMENT_BATCHES_PER_CYCLE,
    CONF_MAX_ADJUSTMENT_BATCHES_PER_WINDOW,
    CONF_MAX_RECALC_PER_CYCLE,
    CONF_MIN_ADJUSTMENT_INTERVAL,
    CONF_MIN_ADJUSTMENT_PERCENT,
    CONF_NOTIFY_EFFICIENCY_CHANGES,
    CONF_OUTDOOR_TEMP_ENTITY,
    CONF_POLL_INTERVAL_ACTIVE,
    CONF_POLL_INTERVAL_IDLE,
    CONF_SAFETY_FLOOR_PCT,
    CONF_SHORT_CYCLE_GAP_MIN,
    CONF_SPREAD_GUARDRAIL_C,
    CONF_SPREAD_IMPROVEMENT_DEADBAND_C,
    CONF_STRUCTURE_ID,
    CONF_TEMP_ERROR_OVERRIDE,
    CONF_TEMP_SENSOR_ENTITY,
    CONF_THERMOSTAT_ENTITY,
    CONF_VENT_ASSIGNMENTS,
    CONF_VENT_BRAND,
    CONF_VENT_GRANULARITY,
    CONTROL_STRATEGY_BALANCE,
    DEFAULT_ADJUSTMENT_WINDOW_MINUTES,
    DEFAULT_AIRFLOW_LIMITED_ERROR_C,
    DEFAULT_AIRFLOW_LIMITED_MARGIN_PCT,
    DEFAULT_CONTROL_STRATEGY,
    DEFAULT_CROSSCOUPLING_ENABLED,
    DEFAULT_DAB_FORCE_MANUAL,
    DEFAULT_DEADBAND_PERCENT,
    DEFAULT_DEVIATION_THRESHOLD,
    DEFAULT_INITIAL_EFFICIENCY_PERCENT,
    DEFAULT_LOG_EFFICIENCY_CHANGES,
    DEFAULT_MAX_ADJUSTMENT_BATCHES_PER_CYCLE,
    DEFAULT_MAX_ADJUSTMENT_BATCHES_PER_WINDOW,
    DEFAULT_MAX_RECALC_PER_CYCLE,
    DEFAULT_MIN_ADJUSTMENT_INTERVAL,
    DEFAULT_MIN_ADJUSTMENT_PERCENT,
    DEFAULT_NOTIFY_EFFICIENCY_CHANGES,
    DEFAULT_POLL_INTERVAL_ACTIVE,
    DEFAULT_POLL_INTERVAL_IDLE,
    DEFAULT_SAFETY_FLOOR_PCT,
    DEFAULT_SHORT_CYCLE_GAP_MIN,
    DEFAULT_SPREAD_GUARDRAIL_C,
    DEFAULT_SPREAD_IMPROVEMENT_DEADBAND_C,
    DEFAULT_TEMP_ERROR_OVERRIDE,
    DOMAIN,
)
from .context import (
    Context,
    apply_context_multipliers,
    build as build_context,
    regime_index as context_regime_index,
)
from .dab import (
    DEFAULT_SETTINGS,
    adjust_for_minimum_airflow,
    calculate_hvac_mode,
    calculate_longest_minutes_to_target,
    calculate_open_percentage_for_all_vents,
    has_room_reached_setpoint,
    rolling_average,
    round_big_decimal,
    round_to_nearest_multiple,
    should_pre_adjust,
)
from .learning import (
    DOOR_MIN_N,
    LEAK_DEFAULT,
    DoorFactorModel,
    RoomEfficiencyModel,
    VentCurve,
    derive_effectiveness,
    door_factor_from_dict,
    door_factor_to_dict,
    effective_rate as learning_effective_rate,
    new_door_factor_model,
    new_room_model,
    resolve_door_factor,
    room_model_from_dict,
    room_model_to_dict,
    seed_room_model_from_v1,
    seed_vent_effectiveness,
    update_door_factor,
    update_room_efficiency,
)
from .utils import get_remote_sensor_id, is_fahrenheit_unit

_LOGGER = logging.getLogger(__name__)


def _slugify(text: str) -> str:
    """Lowercase ``text`` and collapse non-alphanumeric runs into underscores.

    Used to derive a stable persistent-notification id from an error title so
    repeated errors of the same class coalesce (R14.5).
    """
    out: list[str] = []
    prev_us = False
    for ch in text.lower():
        if ch.isalnum():
            out.append(ch)
            prev_us = False
        elif not prev_us:
            out.append("_")
            prev_us = True
    return "".join(out).strip("_") or "error"


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
EFF_REGIME_CONFIDENCE = 0.50
EFF_SIGMA_MIN = 0.05
EFF_SIGMA_REL = 0.25
EFF_REGIME_COUNT = 4

# Persisted DAB-state schema version (Store file ``{DOMAIN}_<entry>_dab.json``).
# v1 (or a missing key) is migrated to v2 in :meth:`async_initialize` (R18.3/R25.7):
# v2 adds the ``room_efficiency`` + ``vent_effectiveness`` sections and the new
# per-strategy spread metric fields. The migrator is idempotent and never discards
# data on a parse failure.
STORE_SCHEMA_VERSION = 2

# New per-strategy spread metric fields backfilled with defaults on migration
# (R13.4/R13.5). Existing metric values are preserved untouched.
_NEW_METRIC_DEFAULTS: dict[str, float] = {
    "avg_spread": 0.0,
    "max_spread": 0.0,
    "time_above_guardrail_min": 0.0,
}


@dataclass(frozen=True)
class EfficiencyContext:
    """Operating context for efficiency learning.

    R20.8/R12/D9: the standalone *door regime* was removed. Door state is folded
    into the learned rate as a bounded multiplier in :mod:`context` rather than
    consuming its own regime cell, so this legacy context is now driven purely by
    occupancy and time-of-day.
    """

    occupied: bool = False
    time_bucket: int = 0  # 0=night(22-6), 1=morning(6-12), 2=afternoon(12-18), 3=evening(18-22)

    def regime_index(self, regime_count: int) -> int:
        """Map context to a preferred regime index (occupancy/time only)."""
        if regime_count <= 2:
            return 1 if self.occupied else 0
        # 4-cell layout: 0=default, 1=occupied, 3=night (index 2 is no longer
        # selected now that the door regime is gone).
        if self.time_bucket == 0:
            return min(3, regime_count - 1)
        if self.occupied:
            return min(1, regime_count - 1)
        return 0


class FlairCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Coordinates API access and polling for vent devices."""

    def __init__(self, hass: HomeAssistant, api: FlairApi | None, entry: ConfigEntry) -> None:
        self.api = api
        self.entry = entry
        self._unsub_thermostat_listeners: list[Callable[[], None]] = []
        self._dab_state: dict[str, dict[str, Any]] = {}
        self._last_hvac_action: dict[str, str | None] = {}
        self._vent_rates: dict[str, dict[str, float]] = {}
        self._vent_last_reading: dict[str, datetime] = {}
        self._vent_last_commanded: dict[str, datetime] = {}
        self._vent_last_target: dict[str, int] = {}
        self._manual_apertures: dict[str, int] = {}
        self._vent_models: dict[str, dict[str, dict[str, float]]] = {}
        self._efficiency_models: dict[str, dict[str, dict[str, Any]]] = {}
        # Per-room regime-aware learning models (R11/R25.1), keyed by room name.
        # Kept in-memory for now; Task 22 wires the schema-v2 persistence store
        # around this dict (clean seam — load seeds it, save reads from it).
        self._room_efficiency_models: dict[str, RoomEfficiencyModel] = {}
        # Per-room door-leakage residual models (R26.4), keyed identically to the
        # room-efficiency store: by room name, falling back to the vent id when a
        # room is unnamed. In-memory only for now; Tasks 7/8 wire the learning
        # writes and read-time resolution, Task 9 the schema-v2 persistence.
        self._door_factor_models: dict[str, DoorFactorModel] = {}
        # Per-vent aperture->airflow effectiveness, schema-v2 ``vent_effectiveness``
        # section (R25.2/25.3): ``{vent: {mode: {leak, n, curve{breakpoints,flow,
        # counts}, knee_pct, sum_*}}}``. Seeded from the regression at migration
        # (Task 22) and refined online by the learned ``VentCurve`` (Task 31). Held
        # as plain JSON-able dicts so the Store round-trips it directly.
        self._vent_effectiveness: dict[str, dict[str, dict[str, Any]]] = {}
        self._vent_adjustments: dict[str, list[dict[str, Any]]] = {}
        self._strategy_metrics: dict[str, dict[str, Any]] = {}
        self._cycle_stats: dict[str, dict[str, Any]] = {}
        self._last_strategy: str | None = None
        self._max_rates: dict[str, float] = {"cooling": 0.0, "heating": 0.0}
        self._max_running_minutes: dict[str, float] = {}
        self._vent_starting_temps: dict[str, float] = {}
        self._vent_starting_open: dict[str, int] = {}
        self._pre_adjust_flags: dict[str, bool] = {}
        self._idle_since: dict[str, datetime] = {}
        # When a thermostat last transitioned active -> idle. Used to measure the
        # idle gap on the next idle -> active transition so a short-cycle
        # re-activation can reuse the prior cycle's anchor instead of recomputing
        # (R7.8). Kept independent of ``_idle_since`` (driven by the pre-adjust
        # state-change listener) so listener ordering can't clear it mid-gap.
        self._cycle_idle_since: dict[str, datetime] = {}
        self._cycle_targets: dict[str, dict[str, Any]] = {}
        self._adjustment_batch_history: dict[str, list[datetime]] = {}
        self._hold_count: int = 0
        self._recalc_count_24h: int = 0
        self._last_max_deviation: float = 0.0
        self._last_active_spread: float = 0.0
        self._hold_status: str = "idle"
        self._total_active_polls: int = 0
        # --- Task 24 observability state (R13/R14/R5.4) ----------------------
        # Recomputed every active poll by :meth:`_update_active_observability`.
        self._last_max_active_error: float = 0.0
        self._room_signed_errors: dict[str, float] = {}
        self._airflow_limited_rooms: set[str] = set()
        self._airflow_limited_vents: set[str] = set()
        # Rolling 24 h event timestamps for the recalculations/holds sensors
        # (kept in memory only; they reset on restart, which is acceptable for
        # a 24 h rolling window and avoids persistence churn).
        self._recalc_events: list[datetime] = []
        self._hold_events: list[datetime] = []
        # Per-strategy spread-sample counts (in-memory; the derived averages are
        # persisted inside ``_strategy_metrics``).
        self._spread_sample_counts: dict[str, int] = {}
        self._store = Store(hass, 1, f"{DOMAIN}_{entry.entry_id}_dab.json")
        self._save_lock = asyncio.Lock()
        self._dab_lock = asyncio.Lock()
        self._pending_finalize: dict[str, asyncio.Task] = {}
        self._background_tasks: set[asyncio.Task] = set()
        self._error_counts: dict[str, int] = {}

        poll_active = entry.options.get(CONF_POLL_INTERVAL_ACTIVE, DEFAULT_POLL_INTERVAL_ACTIVE)
        poll_idle = entry.options.get(CONF_POLL_INTERVAL_IDLE, DEFAULT_POLL_INTERVAL_IDLE)
        self._poll_interval_active = timedelta(minutes=poll_active)
        self._poll_interval_idle = timedelta(minutes=poll_idle)
        self._initial_efficiency_percent = float(
            entry.options.get(CONF_INITIAL_EFFICIENCY_PERCENT, DEFAULT_INITIAL_EFFICIENCY_PERCENT)
        )
        self._notify_efficiency_changes = bool(
            entry.options.get(CONF_NOTIFY_EFFICIENCY_CHANGES, DEFAULT_NOTIFY_EFFICIENCY_CHANGES)
        )
        self._log_efficiency_changes = bool(
            entry.options.get(CONF_LOG_EFFICIENCY_CHANGES, DEFAULT_LOG_EFFICIENCY_CHANGES)
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
        if not isinstance(stored, dict):
            _LOGGER.warning("Stored DAB data has unexpected format; resetting to defaults")
            return

        def _safe_dict(value: Any, default: dict | None = None) -> dict:
            return value if isinstance(value, dict) else (default if default is not None else {})

        self._vent_rates = _safe_dict(stored.get("vent_rates"))
        loaded_max_rates = _safe_dict(stored.get("max_rates"))
        if loaded_max_rates:
            self._max_rates.update(loaded_max_rates)
        self._max_running_minutes = _safe_dict(stored.get("max_running_minutes"))
        self._vent_models = _safe_dict(stored.get("vent_models"))
        self._efficiency_models = _safe_dict(stored.get("efficiency_models"))
        self._vent_adjustments = _safe_dict(stored.get("vent_adjustments"))
        self._strategy_metrics = _safe_dict(stored.get("strategy_metrics"))
        self._last_hvac_action = _safe_dict(stored.get("last_hvac_action"))
        self._pre_adjust_flags = _safe_dict(stored.get("pre_adjust_flags"))

        # Schema-v2 sections (R18.3/R25.7). ``_safe_dict`` degrades a malformed
        # section to an empty dict so a bad payload never crashes init or wipes
        # the other (valid) sections; the migrator below re-seeds what it can.
        self._vent_effectiveness = self._sanitize_vent_effectiveness(
            _safe_dict(stored.get("vent_effectiveness"))
        )
        self._room_efficiency_models = self._load_room_efficiency(_safe_dict(stored.get("room_efficiency")))

        # Additive ``door_factor`` section (R29.3/R29.4). A missing/malformed
        # section degrades to an empty map (every room resolves to the 0.9
        # default) without touching the room_efficiency/vent_effectiveness
        # sections; no STORE_SCHEMA_VERSION bump is required.
        self._door_factor_models = self._load_door_factor(_safe_dict(stored.get("door_factor")))

        # Restore hold/deviation counters
        self._hold_count = int(stored.get("hold_count", 0) or 0)
        self._recalc_count_24h = int(stored.get("recalc_count_24h", 0) or 0)
        self._total_active_polls = int(stored.get("total_active_polls", 0) or 0)

        # Restore cycle targets for restart resilience
        raw_ct = _safe_dict(stored.get("cycle_targets"))
        for thermo, ct_data in raw_ct.items():
            if not isinstance(ct_data, dict):
                continue
            cycle_start_str = ct_data.get("cycle_start")
            if not cycle_start_str:
                continue
            try:
                cycle_start = datetime.fromisoformat(cycle_start_str)
            except (ValueError, TypeError):
                continue
            last_recalc = None
            if ct_data.get("last_recalc"):
                try:
                    last_recalc = datetime.fromisoformat(ct_data["last_recalc"])
                except (ValueError, TypeError):
                    pass
            self._cycle_targets[thermo] = {
                "targets": ct_data.get("targets") or {},
                "initial_temps": ct_data.get("initial_temps") or {},
                "predicted_rates": ct_data.get("predicted_rates") or {},
                "cycle_start": cycle_start,
                "recalc_count": int(ct_data.get("recalc_count", 0) or 0),
                "last_recalc": last_recalc,
                "adjustment_batches": int(ct_data.get("adjustment_batches", 0) or 0),
            }
        if self._cycle_targets:
            _LOGGER.info(
                "Restored cycle targets for %d thermostat(s) from persisted state",
                len(self._cycle_targets),
            )

        self._migrate_symmetric_offsets()
        self._migrate_state_to_v2()

    def _migrate_symmetric_offsets(self) -> None:
        """Fix existing efficiency models with identical (symmetric) offsets.

        Also expands stored models to the current regime count.
        """
        migrated = 0
        expanded = 0
        for _vent_id, modes in self._efficiency_models.items():
            for _mode, model in modes.items():
                offsets = model.get("offsets", [])
                # Fix symmetric offsets
                if len(offsets) >= 2 and len({round(o, 10) for o in offsets}) == 1:
                    baseline = model.get("baseline", 0.0)
                    spread = max(0.025, abs(baseline) * 0.15) if baseline else 0.025
                    model["offsets"] = [
                        -spread + (2 * spread * i / max(1, EFF_REGIME_COUNT - 1))
                        for i in range(EFF_REGIME_COUNT)
                    ]
                    model["confidence"] = 0.0
                    migrated += 1
                # Expand offsets to current regime count if needed
                elif len(offsets) < EFF_REGIME_COUNT:
                    existing = list(offsets)
                    while len(existing) < EFF_REGIME_COUNT:
                        existing.append(0.0)
                    # Re-spread to be asymmetric
                    baseline = model.get("baseline", 0.0)
                    spread = max(0.025, abs(baseline) * 0.15) if baseline else 0.025
                    # Keep first N offsets, add new ones with small asymmetric spread
                    for i in range(len(offsets), EFF_REGIME_COUNT):
                        existing[i] = -spread + (2 * spread * i / max(1, EFF_REGIME_COUNT - 1))
                    model["offsets"] = existing
                    expanded += 1
        if migrated:
            _LOGGER.info("Migrated %d efficiency model(s) with symmetric offsets", migrated)
        if expanded:
            _LOGGER.info(
                "Expanded %d efficiency model(s) from fewer offsets to %d regimes",
                expanded,
                EFF_REGIME_COUNT,
            )

    # ------------------------------------------------------------------
    # Schema-v2 migration + (de)serialization (R18.3 / R25.7 / R13.5)
    # ------------------------------------------------------------------
    @staticmethod
    def _sanitize_vent_effectiveness(data: Any) -> dict[str, dict[str, Any]]:
        """Keep only well-formed ``vent_effectiveness`` entries (never raises).

        A valid entry maps ``vent -> {mode -> {...}}`` where each mode dict has a
        numeric ``leak`` and a ``curve`` with list ``breakpoints`` and ``flow``.
        Malformed entries are dropped (kept out, not crashed on) so a corrupt
        payload can never poison the allocator or lose the rest of the store.
        """
        valid: dict[str, dict[str, Any]] = {}
        if not isinstance(data, dict):
            return valid
        for vent_id, modes in data.items():
            if not isinstance(modes, dict):
                continue
            clean: dict[str, Any] = {}
            for mode, entry in modes.items():
                if not isinstance(entry, dict):
                    continue
                if not isinstance(entry.get("leak"), (int, float)):
                    continue
                curve = entry.get("curve")
                if not (
                    isinstance(curve, dict)
                    and isinstance(curve.get("breakpoints"), list)
                    and isinstance(curve.get("flow"), list)
                ):
                    continue
                clean[mode] = entry
            if clean:
                valid[vent_id] = clean
        return valid

    @staticmethod
    def _load_room_efficiency(data: Any) -> dict[str, RoomEfficiencyModel]:
        """Deserialize the ``room_efficiency`` section to RoomEfficiencyModels.

        Each entry is decoded via the pure :func:`learning.room_model_from_dict`,
        which tolerates malformed/partial entries (they become fresh/padded
        models) so a corrupt entry never raises or drops the others.
        """
        models: dict[str, RoomEfficiencyModel] = {}
        if not isinstance(data, dict):
            return models
        for room, raw in data.items():
            try:
                models[str(room)] = room_model_from_dict(raw)
            except Exception:  # noqa: BLE001 - never lose the rest of the store
                _LOGGER.warning("Skipping malformed room_efficiency entry for %r", room)
        return models

    @staticmethod
    def _load_door_factor(data: Any) -> dict[str, DoorFactorModel]:
        """Deserialize the ``door_factor`` section to DoorFactorModels (R29.3/R29.4).

        Mirrors :meth:`_load_room_efficiency`: a non-dict/missing section yields an
        empty map (every room then resolves to the ``0.9`` default), and each
        per-room entry is decoded via the pure :func:`learning.door_factor_from_dict`,
        which tolerates malformed/partial input (it becomes a fresh model) so a
        garbled entry never raises or drops the others — and never affects the
        ``room_efficiency``/``vent_effectiveness`` sections.
        """
        models: dict[str, DoorFactorModel] = {}
        if not isinstance(data, dict):
            return models
        for room, raw in data.items():
            try:
                models[str(room)] = door_factor_from_dict(raw)
            except Exception:  # noqa: BLE001 - never lose the rest of the store
                _LOGGER.warning("Skipping malformed door_factor entry for %r", room)
        return models

    def _migrate_state_to_v2(self) -> None:
        """Idempotently bring loaded state up to schema v2 (R18.3/R25.7/R13.5).

        Seeds the ``vent_effectiveness`` and ``room_efficiency`` sections from the
        existing v1 learned state (per-vent regression + rates / offset models)
        for any entry not already present, and back-fills the new per-strategy
        spread metric fields. Only *missing* entries are filled, so re-running on
        an already-v2 store is a no-op (idempotent) and never overwrites learned
        values. Each seed is guarded so malformed v1 data is skipped, never fatal.
        """
        self._seed_vent_effectiveness_from_v1()
        self._seed_room_efficiency_from_v1()
        self._backfill_metric_fields()

    def _seed_vent_effectiveness_from_v1(self) -> None:
        """Fill missing ``vent_effectiveness`` entries from v1 regression/rates."""
        vent_ids = set(self._vent_models) | set(self._vent_rates) | set(self._efficiency_models)
        seeded = 0
        for vent_id in vent_ids:
            modes = set(self._vent_models.get(vent_id) or {})
            modes |= set(self._vent_rates.get(vent_id) or {})
            modes |= set(self._efficiency_models.get(vent_id) or {})
            for mode in modes:
                if mode not in {"cooling", "heating"}:
                    continue
                existing = self._vent_effectiveness.get(vent_id, {})
                if mode in existing:
                    continue  # already learned/loaded -> idempotent, don't reseed
                try:
                    params = self._get_model_params(vent_id, mode)
                    sums = (self._vent_models.get(vent_id) or {}).get(mode) or {}
                    n = int(sums.get("n", 0) or 0)
                    if params is not None:
                        slope, intercept = params
                        entry = seed_vent_effectiveness(slope, intercept, n, sums=sums)
                    else:
                        entry = seed_vent_effectiveness(None, None, n)
                except Exception:  # noqa: BLE001 - skip a bad vent, keep the rest
                    _LOGGER.warning(
                        "Could not seed vent_effectiveness for %s/%s; using default",
                        vent_id,
                        mode,
                    )
                    entry = seed_vent_effectiveness(None, None, 0)
                self._vent_effectiveness.setdefault(vent_id, {})[mode] = entry
                seeded += 1
        if seeded:
            _LOGGER.info("Seeded %d vent-effectiveness curve(s) for schema v2", seeded)

    def _seed_room_efficiency_from_v1(self) -> None:
        """Fill missing ``room_efficiency`` models from v1 baselines/offsets."""
        keys = set(self._efficiency_models) | set(self._vent_rates)
        seeded = 0
        for key in keys:
            if key in self._room_efficiency_models:
                continue  # already loaded/learned -> idempotent
            baselines: dict[str, float | None] = {}
            offsets_by_mode: dict[str, Any] = {}
            eff = self._efficiency_models.get(key) or {}
            rates = self._vent_rates.get(key) or {}
            for mode in ("cooling", "heating"):
                model = eff.get(mode) if isinstance(eff, dict) else None
                baseline: float | None = None
                if isinstance(model, dict):
                    raw_baseline = model.get("baseline")
                    if isinstance(raw_baseline, (int, float)):
                        baseline = float(raw_baseline)
                    if isinstance(model.get("offsets"), list):
                        offsets_by_mode[mode] = model["offsets"]
                if baseline is None and isinstance(rates.get(mode), (int, float)):
                    baseline = float(rates[mode])
                if baseline is not None:
                    baselines[mode] = baseline
            if not baselines:
                continue
            try:
                self._room_efficiency_models[key] = seed_room_model_from_v1(baselines, offsets_by_mode)
                seeded += 1
            except Exception:  # noqa: BLE001 - skip a bad entry, keep the rest
                _LOGGER.warning("Could not seed room_efficiency for %r", key)
        if seeded:
            _LOGGER.info("Seeded %d room-efficiency model(s) for schema v2", seeded)

    def _backfill_metric_fields(self) -> None:
        """Back-fill new per-strategy spread metric fields with defaults (R13.5)."""
        for metrics in self._strategy_metrics.values():
            if not isinstance(metrics, dict):
                continue
            for field, default in _NEW_METRIC_DEFAULTS.items():
                metrics.setdefault(field, default)

    def async_detect_active_hvac(self) -> None:
        """Detect already-active HVAC after startup and initialize cycle tracking.

        If HVAC was active when HA restarted and we have persisted cycle targets,
        sync _last_hvac_action so the transition detector doesn't miss the current cycle.
        If HVAC is active but no persisted cycle targets exist, create fresh ones.
        """
        if not self.entry.options.get(CONF_DAB_ENABLED, False):
            return
        if not self.data:
            return

        assignments = self._get_vent_assignments()
        if not assignments:
            return

        vents = self.data.get("vents", {})
        grouped: dict[str, list[str]] = {}
        for vent_id, assignment in assignments.items():
            thermostat = assignment.get(CONF_THERMOSTAT_ENTITY)
            if thermostat and vent_id in vents:
                grouped.setdefault(thermostat, []).append(vent_id)

        for thermostat_entity, vent_ids in grouped.items():
            climate_state = self.hass.states.get(thermostat_entity)
            if not climate_state:
                continue
            hvac_action = self._resolve_hvac_action(climate_state)
            if hvac_action is None or hvac_action not in {HVACAction.COOLING, HVACAction.HEATING}:
                # HVAC not active — clean up any stale persisted cycle targets
                self._cycle_targets.pop(thermostat_entity, None)
                continue

            # HVAC is active right now
            self._last_hvac_action[thermostat_entity] = hvac_action

            if thermostat_entity not in self._cycle_targets:
                # No persisted targets — start fresh cycle tracking
                _LOGGER.info(
                    "HVAC already active (%s) for %s on startup; initializing cycle tracking",
                    hvac_action,
                    thermostat_entity,
                )
                self._start_hvac_cycle(thermostat_entity, hvac_action, vent_ids, self.data)
            else:
                _LOGGER.info(
                    "Restored active cycle for %s from persisted state (HVAC: %s)",
                    thermostat_entity,
                    hvac_action,
                )
                # Also ensure dab_state exists for the running cycle
                if thermostat_entity not in self._dab_state:
                    now = datetime.now(UTC)
                    self._dab_state[thermostat_entity] = {
                        "mode": hvac_action,
                        "started_cycle": self._cycle_targets[thermostat_entity].get("cycle_start", now),
                        "started_running": self._cycle_targets[thermostat_entity].get("cycle_start", now),
                        "samples": {},
                    }
                if thermostat_entity not in self._cycle_stats:
                    self._cycle_stats[thermostat_entity] = {
                        "adjustments": 0,
                        "movement": 0.0,
                        "strategy": self.entry.options.get(CONF_CONTROL_STRATEGY, DEFAULT_CONTROL_STRATEGY),
                        "vent_movement": {},
                    }

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
            _LOGGER.warning("Failed to set structure mode to manual: %s", err or repr(err))

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
        except Exception as err:
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
            except Exception as err:
                _LOGGER.exception("DAB processing failed: %s", err)
                self._async_notify_error("DAB processing failed", str(err))

        self._prune_stale_efficiency_models(data)
        return data

    async def _async_update_manual_data(self) -> dict[str, Any]:
        manual_vents = self._get_manual_vents()
        vents: list[dict[str, Any]] = []
        now = datetime.now(UTC)

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
            except Exception as err:
                _LOGGER.exception("DAB processing failed: %s", err)
                self._async_notify_error("DAB processing failed", str(err))

        self._prune_stale_efficiency_models(data)
        return data

    def _prune_stale_efficiency_models(self, data: dict[str, Any]) -> None:
        """Remove efficiency models for vent IDs no longer present in data.

        Runs on every update so vents removed mid-session (e.g. after a
        ``refresh_devices`` call) are cleaned up rather than lingering until
        the next restart.
        """
        known_vents = set(data.get("vents", {}).keys())
        stale = set(self._efficiency_models.keys()) - known_vents
        if stale:
            _LOGGER.debug("Pruning efficiency models for %d stale vent(s): %s", len(stale), stale)
            for vent_id in stale:
                del self._efficiency_models[vent_id]

    def async_shutdown(self) -> None:
        """Clean up listeners and cancel all pending tasks when unloading."""
        for unsub in self._unsub_thermostat_listeners:
            unsub()
        self._unsub_thermostat_listeners.clear()
        for task in list(self._pending_finalize.values()):
            task.cancel()
        self._pending_finalize.clear()
        for task in list(self._background_tasks):
            task.cancel()
        self._background_tasks.clear()

    async def async_setup_thermostat_listeners(self) -> None:
        """Track thermostat HVAC action changes to adjust polling interval."""
        self.async_shutdown()

        thermostat_entities = self._get_thermostat_entities()
        if not thermostat_entities:
            _LOGGER.debug("No thermostat entities configured for polling control")
            return

        for entity_id in thermostat_entities:
            unsub = async_track_state_change_event(self.hass, entity_id, self._handle_thermostat_event)
            self._unsub_thermostat_listeners.append(unsub)

        await self._recompute_polling_interval()

    async def _async_enrich_vents(
        self,
        vents: list[dict[str, Any]],
        remote_cache: dict[str, asyncio.Task | Any],
    ) -> list[dict[str, Any]]:
        api = self.api
        if api is None:
            return vents
        semaphore = asyncio.Semaphore(6)

        async def enrich(vent: dict[str, Any]) -> dict[str, Any]:
            async with semaphore:
                vent_id = vent["id"]
                try:
                    reading = await api.async_get_vent_reading(vent_id)
                    self._vent_last_reading[vent_id] = datetime.now(UTC)
                except Exception as err:  # noqa: BLE001
                    _LOGGER.warning("Failed to fetch vent reading for %s: %s", vent_id, err)
                    reading = {}
                try:
                    room = await api.async_get_vent_room(vent_id)
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
        api = self.api
        if api is None:
            return pucks
        semaphore = asyncio.Semaphore(6)

        async def enrich(puck: dict[str, Any]) -> dict[str, Any]:
            async with semaphore:
                puck_id = puck["id"]
                try:
                    reading = await api.async_get_puck_reading(puck_id)
                except Exception as err:  # noqa: BLE001
                    _LOGGER.warning("Failed to fetch puck reading for %s: %s", puck_id, err)
                    reading = {}
                try:
                    room = await api.async_get_puck_room(puck_id)
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
        api = self.api
        if api is None:
            return None
        try:
            reading = await api.async_get_remote_sensor_reading(remote_id)
        except Exception:  # noqa: BLE001
            return None
        return reading.get("occupied")

    def _get_thermostat_entities(self) -> list[str]:
        assignments = self._get_vent_assignments()
        entities = {
            thermostat for data in assignments.values() if (thermostat := data.get(CONF_THERMOSTAT_ENTITY))
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
        """Resolve the thermostat's active conditioning action.

        The supported thermostats (the upstairs Ecobee) ALWAYS publish an
        ``hvac_action`` attribute, so this reads it directly:

        * ``cooling`` / ``heating`` -> that action (system is conditioning).
        * ``idle`` / ``off`` / ``fan`` / missing / unavailable -> ``None``
          (no conditioned airflow).

        R20.8: the previous temperature-vs-setpoint *fallback estimator* (which
        inferred cooling/heating from ``current_temperature`` against the
        target(s) plus a hysteresis band when ``hvac_action`` was absent) was
        dead, never-reached code given the Ecobee guarantee. It was removed so
        the resolver has a single, truthful source of conditioning state.
        """
        if not state or state.state in {STATE_UNKNOWN, STATE_UNAVAILABLE}:
            return None

        hvac_action = state.attributes.get("hvac_action")
        if hvac_action in {HVACAction.COOLING, HVACAction.HEATING}:
            return hvac_action
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
        self,
        temp: float,
        setpoint: float,
        rate: float,
        target_minutes: float,
        mode: str = "cooling",
    ) -> float:
        # Directional overshoot guard (R8): a room already past the setpoint is
        # satisfied and must close. The old ``abs(setpoint - temp)`` opened
        # overcooled/overheated rooms (positive target). Gate on direction first.
        if has_room_reached_setpoint(mode, setpoint, temp):
            return 0.0
        if rate <= 0 or target_minutes <= 0:
            return 100.0
        # Signed error in the direction that still needs conditioning (>= 0 here
        # because satisfied rooms already returned 0 above).
        diff = (temp - setpoint) if mode == "cooling" else (setpoint - temp)
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
        cycle_movement: float = 0.0,
    ) -> float:
        if candidate <= 0 or rate <= 0 or target_minutes <= 0:
            time_to_target = float("inf") if abs(setpoint - temp) > 0 else 0.0
        else:
            time_to_target = abs(setpoint - temp) / (rate * (candidate / 100))

        temp_cost = abs(time_to_target - target_minutes) if target_minutes > 0 else 0.0
        move_cost = abs(candidate - current) / 100.0
        open_cost = candidate / 100.0
        cumulative_penalty = 0.1 * (cycle_movement / 100.0)

        return (1.0 * temp_cost) + (0.15 * open_cost) + (0.6 * move_cost) + cumulative_penalty

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
        metrics["avg_temp_error"] = (metrics["avg_temp_error"] * (cycles - 1) + temp_error) / cycles
        metrics["avg_adjustments"] = (metrics["avg_adjustments"] * (cycles - 1) + adjustments) / cycles
        metrics["avg_movement"] = (metrics["avg_movement"] * (cycles - 1) + movement) / cycles
        metrics["last_temp_error"] = temp_error
        metrics["last_adjustments"] = adjustments
        metrics["last_movement"] = movement
        metrics["last_updated"] = datetime.now(UTC).isoformat()
        if active_temp_error is not None:
            active_cycles = metrics.get("active_cycles", 0) + 1
            metrics["active_cycles"] = active_cycles
            metrics["avg_active_temp_error"] = (
                metrics.get("avg_active_temp_error", 0.0) * (active_cycles - 1) + active_temp_error
            ) / active_cycles
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

        def _discard(task: asyncio.Task) -> None:
            self._background_tasks.discard(task)

        for coro in (self._recompute_polling_interval(), self._async_handle_pre_adjust(event)):
            task = self.hass.async_create_task(coro)
            self._background_tasks.add(task)
            task.add_done_callback(_discard)

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
            self._idle_since.pop(entity_id, None)
            return

        # Track when thermostat entered idle
        now = datetime.now(UTC)
        if entity_id not in self._idle_since:
            self._idle_since[entity_id] = now

        # Require thermostat idle for ≥2 minutes before pre-adjust
        idle_duration = (now - self._idle_since[entity_id]).total_seconds() / 60.0
        if idle_duration < 2.0:
            _LOGGER.debug(
                "Pre-adjust skipped for %s: idle only %.1f min (need 2 min)",
                entity_id,
                idle_duration,
            )
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
            cooling = new_state.attributes.get("target_temp_high") or new_state.attributes.get(
                "cooling_setpoint"
            )
            heating = new_state.attributes.get("target_temp_low") or new_state.attributes.get(
                "heating_setpoint"
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
                _LOGGER.debug("Skipping pre-adjust for %s; invalid target temps", entity_id)
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

        # Require temp within 0.3°C of predicted HVAC trigger point
        temp_delta = abs(current_temp - setpoint)
        if temp_delta > 0.3:
            _LOGGER.debug(
                "Pre-adjust skipped for %s: temp delta %.2f°C > 0.3°C from setpoint",
                entity_id,
                temp_delta,
            )
            self._pre_adjust_flags[entity_id] = False
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

        # R7.7: this is the bounded pre-adjust path. It is only reached after
        # _async_handle_pre_adjust has verified the thermostat has been idle for
        # the minimum dwell (>= 2 min) AND the temperature is within the
        # configured threshold (0.3 C) of the predicted trigger. It is the one
        # command path R7.6 permits while idle/fan, so it must bypass the live
        # "HVAC still active" re-verify guard (which would otherwise suppress it
        # because the thermostat is, by definition, idle here).
        await self._async_apply_dab_adjustments(
            thermostat_entity, hvac_action, vent_ids, self.data, pre_adjust=True
        )

    async def _async_process_dab(self, data: dict[str, Any]) -> None:
        assignments = self._get_vent_assignments()
        if not assignments:
            return

        vents = data.get("vents", {})
        grouped: dict[str, list[str]] = {}
        for vent_id, assignment in assignments.items():
            thermostat = assignment.get(CONF_THERMOSTAT_ENTITY)
            # R20.9: vents with no thermostat assignment (or whose assigned
            # device isn't present in the fetched vent data) are intentionally
            # skipped. Balancing is driven per-thermostat, so an unassigned vent
            # has no cycle to participate in; it is left in its current position
            # and never commanded, rather than left to undefined behavior.
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
                _LOGGER.warning(
                    "Thermostat entity '%s' not found in state machine; DAB skipped for this "
                    "group. Verify your vent assignments configuration.",
                    thermo,
                )
                continue
            hvac_action = self._resolve_hvac_action(state)
            if hvac_action is None or hvac_action not in {HVACAction.COOLING, HVACAction.HEATING}:
                _LOGGER.debug("Thermostat %s not actively heating/cooling", thermo)
                continue

            await self._async_apply_dab_adjustments(thermo, hvac_action, vent_ids, self.data)

    async def async_set_room_active(self, room_id: str, active: bool) -> None:
        """Set room active state via API and refresh."""
        if not self.api:
            raise ValueError("Flair API client not available")
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
        now = datetime.now(UTC)
        active_states = {HVACAction.COOLING, HVACAction.HEATING}

        if hvac_action is not None and hvac_action in active_states and prev_action not in active_states:
            # idle/none -> active. If the thermostat just short-cycled (idle gap
            # shorter than ``short_cycle_gap_min``) and the prior cycle's anchor
            # is still intact, reuse it instead of recomputing from scratch
            # (R7.8). Otherwise start a fresh cycle as before.
            if self._is_short_cycle_reactivation(thermostat_entity, now):
                self._cancel_pending_finalize(thermostat_entity)
                _LOGGER.info(
                    "Short-cycle re-activation for %s (idle gap < %.1f min): "
                    "reusing prior cycle anchor instead of recomputing",
                    thermostat_entity,
                    self._short_cycle_gap_min(),
                )
            else:
                self._start_hvac_cycle(thermostat_entity, hvac_action, vent_ids, data)
            self._cycle_idle_since.pop(thermostat_entity, None)

        if hvac_action not in active_states and prev_action is not None and prev_action in active_states:
            # active -> idle. Remember when we went idle so the next
            # re-activation can measure the gap, then schedule the delayed
            # finalize (which would otherwise clear the anchor).
            self._cycle_idle_since[thermostat_entity] = now
            await self._schedule_finalize(thermostat_entity, prev_action, vent_ids)

        self._last_hvac_action[thermostat_entity] = hvac_action

        if hvac_action is not None and hvac_action in active_states:
            await self._async_apply_dab_adjustments(
                thermostat_entity, hvac_action, vent_ids, data, count_as_poll=True
            )

    def _short_cycle_gap_min(self) -> float:
        """Configured idle-gap threshold (minutes) for short-cycle reuse (R7.8).

        Read from ``entry.options`` with a sensible default; the options-flow
        field is wired in Task 23 (this is the seam until then).
        """
        try:
            return float(self.entry.options.get(CONF_SHORT_CYCLE_GAP_MIN, DEFAULT_SHORT_CYCLE_GAP_MIN))
        except (TypeError, ValueError):
            return float(DEFAULT_SHORT_CYCLE_GAP_MIN)

    def _is_short_cycle_reactivation(self, thermostat_entity: str, now: datetime) -> bool:
        """Whether an ``idle -> active`` transition is a short-cycle continuation.

        True only when the prior cycle's anchor still exists (the delayed
        finalize hasn't cleared it yet) AND the idle gap since the thermostat
        went idle is shorter than ``short_cycle_gap_min`` (R7.8). A configured
        threshold of 0 disables reuse entirely.
        """
        if thermostat_entity not in self._cycle_targets:
            return False
        idle_since = self._cycle_idle_since.get(thermostat_entity)
        if idle_since is None:
            return False
        gap_min = (now - idle_since).total_seconds() / 60.0
        return gap_min < self._short_cycle_gap_min()

    def _cancel_pending_finalize(self, thermostat_entity: str) -> None:
        """Cancel a pending delayed finalize so it can't wipe the reused anchor."""
        task = self._pending_finalize.pop(thermostat_entity, None)
        if task is not None and not task.done():
            task.cancel()

    def _start_hvac_cycle(
        self, thermostat_entity: str, hvac_action: str, vent_ids: list[str], data: dict[str, Any]
    ) -> None:
        now = datetime.now(UTC)
        self._dab_state[thermostat_entity] = {
            "mode": hvac_action,
            "started_cycle": now,
            "started_running": now + timedelta(minutes=EFF_ACTION_STABLE_MIN),
            "samples": {},
        }
        self._cycle_stats[thermostat_entity] = {
            "adjustments": 0,
            "movement": 0.0,
            "strategy": self.entry.options.get(CONF_CONTROL_STRATEGY, DEFAULT_CONTROL_STRATEGY),
            "vent_movement": {},
        }
        self._cycle_targets[thermostat_entity] = {
            "targets": {},
            "initial_temps": {},
            "predicted_rates": {},
            "cycle_start": now,
            "recalc_count": 0,
            "last_recalc": None,
            "adjustment_batches": 0,
        }
        self._hold_status = "idle"

        for vent_id in vent_ids:
            temp = self._get_room_temp(vent_id, data)
            if temp is not None:
                self._vent_starting_temps[vent_id] = temp
            self._vent_starting_open[vent_id] = int(
                self._get_vent_attribute(vent_id, data, "percent-open") or 0
            )

    async def _schedule_finalize(self, thermostat_entity: str, hvac_action: str, vent_ids: list[str]) -> None:
        if thermostat_entity in self._pending_finalize:
            return

        # Capture the identity of the cycle being finalized at *schedule* time
        # (R10.1). If the HVAC re-activates and a new cycle starts before this
        # delayed task runs, the token will no longer match the current cycle
        # and we must not wipe the new cycle's state (R10.2).
        cycle_state = self._dab_state.get(thermostat_entity)
        cycle_token = cycle_state.get("started_cycle") if cycle_state else None

        async def finalize_task() -> None:
            await asyncio.sleep(30)
            await self.async_request_refresh()
            await self._async_finalize_cycle(thermostat_entity, hvac_action, vent_ids, cycle_token)

        task = self.hass.async_create_task(finalize_task())
        self._pending_finalize[thermostat_entity] = task

    async def _async_finalize_cycle(
        self,
        thermostat_entity: str,
        hvac_action: str,
        vent_ids: list[str],
        cycle_token: Any = None,
    ) -> None:
        # Mutate cycle bookkeeping under the same lock the apply path uses so a
        # delayed finalize can't race a concurrent re-evaluation (R10.3).
        async with self._dab_lock:
            self._pending_finalize.pop(thermostat_entity, None)

            current_state = self._dab_state.get(thermostat_entity)
            current_token = current_state.get("started_cycle") if current_state else None
            # If a new cycle has started since this finalize was scheduled, the
            # current cycle identity won't match the captured token. Leave the
            # new cycle's state untouched (R10.1/R10.2).
            if cycle_token is not None and current_token is not None and current_token != cycle_token:
                _LOGGER.debug(
                    "Skipping stale finalize for %s: a new cycle started "
                    "(scheduled token=%s, current token=%s)",
                    thermostat_entity,
                    cycle_token,
                    current_token,
                )
                return

            state = self._dab_state.pop(thermostat_entity, None)
            self._cycle_targets.pop(thermostat_entity, None)
            self._hold_status = "idle"
            cycle_stats = self._cycle_stats.pop(thermostat_entity, None) or {}

        if not state:
            return

        started_running = state.get("started_running")
        if not started_running:
            return

        finished_running = datetime.now(UTC)
        total_running_minutes = max(0.0, (finished_running - started_running).total_seconds() / 60.0)

        prev_max = self._max_running_minutes.get(thermostat_entity, DEFAULT_SETTINGS.max_minutes_to_setpoint)
        self._max_running_minutes[thermostat_entity] = rolling_average(prev_max, total_running_minutes, 1, 6)

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
            vent_context = self._get_vent_context(vent_id, self.data) if self.data else None
            baseline_rate, effective_rate, _confidence = self._update_efficiency_model(
                vent_id, rate_prop, efficiency_sample, context=vent_context
            )
            # Fold the same observed full-open rate sample into the regime-aware
            # per-room learning model (R25.4/R25.5). Keyed by room (vents in a
            # room share temp/targets, R23) so a multi-vent room is updated once.
            self._update_room_efficiency_model(vent_id, rate_prop, efficiency_sample)
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

        setpoint = setpoint_target or self._get_thermostat_setpoint(thermostat_entity, hvac_action)
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
        self,
        thermostat_entity: str,
        hvac_action: str,
        vent_ids: list[str],
        data: dict[str, Any],
        count_as_poll: bool = False,
        pre_adjust: bool = False,
    ) -> None:
        """Serialize DAB execution so concurrent triggers can't double-command vents.

        The apply path is reachable from the coordinator poll, the thermostat
        state-change listener (pre-adjust) and the manual ``run_dab`` service.
        A lock guarantees they run one at a time and don't corrupt the shared
        cycle/anti-chatter bookkeeping.

        ``pre_adjust`` marks the bounded pre-adjust path (R7.7), the only command
        path R7.6 permits while the thermostat is idle/fan.
        """
        async with self._dab_lock:
            await self._apply_dab_adjustments_impl(
                thermostat_entity, hvac_action, vent_ids, data, count_as_poll, pre_adjust
            )

    async def _apply_dab_adjustments_impl(
        self,
        thermostat_entity: str,
        hvac_action: str,
        vent_ids: list[str],
        data: dict[str, Any],
        count_as_poll: bool = False,
        pre_adjust: bool = False,
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
        control_strategy = self.entry.options.get(CONF_CONTROL_STRATEGY, DEFAULT_CONTROL_STRATEGY)
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

        self._total_active_polls += 1 if count_as_poll else 0

        # --- Task 24: active-room observability (R13.1/R13.3/R14.1/R5.4) -----
        # Compute the actual active-room spread, max error, per-room signed
        # errors and airflow-limited set every poll while conditioning, and
        # accumulate per-strategy spread metrics. Defensive: a gather failure
        # for one room never breaks the apply path (R22.3).
        if hvac_action in (HVACAction.COOLING, HVACAction.HEATING):
            self._update_active_observability(
                hvac_action, setpoint, vent_ids, data, control_strategy, granularity
            )

        # --- Deviation check: hold positions if tracking within threshold ---
        deviation_threshold = float(
            self.entry.options.get(CONF_DEVIATION_THRESHOLD, DEFAULT_DEVIATION_THRESHOLD)
        )
        max_recalc = int(self.entry.options.get(CONF_MAX_RECALC_PER_CYCLE, DEFAULT_MAX_RECALC_PER_CYCLE))
        cycle_data = self._cycle_targets.get(thermostat_entity)
        _LOGGER.debug(
            "DAB poll #%d for %s: cycle_data=%s, targets=%d, hold_count=%d",
            self._total_active_polls,
            thermostat_entity,
            "present" if cycle_data else "absent",
            len(cycle_data.get("targets", {})) if cycle_data else 0,
            self._hold_count,
        )
        if cycle_data and cycle_data.get("targets"):
            now_check = datetime.now(UTC)
            elapsed_min = (now_check - cycle_data["cycle_start"]).total_seconds() / 60.0

            # Skip deviation check for first 5 minutes of a cycle
            if elapsed_min >= 5.0:
                if cycle_data["recalc_count"] >= max_recalc:
                    _LOGGER.debug(
                        "Holding positions for %s: recalc cap (%d) reached",
                        thermostat_entity,
                        max_recalc,
                    )
                    self._hold_status = "holding"
                    self._hold_count += 1
                    self._note_hold()
                    for vent_id in vent_ids:
                        self._record_cycle_sample(thermostat_entity, vent_id, data)
                    return

                needs_recalc = False
                recalc_reason = ""

                if control_strategy == CONTROL_STRATEGY_BALANCE:
                    # --- A5 hold integration (R5.2/R7.1/R7.2) ---------------
                    # The active-room spread guardrail is the PRIMARY recompute
                    # trigger. Airflow-limited rooms are excluded from the
                    # per-vent "all rooms tracking" determination so a pinned-
                    # but-hot room neither forces churn nor a false hold.
                    gate_settings = self._balance_gate_settings(granularity)
                    spread, airflow_limited_vents = self._balance_hold_metrics(
                        hvac_action, setpoint, vent_ids, data, gate_settings
                    )
                    if spread > gate_settings.spread_guardrail_c:
                        # R7.2: predicted active-room spread exceeds the
                        # guardrail -> a new allocation is permitted even when
                        # every per-vent deviation is within threshold.
                        needs_recalc = True
                        recalc_reason = (
                            f"active-room spread {spread:.2f}°C exceeds guardrail "
                            f"{gate_settings.spread_guardrail_c:.2f}°C"
                        )
                    else:
                        # R7.1: at/below the guardrail -> prefer holding. Still
                        # honor the deviation safety check, but EXCLUDE airflow-
                        # limited rooms (R5.2): they physically cannot track, so
                        # their expected deviation must neither force churn nor
                        # be counted as tracking.
                        (
                            needs_recalc,
                            recalc_reason,
                            max_deviation,
                        ) = self._deviation_recompute_check(
                            cycle_data,
                            data,
                            hvac_action,
                            elapsed_min,
                            deviation_threshold,
                            exclude_vents=airflow_limited_vents,
                        )
                        self._last_max_deviation = max_deviation
                else:
                    # Legacy strategies keep their original deviation-only hold
                    # behavior (no spread guardrail, no airflow-limited
                    # exclusion).
                    (
                        needs_recalc,
                        recalc_reason,
                        max_deviation,
                    ) = self._deviation_recompute_check(
                        cycle_data,
                        data,
                        hvac_action,
                        elapsed_min,
                        deviation_threshold,
                    )
                    self._last_max_deviation = max_deviation

                if not needs_recalc:
                    _LOGGER.debug(
                        "Holding positions for %s: tracking within bounds",
                        thermostat_entity,
                    )
                    self._hold_status = "holding"
                    self._hold_count += 1
                    self._note_hold()
                    for vent_id in vent_ids:
                        self._record_cycle_sample(thermostat_entity, vent_id, data)
                    return

                _LOGGER.debug(
                    "Recalculating for %s (recalc #%d): %s",
                    thermostat_entity,
                    cycle_data["recalc_count"] + 1,
                    recalc_reason,
                )
                self._hold_status = "recalculating"
                self._recalc_count_24h += 1
                self._note_recalc()
                cycle_data["recalc_count"] += 1
                cycle_data["last_recalc"] = now_check

        rate_and_temp: dict[str, dict[str, Any]] = {}
        missing_temp_vents: set[str] = set()
        is_balance = control_strategy == CONTROL_STRATEGY_BALANCE
        for vent_id in vent_ids:
            if is_balance:
                # balance sources its rate from the learned per-room model +
                # context regime (R11/R12/R25); legacy strategies keep the
                # legacy regime/offset effective-rate source unchanged.
                rate = self._get_room_effective_rate(vent_id, hvac_action, data)
            else:
                vent_context = self._get_vent_context(vent_id, data)
                rate = self._get_effective_efficiency_rate(vent_id, hvac_action, context=vent_context)
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

        # --- Target computation (R20.5 refactor) -------------------------------
        # The per-vent independent target math (dab/cost/stats/hybrid) lives in
        # ``_compute_legacy_targets``; the synchronized-convergence ``balance``
        # decision logic lives entirely in the pure ``balance`` module and is
        # gathered/dispatched by ``_compute_balance_targets``. The coordinator's
        # job here is only: gather HA state -> call the right decision path ->
        # dispatch. ``balance_pre_floor`` is non-None only on the balance path and
        # carries the pre-safety-floor per-vent snapshot so the floor's *opening*
        # moves can be detected (and exempted from cooldown) below.
        balance_pre_floor: dict[str, float] | None = None
        if control_strategy == CONTROL_STRATEGY_BALANCE:
            targets, balance_pre_floor = self._compute_balance_targets(
                rate_and_temp,
                hvac_action,
                setpoint,
                granularity,
                thermostat_entity,
                data,
                close_inactive,
                missing_temp_vents,
            )
        else:
            targets = self._compute_legacy_targets(
                rate_and_temp,
                hvac_action,
                setpoint,
                control_strategy,
                thermostat_entity,
                max_running_time,
                close_inactive,
                missing_temp_vents,
                data,
            )

        # Normalize targets within multi-vent room groups
        room_groups = self._build_room_vent_groups(list(targets.keys()), data)
        for _room_name, group_vent_ids in room_groups.items():
            if len(group_vent_ids) <= 1:
                continue
            # Use the first vent's target for the whole group
            group_target = targets.get(group_vent_ids[0], 0)
            for gv_id in group_vent_ids:
                targets[gv_id] = group_target

        if balance_pre_floor is not None:
            # ``balance`` path: the single safety choke point (balance.apply_safety_floor)
            # already ran inside _compute_balance_targets — never run the legacy
            # floor on top (no double-flooring, no bypass). The floor's *opening*
            # moves are detected against the pre-floor snapshot so they can skip
            # the cooldown/deadband below (R9/R7.5).
            pre_safety_targets = balance_pre_floor
        else:
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

        # --- Reach-the-floor vs padding/balancing distinction (R9.1/R9.2/R7.5) -
        # A vent is a "reach-the-floor" open — immediate, bypassing cooldown /
        # deadband / min-percent — ONLY when BOTH hold:
        #   1. the safety floor raised it above the allocation / legacy target
        #      (``target > pre_safety``), proving the *floor* (not balancing)
        #      drove the open; and
        #   2. the command genuinely OPENS the vent past its current position
        #      (``target > current``).
        # Such an open can never be blocked, because holding it would leave the
        # combined airflow below the safety floor. Everything else stays subject
        # to anti-chatter: an allocation/balancing move the floor did not raise
        # (``target <= pre_safety``), and — critically — a floor pad that lands
        # at or below the vent's current position. The latter is a net *close*,
        # and the safety floor must NEVER be used to justify additional closing
        # (R7.5); it is governed by the normal cooldown/deadband like any other
        # balancing move. This replaces the previous behavior where any vent the
        # floor touched bypassed ALL anti-chatter regardless of direction.
        safety_opened: set[str] = set()
        for vent_id, target in targets.items():
            if target <= pre_safety_targets.get(vent_id, 0):
                continue  # not floor-raised — a balancing/allocation move
            current = self._get_vent_attribute(vent_id, data, "percent-open")
            if current is None:
                # No current reading to compare against; a floor-raise can only
                # ever increase aperture toward the floor, and the dispatch loop
                # will not command a vent without a current reading anyway, so
                # this can never produce a close. Treat as a reach-the-floor open.
                safety_opened.add(vent_id)
                continue
            if target > float(current):
                safety_opened.add(vent_id)

        # Re-verify HVAC is still active before commanding vents (R7.6 idle
        # suppression). A poll or manual run that finds the thermostat has gone
        # idle/fan must issue NO vent commands — vent movement has no thermal
        # effect without conditioned airflow. The ONLY exceptions R7.6 allows
        # are the bounded pre-adjust path (R7.7), gated upstream on idle dwell +
        # temperature threshold, and a move strictly required to reach the
        # airflow-safety floor — and the floor is enforced while the HVAC is
        # active (R3), so it never originates from this idle path. Pre-adjust
        # therefore bypasses this guard; every other path is suppressed.
        if not pre_adjust:
            climate_state = self.hass.states.get(thermostat_entity)
            if climate_state:
                current_action = self._resolve_hvac_action(climate_state)
                if current_action not in {HVACAction.COOLING, HVACAction.HEATING}:
                    _LOGGER.debug(
                        "HVAC action changed to %s before vent commands; skipping",
                        current_action,
                    )
                    return

        now = datetime.now(UTC)
        changed = 0
        movement_total = 0.0
        target_rounded_values: dict[str, int] = {}

        # Build group-level anti-chatter lookup: vent_id → max last_commanded across its room group
        group_last_commanded: dict[str, datetime] = {}
        for _room_name, group_vent_ids in room_groups.items():
            if len(group_vent_ids) <= 1:
                continue
            group_max = max(
                (
                    self._vent_last_commanded.get(vid, datetime.min.replace(tzinfo=UTC))
                    for vid in group_vent_ids
                ),
                default=datetime.min.replace(tzinfo=UTC),
            )
            for vid in group_vent_ids:
                group_last_commanded[vid] = group_max

        cycle_data = self._cycle_targets.get(thermostat_entity)
        max_batches_per_cycle = int(
            self.entry.options.get(
                CONF_MAX_ADJUSTMENT_BATCHES_PER_CYCLE,
                DEFAULT_MAX_ADJUSTMENT_BATCHES_PER_CYCLE,
            )
        )
        max_batches_per_window = int(
            self.entry.options.get(
                CONF_MAX_ADJUSTMENT_BATCHES_PER_WINDOW,
                DEFAULT_MAX_ADJUSTMENT_BATCHES_PER_WINDOW,
            )
        )
        adjustment_window_minutes = int(
            self.entry.options.get(
                CONF_ADJUSTMENT_WINDOW_MINUTES,
                DEFAULT_ADJUSTMENT_WINDOW_MINUTES,
            )
        )
        if max_batches_per_cycle > 0 and cycle_data is not None:
            batch_count = int(cycle_data.get("adjustment_batches", 0) or 0)
            if batch_count >= max_batches_per_cycle:
                _LOGGER.warning(
                    "Holding positions for %s: adjustment batch cap reached (%d per cycle)",
                    thermostat_entity,
                    max_batches_per_cycle,
                )
                for vent_id in vent_ids:
                    self._record_cycle_sample(thermostat_entity, vent_id, data)
                return
        if max_batches_per_window > 0 and adjustment_window_minutes > 0:
            cutoff = now - timedelta(minutes=adjustment_window_minutes)
            history = [ts for ts in self._adjustment_batch_history.get(thermostat_entity, []) if ts >= cutoff]
            self._adjustment_batch_history[thermostat_entity] = history
            if len(history) >= max_batches_per_window:
                _LOGGER.warning(
                    "Holding positions for %s: adjustment batch cap reached (%d in %d min)",
                    thermostat_entity,
                    max_batches_per_window,
                    adjustment_window_minutes,
                )
                for vent_id in vent_ids:
                    self._record_cycle_sample(thermostat_entity, vent_id, data)
                return

        deadband = int(self.entry.options.get(CONF_DEADBAND_PERCENT, DEFAULT_DEADBAND_PERCENT))

        def _bump_movement(vent_id: str, movement_value: float) -> None:
            vent_movement = self._cycle_stats.get(thermostat_entity, {}).setdefault("vent_movement", {})
            vent_movement[vent_id] = vent_movement.get(vent_id, 0.0) + movement_value

        # Dispatch per room-group so the vents in a room move together and never
        # diverge through independent rounding, deadband, min-percent or cooldown
        # evaluation (R23.1/23.2/23.3). ONE decision is computed for the whole
        # group and applied identically to every member; a single shared cooldown
        # clock is stamped onto all of the group's vents (R7.4/R23.2). Groups of
        # one collapse to the original per-vent behavior.
        for _room_name, group_vent_ids in room_groups.items():
            gids = [v for v in group_vent_ids if v in targets]
            if not gids:
                continue
            rep = gids[0]
            active = rate_and_temp.get(rep, {}).get("active", True)

            # Inactive rooms with close_inactive off are held at their current
            # position and are NEVER repositioned by balancing; the only move
            # permitted is the safety floor opening them further (R3.7/R19.3).
            # These are outside the balancing objective, so each vent is held at
            # its own current value (no group target applies).
            if not close_inactive and not active:
                for vent_id in gids:
                    current = self._get_vent_attribute(vent_id, data, "percent-open")
                    if current is None:
                        continue
                    if vent_id in safety_opened:
                        tgt = max(float(current), targets[vent_id])
                    else:
                        tgt = float(current)
                    target_rounded = round_to_nearest_multiple(tgt, granularity)
                    target_rounded_values[vent_id] = target_rounded
                    current_int = int(current)
                    if current_int == target_rounded:
                        continue
                    if vent_id not in safety_opened:
                        continue  # held — balancing never repositions inactive
                    # Safety reach-the-floor open: immediate (bypasses cooldown).
                    movement_value = abs(target_rounded - current_int)
                    if not await self._command_vent(vent_id, target_rounded):
                        continue
                    self._vent_last_commanded[vent_id] = now
                    changed += 1
                    movement_total += movement_value
                    self._record_vent_adjustment(vent_id, current_int, target_rounded, now)
                    _bump_movement(vent_id, movement_value)
                continue

            # --- Active group: ONE coherent decision for the whole room -------
            # Every member shares the normalized pre-round target, so the rounded
            # applied target is identical across the group (R23.3).
            shared_target_rounded = round_to_nearest_multiple(targets[rep], granularity)
            currents: dict[str, int] = {}
            for vent_id in gids:
                target_rounded_values[vent_id] = shared_target_rounded
                cur = self._get_vent_attribute(vent_id, data, "percent-open")
                if cur is not None:
                    currents[vent_id] = int(cur)
            if not currents:
                continue

            # Group-level gating uses the LARGEST member deviation so the whole
            # room moves together (or holds together) rather than splitting on an
            # independent per-vent deadband/min-percent decision (R23.3).
            max_dev = max(abs(shared_target_rounded - c) for c in currents.values())
            if max_dev == 0:
                continue  # whole group already at the shared target

            temp = float(rate_and_temp.get(rep, {}).get("temp", 0) or 0)
            error = self._calculate_temp_error(hvac_action, setpoint, temp)
            override = error is not None and error >= temp_error_override
            safety_override = any(vid in safety_opened for vid in gids)

            if not override and not safety_override:
                # Deadband + minimum-adjustment-percent evaluated ONCE per group.
                if deadband > 0 and max_dev <= deadband:
                    continue
                if min_adjust_percent > 0 and max_dev < min_adjust_percent:
                    continue
            # Time-based cooldown always applies (even for temp-error override);
            # only safety_override (reach-the-floor opening) skips it. The group's
            # most-recent command time governs the cooldown for all its vents
            # (R23.2).
            if not safety_override:
                last_change = group_last_commanded.get(rep, self._vent_last_commanded.get(rep))
                if last_change and (now - last_change) < timedelta(minutes=min_adjust_interval):
                    continue

            # Commit the group move: command every member that is not already at
            # the shared target to the IDENTICAL rounded value.
            group_commanded = False
            for vent_id in gids:
                member_cur = currents.get(vent_id)
                if member_cur is None or member_cur == shared_target_rounded:
                    continue
                movement_value = abs(shared_target_rounded - member_cur)
                if not await self._command_vent(vent_id, shared_target_rounded):
                    continue
                group_commanded = True
                changed += 1
                movement_total += movement_value
                self._record_vent_adjustment(vent_id, member_cur, shared_target_rounded, now)
                _bump_movement(vent_id, movement_value)

            # Stamp the shared cooldown clock onto EVERY vent in the group — even
            # ones already at target that weren't physically re-commanded — so the
            # whole room observes one cooldown window and the vents never drift
            # apart in move count (R23.2, the 53-vs-51 fix).
            if group_commanded:
                for vent_id in gids:
                    self._vent_last_commanded[vent_id] = now

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
        else:
            if cycle_data is not None:
                cycle_data["adjustment_batches"] = int(cycle_data.get("adjustment_batches", 0) or 0) + 1
            history = self._adjustment_batch_history.setdefault(thermostat_entity, [])
            history.append(now)

        # Record/update cycle targets for deviation tracking
        cycle_data = self._cycle_targets.get(thermostat_entity)
        if cycle_data is not None:
            if not cycle_data["targets"]:
                # First calculation of this HVAC cycle — record initial targets
                for vent_id, target in targets.items():
                    rate = float(rate_and_temp.get(vent_id, {}).get("rate", 0) or 0)
                    temp = float(rate_and_temp.get(vent_id, {}).get("temp", 0) or 0)
                    applied = target_rounded_values.get(
                        vent_id, round_to_nearest_multiple(target, granularity)
                    )
                    cycle_data["targets"][vent_id] = applied
                    cycle_data["initial_temps"][vent_id] = temp
                    cycle_data["predicted_rates"][vent_id] = rate * (max(applied, 1) / 100)
                cycle_data["last_recalc"] = now
                _LOGGER.debug(
                    "Anchored cycle targets for %s: %d vents, hold system now active",
                    thermostat_entity,
                    len(cycle_data["targets"]),
                )
            else:
                # Recalculation — update targets and reset trajectory anchor
                for vent_id, target in targets.items():
                    rate = float(rate_and_temp.get(vent_id, {}).get("rate", 0) or 0)
                    temp = float(rate_and_temp.get(vent_id, {}).get("temp", 0) or 0)
                    applied = target_rounded_values.get(
                        vent_id, round_to_nearest_multiple(target, granularity)
                    )
                    cycle_data["targets"][vent_id] = applied
                    cycle_data["initial_temps"][vent_id] = temp
                    cycle_data["predicted_rates"][vent_id] = rate * (max(applied, 1) / 100)
                cycle_data["cycle_start"] = now
                _LOGGER.debug(
                    "Recalc anchored for %s: updated %d vents (recalc #%d)",
                    thermostat_entity,
                    len(cycle_data["targets"]),
                    cycle_data.get("recalc_count", 0),
                )

    async def _command_vent(self, vent_id: str, target_rounded: int) -> bool:
        """Dispatch a single vent position command (manual store or Flair API).

        Returns ``True`` when the command was issued (or recorded, in manual
        mode) and ``False`` when the Flair API call failed so the caller can skip
        the per-vent bookkeeping for that vent. The shared group cooldown clock
        (``_vent_last_commanded``) is intentionally stamped by the caller so all
        vents in a room observe one cooldown window (R23.2); this helper only
        records the per-vent last target.
        """
        if self._is_manual():
            self._vent_last_target[vent_id] = target_rounded
            return True
        if self.api:
            try:
                await self.api.async_set_vent_position(vent_id, target_rounded)
            except (TimeoutError, aiohttp.ClientError, FlairApiError) as err:
                _LOGGER.warning(
                    "Failed to set vent %s to %s%%: %s",
                    vent_id,
                    target_rounded,
                    err,
                )
                return False
        self._vent_last_target[vent_id] = target_rounded
        return True

    def _compute_legacy_targets(
        self,
        rate_and_temp: dict[str, dict[str, Any]],
        hvac_action: str,
        setpoint: float,
        control_strategy: str,
        thermostat_entity: str,
        max_running_time: float,
        close_inactive: bool,
        missing_temp_vents: set[str],
        data: dict[str, Any],
    ) -> dict[str, float]:
        """Per-vent independent target math for the legacy strategies (R20.5).

        Extracted verbatim from ``_apply_dab_adjustments_impl`` so the coordinator
        body reads as gather -> decide -> dispatch. Behavior is unchanged for
        ``dab`` / ``cost`` / ``stats`` / ``hybrid`` (each vent is sized toward the
        setpoint independently, with the shared overshoot-close guard, R8). Returns
        a per-vent ``targets`` dict.
        """
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
            dab_targets = dict.fromkeys(rate_and_temp, 100.0)
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
            if has_room_reached_setpoint(hvac_action, setpoint, temp):
                # Satisfied room (R8): close it before the low-rate shortcut can
                # force it wide open.
                cost_targets[vent_id] = 0.0
                continue
            if rate < DEFAULT_SETTINGS.min_temp_change_rate:
                cost_targets[vent_id] = 100.0
                continue
            cost_targets[vent_id] = self._calculate_linear_target_percent(
                temp, setpoint, rate, longest_time, hvac_action
            )

        for vent_id, state_val in rate_and_temp.items():
            if close_inactive and not state_val.get("active", True):
                stats_targets[vent_id] = 0.0
                continue
            temp = float(state_val.get("temp", 0) or 0)
            if has_room_reached_setpoint(hvac_action, setpoint, temp):
                # Satisfied room (R8): close it instead of opening on |error|.
                stats_targets[vent_id] = 0.0
                continue
            # Signed error in the direction still needing conditioning (>= 0).
            diff = (temp - setpoint) if hvac_action == HVACAction.COOLING else (setpoint - temp)
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
            # Get cumulative cycle movement for movement penalty
            cycle_move = float(self._cycle_stats.get(thermostat_entity, {}).get("movement", 0.0) or 0.0)
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
                    temp, setpoint, rate, longest_time, dab_target, current, cycle_move
                )
                cost_cost = self._cost_for_target(
                    temp, setpoint, rate, longest_time, cost_target, current, cycle_move
                )
                stats_cost = self._cost_for_target(
                    temp, setpoint, rate, longest_time, stats_target, current, cycle_move
                )
                # Strategy consistency bonus: 5% discount if matches last cycle
                if self._last_strategy == "dab":
                    dab_cost *= 0.95
                elif self._last_strategy == "cost":
                    cost_cost *= 0.95
                elif self._last_strategy == "stats":
                    stats_cost *= 0.95
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

        return targets

    def _get_vent_leak(self, vent_id: str, hvac_action: str) -> float:
        """Per-vent leakage fraction for ``balance`` (R25.3) — current seam.

        Derives ``leak`` from the existing per-vent aperture->rate regression
        (``slope``/``intercept`` via :meth:`_get_model_params`) using the pure
        :func:`learning.derive_effectiveness`. Falls back to
        :data:`learning.LEAK_DEFAULT` when the regression is unavailable/untrusted.

        This is the clean seam Task 20 replaces with the full
        learning/context-wired leak source; the allocation contract
        (``flow_i(0) = leak``) is identical so wiring Task 20 needs no change here.
        """
        mode = "cooling" if hvac_action == HVACAction.COOLING else "heating"
        params = self._get_model_params(vent_id, mode)
        if params is None:
            return LEAK_DEFAULT
        slope, intercept = params
        stats = (getattr(self, "_vent_models", {}).get(vent_id) or {}).get(mode) or {}
        n = int(stats.get("n", 0) or 0)
        return derive_effectiveness(slope, intercept, n).leak

    def _get_vent_curve(self, vent_id: str, hvac_action: str) -> VentCurve:
        """Per-vent learned aperture->airflow :class:`VentCurve` for ``balance``.

        Task 32: this REPLACES the scalar :meth:`_get_vent_leak` as the airflow
        model passed to the allocator. Resolution order:

        1. The persisted, learned schema-v2 curve under
           ``vent_effectiveness[vent_id][mode]["curve"]`` (Task 22/31) — the
           saturating shape refined online, used once present.
        2. Otherwise a near-linear curve seeded from the existing per-vent
           aperture->rate regression (:meth:`_get_model_params`), so a cold/thin
           model behaves exactly like the scalar-leak model it supersedes
           (``flow(0) = leak`` from the intercept, knee at 100 %).
        3. A flat :data:`learning.LEAK_DEFAULT` seed when no regression exists.

        Never raises: a malformed persisted curve is tolerated by
        :meth:`VentCurve.from_dict`, which falls back to a near-linear seed.
        """
        mode = "cooling" if hvac_action == HVACAction.COOLING else "heating"
        entry = (self._vent_effectiveness.get(vent_id) or {}).get(mode)
        if isinstance(entry, dict):
            curve_data = entry.get("curve")
            if isinstance(curve_data, dict):
                return VentCurve.from_dict(curve_data)
        params = self._get_model_params(vent_id, mode)
        if params is None:
            return VentCurve.seed_from_regression(0.0, 0.0, 0)
        slope, intercept = params
        stats = (getattr(self, "_vent_models", {}).get(vent_id) or {}).get(mode) or {}
        n = int(stats.get("n", 0) or 0)
        return VentCurve.seed_from_regression(slope, intercept, n)

    def _balance_gate_settings(self, granularity: int) -> AllocSettings:
        """Build :class:`AllocSettings` for the A5 hold/gating decision.

        Sources the spread guardrail, cross-coupling toggle, safety floor and
        airflow-limited detection thresholds from ``entry.options`` (the
        dedicated ``CONF_*`` keys wired in Task 23) with the design defaults as
        the fallback. Values are read defensively so a malformed option degrades
        to its default rather than raising in the apply path.
        """
        opts = self.entry.options
        return AllocSettings(
            granularity=int(granularity),
            safety_floor_pct=self._opt_float(CONF_SAFETY_FLOOR_PCT, DEFAULT_SAFETY_FLOOR_PCT),
            crosscoupling=bool(opts.get(CONF_CROSSCOUPLING_ENABLED, DEFAULT_CROSSCOUPLING_ENABLED)),
            spread_guardrail_c=self._opt_float(CONF_SPREAD_GUARDRAIL_C, DEFAULT_SPREAD_GUARDRAIL_C),
            spread_improvement_deadband_c=self._opt_float(
                CONF_SPREAD_IMPROVEMENT_DEADBAND_C,
                DEFAULT_SPREAD_IMPROVEMENT_DEADBAND_C,
            ),
            airflow_limited_margin_pct=self._opt_float(
                CONF_AIRFLOW_LIMITED_MARGIN_PCT, DEFAULT_AIRFLOW_LIMITED_MARGIN_PCT
            ),
            airflow_limited_error_c=self._opt_float(
                CONF_AIRFLOW_LIMITED_ERROR_C, DEFAULT_AIRFLOW_LIMITED_ERROR_C
            ),
        )

    def _opt_float(self, key: str, default: float) -> float:
        """Read a numeric option as a float, falling back to ``default``.

        The options flow already clamps to the documented range on save; this
        guards against a hand-edited or legacy option that is missing or
        non-numeric so the apply path never raises.
        """
        try:
            value = float(self.entry.options.get(key, default))
        except (TypeError, ValueError):
            return float(default)
        if value != value:  # NaN
            return float(default)
        return value

    def _balance_hold_metrics(
        self,
        hvac_action: str,
        setpoint: float,
        vent_ids: list[str],
        data: dict[str, Any],
        settings: AllocSettings,
    ) -> tuple[float, set[str]]:
        """Predicted active-room spread + airflow-limited vent ids (A5/R5.2).

        Runs at hold-check time (before target computation) and only reads
        current state — no mutation, no dispatch. Returns:

        * the predicted active-room spread at the current commanded positions
          (via :func:`balance.predicted_spread`), the PRIMARY recompute trigger
          (R7.1/7.2); and
        * the set of vent ids whose room is currently airflow-limited — a room
          whose representative vent is at/near full open (``>= 100 -
          airflow_limited_margin_pct``) yet still off-target beyond
          ``airflow_limited_error_c`` (R5.1). These are excluded from the
          per-vent tracking determination (R5.2).

        Rooms with no usable temperature/efficiency are skipped (never crash,
        R22.3). Inactive rooms are excluded from the objective entirely.
        """
        mode = MODE_COOLING if hvac_action == HVACAction.COOLING else MODE_HEATING
        groups = self._build_room_vent_groups(vent_ids, data)
        rooms: list[RoomAllocInput] = []
        targets: dict[str, float] = {}
        airflow_limited_vents: set[str] = set()
        margin = settings.airflow_limited_margin_pct
        error_c = settings.airflow_limited_error_c
        for room_name, group_vent_ids in groups.items():
            rep = group_vent_ids[0]
            if not self._get_room_active(rep, data):
                continue
            temp = self._get_room_temp(rep, data)
            if temp is None:
                continue
            rate = self._get_room_effective_rate(rep, hvac_action, data)
            if rate <= 0:
                continue
            cur = self._get_vent_attribute(rep, data, "percent-open")
            current_open = float(cur) if cur is not None else 0.0
            signed_err = (
                (float(temp) - setpoint) if hvac_action == HVACAction.COOLING else (setpoint - float(temp))
            )
            rooms.append(
                RoomAllocInput(
                    room_id=room_name,
                    temp_c=float(temp),
                    active=True,
                    efficiency=float(rate),
                    leak=self._get_vent_leak(rep, hvac_action),
                    current_open=current_open,
                    vent_ids=tuple(group_vent_ids),
                    signed_error_c=signed_err,
                    curve=self._get_vent_curve(rep, hvac_action),
                )
            )
            targets[room_name] = current_open
            if current_open >= 100.0 - margin and signed_err > error_c:
                airflow_limited_vents.update(group_vent_ids)
        spread = predicted_spread(rooms, targets, mode, setpoint, settings.horizon_min)
        return spread, airflow_limited_vents

    # -----------------------------------------------------------------------
    # Task 24 — active-room observability (R13/R14/R5.4/R25.11)
    # -----------------------------------------------------------------------
    def _update_active_observability(
        self,
        hvac_action: str,
        setpoint: float,
        vent_ids: list[str],
        data: dict[str, Any],
        control_strategy: str,
        granularity: int,
    ) -> None:
        """Recompute active-room observability every poll while conditioning.

        Stores, for the observability sensors/attributes:

        * ``_last_active_spread`` -- the **actual** current active-room spread
          (max minus min of active room temps), 0.0 for < 2 rooms
          (R13.1/R13.2);
        * ``_last_max_active_error`` -- the largest absolute active-room error
          vs the shared setpoint (R14.1);
        * ``_room_signed_errors`` -- per-room signed error toward the setpoint,
          negative when overcooled (cooling) / overheated (heating) (R13.3);
        * ``_airflow_limited_rooms`` / ``_airflow_limited_vents`` -- rooms whose
          representative vent is at/near full open yet still off-target (R5.4).

        Also accumulates per-strategy spread metrics (R13.4). Inactive rooms are
        excluded from every active-room aggregate (R2.5). A gather error for one
        room is swallowed so the apply path never crashes (R22.3); strategy is
        independent (spread/error are temperature-only).
        """
        try:
            settings = self._balance_gate_settings(granularity)
        except Exception:  # noqa: BLE001 - defensive at the apply boundary
            settings = AllocSettings()
        margin = settings.airflow_limited_margin_pct
        error_c = settings.airflow_limited_error_c
        cooling = hvac_action == HVACAction.COOLING

        groups = self._build_room_vent_groups(vent_ids, data)
        temps: list[float] = []
        signed_errors: dict[str, float] = {}
        airflow_rooms: set[str] = set()
        airflow_vents: set[str] = set()
        max_err = 0.0
        for room_name, group_vent_ids in groups.items():
            rep = group_vent_ids[0]
            try:
                if not self._get_room_active(rep, data):
                    continue
                temp = self._get_room_temp(rep, data)
                if temp is None:
                    continue
                signed = (float(temp) - setpoint) if cooling else (setpoint - float(temp))
                room = self._get_room_data(rep, data)
                room_id = room.get("id") or room_name
                signed_errors[room_id] = round(signed, 2)
                temps.append(float(temp))
                max_err = max(max_err, abs(signed))
                cur = self._get_vent_attribute(rep, data, "percent-open")
                current_open = float(cur) if cur is not None else 0.0
                if current_open >= 100.0 - margin and signed > error_c:
                    airflow_rooms.add(room_id)
                    airflow_vents.update(group_vent_ids)
            except Exception:  # noqa: BLE001 - skip the room, never crash (R22.3)
                continue

        spread = (max(temps) - min(temps)) if len(temps) >= 2 else 0.0
        spread = round(spread, 3)
        self._last_active_spread = spread
        self._last_max_active_error = max_err
        self._room_signed_errors = signed_errors
        self._airflow_limited_rooms = airflow_rooms
        self._airflow_limited_vents = airflow_vents
        self._record_spread_metrics(control_strategy, spread, settings.spread_guardrail_c)

    def _record_spread_metrics(self, strategy: str, spread: float, guardrail_c: float) -> None:
        """Accumulate per-strategy spread metrics each active poll (R13.4)."""
        metrics = self._strategy_metrics.setdefault(strategy, {})
        for field, default in _NEW_METRIC_DEFAULTS.items():
            metrics.setdefault(field, default)
        n = self._spread_sample_counts.get(strategy, 0) + 1
        self._spread_sample_counts[strategy] = n
        metrics["avg_spread"] = (float(metrics.get("avg_spread", 0.0)) * (n - 1) + spread) / n
        metrics["max_spread"] = max(float(metrics.get("max_spread", 0.0)), spread)
        if spread > guardrail_c:
            interval_min = float(
                self.entry.options.get(CONF_POLL_INTERVAL_ACTIVE, DEFAULT_POLL_INTERVAL_ACTIVE)
            )
            metrics["time_above_guardrail_min"] = (
                float(metrics.get("time_above_guardrail_min", 0.0)) + interval_min
            )

    def _note_hold(self) -> None:
        """Record a hold event timestamp for the 24 h rolling counter."""
        self._hold_events.append(datetime.now(UTC))
        self._prune_events(self._hold_events)

    def _note_recalc(self) -> None:
        """Record a recalculation event timestamp for the 24 h rolling counter."""
        self._recalc_events.append(datetime.now(UTC))
        self._prune_events(self._recalc_events)

    @staticmethod
    def _prune_events(events: list[datetime], window_hours: float = 24.0) -> int:
        """Drop events older than ``window_hours`` in place; return remaining."""
        cutoff = datetime.now(UTC) - timedelta(hours=window_hours)
        kept = [ts for ts in events if ts >= cutoff]
        events[:] = kept
        return len(kept)

    def _deviation_recompute_check(
        self,
        cycle_data: dict[str, Any],
        data: dict[str, Any],
        hvac_action: str,
        elapsed_min: float,
        deviation_threshold: float,
        exclude_vents: set[str] | None = None,
    ) -> tuple[bool, str, float]:
        """Per-vent deviation determination (the "all rooms tracking" test).

        Returns ``(needs_recalc, recalc_reason, max_deviation)``. This is the
        legacy deviation loop extracted verbatim so legacy strategies are
        byte-for-byte unchanged. ``exclude_vents`` is used only by ``balance``
        to drop airflow-limited rooms from the determination (R5.2): they
        physically cannot track, so their (expected) deviation must neither
        force churn nor be counted as tracking.
        """
        exclude = exclude_vents or set()
        needs_recalc = False
        recalc_reason = ""
        max_deviation = 0.0
        for vent_id in cycle_data["targets"]:
            if vent_id in exclude:
                continue
            initial_temp = cycle_data["initial_temps"].get(vent_id)
            predicted_rate = cycle_data["predicted_rates"].get(vent_id, 0)
            if initial_temp is None:
                continue

            actual_temp = self._get_room_temp(vent_id, data)
            if actual_temp is None:
                continue

            if hvac_action == HVACAction.COOLING:
                expected_temp = initial_temp - (abs(predicted_rate) * elapsed_min)
            else:
                expected_temp = initial_temp + (abs(predicted_rate) * elapsed_min)

            deviation = abs(actual_temp - expected_temp)
            max_deviation = max(max_deviation, deviation)

            if deviation > deviation_threshold:
                needs_recalc = True
                recalc_reason = (
                    f"vent {vent_id}: actual={actual_temp:.1f} vs "
                    f"expected={expected_temp:.1f} (deviation={deviation:.2f}°C)"
                )
                break
        return needs_recalc, recalc_reason, max_deviation

    def _compute_balance_targets(
        self,
        rate_and_temp: dict[str, dict[str, Any]],
        hvac_action: str,
        setpoint: float,
        granularity: int,
        thermostat_entity: str,
        data: dict[str, Any],
        close_inactive: bool,
        missing_temp_vents: set[str],
    ) -> tuple[dict[str, float], dict[str, float]]:
        """Gather rooms, run the pure ``balance`` allocation + safety floor (R1/R20.5).

        Returns ``(targets, pre_floor)`` — per-vent commanded apertures after the
        single ``balance.apply_safety_floor`` choke point, and the per-vent
        pre-floor snapshot (so the dispatch loop can tell which opens were forced
        by the floor and exempt them from cooldown).

        * Room temperature is gathered in **Celsius** via the existing unit
          helpers (R18.4). One ``RoomAllocInput`` per room-group (vents in a room
          share temp/active/target, R23).
        * ``effective_rate`` comes from the current effective-rate source and
          ``leak`` from :meth:`_get_vent_leak` (both are the Task 20 seam).
        * Inactive rooms are excluded from the allocation objective and are never
          repositioned by balancing (held). ``close_inactive_rooms`` is still
          honored downstream (their target is set to 0 and the dispatch loop holds
          them at current when the option is off).
        * A room with no usable temperature or efficiency is skipped (held), never
          crashes (R22.3).
        """
        mode = MODE_COOLING if hvac_action == HVACAction.COOLING else MODE_HEATING
        groups = self._build_room_vent_groups(list(rate_and_temp.keys()), data)

        rooms: list[RoomAllocInput] = []
        room_to_vents: dict[str, list[str]] = {}
        inactive_vents: list[str] = []
        for room_name, group_vent_ids in groups.items():
            rep = group_vent_ids[0]
            state_val = rate_and_temp.get(rep, {})
            if not state_val.get("active", True):
                inactive_vents.extend(group_vent_ids)
                continue
            # Skip rooms with no usable temperature or efficiency (held, R22.3).
            if any(vid in missing_temp_vents for vid in group_vent_ids):
                continue
            temp = state_val.get("temp")
            rate = float(state_val.get("rate", 0) or 0)
            if temp is None or rate <= 0:
                continue
            current = self._get_vent_attribute(rep, data, "percent-open")
            current_open = float(current) if current is not None else 0.0
            signed_err = (
                (float(temp) - setpoint) if hvac_action == HVACAction.COOLING else (setpoint - float(temp))
            )
            rooms.append(
                RoomAllocInput(
                    room_id=room_name,
                    temp_c=float(temp),
                    active=True,
                    efficiency=rate,
                    leak=self._get_vent_leak(rep, hvac_action),
                    current_open=current_open,
                    vent_ids=tuple(group_vent_ids),
                    signed_error_c=signed_err,
                    curve=self._get_vent_curve(rep, hvac_action),
                )
            )
            room_to_vents[room_name] = group_vent_ids

        # Held-open inactive airflow counts toward the floor only while the vents
        # are actually open (R3.7) — i.e. when close_inactive is off.
        inactive_open_sum = 0.0
        if not close_inactive:
            for vid in inactive_vents:
                cur = self._get_vent_attribute(vid, data, "percent-open")
                inactive_open_sum += float(cur) if cur is not None else 0.0

        conventional = int(
            self.entry.options.get(CONF_CONVENTIONAL_VENTS_BY_THERMOSTAT, {}).get(thermostat_entity, 0) or 0
        )
        settings = AllocSettings(
            safety_floor_pct=self._opt_float(CONF_SAFETY_FLOOR_PCT, DEFAULT_SAFETY_FLOOR_PCT),
            conventional_vents=conventional,
            inactive_open_pct_sum=inactive_open_sum,
            inactive_count=len(inactive_vents),
            granularity=int(granularity),
            crosscoupling=bool(
                self.entry.options.get(CONF_CROSSCOUPLING_ENABLED, DEFAULT_CROSSCOUPLING_ENABLED)
            ),
            airflow_limited_margin_pct=self._opt_float(
                CONF_AIRFLOW_LIMITED_MARGIN_PCT, DEFAULT_AIRFLOW_LIMITED_MARGIN_PCT
            ),
            airflow_limited_error_c=self._opt_float(
                CONF_AIRFLOW_LIMITED_ERROR_C, DEFAULT_AIRFLOW_LIMITED_ERROR_C
            ),
        )

        result = allocate(rooms, setpoint, mode, settings)
        # Single safety choke point — every balance dispatch routes through here.
        floored, _floor_binding = apply_safety_floor(result.targets, rooms, settings)

        targets: dict[str, float] = {}
        pre_floor: dict[str, float] = {}
        for room_name, group_vent_ids in room_to_vents.items():
            pre_pct = result.targets.get(room_name, 0.0)
            post_pct = floored.get(room_name, pre_pct)
            for vid in group_vent_ids:
                pre_floor[vid] = pre_pct
                targets[vid] = post_pct
        # Inactive rooms: never repositioned by balancing. Target 0 lets
        # close_inactive close them; when close_inactive is off the dispatch loop
        # holds them at their current position.
        for vid in inactive_vents:
            targets[vid] = 0.0
            pre_floor[vid] = 0.0

        return targets, pre_floor

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
        if temp is None:
            return None
        try:
            return float(temp)
        except (TypeError, ValueError):
            return None

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

    def _build_room_vent_groups(self, vent_ids: list[str], data: dict[str, Any]) -> dict[str, list[str]]:
        """Group vent IDs by their Flair room."""
        groups: dict[str, list[str]] = {}
        for vent_id in vent_ids:
            room_name = self._get_room_name(vent_id, data) or vent_id
            groups.setdefault(room_name, []).append(vent_id)
        return groups

    @staticmethod
    def _compute_time_bucket(hour: int) -> int:
        """Map an hour-of-day to a time bucket.

        0=night(22-6), 1=morning(6-12), 2=afternoon(12-18), 3=evening(18-22).
        """
        if hour < 6 or hour >= 22:
            return 0
        if hour < 12:
            return 1
        if hour < 18:
            return 2
        return 3

    def _get_vent_context(self, vent_id: str, data: dict[str, Any]) -> EfficiencyContext:
        """Gather operating context for efficiency learning.

        R20.8/R12/D9: door state is intentionally *not* read here. Doors are
        applied as a bounded multiplier in the pure :mod:`context` path
        (:meth:`_build_context`), so the legacy regime context is occupancy/time
        only.
        """
        # Occupancy from room data
        room = self._get_room_data(vent_id, data)
        occupied = (room.get("attributes") or {}).get("occupied")
        is_occupied = bool(occupied) if occupied is not None else False

        # Time bucket derived from Home Assistant local time (not UTC), so the
        # night/morning/afternoon/evening regimes line up with the site's day.
        time_bucket = self._compute_time_bucket(dt_util.now().hour)

        return EfficiencyContext(
            occupied=is_occupied,
            time_bucket=time_bucket,
        )

    # -- Pure context/learning wiring (Task 20, R11/R12/R25) ----------------
    def _resolve_outdoor_temp_c(self) -> float | None:
        """Resolve the configured outdoor temperature to Celsius (R12.5).

        Reads ``CONF_OUTDOOR_TEMP_ENTITY`` from ``entry.options``. A ``sensor.*``
        provides the temperature as its state; a ``weather.*`` entity provides it
        as a ``temperature`` attribute. Fahrenheit is converted via the existing
        unit helper. Returns ``None`` (graceful, neutral "mild" band downstream)
        when the entity is unset, missing, unavailable or non-numeric.
        """
        entity = self.entry.options.get(CONF_OUTDOOR_TEMP_ENTITY)
        if not entity:
            return None
        state = self.hass.states.get(entity)
        if state is None:
            return None
        unit = state.attributes.get("unit_of_measurement")
        raw: Any = None
        if str(entity).startswith("weather."):
            raw = state.attributes.get("temperature")
            unit = state.attributes.get("temperature_unit", unit)
        if raw is None:
            if state.state in {STATE_UNKNOWN, STATE_UNAVAILABLE}:
                return None
            raw = state.state
        try:
            temp = float(raw)
        except (TypeError, ValueError):
            return None
        if is_fahrenheit_unit(unit):
            return (temp - 32) * 5 / 9
        return temp

    def _resolve_sun_state(self) -> str | None:
        """Return the ``sun.sun`` state (``above_horizon``/``below_horizon``).

        ``None`` when the entity is absent or unavailable, in which case
        :func:`context.is_daytime` falls back to the local-hour window.
        """
        state = self.hass.states.get("sun.sun")
        if state is None or state.state in {STATE_UNKNOWN, STATE_UNAVAILABLE}:
            return None
        return state.state

    def _build_context(self, vent_id: str, data: dict[str, Any]) -> Context:
        """Build a pure :class:`context.Context` from current HA states (R12).

        Resolves the primitives ``context.build`` expects — local hour, outdoor
        temperature (°C), tri-state occupancy, tri-state door-open and the sun
        state — then delegates to the pure builder. Every source degrades
        gracefully: a missing outdoor reading yields the mild band and an unset
        occupancy/door sensor stays ``None`` (multiplier 1.0). Never raises.
        """
        hour = dt_util.now().hour
        outdoor_temp_c = self._resolve_outdoor_temp_c()

        # Occupancy is tri-state: only a present attribute yields True/False.
        room = self._get_room_data(vent_id, data)
        occupied_attr = (room.get("attributes") or {}).get("occupied")
        occupied: bool | None
        if occupied_attr is None:
            occupied = None
        elif isinstance(occupied_attr, str):
            occupied = occupied_attr.strip().lower() in {"true", "on", "1"}
        else:
            occupied = bool(occupied_attr)

        # Door state from the per-vent assignment (R12.2/12.3); tri-state None
        # when unset or unavailable.
        assignment = self._get_vent_assignments().get(vent_id, {})
        door_sensor = assignment.get(CONF_DOOR_SENSOR_ENTITY)
        doors_open: bool | None = None
        if door_sensor:
            door_state = self.hass.states.get(door_sensor)
            if door_state and door_state.state not in {STATE_UNKNOWN, STATE_UNAVAILABLE}:
                doors_open = door_state.state == "on"

        return build_context(
            hour=hour,
            outdoor_temp_c=outdoor_temp_c,
            occupied=occupied,
            doors_open=doors_open,
            sun_state=self._resolve_sun_state(),
        )

    def _get_room_effective_rate(self, vent_id: str, hvac_action: str, data: dict[str, Any]) -> float:
        """Context-adjusted effective rate for ``balance`` from the learned model.

        The synchronized-convergence allocator consumes a context-adjusted
        full-open room efficiency ``e_i`` (R25.4). This sources it from the
        per-room :class:`learning.RoomEfficiencyModel`:

        * build the :class:`context.Context` and its ``regime_index``;
        * if a model exists and :func:`learning.effective_rate` returns a
          positive (trusted regime or baseline) rate, apply the bounded
          occupancy/door context multipliers (R12) and return it;
        * otherwise fall back to the legacy effective-rate source
          (:meth:`_get_effective_efficiency_rate`) so the room is never left
          without a rate while the new model is still cold (R22.3).
        """
        mode = "cooling" if hvac_action == HVACAction.COOLING else "heating"
        ctx = self._build_context(vent_id, data)
        room_name = self._get_room_name(vent_id, data)
        room_key = room_name or vent_id
        model = self._room_efficiency_models.get(room_key) if room_name else None
        if model is not None:
            learned = learning_effective_rate(model, context_regime_index(ctx), mode)
            if learned > 0:
                # A7 "Apply": resolve the learned per-room door factor (door-open
                # only; neutral 0.9 when the model is None/cold) and thread it
                # into the bounded context multipliers (R26.1/26.5/27.4).
                df = resolve_door_factor(self._door_factor_models.get(room_key), mode)
                return apply_context_multipliers(learned, ctx, mode, door_factor=df)
        # Fallback: legacy regime/offset model (unchanged behavior).
        legacy_context = self._get_vent_context(vent_id, data)
        return self._get_effective_efficiency_rate(vent_id, hvac_action, context=legacy_context)

    def _update_room_efficiency_model(self, vent_id: str, mode: str, sample: float) -> None:
        """Route one observed full-open rate ``sample`` per door state (R25/A7).

        Builds the current :class:`context.Context` and derives its
        ``regime_index``. The learning write is then split on ``ctx.doors_open``
        (D11):

        * **Door open** — the sample is a *full-open while open* observation. The
          door-closed reference ``ref = effective_rate(room_model, regime, mode)``
          is read *before* the sample is incorporated. WHEN ``ref > 0`` the
          residual ratio ``sample / ref`` is folded into the room's
          :class:`learning.DoorFactorModel` for ``mode`` via
          :func:`learning.update_door_factor` (created on first use), and the
          :class:`learning.RoomEfficiencyModel` is left untouched so the
          reference stays door-closed-clean (R29.1, removing the latent
          double-count). WHEN ``ref <= 0`` no ratio can be formed, so neither
          learner is updated (R28.4).
        * **Door closed / unknown** (``False``/``None``) — the sample is folded
          into the room model via :func:`learning.update_room_efficiency`
          exactly as before; the door learner is untouched (R29.2).

        Keyed by room name so a multi-vent room learns at the group level; falls
        back to the vent id when the room is unnamed.
        """
        data = self.data or {}
        room_key = self._get_room_name(vent_id, data) or vent_id
        ctx = self._build_context(vent_id, data)
        regime = context_regime_index(ctx)

        if ctx.doors_open is True:
            # D11/R29.1: door-open samples train the door-factor learner against
            # the door-closed reference and never mutate the room model.
            room_model = self._room_efficiency_models.get(room_key)
            ref = learning_effective_rate(room_model, regime, mode) if room_model is not None else 0.0
            if ref <= 0:
                # R28.4: no positive reference -> no ratio -> skip both learners.
                return
            door_model = self._door_factor_models.get(room_key)
            if door_model is None:
                door_model = new_door_factor_model()
                self._door_factor_models[room_key] = door_model
            update_door_factor(door_model, sample / ref, mode)
            return

        # R29.2: door-closed/door-unknown -> fold into the room model as today.
        model = self._room_efficiency_models.get(room_key)
        if model is None:
            model = new_room_model()
            self._room_efficiency_models[room_key] = model
        update_room_efficiency(model, sample, regime, mode)

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
                "t": datetime.now(UTC),
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
        windowed = [sample for sample in samples if sample["t"] >= window_start and sample["t"] <= window_end]
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
        median = (
            temps_sorted[mid] if len(temps_sorted) % 2 else (temps_sorted[mid - 1] + temps_sorted[mid]) / 2
        )
        deviations = [abs(temp - median) for temp in temps]
        deviations_sorted = sorted(deviations)
        mad_mid = len(deviations_sorted) // 2
        mad = (
            deviations_sorted[mad_mid]
            if len(deviations_sorted) % 2
            else (deviations_sorted[mad_mid - 1] + deviations_sorted[mad_mid]) / 2
        )
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
        sum_xy = sum(x * y for x, y in zip(xs, ys, strict=False))
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

        duct_values = [float(d) for sample in windowed if (d := sample.get("duct")) is not None]
        rate_norm = rate_room
        if duct_values:
            mean_duct = sum(duct_values) / len(duct_values)
            variance = sum((v - mean_duct) ** 2 for v in duct_values) / len(duct_values)
            stability = variance**0.5
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
        efficiency = max(0.0, rate_eff) if hvac_action == HVACAction.HEATING else max(0.0, -rate_eff)
        if efficiency <= 0:
            return None, None, None
        observed_rate = abs(rate_room)
        return efficiency, observed_rate, mean_aperture

    def _update_efficiency_model(
        self,
        vent_id: str,
        mode: str,
        sample: float,
        context: EfficiencyContext | None = None,
    ) -> tuple[float, float, float]:
        vent_model = self._efficiency_models.setdefault(vent_id, {})
        model = vent_model.setdefault(
            mode,
            {
                "baseline": None,
                "offsets": [0.05 * (i - (EFF_REGIME_COUNT - 1) / 2) for i in range(EFF_REGIME_COUNT)],
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
        alpha = max(EFF_ALPHA_MIN, EFF_ALPHA0 / (n**0.5))
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

        # Context boost: give 2x weight to the regime matching current context
        if context is not None and len(offsets) >= 2:
            preferred = context.regime_index(len(offsets))
            if 0 <= preferred < len(weights):
                weights[preferred] *= 2.0
                total_w = sum(weights) or 1.0
                weights = [w / total_w for w in weights]

        for idx, offset in enumerate(offsets):
            predict = baseline + offset
            offsets[idx] = offset + (EFF_BETA * weights[idx] * (sample - predict))
            offsets[idx] *= 1.0 - EFF_SHRINKAGE

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

    def _get_effective_efficiency_rate(
        self,
        vent_id: str,
        hvac_action: str,
        context: EfficiencyContext | None = None,
    ) -> float:
        mode = "cooling" if hvac_action == HVACAction.COOLING else "heating"
        efficiency_models = getattr(self, "_efficiency_models", {})
        model = (efficiency_models.get(vent_id) or {}).get(mode)
        if model:
            baseline = model.get("baseline")
            offsets = model.get("offsets", [])
            confidence = model.get("confidence", 0.0)
            # Context-aware regime selection
            if (
                context is not None
                and isinstance(baseline, (int, float))
                and baseline > 0
                and offsets
                and confidence >= EFF_REGIME_CONFIDENCE
            ):
                idx = context.regime_index(len(offsets))
                idx = min(idx, len(offsets) - 1)
                return float(baseline + offsets[idx])
            effective = model.get("effective")
            if isinstance(effective, (int, float)) and effective > 0:
                return float(effective)
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
            target = (
                attrs.get("target_temp_high") or attrs.get("cooling_setpoint") or attrs.get("temperature")
            )
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
        export_date = datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
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
            "version": STORE_SCHEMA_VERSION,
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
            "vent_effectiveness": getattr(self, "_vent_effectiveness", {}),
            "room_efficiency": {
                room: room_model_to_dict(model)
                for room, model in getattr(self, "_room_efficiency_models", {}).items()
            },
            "door_factor": {
                room: door_factor_to_dict(model)
                for room, model in getattr(self, "_door_factor_models", {}).items()
            },
        }

    @staticmethod
    def _sanitize_imported_models(models: dict[str, Any]) -> dict[str, Any]:
        """Keep only well-formed efficiency models from imported data.

        A valid entry maps a vent_id to a non-empty dict of modes, where each
        mode is a dict with a numeric-or-None ``baseline`` and (if present) an
        ``offsets`` list of numbers. Malformed entries are dropped so they
        cannot crash the runtime learning code.
        """
        valid: dict[str, Any] = {}
        for vent_id, modes in models.items():
            if not isinstance(modes, dict) or not modes:
                continue
            ok = True
            for mode_val in modes.values():
                if not isinstance(mode_val, dict):
                    ok = False
                    break
                baseline = mode_val.get("baseline")
                if baseline is not None and not isinstance(baseline, (int, float)):
                    ok = False
                    break
                offsets = mode_val.get("offsets")
                if offsets is not None and not (
                    isinstance(offsets, list) and all(isinstance(o, (int, float)) for o in offsets)
                ):
                    ok = False
                    break
            if ok:
                valid[vent_id] = modes
        return valid

    async def async_import_efficiency(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Import Hubitat efficiency export data."""
        if not isinstance(payload, dict):
            raise ValueError("Efficiency payload must be a JSON object")

        data = payload.get("efficiencyData") or payload
        if not isinstance(data, dict):
            raise ValueError("Missing efficiencyData section")

        models = payload.get("efficiencyModels")
        if isinstance(models, dict):
            validated = self._sanitize_imported_models(models)
            if len(validated) != len(models):
                _LOGGER.warning(
                    "Efficiency import: %d model(s) skipped due to invalid format",
                    len(models) - len(validated),
                )
            self._efficiency_models = validated

        # Schema-v2 sections (R25.8): load any learned vent-effectiveness /
        # room-efficiency carried by the payload (sanitized so malformed entries
        # are dropped, never crash). Older payloads omit these; the seeding pass
        # below then back-fills a curve so old exports import cleanly into v2.
        imported_ve = payload.get("vent_effectiveness")
        if isinstance(imported_ve, dict):
            self._vent_effectiveness.update(self._sanitize_vent_effectiveness(imported_ve))
        imported_re = payload.get("room_efficiency")
        if isinstance(imported_re, dict):
            self._room_efficiency_models.update(self._load_room_efficiency(imported_re))
        imported_df = payload.get("door_factor")
        if isinstance(imported_df, dict):
            self._door_factor_models.update(self._load_door_factor(imported_df))

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

        # Bring imported state up to schema v2: seed a curve / room model for any
        # vent/room the payload didn't carry one for (old exports), back-fill the
        # new metric fields. Idempotent, so v2 payloads with learned curves keep
        # them (the seed pass only fills missing entries).
        self._migrate_state_to_v2()

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

    def get_hold_status(self) -> str:
        """Return current hold status: 'holding', 'recalculating', or 'idle'."""
        return self._hold_status

    def get_max_deviation(self) -> float:
        """Return the maximum deviation across all rooms in the current cycle."""
        return round(self._last_max_deviation, 2)

    def get_hold_count(self) -> int:
        """Return count of hold events (polls skipped)."""
        return self._hold_count

    def get_recalc_count(self) -> int:
        """Return count of recalculations in recent history."""
        return self._recalc_count_24h

    def get_hold_ratio(self) -> float:
        """Return percentage of active polls where positions were held."""
        if self._total_active_polls <= 0:
            return 0.0
        return round(100.0 * self._hold_count / self._total_active_polls, 1)

    # --- Task 24 observability getters (R13/R14/R5.4/R25.11) ---------------
    def get_active_room_spread(self) -> float:
        """Current active-room temperature spread in °C (R13.1/R13.2)."""
        return round(self._last_active_spread, 2)

    def get_max_active_error(self) -> float:
        """Largest absolute active-room error vs the setpoint in °C (R14.1)."""
        return round(self._last_max_active_error, 2)

    def get_recalculations_24h(self) -> int:
        """Recalculations in the trailing 24 h (R14.1)."""
        return self._prune_events(self._recalc_events)

    def get_holds_24h(self) -> int:
        """Hold events in the trailing 24 h (R14.1)."""
        return self._prune_events(self._hold_events)

    def get_room_signed_error(self, room_id: str) -> float | None:
        """Per-room signed error toward setpoint; negative == overshoot (R13.3)."""
        return self._room_signed_errors.get(room_id)

    def is_room_airflow_limited(self, room_id: str) -> bool:
        """Whether the room is currently airflow-limited (R5.4)."""
        return room_id in self._airflow_limited_rooms

    def get_room_efficiency_percent(self, room_id: str, mode: str) -> float | None:
        """Representative learned efficiency for a room as a percent (R25.11)."""
        for vent_id, vent in (self.data or {}).get("vents", {}).items():
            if (vent.get("room") or {}).get("id") == room_id:
                return self.get_vent_efficiency_percent(vent_id, mode)
        return None

    def get_room_door_factor(self, room_id: str) -> float | None:
        """Resolved door-leakage multiplier for a room's active mode (R30.1/30.3).

        Returns the value from :func:`learning.resolve_door_factor` for the
        room's active/most-recent conditioning mode, always within
        ``[DOOR_FACTOR_MIN, DOOR_FACTOR_MAX]``. Returns ``None`` when the room
        has no door sensor configured on any of its vents, so the optimizer
        never surfaces a misleading learned factor for a room it cannot observe
        (R30.2).
        """
        info = self._resolve_room_door_factor(room_id)
        return None if info is None else info[0]

    def get_room_door_factor_trusted(self, room_id: str) -> bool | None:
        """Whether the active mode's door-factor cell meets the gate (R30.1).

        ``True`` only when the active mode's cell has ``n >= DOOR_MIN_N`` and a
        learned ``factor`` present; ``False`` while the resolution falls back to
        the default. Returns ``None`` when the room has no door sensor (R30.2).
        """
        info = self._resolve_room_door_factor(room_id)
        return None if info is None else info[1]

    def _resolve_room_door_factor(self, room_id: str) -> tuple[float, bool] | None:
        """Resolve ``(door_factor, trusted)`` for a Flair ``room_id`` (R30).

        Returns ``None`` unless at least one of the room's vents has a door
        sensor configured (``CONF_DOOR_SENSOR_ENTITY``) — the R30.2 gate that
        keeps a sensorless room from surfacing a learned factor. The room key
        mirrors the learning write (room name, falling back to the vent id when
        unnamed) and the active mode is taken from the room thermostat's
        most-recent ``hvac_action`` (``CONF_THERMOSTAT_ENTITY``). The factor is
        always clamped to ``[DOOR_FACTOR_MIN, DOOR_FACTOR_MAX]`` by
        :func:`learning.resolve_door_factor`; the trust flag inspects the active
        mode's cell directly (not the cross-mode fallback), so a learned factor
        that happens to equal the ``0.9`` default is still reported as trusted.
        """
        data = self.data or {}
        assignments = self._get_vent_assignments()
        room_key: str | None = None
        thermostat: str | None = None
        has_door_sensor = False
        for vent_id, vent in (data.get("vents") or {}).items():
            if (vent.get("room") or {}).get("id") != room_id:
                continue
            if room_key is None:
                room_key = self._get_room_name(vent_id, data) or vent_id
            assignment = assignments.get(vent_id) or {}
            if assignment.get(CONF_DOOR_SENSOR_ENTITY):
                has_door_sensor = True
            if thermostat is None and assignment.get(CONF_THERMOSTAT_ENTITY):
                thermostat = assignment.get(CONF_THERMOSTAT_ENTITY)
        if room_key is None or not has_door_sensor:
            return None
        action = self._last_hvac_action.get(thermostat or "")
        mode = "cooling" if action == HVACAction.COOLING else "heating"
        model = self._door_factor_models.get(room_key)
        factor = resolve_door_factor(model, mode)
        trusted = self._door_factor_cell_trusted(model, mode)
        return factor, trusted

    @staticmethod
    def _door_factor_cell_trusted(model: DoorFactorModel | None, mode: str) -> bool:
        """Whether ``mode``'s door-factor cell meets the confidence gate (R30.1).

        Inspects the cell directly — ``n >= DOOR_MIN_N`` with a learned
        ``factor`` present — rather than comparing the resolved value to the
        default, since a learned factor can legitimately equal the ``0.9``
        fallback.
        """
        if model is None:
            return False
        cell = getattr(model, mode, None)
        return bool(cell is not None and cell.factor is not None and cell.n >= DOOR_MIN_N)

    def get_vent_leak(self, vent_id: str, mode: str) -> float:
        """Per-vent learned leakage fraction for diagnostics (R25.11)."""
        action = HVACAction.COOLING if mode == "cooling" else HVACAction.HEATING
        return round(self._get_vent_leak(vent_id, action), 4)

    def _prune_adjustments(
        self, vent_id: str, now: datetime | None = None, window_hours: float = 48.0
    ) -> None:
        events = self._vent_adjustments.get(vent_id)
        if not events:
            return
        if now is None:
            now = datetime.now(UTC)
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

    def _record_vent_adjustment(self, vent_id: str, previous: int, target: int, when: datetime) -> None:
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

    def get_vent_adjustment_stats(self, vent_id: str, window_hours: float = 24.0) -> dict[str, float]:
        if not hasattr(self, "_vent_adjustments") or self._vent_adjustments is None:
            self._vent_adjustments = {}
        now = datetime.now(UTC)
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
        """Surface an error via a persistent notification, coalesced by class.

        R14.5: the ``notification_id`` is derived from a slug of ``title`` (the
        error class), so repeated failures of the same kind UPDATE one
        notification instead of spamming a new one per occurrence. An
        occurrence count is folded into the message for visibility. Distinct
        error classes (different titles) still get distinct notifications.
        """
        slug = _slugify(title)
        notification_id = f"{DOMAIN}_{self.entry.entry_id}_error_{slug}"

        count = self._error_counts.get(notification_id, 0) + 1
        self._error_counts[notification_id] = count
        body = message if count == 1 else f"{message}\n\n(occurred {count} times)"

        persistent_notification.async_create(
            self.hass,
            body,
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
        message = f"{room_name} {rate_prop} efficiency adjusted: " f"{old_percent:.1f}% → {new_percent:.1f}%"

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
            # Serialize cycle targets for restart resilience
            serialized_cycle_targets: dict[str, dict[str, Any]] = {}
            for thermo, ct in self._cycle_targets.items():
                serialized_cycle_targets[thermo] = {
                    "targets": ct.get("targets", {}),
                    "initial_temps": ct.get("initial_temps", {}),
                    "predicted_rates": ct.get("predicted_rates", {}),
                    "cycle_start": ct["cycle_start"].isoformat() if ct.get("cycle_start") else None,
                    "recalc_count": ct.get("recalc_count", 0),
                    "last_recalc": ct["last_recalc"].isoformat() if ct.get("last_recalc") else None,
                    "adjustment_batches": ct.get("adjustment_batches", 0),
                }
            await self._store.async_save(
                {
                    "version": STORE_SCHEMA_VERSION,
                    "vent_rates": self._vent_rates,
                    "max_rates": self._max_rates,
                    "max_running_minutes": self._max_running_minutes,
                    "vent_models": self._vent_models,
                    "efficiency_models": self._efficiency_models,
                    "vent_adjustments": self._vent_adjustments,
                    "strategy_metrics": self._strategy_metrics,
                    "room_efficiency": {
                        room: room_model_to_dict(model)
                        for room, model in self._room_efficiency_models.items()
                    },
                    "door_factor": {
                        room: door_factor_to_dict(model) for room, model in self._door_factor_models.items()
                    },
                    "vent_effectiveness": self._vent_effectiveness,
                    "last_hvac_action": self._last_hvac_action,
                    "pre_adjust_flags": self._pre_adjust_flags,
                    "cycle_targets": serialized_cycle_targets,
                    "hold_count": self._hold_count,
                    "recalc_count_24h": self._recalc_count_24h,
                    "total_active_polls": self._total_active_polls,
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
