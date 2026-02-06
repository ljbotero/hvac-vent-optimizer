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
CONF_CLOSE_INACTIVE_ROOMS = "close_inactive_rooms"
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
DEFAULT_CLOSE_INACTIVE_ROOMS = True
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
DEFAULT_CONTROL_STRATEGY = "hybrid"
DEFAULT_MIN_ADJUSTMENT_PERCENT = 10
DEFAULT_MIN_ADJUSTMENT_INTERVAL = 30
DEFAULT_TEMP_ERROR_OVERRIDE = 0.6

PLATFORMS: list[str] = ["cover", "sensor", "binary_sensor", "switch", "climate", "number"]
