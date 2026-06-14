"""Tests that exercise _async_apply_dab_adjustments end-to-end.

Covers:
  #4  concurrency lock serializes the apply path
  #5  cycle samples are still recorded when an adjustment-batch cap is hit
  #8  pre-adjust / manual invocations don't inflate the active-poll counter
"""

from __future__ import annotations

import asyncio

import pytest


def test_fixture_sanity_issues_command(ready_coordinator):
    """The wired fixture should produce a concrete vent command."""
    rc = ready_coordinator
    coord = rc["coord"]
    asyncio.run(coord._async_apply_dab_adjustments(rc["thermostat"], "cooling", [rc["vent_id"]], rc["data"]))
    assert rc["api"].set_vent_calls, "expected at least one vent command from the apply path"


# --- #4: concurrency lock ---------------------------------------------------
@pytest.mark.asyncio
async def test_apply_path_serialized_by_lock(ready_coordinator):
    rc = ready_coordinator
    coord = rc["coord"]

    assert hasattr(coord, "_dab_lock"), "coordinator must expose a DAB execution lock"

    await coord._dab_lock.acquire()
    task = asyncio.ensure_future(
        coord._async_apply_dab_adjustments(rc["thermostat"], "cooling", [rc["vent_id"]], rc["data"])
    )
    # Give the task a chance to run; it must block on the held lock.
    for _ in range(5):
        await asyncio.sleep(0)
    assert not task.done(), "apply path ran while the lock was held"

    coord._dab_lock.release()
    await asyncio.wait_for(task, timeout=1.0)
    assert task.done()


# --- #5: sample recording on batch cap --------------------------------------
@pytest.mark.asyncio
async def test_batch_cap_still_records_samples(ready_coordinator):
    from hvac_vent_optimizer import const

    rc = ready_coordinator
    coord = rc["coord"]
    thermostat, vent_id, data = rc["thermostat"], rc["vent_id"], rc["data"]

    coord._start_hvac_cycle(thermostat, "cooling", [vent_id], data)
    # Force the per-cycle adjustment-batch cap to be already reached.
    cap = const.DEFAULT_MAX_ADJUSTMENT_BATCHES_PER_CYCLE
    coord._cycle_targets[thermostat]["adjustment_batches"] = cap

    await coord._async_apply_dab_adjustments(thermostat, "cooling", [vent_id], data)

    # Cap respected: no vent command issued...
    assert rc["api"].set_vent_calls == []
    # ...but learning samples must still be collected.
    samples = coord._dab_state[thermostat]["samples"].get(vent_id, [])
    assert len(samples) >= 1, "samples must be recorded even when the batch cap blocks moves"


# --- #8: poll counter not inflated by pre-adjust ----------------------------
@pytest.mark.asyncio
async def test_pre_adjust_does_not_count_as_active_poll(ready_coordinator):
    rc = ready_coordinator
    coord = rc["coord"]
    assert coord._total_active_polls == 0

    # Default invocation (pre-adjust / manual) must not increment the counter.
    await coord._async_apply_dab_adjustments(rc["thermostat"], "cooling", [rc["vent_id"]], rc["data"])
    assert coord._total_active_polls == 0


@pytest.mark.asyncio
async def test_real_poll_counts_as_active_poll(ready_coordinator):
    rc = ready_coordinator
    coord = rc["coord"]

    await coord._async_apply_dab_adjustments(
        rc["thermostat"], "cooling", [rc["vent_id"]], rc["data"], count_as_poll=True
    )
    assert coord._total_active_polls == 1
