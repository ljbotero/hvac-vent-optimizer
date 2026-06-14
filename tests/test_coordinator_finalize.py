"""Tests for the cycle-finalize race fix (Requirement 10 / Task 18).

Background
----------
``_schedule_finalize`` arms a delayed task (30 s in production) that calls
``_async_finalize_cycle`` to tear down the cycle bookkeeping
(``_dab_state`` / ``_cycle_targets``) and record learning/metrics.

The race: if the HVAC goes active -> idle -> active again *within* the finalize
delay window, a brand-new cycle starts while the previous cycle's finalize is
still pending. When that delayed finalize finally runs it must NOT wipe the new
cycle's state (R10.1/R10.2), and it must perform the state mutation under the
same lock the apply path uses (R10.3).

These tests drive the real ``_schedule_finalize`` code path and gate the inner
``asyncio.sleep`` so the delayed run can be interleaved deterministically with a
re-activation, instead of waiting a real 30 s.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest

from tests._fakes import FakeState


def _setup(make_coordinator):
    """Build a coordinator wired for one thermostat + one vent/room."""
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
    coord, hass, api, _entry = make_coordinator(options=options, data=data)
    hass.states.set(
        thermostat,
        FakeState(
            "cool",
            {
                "hvac_action": "cooling",
                "current_temperature": 26.0,
                "temperature": 24.0,
                "temperature_unit": "°C",
            },
        ),
    )
    coord._vent_rates[vent_id] = {"cooling": 0.5, "heating": 0.5}
    return coord, hass, api, thermostat, vent_id, data


def _gate_sleep(monkeypatch):
    """Patch asyncio.sleep so the long finalize delay waits on a test gate.

    Short sleeps (delay < 1) pass straight through to the real sleep so the
    event loop keeps yielding normally; the 30 s finalize sleep blocks until the
    test sets the gate.
    """
    gate = asyncio.Event()
    real_sleep = asyncio.sleep

    async def fake_sleep(delay, *args, **kwargs):
        if delay and delay >= 1:
            await gate.wait()
            return None
        return await real_sleep(delay, *args, **kwargs)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    return gate, real_sleep


async def _drain(real_sleep, times: int = 5) -> None:
    for _ in range(times):
        await real_sleep(0)


@pytest.mark.asyncio
async def test_finalize_race_does_not_wipe_new_cycle(make_coordinator, monkeypatch):
    """active -> idle (schedule finalize) -> active (new cycle): new state survives."""
    coord, _hass, _api, thermostat, vent_id, data = _setup(make_coordinator)
    gate, _real_sleep = _gate_sleep(monkeypatch)

    # Cycle #1 begins.
    coord._start_hvac_cycle(thermostat, "cooling", [vent_id], data)
    token_old = coord._dab_state[thermostat]["started_cycle"]

    # HVAC goes idle -> finalize for cycle #1 is scheduled (still pending/sleeping).
    await coord._schedule_finalize(thermostat, "cooling", [vent_id])
    pending_task = coord._pending_finalize[thermostat]

    # HVAC re-activates within the finalize window -> a NEW cycle starts.
    coord._start_hvac_cycle(thermostat, "cooling", [vent_id], data)
    # Guarantee a distinct cycle identity (new cycle, later timestamp).
    coord._dab_state[thermostat]["started_cycle"] = token_old + timedelta(minutes=5)
    coord._cycle_targets[thermostat]["targets"] = {vent_id: 80.0}
    new_state = coord._dab_state[thermostat]
    new_targets = coord._cycle_targets[thermostat]

    # Let the delayed finalize for cycle #1 run now.
    gate.set()
    await asyncio.wait_for(pending_task, timeout=1.0)

    # The new cycle's state MUST survive the stale finalize.
    assert thermostat in coord._dab_state, "new cycle's _dab_state was wiped by stale finalize"
    assert thermostat in coord._cycle_targets, "new cycle's _cycle_targets was wiped"
    assert coord._dab_state[thermostat] is new_state
    assert coord._cycle_targets[thermostat] is new_targets
    assert coord._cycle_targets[thermostat]["targets"] == {vent_id: 80.0}


@pytest.mark.asyncio
async def test_normal_finalize_clears_state_and_records_metrics(make_coordinator, monkeypatch):
    """active -> idle -> finalize runs (no re-activation): state cleared, metrics recorded."""
    coord, _hass, _api, thermostat, vent_id, data = _setup(make_coordinator)
    gate, _real_sleep = _gate_sleep(monkeypatch)

    coord._start_hvac_cycle(thermostat, "cooling", [vent_id], data)
    # Make the cycle look like it ran long enough to record metrics.
    coord._cycle_stats[thermostat]["adjustments"] = 3
    coord._cycle_stats[thermostat]["movement"] = 25.0

    metrics_calls: list[tuple] = []
    orig_update = coord._update_strategy_metrics

    def _spy(*args, **kwargs):
        metrics_calls.append((args, kwargs))
        return orig_update(*args, **kwargs)

    monkeypatch.setattr(coord, "_update_strategy_metrics", _spy)

    await coord._schedule_finalize(thermostat, "cooling", [vent_id])
    pending_task = coord._pending_finalize[thermostat]

    gate.set()
    await asyncio.wait_for(pending_task, timeout=1.0)

    # Legitimately finished cycle: state is cleared.
    assert thermostat not in coord._dab_state
    assert thermostat not in coord._cycle_targets
    assert thermostat not in coord._pending_finalize
    # ...and metrics/learning were still recorded for it.
    assert metrics_calls, "strategy metrics were not recorded for a normally-finished cycle"


@pytest.mark.asyncio
async def test_finalize_mutation_happens_under_dab_lock(make_coordinator, monkeypatch):
    """The clear of _dab_state/_cycle_targets must occur under _dab_lock (R10.3)."""
    coord, _hass, _api, thermostat, vent_id, data = _setup(make_coordinator)
    gate, real_sleep = _gate_sleep(monkeypatch)

    coord._start_hvac_cycle(thermostat, "cooling", [vent_id], data)

    await coord._schedule_finalize(thermostat, "cooling", [vent_id])
    pending_task = coord._pending_finalize[thermostat]

    # Hold the apply-path lock, then release the finalize sleep gate.
    await coord._dab_lock.acquire()
    gate.set()
    await _drain(real_sleep, 5)

    # While the lock is held, finalize must NOT have mutated cycle state.
    assert thermostat in coord._dab_state, "finalize cleared state without holding _dab_lock"
    assert thermostat in coord._cycle_targets

    # Release the lock; finalize can now proceed and clear.
    coord._dab_lock.release()
    await asyncio.wait_for(pending_task, timeout=1.0)
    assert thermostat not in coord._dab_state
    assert thermostat not in coord._cycle_targets
