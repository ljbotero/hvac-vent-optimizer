"""Efficiency-context -> regime mapping for DAB v2 (pure, HA-free).

This module classifies the ambient situation of a run into a small, stable set
of *regimes* so that learned efficiency can be bucketed and recalled per
context (R12). It is deliberately **pure**: it imports nothing from Home
Assistant and operates only on already-resolved primitive values (an hour, an
outdoor temperature in Celsius, occupancy/door booleans, an optional sun
state). Resolving HA states into those primitives is the caller's job, which
keeps this logic trivially testable in isolation (mirrors ``dab.py``).

Concepts
--------
* ``outdoor_band`` folds season + weather into 3 coarse bands
  (cold / mild / hot) using strict thresholds around :data:`COLD_C` /
  :data:`HOT_C`.
* ``is_daytime`` prefers an explicit sun state when available, otherwise falls
  back to a local-hour window ``[DAY_START, DAY_END)``.
* ``regime_index`` collapses those into the 4 regimes the learner tracks:
  ``[day-mild, day-hot, night-mild, night-hot]`` — cold collapses into mild,
  i.e. the regimes only distinguish *hot* from *not-hot*.
* ``apply_context_multipliers`` applies bounded secondary multipliers
  (occupancy, open doors) to a learned rate, clamped to
  ``[FACTOR_MIN, FACTOR_MAX]``.
"""

from __future__ import annotations

from dataclasses import dataclass

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
# Outdoor band thresholds (Celsius). Boundaries are mild (strict comparisons):
# cold is strictly below COLD_C, hot is strictly above HOT_C.
COLD_C: float = 10.0
HOT_C: float = 25.0

# Local-hour daytime window: DAY_START inclusive .. DAY_END exclusive.
DAY_START: int = 7
DAY_END: int = 21

# Secondary multipliers (cooling): occupants add heat and open doors leak
# conditioned air, both of which slow the observed cooling rate (~0.9x).
OCC_FACTOR: float = 0.9
DOOR_FACTOR: float = 0.9

# The effective secondary multiplier is confined to this clamped band.
FACTOR_MIN: float = 0.5
FACTOR_MAX: float = 1.5


# ---------------------------------------------------------------------------
# Context dataclass
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Context:
    """Resolved ambient context for a single DAB run.

    All fields are already-resolved primitives (no Home Assistant objects).
    ``occupied`` / ``doors_open`` are tri-state: ``None`` means the source was
    unavailable, while ``True`` / ``False`` carry a definite reading.
    """

    hour: int
    is_daytime: bool
    outdoor_band: int
    occupied: bool | None = None
    doors_open: bool | None = None


# ---------------------------------------------------------------------------
# Classifiers
# ---------------------------------------------------------------------------
def outdoor_band(outdoor_temp_c: float | None) -> int:
    """Classify an outdoor temperature into a coarse band.

    Returns ``0`` cold (strictly below :data:`COLD_C`), ``2`` hot (strictly
    above :data:`HOT_C`), and ``1`` mild otherwise. A missing reading
    (``None``) degrades gracefully to the neutral mild band.
    """
    if outdoor_temp_c is None:
        return 1
    if outdoor_temp_c < COLD_C:
        return 0
    if outdoor_temp_c > HOT_C:
        return 2
    return 1


def is_daytime(hour: int, sun_state: str | None = None) -> bool:
    """Decide whether it is daytime.

    When ``sun_state`` is provided it wins outright: daytime iff the sun is
    ``"above_horizon"``. Otherwise fall back to the local-hour window
    ``[DAY_START, DAY_END)``.
    """
    if sun_state is not None:
        return sun_state == "above_horizon"
    return DAY_START <= hour < DAY_END


# ---------------------------------------------------------------------------
# Builder
# ---------------------------------------------------------------------------
def build(
    hour: int,
    outdoor_temp_c: float | None = None,
    occupied: bool | None = None,
    doors_open: bool | None = None,
    sun_state: str | None = None,
) -> Context:
    """Build a :class:`Context` from already-resolved primitive values.

    Pure: it takes resolved values, never Home Assistant states. Missing inputs
    degrade gracefully — a missing outdoor temperature yields the mild band,
    and unset occupancy / door sensors stay ``None``.
    """
    return Context(
        hour=hour,
        is_daytime=is_daytime(hour, sun_state),
        outdoor_band=outdoor_band(outdoor_temp_c),
        occupied=occupied,
        doors_open=doors_open,
    )


# ---------------------------------------------------------------------------
# Regime mapping
# ---------------------------------------------------------------------------
def regime_index(ctx: Context) -> int:
    """Map a context to one of the 4 regimes.

    Ordering: ``0`` day-mild, ``1`` day-hot, ``2`` night-mild, ``3`` night-hot.
    The base is ``0`` for daytime and ``2`` for night, plus ``1`` when the
    outdoor band is hot. Cold collapses with mild, so the result is always in
    ``{0, 1, 2, 3}``.
    """
    base = 0 if ctx.is_daytime else 2
    return base + (1 if ctx.outdoor_band == 2 else 0)


# ---------------------------------------------------------------------------
# Secondary multipliers
# ---------------------------------------------------------------------------
def apply_context_multipliers(rate: float, ctx: Context, mode: str = "cooling") -> float:
    """Apply bounded secondary multipliers to a learned ``rate``.

    Occupancy and open doors each scale the rate (compounding when both apply);
    ``None`` / ``False`` readings contribute a neutral ``1.0``. The combined
    multiplier is clamped to ``[FACTOR_MIN, FACTOR_MAX]`` before being applied.
    """
    factor = 1.0
    if ctx.occupied is True:
        factor *= OCC_FACTOR
    if ctx.doors_open is True:
        factor *= DOOR_FACTOR
    factor = max(FACTOR_MIN, min(FACTOR_MAX, factor))
    return rate * factor
