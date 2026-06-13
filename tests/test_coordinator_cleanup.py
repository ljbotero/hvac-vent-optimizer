"""Tests for Task 21 cleanup work (R14.5 / R20.8 / R20.9).

Three independent concerns are covered here:

* **R20.8 — dead ``hvac_action`` fallback estimator removed.** The upstairs
  Ecobee *always* publishes an ``hvac_action`` attribute, so the branch in
  ``_resolve_hvac_action`` that *estimated* cooling/heating from
  ``current_temperature`` vs the target(s) plus hysteresis was unreachable dead
  code. After removal, only the supported path remains: read ``hvac_action``;
  ``cooling``/``heating`` map to that action, everything else (``idle`` /
  ``off`` / ``fan`` / missing) maps to ``None``.

* **R20.9 — unassigned vents handled explicitly.** A vent with no thermostat
  assignment is intentionally skipped: it is never grouped, never commanded, and
  never crashes the apply path.

* **R14.5 — error-notification coalescing.** Repeated failures of the same
  error class collapse into ONE persistent notification (stable
  ``notification_id`` derived from the title) instead of spawning a new
  notification per occurrence.
"""

from __future__ import annotations

import dataclasses

import pytest

from hvac_vent_optimizer import coordinator as coord_mod
from tests._fakes import FakeState


# ---------------------------------------------------------------------------
# R20.8 — _resolve_hvac_action supported path + estimator removal
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("hvac_action", "expected_attr"),
    [
        ("cooling", "COOLING"),
        ("heating", "HEATING"),
    ],
)
def test_resolve_hvac_action_supported_active(make_coordinator, hvac_action, expected_attr):
    """cooling/heating ``hvac_action`` map straight through to that action."""
    from homeassistant.components.climate.const import HVACAction

    coord, _hass, _api, _entry = make_coordinator()
    state = FakeState(
        "cool",
        {
            "hvac_action": hvac_action,
            "current_temperature": 26.0,
            "temperature": 24.0,
            "temperature_unit": "°C",
        },
    )
    assert coord._resolve_hvac_action(state) == getattr(HVACAction, expected_attr)


@pytest.mark.parametrize("hvac_action", ["idle", "off", "fan"])
def test_resolve_hvac_action_inactive_returns_none(make_coordinator, hvac_action):
    """idle/off/fan map to None (not actively conditioning)."""
    coord, _hass, _api, _entry = make_coordinator()
    state = FakeState(
        "cool",
        {
            "hvac_action": hvac_action,
            "current_temperature": 26.0,
            "temperature": 24.0,
            "temperature_unit": "°C",
        },
    )
    assert coord._resolve_hvac_action(state) is None


def test_resolve_hvac_action_unavailable_returns_none(make_coordinator):
    coord, _hass, _api, _entry = make_coordinator()
    from homeassistant.const import STATE_UNAVAILABLE

    assert coord._resolve_hvac_action(None) is None
    assert coord._resolve_hvac_action(FakeState(STATE_UNAVAILABLE, {})) is None


def test_resolve_hvac_action_no_estimator_when_attribute_absent(make_coordinator):
    """R20.8: with NO ``hvac_action`` attribute the estimator is gone.

    Previously a ``cool`` thermostat that was hot relative to its target would
    be *estimated* as COOLING. Ecobee always supplies ``hvac_action``, so this
    path was dead/misleading. After removal, a missing ``hvac_action`` resolves
    to ``None`` regardless of temperatures.
    """
    coord, _hass, _api, _entry = make_coordinator()

    # cool mode, current well above target -> old estimator said COOLING.
    cool_hot = FakeState(
        "cool",
        {
            "current_temperature": 28.0,
            "temperature": 22.0,
            "temperature_unit": "°C",
        },
    )
    # heat mode, current well below target -> old estimator said HEATING.
    heat_cold = FakeState(
        "heat",
        {
            "current_temperature": 16.0,
            "temperature": 22.0,
            "temperature_unit": "°C",
        },
    )
    # heat_cool with both setpoints, current above the high band.
    heat_cool_hot = FakeState(
        "heat_cool",
        {
            "current_temperature": 28.0,
            "target_temp_low": 20.0,
            "target_temp_high": 24.0,
            "temperature_unit": "°C",
        },
    )

    assert coord._resolve_hvac_action(cool_hot) is None
    assert coord._resolve_hvac_action(heat_cold) is None
    assert coord._resolve_hvac_action(heat_cool_hot) is None


# ---------------------------------------------------------------------------
# R20.8 / R12 / D9 — standalone door-regime path removed from the legacy
# per-vent ``EfficiencyContext``. Doors are now a bounded *multiplier*
# (``context.apply_context_multipliers``), never a dedicated regime cell.
# ---------------------------------------------------------------------------
def test_legacy_efficiency_context_has_no_doors_field():
    """The legacy ``EfficiencyContext`` no longer carries ``doors_open``.

    Per D9 the regime set is time/occupancy driven only; door state is folded
    into the learned rate as a multiplier in ``context.py``. The dead
    door-regime field/branch is removed (R20.8).
    """
    field_names = {f.name for f in dataclasses.fields(coord_mod.EfficiencyContext)}
    assert "doors_open" not in field_names


def test_legacy_regime_index_never_selects_door_regime():
    """``regime_index`` is driven only by occupancy/time, never by a door flag.

    The old mapping reserved index ``2`` for ``doors_open``; after cleanup that
    index is never produced by the context selector (only 0=default,
    1=occupied, 3=night are reachable).
    """
    reachable = set()
    for occupied in (False, True):
        for time_bucket in (0, 1, 2, 3):
            ctx = coord_mod.EfficiencyContext(occupied=occupied, time_bucket=time_bucket)
            reachable.add(ctx.regime_index(4))
    assert 2 not in reachable
    assert reachable <= {0, 1, 3}


def test_legacy_vent_context_ignores_door_sensor(make_coordinator):
    """``_get_vent_context`` no longer reads a door sensor into the legacy context.

    A configured + open door sensor must not change the legacy regime selection
    (doors are handled by the new multiplier path instead).
    """
    from hvac_vent_optimizer import const

    vent_id = "v1"
    options = {
        const.CONF_VENT_ASSIGNMENTS: {
            vent_id: {
                const.CONF_THERMOSTAT_ENTITY: "climate.t",
                const.CONF_TEMP_SENSOR_ENTITY: None,
                const.CONF_DOOR_SENSOR_ENTITY: "binary_sensor.door",
            }
        }
    }
    data = {"vents": {vent_id: {"id": vent_id, "name": "V1", "room": {}}}, "pucks": {}}
    coord, hass, _api, _entry = make_coordinator(options=options, data=data)
    hass.states.set("binary_sensor.door", FakeState("on", {}))

    ctx = coord._get_vent_context(vent_id, data)
    # The legacy context exposes no door state at all.
    assert not hasattr(ctx, "doors_open")


# ---------------------------------------------------------------------------
# R20.9 — unassigned vents skipped gracefully
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_unassigned_vent_is_skipped_not_commanded(make_coordinator):
    """A vent with no thermostat assignment is never grouped or commanded."""
    from hvac_vent_optimizer import const

    assigned_vent = "v_assigned"
    unassigned_vent = "v_unassigned"
    thermostat = "climate.t"

    data = {
        "vents": {
            assigned_vent: {
                "id": assigned_vent,
                "name": "Assigned",
                "attributes": {"percent-open": 50},
                "room": {
                    "id": "room_a",
                    "attributes": {
                        "name": "RoomA",
                        "active": True,
                        "current-temperature-c": 26.0,
                    },
                },
            },
            unassigned_vent: {
                "id": unassigned_vent,
                "name": "Unassigned",
                "attributes": {"percent-open": 50},
                "room": {
                    "id": "room_b",
                    "attributes": {
                        "name": "RoomB",
                        "active": True,
                        "current-temperature-c": 26.0,
                    },
                },
            },
        },
        "pucks": {},
    }
    options = {
        const.CONF_VENT_ASSIGNMENTS: {
            assigned_vent: {
                const.CONF_THERMOSTAT_ENTITY: thermostat,
                const.CONF_TEMP_SENSOR_ENTITY: None,
            },
            # No CONF_THERMOSTAT_ENTITY for the unassigned vent.
            unassigned_vent: {
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
                "hvac_action": "idle",  # idle so nothing is commanded anyway
                "current_temperature": 26.0,
                "temperature": 24.0,
                "temperature_unit": "°C",
            },
        ),
    )

    # The unassigned vent must not be part of the thermostat set...
    assert coord._get_thermostat_entities() == [thermostat]

    # ...and processing the DAB path must not crash nor command the unassigned vent.
    await coord._async_process_dab(data)
    commanded = {vent_id for vent_id, _pct in api.set_vent_calls}
    assert unassigned_vent not in commanded


# ---------------------------------------------------------------------------
# R14.5 — error-notification coalescing
# ---------------------------------------------------------------------------
@pytest.fixture
def capture_notifications(monkeypatch):
    calls: list[dict] = []

    def _record(hass, message, *, title=None, notification_id=None, **kwargs):
        calls.append({"message": message, "title": title, "notification_id": notification_id})

    monkeypatch.setattr(coord_mod.persistent_notification, "async_create", _record)
    return calls


def test_repeated_same_class_errors_coalesce(make_coordinator, capture_notifications):
    """Two errors with the same title share ONE stable notification_id."""
    coord, _hass, _api, _entry = make_coordinator()

    coord._async_notify_error("Flair update failed", "boom 1")
    coord._async_notify_error("Flair update failed", "boom 2")

    ids = [c["notification_id"] for c in capture_notifications]
    assert len(ids) == 2
    assert ids[0] == ids[1], "repeated same-class errors must reuse one notification_id"


def test_different_error_classes_get_distinct_ids(make_coordinator, capture_notifications):
    """Different error titles map to different notification_ids."""
    coord, _hass, _api, _entry = make_coordinator()

    coord._async_notify_error("Flair update failed", "a")
    coord._async_notify_error("DAB processing failed", "b")

    ids = [c["notification_id"] for c in capture_notifications]
    assert ids[0] != ids[1]


def test_notification_id_is_stable_slug(make_coordinator, capture_notifications):
    """notification_id is derived from a slug of the title (no per-occurrence counter)."""
    from hvac_vent_optimizer.const import DOMAIN

    coord, _hass, _api, entry = make_coordinator()
    coord._async_notify_error("Flair update failed!", "x")

    expected = f"{DOMAIN}_{entry.entry_id}_error_flair_update_failed"
    assert capture_notifications[0]["notification_id"] == expected
