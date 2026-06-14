"""Idle/fan vent-command suppression and bounded pre-adjust (R7.6 / R7.7).

Policy under test:
  * While the thermostat ``hvac_action`` is ``idle`` / ``fan`` / ``off`` the
    coordinator SHALL NOT issue balancing/normal vent commands — vent movement
    has no thermal effect without conditioned airflow (R7.6).
  * The ONLY command path permitted during idle/fan is the *bounded* pre-adjust
    path (R7.7): it pre-positions vents only when the thermostat has been idle
    for a minimum dwell (>= 2 min) AND the temperature is within the configured
    threshold (0.3 C) of the predicted trigger. The idle suppression guard must
    not accidentally block this path.
  * The safety-reach-floor exception in R7.6 is enforced while the HVAC is
    active (R3 floor applies "while the HVAC is active"), so it never originates
    from the idle path.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from tests._fakes import FakeState


class _Event:
    """Minimal stand-in for a HA state-change event."""

    def __init__(self, new_state: FakeState):
        self.data = {"new_state": new_state}


def _set_thermostat(
    rc, *, hvac_action: str, state: str = "cool", current_temperature: float = 26.0, temperature: float = 24.0
):
    rc["hass"].states.set(
        rc["thermostat"],
        FakeState(
            state,
            {
                "hvac_action": hvac_action,
                "current_temperature": current_temperature,
                "temperature": temperature,
                "temperature_unit": "°C",
            },
        ),
    )


# --- R7.6: poll while idle/fan issues NO balancing commands -----------------
async def test_poll_while_idle_issues_no_commands(ready_coordinator):
    rc = ready_coordinator
    coord = rc["coord"]
    _set_thermostat(rc, hvac_action="idle")

    await coord._async_process_thermostat_group(rc["thermostat"], [rc["vent_id"]], rc["data"])

    assert rc["api"].set_vent_calls == [], "no vent commands may be issued while idle"


async def test_poll_while_fan_issues_no_commands(ready_coordinator):
    rc = ready_coordinator
    coord = rc["coord"]
    _set_thermostat(rc, hvac_action="fan", state="fan_only")

    await coord._async_process_thermostat_group(rc["thermostat"], [rc["vent_id"]], rc["data"])

    assert rc["api"].set_vent_calls == [], "no vent commands may be issued while fan-only"


async def test_apply_path_reverifies_live_idle_and_skips(ready_coordinator):
    """A balancing/manual apply that finds the thermostat gone idle commands nothing.

    Even when invoked with a stale ``cooling`` action, the apply path re-reads
    the live thermostat state and suppresses commands when it is no longer
    actively conditioning (race protection + R7.6).
    """
    rc = ready_coordinator
    coord = rc["coord"]
    _set_thermostat(rc, hvac_action="idle")

    await coord._async_apply_dab_adjustments(rc["thermostat"], "cooling", [rc["vent_id"]], rc["data"])

    assert rc["api"].set_vent_calls == [], "stale cooling apply must not command while idle"


# --- R7.7: bounded pre-adjust still fires during idle -----------------------
async def test_pre_adjust_fires_during_idle_when_dwell_and_threshold_met(ready_coordinator):
    """Pre-adjust is the explicit exception (R7.7) and must NOT be blocked by R7.6."""
    rc = ready_coordinator
    coord = rc["coord"]
    entity = rc["thermostat"]

    # Cooling: raw target 24.0 -> effective cooling setpoint 24.0 - 0.7 = 23.3.
    # current_temperature 23.5 -> |23.5 - 23.3| = 0.2 C <= 0.3 C threshold, and
    # should_pre_adjust(cooling): 23.5 + 0.7 - 0.2 = 24.0 >= 23.3 -> True.
    _set_thermostat(rc, hvac_action="idle", current_temperature=23.5, temperature=24.0)
    # Thermostat has been idle for well over the 2-minute dwell.
    coord._idle_since[entity] = datetime.now(UTC) - timedelta(minutes=5)

    await coord._async_handle_pre_adjust(_Event(rc["hass"].states.get(entity)))

    assert rc["api"].set_vent_calls, "bounded pre-adjust must command vents during idle"


async def test_pre_adjust_suppressed_when_dwell_too_short(ready_coordinator):
    rc = ready_coordinator
    coord = rc["coord"]
    entity = rc["thermostat"]

    _set_thermostat(rc, hvac_action="idle", current_temperature=23.5, temperature=24.0)
    # Only 30 s idle -> below the 2-minute dwell requirement.
    coord._idle_since[entity] = datetime.now(UTC) - timedelta(seconds=30)

    await coord._async_handle_pre_adjust(_Event(rc["hass"].states.get(entity)))

    assert rc["api"].set_vent_calls == [], "pre-adjust must not fire below the dwell minimum"


async def test_pre_adjust_suppressed_when_temp_delta_exceeds_threshold(ready_coordinator):
    rc = ready_coordinator
    coord = rc["coord"]
    entity = rc["thermostat"]

    # Effective cooling setpoint 23.3; current_temperature 22.0 -> delta 1.3 C > 0.3 C.
    _set_thermostat(rc, hvac_action="idle", current_temperature=22.0, temperature=24.0)
    coord._idle_since[entity] = datetime.now(UTC) - timedelta(minutes=5)

    await coord._async_handle_pre_adjust(_Event(rc["hass"].states.get(entity)))

    assert rc["api"].set_vent_calls == [], "pre-adjust must not fire beyond the temp threshold"
