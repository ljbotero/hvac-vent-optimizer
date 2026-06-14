"""Entity-level tests for Task 11 — observability of the learned door factor (R30).

These pin the diagnostic surface for the per-room *learned door-leakage
multiplier* on the **real** ``sensor.py`` entity, mirroring the harness in
``tests/test_observability_entities.py`` (build a ``FlairRoomSensor`` over a
coordinator whose learning state is set directly).

Chosen surface (R30.1/30.2/30.3): the per-room temperature sensor's
``extra_state_attributes`` gains two keys *only* for a room that has a door
sensor configured (``CONF_DOOR_SENSOR_ENTITY`` on any of its vents):

* ``door_factor`` — the value resolved by ``learning.resolve_door_factor`` for
  the room's active / most-recent conditioning mode, always within
  ``[DOOR_FACTOR_MIN, 1.0]`` (so it folds in the cold-start ``0.9`` fallback and
  the per-mode confidence gate / clamp).
* ``door_factor_trusted`` — ``True`` only when the active mode's cell meets the
  gate (``n >= DOOR_MIN_N`` with a learned factor present); ``False`` while the
  resolution is using the default fallback.

A room with **no** door sensor configured surfaces neither key (R30.2), so the
optimizer never shows a misleading learned factor for a room it cannot observe.

These are TESTS-FIRST (red): the attributes do not exist yet (Task 11.2 adds
them to ``sensor.py`` / ``coordinator.py``).
"""

from __future__ import annotations

from hvac_vent_optimizer import const
from hvac_vent_optimizer.learning import DOOR_MIN_N, DoorFactorCell, new_door_factor_model
from tests._fakes import FakeApi, FakeEntry, FakeHass

ROOM_ID = "room1"
ROOM_NAME = "Bedroom 2"
VENT_ID = "v1"
THERMOSTAT = "climate.t"
DOOR_SENSOR = "binary_sensor.bedroom_2_door"


# ---------------------------------------------------------------------------
# Harness (mirrors tests/test_observability_entities.py::_coord)
# ---------------------------------------------------------------------------
def _coord(options=None, data=None):
    from hvac_vent_optimizer.coordinator import FlairCoordinator

    opts = {const.CONF_VENT_BRAND: const.BRAND_FLAIR, const.CONF_DAB_ENABLED: True}
    if options:
        opts.update(options)
    entry = FakeEntry(
        data={
            const.CONF_STRUCTURE_ID: "s1",
            const.CONF_CLIENT_ID: "id",
            const.CONF_CLIENT_SECRET: "sec",
        },
        options=opts,
    )
    hass = FakeHass(unit="°C")
    coord = FlairCoordinator(hass, FakeApi(), entry)
    coord.data = data if data is not None else {"vents": {}, "pucks": {}}
    return coord


def _make(*, door_sensor=True, door_model=None, mode="cooling"):
    """Build a coordinator with one room+vent, optional door sensor + door model."""
    data = {
        "vents": {
            VENT_ID: {
                "id": VENT_ID,
                "name": f"{ROOM_NAME} Vent",
                "attributes": {"percent-open": 100},
                "room": {"id": ROOM_ID, "attributes": {"name": ROOM_NAME}},
            }
        },
        "pucks": {},
    }
    assignment = {const.CONF_THERMOSTAT_ENTITY: THERMOSTAT}
    if door_sensor:
        assignment[const.CONF_DOOR_SENSOR_ENTITY] = DOOR_SENSOR
    options = {const.CONF_VENT_ASSIGNMENTS: {VENT_ID: assignment}}
    coord = _coord(options=options, data=data)
    # Active / most-recent mode is sourced from the room's thermostat action.
    coord._last_hvac_action[THERMOSTAT] = "cooling" if mode == "cooling" else "heating"
    if door_model is not None:
        # Keyed by room name (group level), falling back to vent id when unnamed.
        coord._door_factor_models[ROOM_NAME] = door_model
    return coord


def _room_attrs(coord):
    from hvac_vent_optimizer import sensor as sensor_mod

    desc = next(d for d in sensor_mod.ROOM_SENSOR_DESCRIPTIONS if d.key == "room_temperature")
    ent = sensor_mod.FlairRoomSensor(coord, "e1", ROOM_ID, desc)
    return ent.extra_state_attributes or {}


# ---------------------------------------------------------------------------
# R30.1 — a trusted cell surfaces its learned factor + trusted=True
# ---------------------------------------------------------------------------
def test_trusted_cooling_cell_exposes_learned_factor():
    model = new_door_factor_model()
    model.cooling = DoorFactorCell(factor=0.7, n=DOOR_MIN_N)
    attrs = _room_attrs(_make(door_model=model, mode="cooling"))
    assert attrs.get("door_factor") == 0.7
    assert 0.5 <= attrs["door_factor"] <= 1.0
    assert attrs.get("door_factor_trusted") is True


# ---------------------------------------------------------------------------
# R30.1 — below the confidence gate -> default 0.9, trusted=False
# ---------------------------------------------------------------------------
def test_below_gate_cell_uses_default_and_is_untrusted():
    model = new_door_factor_model()
    model.cooling = DoorFactorCell(factor=0.7, n=DOOR_MIN_N - 1)
    attrs = _room_attrs(_make(door_model=model, mode="cooling"))
    assert attrs.get("door_factor") == 0.9
    assert attrs.get("door_factor_trusted") is False


# ---------------------------------------------------------------------------
# R27.4 — cold install (no model for the room) -> default 0.9, trusted=False
# ---------------------------------------------------------------------------
def test_no_model_resolves_to_default_untrusted():
    attrs = _room_attrs(_make(door_model=None, mode="cooling"))
    assert attrs.get("door_factor") == 0.9
    assert attrs.get("door_factor_trusted") is False


# ---------------------------------------------------------------------------
# R30.2 — a room with no door sensor surfaces no misleading learned factor
# ---------------------------------------------------------------------------
def test_room_without_door_sensor_has_no_door_factor():
    # Even seed a (trusted) model to prove it is NOT surfaced without a sensor.
    model = new_door_factor_model()
    model.cooling = DoorFactorCell(factor=0.7, n=DOOR_MIN_N)
    attrs = _room_attrs(_make(door_sensor=False, door_model=model, mode="cooling"))
    assert "door_factor" not in attrs
    assert attrs.get("door_factor") is None
    assert "door_factor_trusted" not in attrs


# ---------------------------------------------------------------------------
# R30.3 / R28.1 — the reported value is always clamped to [0.5, 1.0]
# ---------------------------------------------------------------------------
def test_resolved_factor_is_clamped_into_bounds():
    model = new_door_factor_model()
    # A stored factor below the lower clamp must resolve to DOOR_FACTOR_MIN.
    model.cooling = DoorFactorCell(factor=0.4, n=DOOR_MIN_N)
    attrs = _room_attrs(_make(door_model=model, mode="cooling"))
    assert attrs.get("door_factor") == 0.5
    assert 0.5 <= attrs["door_factor"] <= 1.0


# ---------------------------------------------------------------------------
# R30.3 — the value reflects the per-mode resolution for the active mode
# ---------------------------------------------------------------------------
def test_value_reflects_active_mode_per_mode_resolution():
    # Both modes trusted with DIFFERENT factors so the active mode is decisive
    # (no cross-mode fallback ambiguity).
    def _model():
        m = new_door_factor_model()
        m.cooling = DoorFactorCell(factor=0.7, n=DOOR_MIN_N)
        m.heating = DoorFactorCell(factor=0.6, n=DOOR_MIN_N)
        return m

    cooling_attrs = _room_attrs(_make(door_model=_model(), mode="cooling"))
    heating_attrs = _room_attrs(_make(door_model=_model(), mode="heating"))
    assert cooling_attrs.get("door_factor") == 0.7
    assert heating_attrs.get("door_factor") == 0.6
    assert cooling_attrs.get("door_factor_trusted") is True
    assert heating_attrs.get("door_factor_trusted") is True
