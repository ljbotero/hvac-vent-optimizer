# HVAC Vent Optimizer - AI Development Guide

> This file provides context for any AI assistant or code-generation tool working on this codebase.

## Project overview

A Home Assistant custom integration that optimizes HVAC vent positions to balance airflow across rooms. Two operating modes:

- **Flair mode**: Controls [Flair](https://flair.co) smart vents, pucks, and rooms via their REST API using OAuth2 client credentials.
- **Manual mode**: No smart vents required. Users define vents with thermostat + temp sensor assignments and receive calculated aperture recommendations.

Domain: `hvac_vent_optimizer` | Version: `0.1.0` | Owner: `@ljbotero`

### What the optimizer is actually optimizing (read this first)

The control objective is **minimize the temperature spread (hottest − coldest) across *active* rooms**, anchored on the single shared thermostat setpoint, while minimizing vent movements and never dropping below an inviolable airflow-safety floor. It is **not** minimizing each room's absolute deviation from setpoint independently — doing that is what caused the original ~3 °C structural spread (the most efficient room overcools and ends the cycle before the laggard room catches up).

The current default strategy, **`balance` (a.k.a. "DAB v2")**, implements this via **synchronized convergence**: every active room is throttled to finish reaching the setpoint at the *same time as the slowest (bottleneck) room*, so no room overcools/overheats early and the spread collapses.

The full requirements, design, and task breakdown live in the Kiro spec (authoritative — see "Design docs" below):

```
.kiro/specs/hvac-vent-balancing/{requirements.md, design.md, tasks.md}
```

---

## Architecture: pure modules + thin coordinator

The decision logic is split into **dependency-free pure modules** (zero Home Assistant imports) that mirror the long-standing `dab.py` pattern. This is what makes the algorithm unit-testable in isolation and reusable by the offline simulator (which cannot import the HA runtime). The coordinator is **orchestration only**: it reads HA state into plain dataclasses, calls the pure modules, dispatches vent commands, persists state, and manages the cycle lifecycle and listeners.

```
Pure (no HA imports — testable standalone with plain pytest):
  dab.py        Legacy DAB exponential-curve math (used by dab/cost/stats/hybrid).
  balance.py    NEW. The `balance` strategy: synchronized-convergence allocation,
                the single airflow safety-floor choke point (apply_safety_floor),
                airflow-limited detection, cross-coupling guard, movement gating.
  learning.py   NEW. Room dual-index (heat/cool) + regime efficiency learning,
                per-vent aperture->airflow VentCurve with learned leakage/knee,
                multi-vent room-group learning.
  context.py    NEW. Ambient context -> regime mapping (time-of-day, outdoor band,
                occupancy, door) as a small 4-regime lookup, plus bounded multipliers.
  simulator.py  NEW. Offline closed-loop thermal simulator + multi-strategy
                comparison runner (CLI-runnable, deterministic, HA-free).

Orchestration / HA-coupled:
  __init__.py     Entry setup: API client, coordinator, services, platforms,
                  options + config-entry (v1->v2) migration.
  coordinator.py  Central orchestrator. Polling, strategy dispatch
                  (_compute_balance_targets / _compute_legacy_targets), learning &
                  context sampling, cycle lifecycle, persistence, observability,
                  thermostat listeners. Extends DataUpdateCoordinator.
  api.py          Flair REST client with OAuth2 token lifecycle and rate limiting.
  config_flow.py  Setup wizard + options flow (brand, OAuth, vent assignments,
                  algorithm tuning + balance tunables, context sources,
                  conventional vent counts).
  services.py     Registered HA services (set_room_active, set_room_setpoint,
                  set_structure_mode, run_dab, refresh_devices,
                  export_efficiency, import_efficiency).
  services.yaml   Service descriptions for the HA UI.
  const.py        All constants, config keys, defaults, validation ranges, platforms.
  utils.py        AsyncRateLimiter, unit conversion, remote sensor ID extraction.

Platform entity files:
  cover.py         Flair vents as cover entities (open/close/set position)
  sensor.py        Puck/vent/room sensors, DAB strategy metrics, spread &
                   observability sensors, manual suggested aperture
  binary_sensor.py Puck room occupancy + per-room airflow-limited indicator
  switch.py        Room active/away toggle
  climate.py       Room setpoint control as a climate entity
  number.py        Manual vent aperture input (persisted via RestoreNumber)

translations/en.json  UI strings for config and options flows

Tooling / quality:
  pyproject.toml        ruff + black + mypy config (tooling target py313)
  .pre-commit-config.yaml  pre-commit hooks (lint/format/type gates)
  requirements_test.txt    test deps (pytest, hypothesis, ...)
  pytest.ini               pytest config
  .github/workflows/quality.yml  CI lint/format/type/test gates
```

---

## Key concepts

### Control strategies

Configured via `CONF_CONTROL_STRATEGY`. `balance` is the default for **new** installs; existing installs keep their explicitly chosen strategy (see "Rollout & migration").

| Strategy  | Description |
|-----------|-------------|
| `balance` | **Default (DAB v2).** Synchronized-convergence allocation in `balance.py`. Treats active rooms as competing for one safety-bounded air budget and minimizes active-room spread. |
| `dab`     | Legacy exponential airflow curve from `dab.py` |
| `cost`    | Linear target with movement and open-percentage penalties |
| `stats`   | Learned linear regression model per vent |
| `hybrid`  | Evaluates `dab`/`cost`/`stats`, picks the lowest-cost result (the pre-`balance` default) |

### Synchronized-convergence allocation (`balance`)

The core control law in `balance.allocate()`:

1. **Classify** — a room at/past setpoint (within a ~0.3 °C hysteresis band) is *satisfied* and allocated 0 % (overshoot close).
2. **Bottleneck horizon** — for each unsatisfied room, finish time `tau_i = err_i / rate_i(knee_i)`; `tau* = max tau_i`. The slowest room is the bottleneck and runs at its knee (≈ full effective airflow).
3. **Throttle the rest to finish at `tau*`** so every room arrives together (no early overcooling). Apertures come from inverting the learned vent curve over the feasible flow band.
4. **Safety floor** (`apply_safety_floor`) raises apertures only if combined airflow is below the floor.
5. **Round** to granularity and emit targets + diagnostics (`predicted_spread_c`, `predicted_finish_min`, `airflow_limited`, `floor_binding`).

Supporting pieces in `balance.py`: airflow-limited detection (A3), the cross-coupling guard that closes satisfied rooms to feed an airflow-limited laggard (A4, optional duct signals), and `should_apply()` movement gating (A5).

### Movement gating (spread guardrail + improvement deadband)

A new allocation is only **commanded** when predicted spread exceeds `spread_guardrail_c` (default 1.0 °C) AND the move improves predicted spread by at least `spread_improvement_deadband_c` (default 0.3 °C) — UNLESS a vent must open to reach the safety floor (always immediate). Existing anti-chatter (min interval, min percent, position deadband, per-cycle/per-window batch caps) still applies, at room-**group** granularity. No commands while `hvac_action` is idle/fan except the bounded pre-adjust path and floor-reaching moves. Short idle gaps (< `short_cycle_gap_min`) reuse the prior cycle anchor instead of recomputing.

### Learning subsystem (`learning.py` + `context.py`)

The thermal model separates **room efficiency** from **vent effectiveness**: `observed_rate = e_room(mode, regime) * flow_vent(aperture)`.

- **Room efficiency** — per room, per mode (cooling/heating): a baseline adaptive-alpha EMA plus a small bounded set of **regime cells**. A regime's learned rate is selected once it has `>= REGIME_MIN_N` samples (a reachable sample-count gate — fixes the old unreachable confidence gate).
- **Context regimes** — `context.py` folds time-of-day + outdoor band (cold/mild/hot) into 4 regimes (`day-mild, day-hot, night-mild, night-hot`). Occupancy and open-door state apply as bounded secondary multipliers, not extra regimes. Missing outdoor source degrades gracefully to the mild band.
- **Vent effectiveness** — a learned, monotonic, saturating `VentCurve` per vent/mode over breakpoints `[0,5,10,20,35,50,75,100]%`, with `flow(0)=leak` and `flow(100%)=1`. Exposes `flow(a)`, `inverse(f)`, and `knee()` (the effective-max aperture beyond which airflow barely rises). Cold-starts from the existing rate-vs-aperture regression and refines online (isotonic-clamped to stay monotonic).
- **Multi-vent rooms** — a room served by ≥2 vents is one logical group: vents share a target and learning is done at the combined-flow group level (per-vent curves default equal until independent data exists).

### Adaptive polling

Two-tier: active interval (default 3 min) when any thermostat is heating/cooling, idle interval (default 10 min) otherwise. Switches via thermostat state-change listeners.

### Observability (spread blind-spot fix)

The integration now measures the true comfort outcome (spread), not just an averaged one-sided error:

- `sensor.dab_active_room_spread` (°C), `sensor.dab_max_active_error` (°C)
- `sensor.dab_recalculations_24h`, `sensor.dab_holds_24h`, hold/recalc counters & ratio
- `binary_sensor.<room>_airflow_limited` (room pinned near max yet off-target)
- Per-room sensor attributes: `signed_error_c`, `airflow_limited`, cooling/heating efficiency %
- Per-vent efficiency-sensor attribute: learned `leak`
- Per-strategy metrics for A/B: `avg_spread`, `max_spread`, `time_above_guardrail_min` (alongside the existing adjustment/movement/error metrics)

### Offline simulator & evidence gate

`simulator.py` is a deterministic, HA-free, CLI-runnable closed-loop thermal simulator. It runs any strategy against the same scenario, honors the safety floor, and reports avg/max spread, time-above-guardrail, total moves, moves/room, and avg/max active error. It is the gate that proved `balance` before it shipped, and is the place to validate any algorithm change.

```
python -m custom_components.hvac_vent_optimizer.simulator --compare
```

---

## Development conventions

### Runtime, language, tooling
- **Python 3.14+** at runtime (matches Home Assistant OS / Core). Lint/format/type tooling targets **py313** (highest the pinned black release supports) — see `pyproject.toml`.
- **Fully async** for all I/O: `aiohttp` for HTTP, `asyncio` for concurrency. Never block.
- **Pure modules import no Home Assistant.** Keep `dab.py`, `balance.py`, `learning.py`, `context.py`, `simulator.py` HA-free so they stay standalone-testable and simulator-usable.
- **Strict TDD + quality gates.** Write the failing test/repro first. `ruff`, `black --check`, and `mypy` run in pre-commit and CI; do not increase a file's warning count.

### Data flow
- Coordinator stores fetched data as `{"vents": {id: {...}}, "pucks": {id: {...}}}`.
- Platform entities read from `self.coordinator.data` and derive state via properties; all extend `CoordinatorEntity`.
- The coordinator gathers HA state into the pure-module dataclasses (`balance.RoomAllocInput`, `learning`/`context` inputs), calls the pure functions, then dispatches commands.

### Naming patterns
- **Entity display names**: `{device_name} {sensor_name}`
- **Unique IDs**: `{entry_id}_{type}_{device_id}_{key}`
- **Config constants**: `CONF_*` with matching `DEFAULT_*` (and `*_RANGE` for validated numerics) in `const.py`. `balance` tunable option keys deliberately equal their `balance.AllocSettings` field names so the coordinator can read them straight into the allocator.

### Error handling
- Broad `except Exception` (`# noqa: BLE001`) is intentional at service/update boundaries to surface errors via HA persistent notifications rather than crashing. Inner/library code uses specific exceptions.
- A room with missing/unavailable temp or efficiency is excluded from allocation and spread, never crashes. Repeated error notifications are coalesced by error class (no per-occurrence spam).

### Logging
- Each module defines `_LOGGER = logging.getLogger(__name__)`. Debug for routine ops, warning for recoverable failures, error/exception for service failures. The safety-floor last-resort branch logs its reason.

### Temperature units
- All internal calculations use **Celsius**. Fahrenheit conversion only at system boundaries. Use `utils.is_fahrenheit_unit()` for detection.

### State persistence
- DAB/learned state persists via HA `Store` at `{DOMAIN}_{entry_id}_dab.json` — now **schema v2** (room efficiency dual-index + regimes, per-vent effectiveness curves + leak + knee, spread strategy metrics). The v1→v2 migrator runs in `async_initialize`, is idempotent, and never discards data on parse failure. Import/export carry `version` and load older payloads with defaults.
- Manual vent apertures persist via `RestoreNumber`.

### Rate limiting
- Flair API client uses `AsyncRateLimiter`: 4 req/sec standard, 1 req/sec search endpoints.

### Rollout & migration
- `balance` is the default for **new** installs only. The config-entry migration (`async_migrate_entry`, v1→v2) pins the legacy default (`hybrid`) for existing installs that never explicitly chose a strategy, so upgrades don't silently change behavior. An explicitly chosen strategy is always preserved.

---

## Recipes for common changes

### Changing the `balance` algorithm

1. Edit the pure function(s) in `balance.py` (keep it HA-free and deterministic).
2. Add/extend unit + property tests (`tests/test_balance_*.py`) — write the failing test first.
3. Validate against the simulator (`tests/test_simulator.py` and the `--compare` CLI) so spread/movement don't regress.
4. The coordinator consumes results via `_compute_balance_targets`; only touch it for new inputs/outputs.

### Adding a new sensor
1. Define a description in the appropriate tuple in `sensor.py`.
2. If it needs new coordinator data, add a getter method to `coordinator.py`.
3. The existing entity class + `CoordinatorEntity` pattern handles the rest.

### Adding a new service
1. Add `SERVICE_*` to `const.py`.
2. Define a voluptuous schema + async handler in `services.py`.
3. Register/unregister in `async_register_services()` / `async_unregister_services()`.
4. Add a description to `services.yaml`.

### Adding a new config option
1. Add `CONF_*` + `DEFAULT_*` (and a `*_RANGE` if numeric/validated) to `const.py`.
2. Add the field to the appropriate options-flow step in `config_flow.py` (validate/clamp to range).
3. Add the translation string to `translations/en.json`.
4. Read via `entry.options.get(CONF_*, DEFAULT_*)` in `coordinator.py`. For a `balance` tunable, surface it through `_balance_gate_settings` into `AllocSettings`.

---

## Design docs / optimization objectives

The **authoritative** spec for the current `balance`/DAB v2 work is the Kiro spec:

```
.kiro/specs/hvac-vent-balancing/requirements.md   Requirements R1–R25, decisions D1–D9, success targets
.kiro/specs/hvac-vent-balancing/design.md         Architecture, algorithms A1–A6, learning subsystem,
                                                   data models, correctness Properties 1–13, test strategy
.kiro/specs/hvac-vent-balancing/tasks.md          Implementation tasks + status
```

Supporting docs in the repo:

```
docs/design/00-INDEX.md          Index / reading order (the original movement-optimization analysis)
docs/design/01-DATA-ANALYSIS.md  Observed behavior from recorder data
docs/design/02-ROOT-CAUSES.md    Diagnosed root causes
docs/design/03-DESIGN.md         Proposed design
docs/design/04-TASKS.md          Implementation tasks
docs/quality-baseline.md         Captured lint/type baseline + the evidence-gate outcome
docs/usage-balance-ab.md         How to run the live A/B comparison and read the results
```

Treat these as authoritative project context for algorithm work. Primary objectives, in order:

1. **Minimize active-room spread** — the key comfort metric is the temperature **difference between active rooms**, anchored on the shared setpoint (not absolute deviation).
2. **Minimize vent movements** — fewer moves preserve battery life and reduce motor noise.
3. **Find optimal steady-state hold positions** — converge on apertures that can be held, not constantly retuned.

> Note on the rollout decision: the simulator's spread evidence gate (R15.6) was not fully met, but `balance` was made the default on the decisive vent-movement win (it uses ~16–93 % of `dab`'s moves) with the spread criterion explicitly waived by the homeowner. See `docs/quality-baseline.md` and `docs/usage-balance-ab.md`.

### Hardware / topology

- **Thermostat:** Ecobee, upstairs zone — entity `climate.upstairs_motion_and_temp` (reports `hvac_action`). 4 conventional (always-open) vents configured on this thermostat.
- **Vents:** Flair smart vents + pucks. Rooms with vents:
  - Master Bedroom (2 vents — a multi-vent group)
  - Matias
  - Mariana (chronic laggard / airflow-limited; learned cooling efficiency ~0.017)
  - Tomas
  - Main Bathroom (most efficient; tends to overcool)
  - Guest Room
  - Game Room

---

## Remote instance & operational environment

This component lives on a **remote** Home Assistant instance, which is the single source of record.
Connect with `ssh ha`; the config root is `/homeassistant` and this component is at
`/homeassistant/custom_components/hvac_vent_optimizer/`. Local copies are scratch only — push changes
back to the remote to take effect. The following facts about `ssh ha` are verified; fold them in so
they don't have to be rediscovered the hard way.

### The `ssh ha` shell (Advanced SSH & Web Terminal add-on)
- Lands in a **minimal Alpine add-on container** — NOT the HA Core container and NOT the host.
- **No `python3`** and **no `sqlite3`** are preinstalled. **`apk` IS available**:
  `apk add --no-cache sqlite`. The add-on filesystem is **ephemeral** — anything `apk`-installed is lost
  on add-on restart; reinstall as needed.
- `$SUPERVISOR_TOKEN` is present but scoped to the **Supervisor API only**.

### What works vs what doesn't
- **Config validation:** `ha core check` works (Supervisor API). Always run it before reloading/restarting.
- **Core API proxy is BLOCKED (401):** you CANNOT call HA services (e.g. `script/reload`,
  `automation/reload`) or pull `/api/history` from SSH.
- **Applying config changes:** preferred zero-downtime path is reloading from the HA UI
  (Developer Tools → YAML → Reload). From SSH the only mechanism is a full restart: `ha core restart`.
- **Backups:** write to `/homeassistant/*.bak.<timestamp>` (config root). Files there are NOT auto-loaded
  as config, so they are safe to keep.

### Recorder DB (read history from SSH)
- SQLite at `/homeassistant/home-assistant_v2.db` (multi-GB, WAL mode, `-wal`/`-shm` sidecars).
  Recorder config: `commit_interval: 5`, `purge_keep_days: 30`. Too large to copy locally — query
  **in place, read-only**:
  ```
  sqlite3 "file:/homeassistant/home-assistant_v2.db?mode=ro" ".timeout 5000" "<SQL>"
  ```
- **Modern schema** (HA 2022.4+):
  - `states_meta(metadata_id, entity_id)` — map entity_id → metadata_id first.
  - `states(state_id, metadata_id, state, last_updated_ts, old_state_id, attributes_id, ...)` —
    `last_updated_ts` is a **Unix epoch float** (use `datetime(last_updated_ts,'unixepoch','localtime')`).
  - Join `states` → `states_meta` on `metadata_id`. Attributes JSON in `state_attributes(attributes_id, shared_attrs)`.

### Running tests
- Run with **pytest** from the component dir (`pytest.ini`, `conftest.py`, `tests/` present). Test deps in `requirements_test.txt`.
- Pure-module tests (`test_balance_*`, `test_learning_*`, `test_context`, `test_simulator`) need no HA stubs and run fast standalone. Coordinator tests reuse the HA fakes in `tests/`.
- Property-based tests use **Hypothesis** (`test_*_properties.py`) and assert design Properties 1–13.
- Run the simulator comparison directly: `python -m custom_components.hvac_vent_optimizer.simulator --compare`.

---

## Safety constraints

These invariants must be preserved in any change:

- **Inviolable airflow safety floor**: `balance.apply_safety_floor()` is the **single choke point** every `balance` dispatch routes through (`_compute_balance_targets` calls it; the legacy path keeps `dab.adjust_for_minimum_airflow()`). The combined open percentage — smart targets + conventional vents at their default open + held-open inactive vents — must never drop below the floor (default **40 %**, configurable 20–90) while the HVAC is active. The floor is enforced on **commanded aperture only** and is never relaxed by assumed leakage. Padding biases added airflow toward the rooms that most need conditioning and never reopens satisfied rooms; repositioning inactive rooms is a logged last resort only. Do not add any code path that can bypass this.
- **Overshoot close (all strategies)**: a room at/past setpoint in the conditioning direction computes a 0 % target in `dab`, `cost`, `stats`, `hybrid`, and `balance` (subject only to the safety floor). The directional `has_room_reached_setpoint` check must stay — never revert to `abs(setpoint − temp)`.
- **Credentials**: `client_id` and `client_secret` live in `entry.data`. Never log, expose in UI, or include in error messages.
- **File path validation**: `_resolve_efficiency_path()` in `services.py` checks paths against Home Assistant's allowed-path list. Do not remove this check.
- **Structure mode override**: when DAB is active with `dab_force_manual=True`, the integration sets the Flair structure to manual mode, overriding Flair's own automation. Intentional, but always gated behind the user's config flag.
- **Determinism of pure modules**: `balance.allocate()` and the learning/context functions must stay pure and deterministic (no time/RNG/global state) so tests and the simulator are reproducible.
