HVAC Vent Optimizer is a Home Assistant custom integration for smart vents (Flair and
Manual mode), with optional Dynamic Airflow Balancing (DAB). Configuration is handled
via the UI config flow and options flow.

## Install (HACS)

### HACS custom repository (recommended)
1) HACS -> Settings -> Custom repositories -> Add
2) Repository: https://github.com/ljbotero/smarter-flair-vents
3) Category: Integration
4) Install and restart Home Assistant

### Manual install
Copy the `custom_components/hvac_vent_optimizer` folder into your Home Assistant
`config/custom_components` directory, then restart Home Assistant.

## Support

If you'd like to support development, you can sponsor via GitHub Sponsors:
https://github.com/sponsors/ljbotero

## Efficiency visualization (Home Assistant)

If you are coming from Hubitat, this mirrors the "Discovered devices" efficiency list.
The integration exposes
two extra sensors per vent:

- `<vent name> Cooling Efficiency` (%)
- `<vent name> Heating Efficiency` (%)

These are derived from the learned DAB rates and shown as percentages (0-100). A
value of `unknown` means the rate has not been learned yet.

Example Lovelace table (adjust entity_ids to match your vents):

```yaml
type: entities
title: Vent Efficiency
entities:
  - entity: sensor.tomas_1222_cooling_efficiency
    name: Tomas Cooling
  - entity: sensor.tomas_1222_heating_efficiency
    name: Tomas Heating
  - entity: sensor.master_bedroom_cooling_efficiency
    name: Master Bedroom Cooling
  - entity: sensor.master_bedroom_heating_efficiency
    name: Master Bedroom Heating
```

Below is a feature inventory derived from the original Hubitat implementation. It is
retained as a parity checklist for the Home Assistant port and grouped into functional
features and non-functional behaviors.

## Efficiency model specification (implementation guide)

The recommended approach is a regime-aware baseline model that hardens a room’s
baseline efficiency over time while allowing temporary shifts to secondary modes
(e.g., doors open, occupancy, duct temperature shifts). This prevents overreacting
to short-term variability while still adapting to meaningful changes.

Full specification (measurement windows, formulas, regime model, and defaults):
- `docs/efficiency_model_spec.md`

## Configuration guide (user-friendly)

### Initial setup (Config Flow)
When you add the integration:
- **Vent brand**: choose Flair or Manual.
- **Flair**: provide OAuth 2.0 Client ID/Secret and select a structure.
- **Manual**: enter the number of vents, name each vent, and select a thermostat + room temperature sensor.

### Options (Options Flow)

**Algorithm & Polling Settings**
- **Use Dynamic Airflow Balancing (DAB)**: Enables the adaptive vent-balancing algorithm.
  - If disabled, vents are not automatically adjusted by the integration.
- **Force structure mode to manual**: When DAB is enabled, force vendor structure mode to
  `manual` to allow vent control. Disable if you prefer to keep Flair in `auto` and
  understand that DAB may not work reliably.
- **Close vents in inactive rooms**: If enabled, DAB will close vents for rooms marked
  inactive (inactive rooms).
- **Vent adjustment granularity (%)**: Rounds vent changes to a set increment (5/10/25/50/100).
  - Smaller values = finer control, more frequent adjustments.
  - Larger values = fewer adjustments, less vent wear.
- **Polling interval (active HVAC)**: How often data is refreshed while heating/cooling.
- **Polling interval (idle HVAC)**: How often data is refreshed while idle.
- **Initial efficiency percent**: Starting efficiency value used until real rates are learned.
- **Notify on efficiency adjustments**: Optional HA notification whenever DAB updates a room's efficiency.
- **Log efficiency adjustments**: Add an entry to the Logbook whenever efficiency changes.

**Vent Assignments**
- **Thermostat per vent**: Each vent must be mapped to the thermostat that controls the
  HVAC serving that room.
- **Optional temperature sensor per vent**: Overrides vendor room temperature with a
  specific HA sensor (e.g., room sensor).

**Manual Mode**
- Manual mode creates a **Manual Aperture** number entity per vent.
- DAB computes **Suggested Aperture** sensors you can apply by hand.
- Use the thermostat + room temperature sensors you selected to drive the calculations.

**Conventional Vent Counts**
- **Conventional vents per thermostat**: Number of non-smart (standard) vents on that HVAC
  system. Used to prevent total airflow from dropping too low when DAB closes vents.

## Services

- `hvac_vent_optimizer.set_room_active`: Set a room to active/inactive by room_id or vent_id.
- `hvac_vent_optimizer.set_room_setpoint`: Set room setpoint (C) by room_id or vent_id.
- `hvac_vent_optimizer.set_structure_mode`: Force structure mode `auto`/`manual`.
- `hvac_vent_optimizer.run_dab`: Manually trigger a DAB run (optional thermostat filter).
- `hvac_vent_optimizer.refresh_devices`: Force a refresh (useful after adding hardware).
- `hvac_vent_optimizer.export_efficiency`: Export learned efficiency to a JSON file.
  - `efficiency_path` is optional (defaults to `hvac_vent_optimizer_efficiency_export_<entry>.json`).
  - Path must be under your HA config directory.
- `hvac_vent_optimizer.import_efficiency`: Import learned efficiency from a JSON file.
  - Use either `efficiency_path` (file under your HA config directory),
    `efficiency_payload` (inline JSON), or pass `exportMetadata` + `efficiencyData`
    directly in the service call.
  - Supports Hubitat's export format (roomId/roomName/ventId rates).

## Troubleshooting

**Config flow error: `cannot_connect`**
- Usually indicates an auth or network issue. Check:
  - Client ID/secret are correct and have no extra spaces.
  - Your HA instance can reach `https://api.flair.co`.
  - OAuth app is **client_credentials**.

**Auth error: `invalid_scope`**
- Your Flair app does not have all requested scopes.
- The integration will fall back to a reduced scope set, but some features
  (room setpoint/active) may be limited.
- Ask Flair to enable the missing scopes for your OAuth app if needed.

**DAB not adjusting vents**
- Confirm DAB is enabled in options.
- Ensure each vent has a thermostat assignment.
- Verify HVAC action is `heating` or `cooling` in the thermostat entity.

**Efficiency sensors show `unknown`**
- DAB learns rates after full HVAC cycles. Values appear after a few heating/cooling
  runs when the algorithm can compute room efficiency.

## Integration tests (PHACC)

We support a lightweight integration test suite using
`pytest-homeassistant-custom-component` (PHACC).

1) Install PHACC in your virtualenv:
```
pip install pytest-homeassistant-custom-component
```

2) Run the integration tests:
```
pytest -q config/custom_components/hvac_vent_optimizer/tests_integration
```

## Manual refresh (optional)

A lightweight service is available to force a refresh if you add/rename devices
and want them to appear immediately:

```
service: hvac_vent_optimizer.refresh_devices
```

## Functional features (Hubitat parity reference)

### Authentication & API access
- OAuth 2.0 client-credentials authentication against `https://api.flair.co`.
- Token refresh and re-auth on 401/403 responses.
- Support for structures, vents, pucks, rooms, and remote sensors endpoints.

### Discovery & device model
- Discover structures and select a structure for control.
- Discover vents and pucks:
  - Primary: `/structures/{id}/vents`, `/structures/{id}/pucks`.
  - Fallback: `/rooms-include=pucks` and `/pucks` (Hubitat used both).
- Map to device entities:
  - Vents: percent-open control + readings.
  - Pucks: temperature, humidity, battery, motion/occupancy, signal metrics.

### Vent data & control
- Read vent attributes: percent-open, duct temperature, duct pressure, voltage, RSSI.
- Set vent position (0-100%).
- Maintain unique IDs, names, and per-vent state.
- Expose room metadata on vent entities (room id, name, setpoint, occupancy, etc.).

### Puck data & sensors
- Read puck attributes: temperature, humidity, battery, voltage, RSSI, firmware, status.
- Expose puck motion/occupancy (binary sensor) and signal metrics.
- Expose room metadata on puck entities.

### Room controls
- Set room `active` flag (home/away equivalent).
- Set room setpoint in Celsius with optional hold-until timestamp.
- Read room state, occupancy mode, and associated metadata.

### Structure controls
- Set structure mode (auto/manual) as required by the DAB logic.

### Dynamic Airflow Balancing (DAB)
- Optional algorithm that adjusts vent openings based on:
  - Thermostat HVAC state (heating/cooling/idle).
  - Thermostat setpoints.
  - Room temperatures (via Flair rooms or per-vent temp sensors).
  - Learned room efficiency rates (heating and cooling).
- Pre-adjustment logic when approaching setpoints.
- Learned efficiency computation after HVAC cycle completes.
- Vent opening calculation using learned efficiency and target time-to-setpoint.
- Minimum airflow protection using conventional vent count and iterative increments.
- Rebalancing during active HVAC cycles when rooms reach setpoints.

### Polling & updates
- Dynamic polling interval based on HVAC state:
  - Active HVAC: fast refresh (Hubitat used 3 min).
  - Idle HVAC: slower refresh (Hubitat used 10 min).
- Per-device refresh scheduling and state updates.

### Efficiency export/import (Hubitat only)
- JSON export of learned efficiency data (rates per room).
- JSON import with validation and matching by room ID or name.

## Non-functional behaviors (Hubitat parity reference)

### Performance & concurrency
- API rate limiting to avoid throttling:
  - Basic endpoints: 4 requests/second.
  - Search endpoints: 1 request/second.
  - HTTP 429: waits for `Retry-After` (or 1s) and retries once.

### Caching
- Per-refresh task cache to avoid duplicate remote-sensor requests during a single update cycle.

### Resilience & error handling
- Treat 401/403 as auth failures and re-auth automatically.
- Log and skip 404s for missing pucks/sensors rather than failing.
- Handle transient API failures without breaking the flow.

### Scheduling & timing
- HVAC state change listeners to switch polling strategy.

### Data storage
- Token stored in-memory per integration instance.
- Efficiency data persisted to storage for reuse across restarts.

### Observability
- Multiple debug levels in Hubitat (0-3).
- Structured logs for API errors, retries, and DAB decisions.

## Porting parity notes
- The list above is the baseline feature inventory from Hubitat.
- Some items are intentionally optional or deferred in the HA port
  (e.g., efficiency export/import).
- Use this list as the checklist for "feature-complete" parity.
