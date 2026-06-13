"""Edge-case tests for balance.allocate (Task 9.3 — TDD).

Covers the three degenerate allocation inputs called out by the design
("Degenerate allocation inputs handled: single active room (R4.6), all
satisfied (R4.7), mid-operation mode change (R4.8)"):

  1. R4.6 — exactly one active room: drive it toward setpoint, predicted
     spread is 0 (single room => undefined/zero), and allocate never errors.
  2. R4.7 — all active rooms satisfied: every PRE-FLOOR target is 0 % (the
     safety floor that maintains minimum airflow is Task 10, so here we only
     assert the pure A1 result is all-zero and does not crash).
  3. R4.8 — conditioning mode change mid-operation: allocate is a pure,
     stateless function, so a mode change is just a different ``mode`` arg.
     The same rooms recomputed with ``mode="heating"`` vs ``mode="cooling"``
     use the correct directional error, so a room that is *satisfied* cooling
     can be *unsatisfied* heating and the allocation reflects that. The "new
     cycle anchor" behaviour itself is owned by the coordinator (Task 15/18);
     the pure function correctly handles a mode-argument change by recomputing
     from scratch with no carried-over state.

balance.py is a PURE module (no Home Assistant imports). It is loaded
standalone by absolute path so we never import the ``hvac_vent_optimizer``
package (whose __init__ pulls in Home Assistant, which is not installed in the
test environment). This mirrors the convention in test_balance_allocate.py.

    cd tests && python3 -m pytest test_balance_edgecases.py -q
    python3 -m pytest tests/test_balance_edgecases.py -q --import-mode=importlib
"""

from __future__ import annotations

import importlib.util
import pathlib
import sys

_BALANCE_PATH = pathlib.Path(__file__).resolve().parent.parent / "custom_components" / "hvac_vent_optimizer" / "balance.py"
_spec = importlib.util.spec_from_file_location("hvo_balance", _BALANCE_PATH)
balance = importlib.util.module_from_spec(_spec)
# Register before exec so dataclasses introspection (with `from __future__
# import annotations`) can resolve the module by name.
sys.modules[_spec.name] = balance
_spec.loader.exec_module(balance)


SETPOINT_C = 26.1
LEAK = 0.1


def _room(room_id: str, temp_c: float, efficiency: float, active: bool = True):
    return balance.RoomAllocInput(
        room_id=room_id,
        temp_c=temp_c,
        active=active,
        efficiency=efficiency,
        leak=LEAK,
        current_open=0.0,
        vent_ids=(f"vent_{room_id.lower()}",),
    )


def _settings():
    """Isolate pure A1: no floor padding, fine granularity, no cross-coupling."""
    return balance.AllocSettings(
        safety_floor_pct=0.0,
        granularity=1,
        crosscoupling=False,
        hysteresis_c=balance.DEFAULT_HYSTERESIS_C,
    )


# ---------------------------------------------------------------------------
# R4.6 — exactly one active room.
# _Requirements: 4.6_
# ---------------------------------------------------------------------------
class TestSingleActiveRoom:
    def test_single_unsatisfied_room_drives_to_full_open(self):
        # One room well above the cooling setpoint => it is the sole bottleneck
        # (tau* = its own full-open finish) => 100 % open to drive to setpoint.
        rooms = [_room("Solo", temp_c=28.0, efficiency=0.02)]
        result = balance.allocate(rooms, SETPOINT_C, balance.MODE_COOLING, _settings())
        assert result.targets == {"Solo": 100.0}

    def test_single_room_predicted_spread_is_zero(self):
        # Spread is undefined/zero with a single room (R4.6).
        rooms = [_room("Solo", temp_c=28.0, efficiency=0.02)]
        result = balance.allocate(rooms, SETPOINT_C, balance.MODE_COOLING, _settings())
        assert result.predicted_spread_c == 0.0

    def test_single_satisfied_room_closes_and_spread_zero(self):
        # Already at/below setpoint (overshoot close) => 0 %, spread still 0.
        rooms = [_room("Solo", temp_c=25.0, efficiency=0.02)]
        result = balance.allocate(rooms, SETPOINT_C, balance.MODE_COOLING, _settings())
        assert result.targets == {"Solo": 0.0}
        assert result.predicted_spread_c == 0.0

    def test_single_room_with_inactive_others_uses_only_the_active_one(self):
        # Inactive rooms are excluded from the objective and from targets.
        rooms = [
            _room("Solo", temp_c=28.0, efficiency=0.02, active=True),
            _room("Off1", temp_c=29.0, efficiency=0.02, active=False),
            _room("Off2", temp_c=24.0, efficiency=0.02, active=False),
        ]
        result = balance.allocate(rooms, SETPOINT_C, balance.MODE_COOLING, _settings())
        assert set(result.targets) == {"Solo"}
        assert result.targets["Solo"] == 100.0
        assert result.predicted_spread_c == 0.0

    def test_single_room_does_not_error(self):
        # Sanity: the call simply must not raise (no spread div-by-zero, etc.).
        rooms = [_room("Solo", temp_c=27.0, efficiency=0.033)]
        result = balance.allocate(rooms, SETPOINT_C, balance.MODE_COOLING, _settings())
        assert "Solo" in result.targets
        assert isinstance(result.predicted_spread_c, float)


# ---------------------------------------------------------------------------
# R4.7 — all active rooms satisfied.
# _Requirements: 4.7_
# ---------------------------------------------------------------------------
class TestAllSatisfied:
    def _all_satisfied_rooms(self):
        # All comfortably below the cooling setpoint (past the hysteresis band).
        return [
            _room("A", temp_c=25.0, efficiency=0.02),
            _room("B", temp_c=24.5, efficiency=0.05),
            _room("C", temp_c=25.4, efficiency=0.07),
        ]

    def test_all_pre_floor_targets_are_zero(self):
        # The safety floor (minimum airflow) is Task 10; the pure A1 result
        # before the floor must drive every satisfied vent to 0 %.
        result = balance.allocate(
            self._all_satisfied_rooms(), SETPOINT_C, balance.MODE_COOLING, _settings()
        )
        assert result.targets == {"A": 0.0, "B": 0.0, "C": 0.0}

    def test_all_satisfied_no_airflow_limited_and_floor_not_binding(self):
        result = balance.allocate(
            self._all_satisfied_rooms(), SETPOINT_C, balance.MODE_COOLING, _settings()
        )
        assert result.airflow_limited == frozenset()
        assert result.floor_binding is False

    def test_all_satisfied_predicted_spread_is_zero(self):
        # Satisfied temps are clamped at setpoint in the projection, so leak
        # drift cannot inflate spread; all land at setpoint => spread 0.
        result = balance.allocate(
            self._all_satisfied_rooms(), SETPOINT_C, balance.MODE_COOLING, _settings()
        )
        assert result.predicted_spread_c == 0.0

    def test_all_satisfied_does_not_crash_and_finishes_are_zero(self):
        result = balance.allocate(
            self._all_satisfied_rooms(), SETPOINT_C, balance.MODE_COOLING, _settings()
        )
        assert all(v == 0.0 for v in result.predicted_finish_min.values())


# ---------------------------------------------------------------------------
# R4.8 — conditioning mode change mid-operation.
# _Requirements: 4.8_
# ---------------------------------------------------------------------------
class TestModeChange:
    """allocate is pure: a mode change is a different ``mode`` argument.

    The "treat as a new cycle anchor" behaviour is enforced by the coordinator
    (Task 15/18) because the pure function holds no anchor state. What we prove
    here is that the pure function correctly *recomputes from scratch* with the
    directionally-correct error for the supplied mode, so the coordinator can
    rely on it after a mode flip.
    """

    def test_room_below_setpoint_flips_satisfaction_with_mode(self):
        # Same room+setpoint, only the mode differs. 24.0 °C vs setpoint 26.1:
        #   cooling  -> already below setpoint  -> satisfied  -> 0 %
        #   heating  -> below setpoint, needs heat -> unsatisfied -> opens
        room = [_room("Flip", temp_c=24.0, efficiency=0.02)]

        cooling = balance.allocate(room, SETPOINT_C, balance.MODE_COOLING, _settings())
        heating = balance.allocate(room, SETPOINT_C, balance.MODE_HEATING, _settings())

        assert cooling.targets["Flip"] == 0.0
        assert heating.targets["Flip"] > 0.0

    def test_room_above_setpoint_flips_satisfaction_with_mode(self):
        # 28.0 °C vs setpoint 26.1:
        #   cooling -> above setpoint, needs cooling -> unsatisfied -> opens
        #   heating -> already above setpoint        -> satisfied   -> 0 %
        room = [_room("Flip", temp_c=28.0, efficiency=0.02)]

        cooling = balance.allocate(room, SETPOINT_C, balance.MODE_COOLING, _settings())
        heating = balance.allocate(room, SETPOINT_C, balance.MODE_HEATING, _settings())

        assert cooling.targets["Flip"] > 0.0
        assert heating.targets["Flip"] == 0.0

    def test_mode_change_recomputes_whole_set_from_scratch(self):
        # A mixed set: under heating the bottleneck/throttle differs from
        # cooling because each room's directional error differs. Assert the
        # two modes are self-consistent and independent (no carried state):
        # the coldest room is the heating bottleneck (100 %), the hottest the
        # cooling bottleneck (100 %).
        rooms = [
            _room("Hot", temp_c=28.0, efficiency=0.02),
            _room("Warm", temp_c=26.5, efficiency=0.02),
            _room("Cold", temp_c=23.0, efficiency=0.02),
        ]

        cooling = balance.allocate(rooms, SETPOINT_C, balance.MODE_COOLING, _settings())
        heating = balance.allocate(rooms, SETPOINT_C, balance.MODE_HEATING, _settings())

        # Cooling: Hot is farthest above setpoint -> full-open bottleneck;
        # Cold is below setpoint -> satisfied -> 0 %.
        assert cooling.targets["Hot"] == 100.0
        assert cooling.targets["Cold"] == 0.0

        # Heating: Cold is farthest below setpoint -> full-open bottleneck;
        # Hot is above setpoint -> satisfied -> 0 %.
        assert heating.targets["Cold"] == 100.0
        assert heating.targets["Hot"] == 0.0

    def test_mode_change_is_stateless_recompute_order_independent(self):
        # Running cooling-then-heating must give the same heating result as
        # heating alone (no anchor/state leaks between calls -> "new anchor").
        rooms = [
            _room("Hot", temp_c=28.0, efficiency=0.02),
            _room("Cold", temp_c=23.0, efficiency=0.02),
        ]
        balance.allocate(rooms, SETPOINT_C, balance.MODE_COOLING, _settings())
        heating_after_cooling = balance.allocate(
            rooms, SETPOINT_C, balance.MODE_HEATING, _settings()
        )
        heating_alone = balance.allocate(
            rooms, SETPOINT_C, balance.MODE_HEATING, _settings()
        )
        assert heating_after_cooling.targets == heating_alone.targets
        assert (
            heating_after_cooling.predicted_finish_min
            == heating_alone.predicted_finish_min
        )
