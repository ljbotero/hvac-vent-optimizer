# `balance` (DAB v2): usage, live A/B procedure, rollout & deferred scope

> Covers Requirements **R16** (live A/B), **R17** (rollout / defaults / legacy
> retention + removal plan) and **R24** (explicitly deferred / out-of-scope
> items). Companion to `docs/quality-baseline.md` (the R15.6 evidence-gate
> tables and the Task 27 ship decision) and `.kiro/specs/hvac-vent-balancing/`.

## What `balance` is

`balance` ("DAB v2") treats all **active** rooms as competing for one fixed,
safety-bounded air budget and explicitly minimises the active-room temperature
**spread** (hottest − coldest), anchored on the shared thermostat setpoint,
while minimising vent movements — subject to the inviolable 40 % combined-airflow
floor. It reuses the DAB rate/efficiency machinery (per-room dual heat/cool
indices + per-vent saturating airflow curve with leakage) as its thermal model.
The allocation math is pure and lives in `balance.py`; the learning model in
`learning.py`; both are unit- and property-tested.

## Default & rollout (R17.1 / R17.3 / R16.2)

- **New installs default to `balance`** (`DEFAULT_CONTROL_STRATEGY = "balance"`
  in `const.py`).
- **Existing installs are never silently overridden.** On upgrade the
  config-entry migration (`async_migrate_entry`, config-flow `VERSION` 1 → 2):
  - keeps any **explicitly selected** strategy (`dab`/`cost`/`stats`/`hybrid`/
    `balance`) exactly as the user set it; and
  - for a pre-`balance` install that **never** explicitly chose a strategy,
    pins the **legacy default** (`hybrid`, `LEGACY_DEFAULT_CONTROL_STRATEGY`)
    so the running behaviour is preserved rather than flipped to `balance`.
- **Switching strategy needs no reinstall** (R16.2): Settings → Devices &
  Services → HVAC Vent Optimizer → **Configure** → *Algorithm settings* →
  **Control strategy** dropdown. The change reloads the entry in place.

### Why `balance` shipped despite the spread gate (Task 27 decision)

The R15.6 evidence gate required, vs `dab`: (a) ≥ 30 % lower avg spread, (b) max
spread no worse, (c) ≤ 110 % of `dab`'s movements. Against the **already
bug-fixed** `dab` (the Task-14 overshoot fix), `balance` is essentially **tied**
on avg spread and marginally worse on max spread, but cuts movements
dramatically (**18–93 % of `dab`'s** moves). The original "~30 % lower spread"
target had been framed against the *legacy, pre-fix* `dab`; once `dab` itself is
fixed, the realistic remaining headroom is **movement, not spread**.

The homeowner reviewed this analysis and **explicitly chose to ship `balance` as
the default on the movement win, waiving the spread criteria.** The full gate
remains executable and visible (`tests/test_evidence_gate.py`) so it can be
re-earned on merit later. Recorded comparison tables: `docs/quality-baseline.md`.

## Live A/B procedure (R16.3)

The goal of the live A/B is to confirm on the real upstairs zone what the
simulator predicted — **`balance` moves the vents far less while not making the
room-to-room spread worse** — and to watch for any spread regression.

### What to read

Per-evaluation / live (while the HVAC is active):

| Entity | Meaning |
|---|---|
| `sensor.dab_active_room_spread` | current active-room spread, °C (R13.2) |
| `sensor.dab_max_active_error` | worst active-room error to setpoint, °C |
| `sensor.dab_hold_status` | holding / recalculating / idle |
| `sensor.dab_recalculations_24h` | recompute events in last 24 h |
| `sensor.dab_holds_24h` | hold events in last 24 h |
| `binary_sensor.<room>_airflow_limited` | room pinned yet off-target |

Per-strategy aggregates for the actual comparison (persisted, survive restarts):

| Entity | Meaning |
|---|---|
| `sensor.dab_strategy_effectiveness` | state = last strategy; **attributes carry the full per-strategy `strategies` breakdown** |
| `sensor.dab_avg_spread` / `sensor.dab_max_spread` | spread metrics for the active strategy |
| `sensor.dab_avg_movement` / `sensor.dab_avg_adjustments` | movement metrics for the active strategy |
| `sensor.dab_time_above_guardrail` | minutes spent above the spread guardrail |

The `sensor.dab_strategy_effectiveness` **attributes** are the richest source:
they hold a `strategies` map keyed by strategy name, each with `avg_spread`,
`max_spread`, `avg_movement`, `avg_adjustments`, `avg_active_temp_error`, etc.
Because they are keyed by strategy, both arms of the A/B accumulate side by side
and persist across restarts.

### How to alternate

1. **Pick a comparable window.** Alternate by **day** (or by full cooling
   cycles) so each arm sees similar outdoor conditions. Cooling season is ideal
   since the baseline analysis is cooling.
2. **Run arm A = `dab`** for the window (set Control strategy → `dab`). Let it
   run normally; do not change other tunables mid-test.
3. **Run arm B = `balance`** for an equivalent window (Control strategy →
   `balance`).
4. Keep everything else fixed: setpoint, active-room selection, conventional
   vent count, guardrail/deadband. Only the strategy changes.
5. Repeat A/B/A/B over several days to average out weather.

### How to read the comparison

- **Primary (movement win):** compare `avg_movement` / `avg_adjustments` (and
  the recorder history of each vent's `cover.*` position changes, or
  `sensor.dab_recalculations_24h`) between the two arms. Expect `balance`
  materially lower — this is the accepted basis for shipping it.
- **Guardrail (spread must not regress):** compare `avg_spread` and `max_spread`
  per strategy. `balance` should be **no worse** than `dab`. If `balance`'s
  spread is meaningfully worse over a representative window, fall back to `dab`
  (or `hybrid`) via the dropdown and file it against the spread objective.
- **Safety:** combined commanded airflow must never drop below 40 %. The floor
  is enforced in code (single `apply_safety_floor` choke point); the live A/B is
  a confirmation, not the guarantee.

### Reading metrics from the recorder (SSH)

The live sensors are recorded. Per `AGENTS.md`, query the recorder DB read-only
on the remote (the Core API proxy is blocked from the SSH add-on):

```
apk add --no-cache sqlite   # ephemeral; reinstall per session
sqlite3 "file:/homeassistant/home-assistant_v2.db?mode=ro" ".timeout 5000" "
  SELECT datetime(s.last_updated_ts,'unixepoch','localtime'), s.state
  FROM states s JOIN states_meta m ON s.metadata_id = m.metadata_id
  WHERE m.entity_id = 'sensor.dab_active_room_spread'
    AND s.last_updated_ts >= strftime('%s','now','-7 days')
  ORDER BY s.last_updated_ts;"
```

Compare the `balance`-window rows against the `dab`-window rows for spread, and
do the same for the movement/adjustment sensors. The documented baseline to beat
is **~3.09 °C avg spread / ~42 vent-moves per day**.

### Post-deploy starting point (Task 30)

The post-deploy live confirmation (Task 30, R16) captured the recorder snapshot
immediately after the Task 29 deploy (`balance` reloaded ≈ 2026-06-09 15:01 CDT).
At that point the thermostat was **idle**, so all spread/observability sensors
read a correct **0.0 / idle / 0**, and the `sensor.dab_strategy_effectiveness`
`strategies` map still held **only the pre-`balance` `hybrid` arm** (1403 cycles,
`avg_active_temp_error` 0.231 °C, `avg_movement` 92.3/cycle). The safety-floor
check passed (combined open ≈ **62 %**, smart 545 % over 8 vents + 4 conventional
@50 %, vs the live configured 30 % floor). A full multi-cycle A/B window did
**not** exist yet — the steps below are the forward plan to populate it. The
captured starting point, the per-strategy baseline, the safety-floor arithmetic,
and the configured-floor note (live 30 % vs spec-default 40 %) are recorded in
`docs/quality-baseline.md` → *"Task 30 — post-deploy live confirmation"*.

## Legacy-strategy removal plan (R17.4 / R24.5 — deferred)

The legacy strategies `dab`, `cost`, `stats`, `hybrid` are **retained as
selectable fallbacks** and their removal is **explicitly deferred** to a later
cleanup, executed only **after `balance` is validated live**. Planned sequence:

1. **Validate live (now → next cooling/heating season).** Run the A/B above;
   confirm `balance` holds the movement win with no spread regression and zero
   floor violations on the real system.
2. **Soft-deprecate.** Once validated, mark the legacy strategies as deprecated
   in `translations/en.json` (label them "legacy"), keep them selectable, and
   note the planned removal in the options-flow description.
3. **Hard-remove (later release).** Drop `cost`/`stats`/`hybrid` allocation
   paths from the coordinator and the `CONTROL_STRATEGIES` list, keeping `dab`
   only if it is still useful as a diagnostic baseline for the simulator gate.
   Migrate any entry still pinned to a removed strategy to `balance` (with a
   one-time persistent notification, never a silent change of comfort
   behaviour). `balance` itself routes through the same shared bug fixes
   (overshoot, cooldown, finalize race) so no safety behaviour is lost.
4. **Keep the gate.** `tests/test_evidence_gate.py` stays as the regression
   anchor; removing `dab` would require re-anchoring the gate against recorded
   tables instead of a live `dab` run.

Removal is **not** part of this spec; it is recorded here so nothing is silently
dropped.

## Explicitly deferred / out-of-scope (R24)

These were analysed and **deliberately not built** in this spec. Recorded so the
decisions and rationale are preserved and future work is clear.

- **D3 — Active booster-fan control (R24.1): out of scope.** The `ac_booster_*`
  fans are **not** actively controlled or coordinated. The per-room/per-vent
  efficiency model **absorbs their effect implicitly** (a room that runs with a
  booster simply learns a higher effective rate). Awareness / coordination /
  active control of boosters is recorded as possible **future work**.
- **Per-room (Flair) setpoints (R24.2): not used as independent targets.** A
  **single shared thermostat setpoint** governs HVAC runtime and anchors the
  spread objective. Per-room-setpoint optimisation (treating each room's Flair
  setpoint as its own target) is recorded as **future work**.
- **Humidity-based comfort-adjusted setpoints (R24.3): out of scope.** Adjusting
  the effective setpoint by humidity/heat-index was noted in the original
  analysis as future-only and is **not** implemented.
- **D4 — Auto-closing inactive rooms (R24.4): deliberately not enabled.** Current
  **hold** behaviour for inactive rooms is preserved (they are held in place, not
  repositioned, and only ever counted in the combined-flow safety math). The
  `close_inactive_rooms` option is **retained for the user** and defaults to the
  user's existing setting; balancing never auto-closes inactive rooms. The one
  exception, per R3.9, is the mathematical last resort where active + conventional
  capacity cannot reach the safety floor — there the floor (D1) outranks the hold
  preference (D4) and the reason is logged.
- **D9 — Regression / feature-based learning model (R24, R12.7): deferred.** The
  spec uses the interpretable **discrete-regime** approach (a small bounded set
  of named contexts per room/mode, each holding its own learned rate), **not** a
  regression/feature model. A regression model (and richer context features) is
  recorded as **future work**; investing in it is one of the candidate paths to
  later earn the R15.6 spread gate on merit.
- **D1 — Safety floor expressed as a combined open percentage (R24.6).** The
  minimum-airflow floor is a **combined open percentage** (default 40 %, the
  average open % across all airflow devices including conventional vents at their
  assumed open value). A **vent-count floor** ("keep N vents open") and a
  **CFM / free-area floor** were both considered and **not chosen**: the
  percentage form needs no per-vent CFM calibration, degrades gracefully as the
  device set changes, and maps directly onto what the integration already
  commands. The rationale is preserved here so the choice is not re-litigated
  silently. The floor is defined on **commanded aperture** and is never relaxed
  on the assumption that leakage provides extra airflow (R25.9).

## Commands

```
pytest                                  # unit + property + simulator + gate
pytest tests/test_evidence_gate.py -q   # the R15.6 gate report + waiver record
pytest tests/test_strategy_default.py   # default flip + upgrade preservation
python -m custom_components.hvac_vent_optimizer.simulator --compare
ruff check . ; black --check . ; mypy custom_components/hvac_vent_optimizer
```
