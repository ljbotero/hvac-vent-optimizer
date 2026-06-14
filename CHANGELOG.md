# Changelog

All notable changes to the HVAC Vent Optimizer custom integration are documented
in this file. The format is loosely based on [Keep a Changelog](https://keepachangelog.com/).

## [Unreleased]

### Added — Learned per-room door-leakage multiplier (`door-leakage-learning` spec)

Replaces the single hardcoded `context.DOOR_FACTOR = 0.9` open-door discount with a
**learned, per-room, per-mode door-leakage multiplier** — a bounded multiplicative
residual on each room's door-closed reference rate. (Continues the
`hvac-vent-balancing` spec: Requirements 26–30, decisions D10–D12, algorithm A7,
Properties 14–18.)

- **Per-room/per-mode learning (`learning.py`, pure/HA-free):** new
  `DoorFactorCell` / `DoorFactorModel`, `new_door_factor_model()`,
  `update_door_factor()`, `resolve_door_factor()`, and the
  `door_factor_to_dict()` / `door_factor_from_dict()` converters, plus the
  `DOOR_FACTOR_MIN=0.5`, `DOOR_FACTOR_MAX=1.0`, `DOOR_FACTOR_DEFAULT=0.9`, and
  `DOOR_MIN_N=5` constants. The door factor is learned online with the same
  adaptive-alpha EMA used by the room learner and gated by a sample-count
  confidence threshold.
- **Fallback chain (D12):** resolution order is this-room/this-mode learned →
  this-room/other-mode learned → legacy `0.9` default; every result is clamped
  to `[0.5, 1.0]`, so an open door can only slow (never speed) conditioning.
- **Graceful cold start (R27.4):** below the confidence gate (and with no model)
  the resolved factor is exactly `0.9`, so a cold install is bit-for-bit
  equivalent to prior behavior.
- **Pure context seam (`context.py`):** `apply_context_multipliers()` gains an
  optional `door_factor` parameter; `None` falls back to the module
  `DOOR_FACTOR` constant. The module stays pure and knows nothing about the
  learner. Module docstring updated to document the learned door term.
- **Coordinator wiring (`coordinator.py`):** holds `self._door_factor_models`
  keyed by room name (vent id fallback); resolves and threads the learned factor
  through `_get_room_effective_rate`; and **splits the learning write** so
  door-open full-open samples feed only the door learner while the
  `RoomEfficiencyModel` reference stays door-closed-clean. This closes a latent
  double-count (door-open samples previously lowered the room baseline *and*
  were discounted again at read time).
- **Persistence / migration / export-import (R29):** additive `door_factor`
  store section serialized via the tolerant converters; a pre-feature store with
  no section loads to neutral (`0.9`) resolution and round-trips losslessly;
  malformed input is dropped without affecting other sections. No
  `STORE_SCHEMA_VERSION` bump (additive, consistent with `room_efficiency` /
  `vent_effectiveness`). Included in `export_efficiency` / `import_efficiency`
  with backward compatibility.
- **Observability (`sensor.py`, R30):** rooms with a configured door sensor
  expose the resolved `door_factor` and a `door_factor_trusted` flag for the
  active mode; rooms without a door sensor surface no misleading learned value.
- **Tests:** new `tests/test_learning_doorfactor.py` and
  `tests/test_sensor_door_factor.py`; extended context, coordinator, persistence,
  import, simulator, and property suites. Property tests 14–18 encode the
  bounded/non-amplifying, cold-start-equivalent, per-mode-independent, stable,
  and reference-clean invariants.

### Out of scope / future work

- Learning the occupancy multiplier (`OCC_FACTOR = 0.9`) is deferred (weaker
  observability — occupancy correlates with HVAC demand). This feature touches
  the door path only and leaves the occupancy multiplier unchanged.
