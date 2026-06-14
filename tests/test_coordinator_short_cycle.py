"""Short-cycle anchor-preservation tests (Task 19.3 / Requirement 7.8).

Background
----------
The thermostat can short-cycle aggressively (data: up to 6 cooling cycles in
4 hours). The existing lifecycle treats every ``idle -> active`` transition as a
brand-new cycle: ``_start_hvac_cycle`` wipes ``_cycle_targets`` and starts a
fresh anchor, which forces the ``balance`` allocator to recompute from scratch
on every short cycle (movement/churn we explicitly want to avoid, R7/R7.8).

R7.8 requires that when the *idle gap* between two active periods is shorter
than ``short_cycle_gap_min`` (default 10 min), the prior cycle's anchored
allocation is **preserved** rather than recomputed.

Mechanism under test
--------------------
* ``active -> idle`` records the idle-entry time (``_cycle_idle_since``) and
  schedules the delayed finalize (which would normally clear the anchor).
* ``idle -> active`` computes the idle gap. If it is shorter than
  ``short_cycle_gap_min`` AND the prior anchor still exists (finalize hasn't
  fired yet), the pending finalize is CANCELLED and the existing anchor is
  REUSED (``_start_hvac_cycle`` is NOT called). Otherwise the existing
  fresh-cycle behavior applies.

These tests drive the real ``_async_process_thermostat_group`` transition path
through the Home Assistant fakes. The delayed finalize's 30 s ``asyncio.sleep``
is gated so it never fires during the test, and ``_async_apply_dab_adjustments``
is stubbed to a no-op so the tests isolate the cycle-anchor lifecycle.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from tests._fakes import FakeState


def _setup(make_coordinator, *, short_cycle_gap_min=None):
    from hvac_vent_optimizer import const

    thermostat = "climate.t"
    vent_id = "v1"
    data = {
        "vents": {
            vent_id: {
                "id": vent_id,
                "name": "Vent 1",
                "attributes": {"percent-open": 50},
                "room": {
                    "id": "room1",
                    "attributes": {
                        "name": "Room1",
                        "active": True,
                        "current-temperature-c": 26.0,
                    },
                },
            }
        },
        "pucks": {},
    }
    options = {
        const.CONF_VENT_ASSIGNMENTS: {
            vent_id: {
                const.CONF_THERMOSTAT_ENTITY: thermostat,
                const.CONF_TEMP_SENSOR_ENTITY: None,
            },
        },
        const.CONF_CONTROL_STRATEGY: "balance",
    }
    if short_cycle_gap_min is not None:
        options[const.CONF_SHORT_CYCLE_GAP_MIN] = short_cycle_gap_min
    coord, hass, api, _entry = make_coordinator(options=options, data=data)
    coord._vent_rates[vent_id] = {"cooling": 0.5, "heating": 0.5}
    return coord, hass, api, thermostat, vent_id, data


def _gate_sleep(monkeypatch):
    """Gate the 30 s finalize sleep so it never fires during the test."""
    gate = asyncio.Event()
    real_sleep = asyncio.sleep

    async def fake_sleep(delay, *args, **kwargs):
        if delay and delay >= 1:
            await gate.wait()
            return None
        return await real_sleep(delay, *args, **kwargs)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    return gate, real_sleep


def _set_action(hass, thermostat, action):
    hass.states.set(
        thermostat,
        FakeState(
            "cool",
            {
                "hvac_action": action,
                "current_temperature": 26.0,
                "temperature": 24.0,
                "temperature_unit": "°C",
            },
        ),
    )


def _stub_apply(monkeypatch, coord):
    async def _noop(*args, **kwargs):
        return None

    monkeypatch.setattr(coord, "_async_apply_dab_adjustments", _noop)


@pytest.mark.asyncio
async def test_short_gap_reuses_prior_anchor_and_cancels_finalize(make_coordinator, monkeypatch):
    """active -> idle -> active within < short_cycle_gap_min reuses the anchor."""
    coord, hass, _api, thermostat, vent_id, data = _setup(make_coordinator)
    _gate_sleep(monkeypatch)
    _stub_apply(monkeypatch, coord)

    # Cycle #1 begins (idle/none -> active).
    _set_action(hass, thermostat, "cooling")
    await coord._async_process_thermostat_group(thermostat, [vent_id], data)
    anchor1 = coord._cycle_targets[thermostat]
    token1 = coord._dab_state[thermostat]["started_cycle"]
    # Stamp the anchored allocation so we can prove it survives untouched.
    anchor1["targets"] = {vent_id: 80.0}

    # active -> idle: schedules the (gated) finalize and records idle time.
    _set_action(hass, thermostat, "idle")
    await coord._async_process_thermostat_group(thermostat, [vent_id], data)
    assert thermostat in coord._pending_finalize
    pending = coord._pending_finalize[thermostat]

    # idle -> active again, almost immediately (gap ~0 < 10 min): short cycle.
    _set_action(hass, thermostat, "cooling")
    await coord._async_process_thermostat_group(thermostat, [vent_id], data)

    # The prior anchor must be preserved (same object, same stamped targets),
    # NOT recomputed from scratch.
    assert coord._cycle_targets[thermostat] is anchor1, "anchor was recreated"
    assert coord._cycle_targets[thermostat]["targets"] == {vent_id: 80.0}
    assert coord._dab_state[thermostat]["started_cycle"] == token1
    # The pending finalize must have been cancelled so it can't wipe the anchor.
    assert thermostat not in coord._pending_finalize
    await asyncio.sleep(0)
    assert pending.cancelled()


@pytest.mark.asyncio
async def test_long_gap_starts_fresh_cycle(make_coordinator, monkeypatch):
    """active -> idle -> active after >= short_cycle_gap_min starts fresh."""
    coord, hass, _api, thermostat, vent_id, data = _setup(make_coordinator)
    _gate_sleep(monkeypatch)
    _stub_apply(monkeypatch, coord)

    _set_action(hass, thermostat, "cooling")
    await coord._async_process_thermostat_group(thermostat, [vent_id], data)
    anchor1 = coord._cycle_targets[thermostat]
    token1 = coord._dab_state[thermostat]["started_cycle"]
    anchor1["targets"] = {vent_id: 80.0}

    _set_action(hass, thermostat, "idle")
    await coord._async_process_thermostat_group(thermostat, [vent_id], data)
    # Backdate the idle-entry time so the gap exceeds short_cycle_gap_min (10).
    coord._cycle_idle_since[thermostat] = datetime.now(UTC) - timedelta(minutes=15)

    _set_action(hass, thermostat, "cooling")
    await coord._async_process_thermostat_group(thermostat, [vent_id], data)

    # A fresh cycle anchor must have been created (existing behavior).
    assert coord._cycle_targets[thermostat] is not anchor1, "anchor should be fresh"
    assert coord._cycle_targets[thermostat]["targets"] == {}
    assert coord._dab_state[thermostat]["started_cycle"] != token1


@pytest.mark.asyncio
async def test_short_cycle_disabled_when_gap_min_zero(make_coordinator, monkeypatch):
    """short_cycle_gap_min = 0 disables reuse: every reactivation is fresh."""
    coord, hass, _api, thermostat, vent_id, data = _setup(make_coordinator, short_cycle_gap_min=0)
    _gate_sleep(monkeypatch)
    _stub_apply(monkeypatch, coord)

    _set_action(hass, thermostat, "cooling")
    await coord._async_process_thermostat_group(thermostat, [vent_id], data)
    anchor1 = coord._cycle_targets[thermostat]
    anchor1["targets"] = {vent_id: 80.0}

    _set_action(hass, thermostat, "idle")
    await coord._async_process_thermostat_group(thermostat, [vent_id], data)

    _set_action(hass, thermostat, "cooling")
    await coord._async_process_thermostat_group(thermostat, [vent_id], data)

    assert coord._cycle_targets[thermostat] is not anchor1
    assert coord._cycle_targets[thermostat]["targets"] == {}


@pytest.mark.asyncio
async def test_short_cycle_reuse_skipped_when_anchor_already_cleared(make_coordinator, monkeypatch):
    """If finalize already cleared the anchor, a fresh cycle starts even on a
    short gap (nothing to reuse)."""
    coord, hass, _api, thermostat, vent_id, data = _setup(make_coordinator)
    _gate_sleep(monkeypatch)
    _stub_apply(monkeypatch, coord)

    _set_action(hass, thermostat, "cooling")
    await coord._async_process_thermostat_group(thermostat, [vent_id], data)

    _set_action(hass, thermostat, "idle")
    await coord._async_process_thermostat_group(thermostat, [vent_id], data)

    # Simulate the finalize having already run and cleared the anchor, while the
    # idle-entry time is still recent (short gap).
    coord._cycle_targets.pop(thermostat, None)
    coord._dab_state.pop(thermostat, None)
    coord._pending_finalize.pop(thermostat, None)

    _set_action(hass, thermostat, "cooling")
    await coord._async_process_thermostat_group(thermostat, [vent_id], data)

    # A brand-new anchor must have been created (no stale state to reuse).
    assert thermostat in coord._cycle_targets
    assert coord._cycle_targets[thermostat]["targets"] == {}
    assert thermostat in coord._dab_state
