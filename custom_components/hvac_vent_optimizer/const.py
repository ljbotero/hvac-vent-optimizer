"""Constants for HVAC Vent Optimizer integration."""

from __future__ import annotations

DOMAIN = "hvac_vent_optimizer"

CONF_VENT_BRAND = "vent_brand"
CONF_MANUAL_VENT_COUNT = "manual_vent_count"
CONF_MANUAL_VENTS = "manual_vents"

BRAND_FLAIR = "flair"
BRAND_MANUAL = "manual"

CONF_CLIENT_ID = "client_id"
CONF_CLIENT_SECRET = "client_secret"
CONF_STRUCTURE_ID = "structure_id"
CONF_STRUCTURE_NAME = "structure_name"
CONF_ENTRY_ID = "entry_id"

CONF_DAB_ENABLED = "dab_enabled"
# Inactive-room airflow handling. The option is framed positively as "open vents
# in rooms marked inactive" and defaults to ON (keep inactive vents open). This
# is safer for forced-air duct static pressure and keeps room-efficiency learning
# fresh for inactive rooms. The legacy ``close_inactive_rooms`` key (inverse
# meaning) is kept for back-compat reads + the v2->v3 migration only.
CONF_OPEN_INACTIVE_ROOMS = "open_inactive_rooms"
CONF_CLOSE_INACTIVE_ROOMS = "close_inactive_rooms"  # legacy (pre-v3); migrated to CONF_OPEN_INACTIVE_ROOMS
CONF_VENT_GRANULARITY = "vent_granularity"
CONF_POLL_INTERVAL_ACTIVE = "poll_interval_active"
CONF_POLL_INTERVAL_IDLE = "poll_interval_idle"
CONF_DAB_FORCE_MANUAL = "dab_force_manual"
CONF_INITIAL_EFFICIENCY_PERCENT = "initial_efficiency_percent"
CONF_NOTIFY_EFFICIENCY_CHANGES = "notify_efficiency_changes"
CONF_LOG_EFFICIENCY_CHANGES = "log_efficiency_changes"
CONF_CONTROL_STRATEGY = "control_strategy"
CONF_MIN_ADJUSTMENT_PERCENT = "min_adjustment_percent"
CONF_MIN_ADJUSTMENT_INTERVAL = "min_adjustment_interval"
CONF_TEMP_ERROR_OVERRIDE = "temp_error_override_c"

CONF_DEADBAND_PERCENT = "deadband_percent"
CONF_DEVIATION_THRESHOLD = "deviation_threshold_c"
CONF_MAX_RECALC_PER_CYCLE = "max_recalc_per_cycle"
CONF_MAX_ADJUSTMENT_BATCHES_PER_CYCLE = "max_adjustment_batches_per_cycle"
CONF_MAX_ADJUSTMENT_BATCHES_PER_WINDOW = "max_adjustment_batches_per_window"
CONF_ADJUSTMENT_WINDOW_MINUTES = "adjustment_window_minutes"
# Idle gap (minutes) below which a re-activation is treated as a continuation of
# the prior cycle (the anchored allocation is reused rather than recomputed) to
# avoid churn when the thermostat short-cycles (R7.8). The options-flow field +
# translation are wired in Task 23; this is the seam/default until then.
CONF_SHORT_CYCLE_GAP_MIN = "short_cycle_gap_min"

# --- balance (DAB v2) strategy tunables (Task 23, R18.1) -------------------
# Option keys deliberately equal their ``balance.AllocSettings`` field names so
# the coordinator can read them straight into the allocator. Defaults/ranges
# come from the design "New configuration" table; the options flow validates and
# clamps every value to its documented range (R18.2).
CONF_SAFETY_FLOOR_PCT = "safety_floor_pct"
CONF_SPREAD_GUARDRAIL_C = "spread_guardrail_c"
CONF_SPREAD_IMPROVEMENT_DEADBAND_C = "spread_improvement_deadband_c"
CONF_CROSSCOUPLING_ENABLED = "crosscoupling_enabled"
CONF_AIRFLOW_LIMITED_MARGIN_PCT = "airflow_limited_margin_pct"
CONF_AIRFLOW_LIMITED_ERROR_C = "airflow_limited_error_c"

CONF_DOOR_SENSOR_ENTITY = "door_sensor_entity"

# Optional outdoor-temperature / weather source for context-aware learning
# (R12.5). A ``sensor.*`` (numeric outdoor temperature) or a ``weather.*`` entity
# (temperature read from its ``temperature`` attribute) is accepted; when unset
# or unavailable the outdoor band degrades gracefully to "mild". The options-flow
# field + translation are wired in Task 23; this is the documented config-key
# seam (read from ``entry.options``) until then.
CONF_OUTDOOR_TEMP_ENTITY = "outdoor_temp_entity"

CONF_VENT_ASSIGNMENTS = "vent_assignments"
CONF_THERMOSTAT_ENTITY = "thermostat_entity"
CONF_TEMP_SENSOR_ENTITY = "temp_sensor_entity"
CONF_CONVENTIONAL_VENTS_BY_THERMOSTAT = "conventional_vents_by_thermostat"
CONF_ROOM_ID = "room_id"
CONF_VENT_ID = "vent_id"
CONF_ACTIVE = "active"
CONF_SET_POINT_C = "set_point_c"
CONF_HOLD_UNTIL = "hold_until"
CONF_STRUCTURE_MODE = "structure_mode"
CONF_EFFICIENCY_PATH = "efficiency_path"
CONF_EFFICIENCY_PAYLOAD = "efficiency_payload"

SERVICE_SET_ROOM_ACTIVE = "set_room_active"
SERVICE_SET_ROOM_SETPOINT = "set_room_setpoint"
SERVICE_RUN_DAB = "run_dab"
SERVICE_SET_STRUCTURE_MODE = "set_structure_mode"
SERVICE_REFRESH_DEVICES = "refresh_devices"
SERVICE_EXPORT_EFFICIENCY = "export_efficiency"
SERVICE_IMPORT_EFFICIENCY = "import_efficiency"

DEFAULT_DAB_ENABLED = False
# Inactive vents stay OPEN by default (R: see CONF_OPEN_INACTIVE_ROOMS). The
# legacy default below (close=ON) is only used by the v2->v3 migration to
# preserve the prior behaviour of installs that never set the option.
DEFAULT_OPEN_INACTIVE_ROOMS = True
DEFAULT_CLOSE_INACTIVE_ROOMS = True  # legacy (pre-v3) default; migration use only
DEFAULT_VENT_GRANULARITY = 5
DEFAULT_POLL_INTERVAL_ACTIVE = 3
DEFAULT_POLL_INTERVAL_IDLE = 10
DEFAULT_CONVENTIONAL_VENTS = 0
MAX_CONVENTIONAL_VENTS = 50
DEFAULT_MANUAL_VENT_COUNT = 1
DEFAULT_DAB_FORCE_MANUAL = True
DEFAULT_INITIAL_EFFICIENCY_PERCENT = 50
DEFAULT_NOTIFY_EFFICIENCY_CHANGES = True
DEFAULT_LOG_EFFICIENCY_CHANGES = True
# The ``balance`` (DAB v2) synchronized-convergence strategy. Registered here as
# a selectable value so the coordinator can branch on it (Task 15).
CONTROL_STRATEGY_BALANCE = "balance"
# ``balance`` is the default for NEW installs (Task 27, R16.1/R17.1). The R15.6
# spread evidence gate was not met, but the homeowner explicitly accepted the
# change on the decisive vent-movement win (balance uses 16-93 % of dab's moves)
# with the spread criterion waived — see ``docs/quality-baseline.md`` and
# ``docs/usage-balance-ab.md``. Existing installs are NOT silently flipped:
# ``async_migrate_entry`` pins the legacy default below for pre-``balance``
# entries that never explicitly chose a strategy (R17.3).
DEFAULT_CONTROL_STRATEGY = CONTROL_STRATEGY_BALANCE
# The pre-``balance`` implicit default. Used by the config-entry migration to
# preserve the running behaviour of installs that upgraded without ever having
# explicitly selected a strategy (R17.3).
LEGACY_DEFAULT_CONTROL_STRATEGY = "hybrid"
# Allowed control strategies (the options-flow dropdown + translations are wired
# in Task 23). Ordered so the legacy strategies stay first and ``balance`` is the
# newest selectable option.
CONTROL_STRATEGIES = ["dab", "cost", "stats", "hybrid", CONTROL_STRATEGY_BALANCE]
DEFAULT_DEADBAND_PERCENT = 15
DEFAULT_DEVIATION_THRESHOLD = 1.0
DEFAULT_MAX_RECALC_PER_CYCLE = 3
DEFAULT_MAX_ADJUSTMENT_BATCHES_PER_CYCLE = 3
DEFAULT_MAX_ADJUSTMENT_BATCHES_PER_WINDOW = 4
DEFAULT_ADJUSTMENT_WINDOW_MINUTES = 120
DEFAULT_SHORT_CYCLE_GAP_MIN = 10
DEFAULT_MIN_ADJUSTMENT_PERCENT = 10
DEFAULT_MIN_ADJUSTMENT_INTERVAL = 30
DEFAULT_TEMP_ERROR_OVERRIDE = 1.0

# balance (DAB v2) tunable defaults + documented validation ranges (Task 23,
# R18.1/R18.2). Each ``*_RANGE`` is the inclusive ``(min, max)`` the options
# flow clamps to. Defaults mirror ``balance.AllocSettings`` so the UI and the
# allocator agree out of the box.
DEFAULT_SAFETY_FLOOR_PCT = 40
DEFAULT_SPREAD_GUARDRAIL_C = 1.0
DEFAULT_SPREAD_IMPROVEMENT_DEADBAND_C = 0.3
DEFAULT_CROSSCOUPLING_ENABLED = True
DEFAULT_AIRFLOW_LIMITED_MARGIN_PCT = 5
DEFAULT_AIRFLOW_LIMITED_ERROR_C = 0.5

SAFETY_FLOOR_PCT_RANGE = (20, 90)
SPREAD_GUARDRAIL_C_RANGE = (0.2, 5.0)
SPREAD_IMPROVEMENT_DEADBAND_C_RANGE = (0.0, 2.0)
AIRFLOW_LIMITED_MARGIN_PCT_RANGE = (0, 20)
AIRFLOW_LIMITED_ERROR_C_RANGE = (0.1, 3.0)
SHORT_CYCLE_GAP_MIN_RANGE = (0, 60)

PLATFORMS: list[str] = ["cover", "sensor", "binary_sensor", "switch", "climate", "number"]
