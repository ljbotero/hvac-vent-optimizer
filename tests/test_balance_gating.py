"""Tests for movement gating (design A5 / R7) — Task 12 (TDD).

TESTS-FIRST: this file is written BEFORE ``should_apply`` (and the
``GateContext`` gate object + the two new ``AllocSettings`` fields) exist in
``balance.py``. They are RED until Task 12 implements the A5 gating helper.

``balance.py`` is a PURE module (no Home Assistant imports). It is loaded
standalone by absolute path so we never import the ``hvac_vent_optimizer``
package (whose ``__init__`` pulls in Home Assistant, which is not installed in
the test environment). This mirrors the ``hvo_balance`` convention used by the
sibling test_balance_*.py files.

    cd tests && python3 -m pytest test_balance_gating.py -q
    python3 -m pytest tests/test_balance_gating.py -q --import-mode=importlib

============================================================================
PINNED CONTRACT FOR TASK 12 (design A5 / R7.1, R7.2, R7.3, R7.5)
============================================================================

``should_apply(current, proposed, rooms, settings, gate) -> bool``

    Pure helper. ``current`` and ``proposed`` are targets dicts
    (room_id -> open %). ``rooms`` is ``list[RoomAllocInput]``. ``settings`` is
    ``AllocSettings`` (now carrying ``spread_guardrail_c`` default 1.0 and
    ``spread_improvement_deadband_c`` default 0.3). ``gate`` is a
    ``GateContext`` carrying the prediction context the helper needs:
    ``mode``, ``setpoint_c``, and ``floor_requires_open``.

    A new allocation is COMMANDED (return True) only if BOTH:
      1. predicted active-room spread at the CURRENT positions is ABOVE the
         guardrail (R7.1/7.2 — while at/below the guardrail, hold), AND
      2. the predicted spread improvement (current_spread - proposed_spread)
         is AT LEAST the improvement deadband (R7.3).

    UNLESS ``gate.floor_requires_open`` is True — a move strictly required to
    reach the airflow-safety floor is ALWAYS allowed and bypasses both gates
    (R7.5). Anti-chatter / batch limits are enforced later in the coordinator,
    NOT here.
"""

from __future__ import annotations

import importlib.util
import pathlib
import sys

_BALANCE_PATH = pathlib.Path(__file__).resolve().parent.parent / "custom_components" / "hvac_vent_optimizer" / "balance.py"
_spec = importlib.util.spec_from_file_location("hvo_balance", _BALANCE_PATH)
balance = importlib.util.module_from_spec(_spec)
# Register before exec so dataclass introspection (with `from __future__ import
# annotations`) can resolve the module by name.
sys.modules[_spec.name] = balance
_spec.loader.exec_module(balance)


SETPOINT_C = 26.1
MODE = balance.MODE_COOLING


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _room(
    room_id: str,
    temp_c: float,
    *,
    efficiency: float = 0.02,
    leak: float = 0.0,
    active: bool = True,
    current_open: float = 0.0,
):
    """Build a RoomAllocInput. ``leak=0`` by default so projected rates are
    exact (rate == efficiency * aperture), which lets the spread-math tests use
    clean, hand-computed boundary values with no leak-drift fuzz."""
    return balance.RoomAllocInput(
        room_id=room_id,
        temp_c=temp_c,
        active=active,
        efficiency=efficiency,
        leak=leak,
        current_open=current_open,
        vent_ids=(f"{room_id}_v",),
    )


def _gate(*, floor_requires_open: bool = False):
    return balance.GateContext(
        mode=MODE,
        setpoint_c=SETPOINT_C,
        floor_requires_open=floor_requires_open,
    )


def _settings(**overrides):
    """Default A5 tunables (guardrail 1.0, deadband 0.3); horizon 30 min."""
    base = {"horizon_min": 30.0}
    base.update(overrides)
    return balance.AllocSettings(**base)


# ---------------------------------------------------------------------------
# AllocSettings now carries the A5 tunables (design config table / R7.1/R7.3)
# ---------------------------------------------------------------------------
class TestAllocSettingsGatingFields:
    def test_defaults_match_design(self):
        s = balance.AllocSettings()
        assert s.spread_guardrail_c == 1.0
        assert s.spread_improvement_deadband_c == 0.3

    def test_fields_are_overridable(self):
        s = balance.AllocSettings(spread_guardrail_c=1.5, spread_improvement_deadband_c=0.5)
        assert s.spread_guardrail_c == 1.5
        assert s.spread_improvement_deadband_c == 0.5


# ---------------------------------------------------------------------------
# R7.1/R7.2 — hold while predicted spread is at/below the guardrail
# ---------------------------------------------------------------------------
class TestGuardrailHold:
    def test_below_guardrail_holds(self):
        # Two unsatisfied cooling rooms 0.5 C apart, all vents closed → current
        # predicted spread ~0.5 C <= 1.0 guardrail → hold (don't chase).
        rooms = [_room("A", 26.5), _room("B", 27.0)]
        current = {"A": 0.0, "B": 0.0}
        # Proposed would equalize further, but we are already balanced enough.
        proposed = {"A": 0.0, "B": 100.0}
        assert balance.should_apply(current, proposed, rooms, _settings(), _gate()) is False

    def test_spread_exactly_at_guardrail_holds(self):
        # leak=0 + closed vents → projected temps == current temps exactly.
        # Spread is exactly 1.0 == guardrail → "at or below" → hold (R7.1).
        rooms = [_room("A", 26.5), _room("B", 27.5)]
        current = {"A": 0.0, "B": 0.0}
        proposed = {"A": 0.0, "B": 100.0}
        assert balance.should_apply(current, proposed, rooms, _settings(), _gate()) is False


# ---------------------------------------------------------------------------
# R7.3 — improvement deadband
# ---------------------------------------------------------------------------
class TestImprovementDeadband:
    def test_trivial_improvement_holds(self):
        # Current spread 1.5 C (> guardrail) but proposed barely changes it:
        # opening B 5 % drops it ~0.09 C → improvement < 0.3 deadband → hold.
        rooms = [_room("A", 26.5), _room("B", 28.0)]
        current = {"A": 0.0, "B": 0.0}
        proposed = {"A": 0.0, "B": 5.0}
        assert balance.should_apply(current, proposed, rooms, _settings(), _gate()) is False

    def test_improvement_exactly_at_deadband_applies(self):
        # leak=0: rate = efficiency * aperture. Open B to 50 % → rate 0.01,
        # 30 min → 0.3 C drop. current spread 1.5 → proposed spread 1.2 →
        # improvement exactly 0.3 == deadband → apply (R7.3 "at least").
        rooms = [_room("A", 26.6), _room("B", 28.1)]
        current = {"A": 0.0, "B": 0.0}
        proposed = {"A": 0.0, "B": 50.0}
        assert balance.should_apply(current, proposed, rooms, _settings(), _gate()) is True

    def test_meaningful_improvement_applies(self):
        # Current spread 1.5 C (> guardrail); opening B fully drops it to
        # ~0.96 C → improvement ~0.54 >= deadband AND spread > guardrail → apply.
        rooms = [_room("A", 26.5), _room("B", 28.0)]
        current = {"A": 0.0, "B": 0.0}
        proposed = {"A": 0.0, "B": 100.0}
        assert balance.should_apply(current, proposed, rooms, _settings(), _gate()) is True


# ---------------------------------------------------------------------------
# R7.5 — a move required to reach the safety floor is always allowed
# ---------------------------------------------------------------------------
class TestFloorBypass:
    def test_floor_open_bypasses_guardrail_hold(self):
        # Below-guardrail scenario that would otherwise hold, but the floor
        # requires opening a vent → always allowed (immediate), gating bypassed.
        rooms = [_room("A", 26.5), _room("B", 27.0)]
        current = {"A": 0.0, "B": 0.0}
        proposed = {"A": 0.0, "B": 0.0}
        assert (
            balance.should_apply(current, proposed, rooms, _settings(), _gate(floor_requires_open=True))
            is True
        )

    def test_floor_open_bypasses_deadband_hold(self):
        # Trivial-improvement scenario that would otherwise hold, but floor
        # requires opening → allowed.
        rooms = [_room("A", 26.5), _room("B", 28.0)]
        current = {"A": 0.0, "B": 0.0}
        proposed = {"A": 0.0, "B": 5.0}
        assert (
            balance.should_apply(current, proposed, rooms, _settings(), _gate(floor_requires_open=True))
            is True
        )


# ---------------------------------------------------------------------------
# Configurability (R7.1/R7.3) + purity (R19.2)
# ---------------------------------------------------------------------------
class TestConfigurableThresholds:
    def test_raised_guardrail_holds_a_move_that_would_otherwise_apply(self):
        rooms = [_room("A", 26.5), _room("B", 28.0)]
        current = {"A": 0.0, "B": 0.0}
        proposed = {"A": 0.0, "B": 100.0}
        # Current spread ~1.5; with guardrail 2.0 we are at/below it → hold.
        settings = _settings(spread_guardrail_c=2.0)
        assert balance.should_apply(current, proposed, rooms, settings, _gate()) is False

    def test_raised_deadband_holds_a_modest_move(self):
        rooms = [_room("A", 26.5), _room("B", 28.0)]
        current = {"A": 0.0, "B": 0.0}
        proposed = {"A": 0.0, "B": 100.0}
        # Improvement ~0.54; with deadband 1.0 it is too small → hold.
        settings = _settings(spread_improvement_deadband_c=1.0)
        assert balance.should_apply(current, proposed, rooms, settings, _gate()) is False


class TestPurity:
    def test_does_not_mutate_inputs(self):
        rooms = [_room("A", 26.5), _room("B", 28.0)]
        current = {"A": 0.0, "B": 0.0}
        proposed = {"A": 0.0, "B": 100.0}
        current_copy = dict(current)
        proposed_copy = dict(proposed)
        balance.should_apply(current, proposed, rooms, _settings(), _gate())
        assert current == current_copy
        assert proposed == proposed_copy

    def test_deterministic(self):
        rooms = [_room("A", 26.5), _room("B", 28.0)]
        current = {"A": 0.0, "B": 0.0}
        proposed = {"A": 0.0, "B": 100.0}
        r1 = balance.should_apply(current, proposed, rooms, _settings(), _gate())
        r2 = balance.should_apply(current, proposed, rooms, _settings(), _gate())
        assert r1 == r2 is True
