HVAC Vent Optimizer - Efficiency Model Specification (v1)

Goal
- Learn stable, room-specific baseline efficiencies that harden over time.
- Allow temporary shifts to secondary efficiencies (regimes) when conditions change.
- Provide a spec that is deterministic, measurable, and AI-friendly for future implementations.

Scope
- Applies to both Heating and Cooling.
- Works with either Flair (auto) vents or Manual vents.
- Efficiency values are used by DAB to select vent apertures.

Definitions
Room:
- A logical space with a temperature signal T_room(t) and a thermostat entity.

Vent Aperture:
- A percent-open value in [0,100]. For Manual mode, it is user-entered.

HVAC Action:
- One of {"heating","cooling","idle"} from thermostat hvac_action.

Supply Temperature (optional):
- Duct temperature sensor T_duct(t). If not available, it is ignored.

Efficiency:
- A positive scalar rate describing how quickly a room temperature moves toward target
  when HVAC is actively heating/cooling and the vent aperture is > 0.
- Base unit: C/min at 100% aperture. If duct normalization is enabled, it becomes
  (C/min) per degree of supply-room delta; this must be used consistently.

Key Inputs (per room)
- T_room(t): room temperature in C.
- hvac_action(t): heating/cooling/idle.
- setpoint(t): target temperature (temp or target_low/high depending on mode).
- aperture(t): vent aperture in %.
- T_duct(t): optional duct temperature in C.
- occupied(t): optional occupancy.

Core Measurement Rules
1) Measure only during HVAC ACTIVE windows:
   - heating window: hvac_action == "heating"
   - cooling window: hvac_action == "cooling"
   - If hvac_action is None, unknown, or HVAC mode is off, do not measure.

2) Window start:
   - Start when hvac_action transitions idle -> heating/cooling.
   - Require hvac_action to remain stable for ACTION_STABLE_MIN (default: 1 min).
   - Ignore the first WARMUP_MIN minutes of each window (default: 2 min) to avoid
     transient ramp-up and fan delays.

3) Window end:
   - hvac_action returns to idle, OR
   - total active duration reaches MAX_WINDOW_MIN (default: 30 min), OR
   - room crosses its setpoint band (if setpoint is known):
       - heating: T_room >= target_low (or setpoint)
       - cooling: T_room <= target_high (or setpoint)
   Notes:
     - For heat_cool, target_low/high are preferred.
     - For heat-only or cool-only, use single target setpoint.

4) Minimum data quality:
   - Minimum window length after warmup: MIN_WINDOW_MIN (default: 5 min).
   - Minimum temperature delta magnitude: MIN_DELTA_C (default: 0.2 C).
     Use absolute change across the window: |T_room(end) - T_room(start)|.
   - Minimum mean aperture: MIN_APERTURE_PCT (default: 5%).

If a window fails quality checks, it is discarded.

Efficiency Calculation (per room, per mode)
Let:
  t = timestamps within a valid window (after warmup)
  T_room(t) = room temperature samples
  A(t) = vent aperture samples (%)
  T_duct(t) = duct temp samples (optional)

Unit handling
- All temperature calculations MUST use Celsius internally.
- If a source entity reports Fahrenheit, convert to Celsius before any computation.
- Store/report efficiency in terms of C/min (and C/min per delta-C if duct-normalized).

Step 1: Compute slope (room rate)
  Use robust linear regression on T_room(t) vs time to estimate:
    rate_room = dT_room/dt (C/min)
  Use a robust estimator (e.g., Theil-Sen) and discard outlier samples.
  Convert F to C before any computation.

Step 2: Normalize for duct temperature (optional)
  If T_duct is available and stable in the window:
    stability = stddev(T_duct(t))
    require stability <= DUCT_STABILITY_C
    mean_delta = mean(|T_duct - T_room|)
    If mean_delta >= MIN_DUCT_DELTA_C (default: 2.0 C):
      rate_norm = rate_room / mean_delta
    Else:
      rate_norm = rate_room
  Else:
    rate_norm = rate_room
  Note: if duct normalization is enabled, it must be applied consistently to
  learned efficiencies and any DAB prediction/comparison logic.

Step 3: Normalize for aperture (optional, recommended)
  mean_aperture = mean(A(t))
  If mean_aperture >= MIN_APERTURE_PCT:
    rate_eff = rate_norm / (mean_aperture / 100.0)
  Else:
    discard window
  Note: very low apertures (near 0%) can still pass some air, but they tend to
  produce noisy temperature responses; we skip learning in that case.

Step 4: Convert to positive efficiency
  For heating: efficiency = max(0, rate_eff)
  For cooling: efficiency = max(0, -rate_eff)
  If efficiency is ~0 or the sign is opposite the HVAC action, discard the window
  (e.g., heating with a strong negative slope).

Store:
  efficiency_sample = efficiency

Regime-Aware Baseline Model (Recommended)
For each room r and mode m (heating/cooling):

Model:
  efficiency_r,m(t) = baseline_r,m + regime_offset_r,m(z_t) + noise

Where:
  baseline_r,m is a slow-moving mean.
  z_t is a latent regime index in {0..K-1}.
  regime_offset_r,m captures secondary efficiencies.

Recommended defaults:
  K = 2 or 3 regimes per room per mode.
  baseline learning rate: alpha = alpha0 / sqrt(N), alpha0=0.10
  regime learning rate: beta = 0.20 (faster than baseline)
  regime prior: offset shrinks toward 0 unless evidence supports it.

Regime Assignment (Soft)
Given a new efficiency_sample:
  For each regime k:
    predict_k = baseline + offset_k
    error_k = |sample - predict_k|
    weight_k = exp(-error_k / sigma)

Normalize weights and update each regime with its weight.
Confidence = max(weight_k) after normalization.
Sigma (noise scale) should be adaptive:
  sigma = max(SIGMA_MIN, SIGMA_REL * baseline), SIGMA_REL default 0.25.

Hardening Strategy
Maintain N = effective sample count per (room, mode).
  baseline update:
    baseline += alpha * (sample - baseline)
  alpha = max(alpha_min, alpha0 / sqrt(N))
  alpha_min default: 0.01
  Increment N by 1 for each valid sample.

Regime offsets update:
  offset_k += beta * weight_k * (sample - (baseline + offset_k))
  apply shrinkage:
    offset_k *= (1 - shrinkage), shrinkage default 0.01

Fallback Behavior
If no valid samples are available:
  - Use last baseline value.
  - If no baseline exists, use initial_efficiency_percent from options.

Operational Use in DAB
When selecting target aperture:
  - Use baseline when regime confidence is low.
  - Use baseline + most-likely regime when confidence is high.
  - Confidence threshold default: 0.6.
  - Apply vent granularity and min adjustment rules as usual.

Quality & Safety
1) Only learn when HVAC is active.
2) Discard data during rapid vent movement (avoid confounding):
   - If aperture changes more than APERTURE_JITTER_PCT within a window, discard.
3) Respect airflow safety:
   - Never reduce total active vent area below min_airflow percent.

Suggested Default Constants
- WARMUP_MIN = 2
- ACTION_STABLE_MIN = 1
- MIN_WINDOW_MIN = 5
- MAX_WINDOW_MIN = 30
- MIN_DELTA_C = 0.2
- MIN_APERTURE_PCT = 5
- MIN_DUCT_DELTA_C = 2.0
- DUCT_STABILITY_C = 1.0 (max std dev within window)
- APERTURE_JITTER_PCT = 15
- alpha0 = 0.10
- alpha_min = 0.01
- beta = 0.20
- shrinkage = 0.01
- regime_confidence = 0.60
- SIGMA_MIN = 0.05
- SIGMA_REL = 0.25

Outputs
For each room:
- baseline_heating, baseline_cooling
- regime_offsets_heating[K], regime_offsets_cooling[K]
- last_efficiency_sample
- confidence of active regime

Notes for Implementation
- Store learned values in HA storage (same as current efficiency).
- If new sensors (duct temp, occupancy) become available, they should be optional.
- Provide an export/import format that includes baseline + regimes + metadata.
