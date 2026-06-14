"""Tests for airflow-limited detection (A3) + cross-coupling (A4) — Task 11 (TDD).

TESTS-FIRST: this file is written BEFORE the A4 cross-coupling guard
(``apply_cross_coupling`` / ``DuctSignals``) exists. The A3 airflow-limited
detection already lives in ``allocate()`` (Task 9.2); the tests in
``TestAirflowLimitedDetection`` *verify and pin* that behaviour (the Bedroom 2
case + margin/error boundaries). The tests in ``TestCrossCoupling`` /
``TestDuctSignals`` are RED until Task 11 implements the explicit A4 guard.

balance.py is a PURE module (no Home Assistant imports). It is loaded standalone
by absolute path so we never import the ``hvac_vent_optimizer`` package (whose
``__init__`` pulls in Home Assistant, which is not installed in the test
environment). This mirrors the ``hvo_balance`` convention used by the sibling
test_balance_*.py files.

    cd tests && python3 -m pytest test_balance_crosscoupling.py -q
    python3 -m pytest tests/test_balance_crosscoupling.py -q --import-mode=importlib

============================================================================
PINNED CONTRACT FOR TASK 11
============================================================================

A3 — Airflow-limited detection (already in ``allocate``, design A3 / R5.1):
    A room is airflow-limited iff its commanded target ``>= 100 - margin_pct``
    AND its signed error ``> error_c``. Surfaced as ``AllocResult.airflow_limited``
    (a ``frozenset`` of room ids). Only *unsatisfied* rooms qualify.

A4 — Cross-coupling guard (design A4 / R6):
    ``apply_cross_coupling(targets, rooms, mode, setpoint_c, settings,
                           airflow_limited, duct=None) -> dict[str, float]``

    Pure. Returns a NEW targets dict (never mutates the input). Behaviour:
    * When ``settings.crosscoupling`` is True AND at least one room is
      airflow-limited, every active room that is at/past setpoint
      (``signed_error <= 0``) and NOT itself airflow-limited is driven to 0 %
      to redirect airflow / raise duct pressure toward the laggard (R6.1).
    * The reduction is floor-agnostic: cross-coupling only ever *closes* rooms;
      the safety-floor choke point (``apply_safety_floor``) is what guarantees
      the combined never drops below the floor (R6.2). Because cross-coupling
      pushes satisfied rooms to 0 % and the floor only ever re-pads
      *not-yet-satisfied* rooms, the floor never reopens a satisfied room.
    * ``settings.crosscoupling == False`` disables the extra push entirely (R6.4).
    * No airflow-limited room ⇒ no push (nothing to feed).

    Optional duct signals (``DuctSignals``, R6.3):
    * WHERE duct-temperature and/or duct-pressure signals are provided and they
      indicate conditioned air is NOT actually flowing, the cross-coupling push
      is vetoed (a pointless move): targets are returned unchanged.
    * Their ABSENCE (``duct=None`` or no usable fields) degrades gracefully —
      the temperature/efficiency heuristic still applies (push proceeds).
"""

from __future__ import annotations

import importlib.util
import pathlib
import sys

_BALANCE_PATH = (
    pathlib.Path(__file__).resolve().parent.parent
    / "custom_components"
    / "hvac_vent_optimizer"
    / "balance.py"
)
_spec = importlib.util.spec_from_file_location("hvo_balance", _BALANCE_PATH)
balance = importlib.util.module_from_spec(_spec)
# Register before exec so dataclass introspection (with `from __future__ import
# annotations`) can resolve the module by name.
sys.modules[_spec.name] = balance
_spec.loader.exec_module(balance)


SETPOINT_C = 26.1
LEAK = 0.1
MODE = balance.MODE_COOLING


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _room(
    room_id: str,
    temp_c: float,
    efficiency: float,
    *,
    active: bool = True,
    leak: float = LEAK,
    vent_ids: tuple[str, ...] | None = None,
):
    if vent_ids is None:
        vent_ids = (f"vent_{room_id.lower()}",)
    return balance.RoomAllocInput(
        room_id=room_id,
        temp_c=temp_c,
        active=active,
        efficiency=efficiency,
        leak=leak,
        current_open=0.0,
        vent_ids=vent_ids,
        signed_error_c=balance._signed_error(MODE, SETPOINT_C, temp_c),
    )


def _settings(**overrides):
    base = {
        "safety_floor_pct": 0.0,
        "granularity": 1,
        "crosscoupling": True,
        "hysteresis_c": balance.DEFAULT_HYSTERESIS_C,
        "airflow_limited_margin_pct": 5.0,
        "airflow_limited_error_c": 0.5,
    }
    base.update(overrides)
    return balance.AllocSettings(**base)


# Worked-example rooms (design A1b — cooling, setpoint 26.1 °C).
_ROOM_DATA: dict[str, tuple[float, float]] = {
    "Bedroom 2": (27.9, 0.017),
    "Bedroom 3": (27.7, 0.020),
    "Bedroom 1": (26.6, 0.072),
    "Master": (26.4, 0.053),
    "Guest": (27.0, 0.033),
    "Bathroom": (25.7, 0.438),
}


def _worked_rooms():
    return [_room(rid, temp, eff) for rid, (temp, eff) in _ROOM_DATA.items()]


# ===========================================================================
# A3 — airflow-limited detection (verify the existing allocate() behaviour).
# _Requirements: 5.1_
# ===========================================================================
class TestAirflowLimitedDetection:
    def test_bedroom_2_case_is_flagged(self):
        # Bedroom 2 is the bottleneck (lowest efficiency, largest tau) → pinned at
        # 100 % with err 1.8 > error_c 0.5 → airflow-limited.
        result = balance.allocate(_worked_rooms(), SETPOINT_C, MODE, _settings())
        assert result.targets["Bedroom 2"] == 100.0
        assert "Bedroom 2" in result.airflow_limited

    def test_throttled_room_below_margin_not_flagged(self):
        # Bedroom 3 is off-target by 1.6 °C (> error_c) but throttled to ~75 % which
        # is below the 95 % margin threshold → NOT airflow-limited (margin gate).
        result = balance.allocate(_worked_rooms(), SETPOINT_C, MODE, _settings())
        assert result.targets["Bedroom 3"] < 95.0
        assert "Bedroom 3" not in result.airflow_limited

    def test_satisfied_room_never_flagged(self):
        result = balance.allocate(_worked_rooms(), SETPOINT_C, MODE, _settings())
        assert "Bathroom" not in result.airflow_limited

    def test_error_threshold_boundary(self):
        # A single room pinned at 100 % (sole bottleneck). Just above error_c →
        # flagged; just below error_c → NOT flagged. temp = setpoint + err.
        just_above = [_room("Solo", temp_c=SETPOINT_C + 0.6, efficiency=0.02)]
        just_below = [_room("Solo", temp_c=SETPOINT_C + 0.4, efficiency=0.02)]
        s = _settings(airflow_limited_error_c=0.5)

        r_above = balance.allocate(just_above, SETPOINT_C, MODE, s)
        r_below = balance.allocate(just_below, SETPOINT_C, MODE, s)

        assert r_above.targets["Solo"] == 100.0
        assert r_below.targets["Solo"] == 100.0
        assert "Solo" in r_above.airflow_limited  # err 0.6 > 0.5
        assert "Solo" not in r_below.airflow_limited  # err 0.4 !> 0.5

    def test_margin_threshold_boundary(self):
        # A room pinned at 100 % is flagged at margin 0 (threshold 100, 100>=100)
        # and still flagged at margin 5 (threshold 95). The error is large.
        rooms = [_room("Solo", temp_c=SETPOINT_C + 1.5, efficiency=0.02)]
        r0 = balance.allocate(rooms, SETPOINT_C, MODE, _settings(airflow_limited_margin_pct=0.0))
        r5 = balance.allocate(rooms, SETPOINT_C, MODE, _settings(airflow_limited_margin_pct=5.0))
        assert "Solo" in r0.airflow_limited
        assert "Solo" in r5.airflow_limited


# ===========================================================================
# A4 — cross-coupling guard contract surface (RED until Task 11).
# ===========================================================================
class TestCrossCouplingContractExists:
    def test_apply_cross_coupling_exists(self):
        assert hasattr(balance, "apply_cross_coupling")
        assert callable(balance.apply_cross_coupling)

    def test_duct_signals_dataclass_exists(self):
        assert hasattr(balance, "DuctSignals")
        # Constructible with no args (all fields optional → graceful absence).
        balance.DuctSignals()


# ===========================================================================
# A4 — cross-coupling push behaviour.
# _Requirements: 5.3, 6.1, 6.2, 6.4_
# ===========================================================================
class TestCrossCoupling:
    def test_satisfied_room_pushed_to_zero_when_airflow_limited(self):
        # Laggard "Bedroom 2" is airflow-limited; a satisfied room hypothetically
        # carrying residual aperture must be pushed to 0 % to feed the laggard.
        rooms = [
            _room("Bedroom 2", temp_c=27.9, efficiency=0.017),  # err +1.8
            _room("Bathroom", temp_c=25.7, efficiency=0.438),  # err -0.4 (satisfied)
        ]
        targets = {"Bedroom 2": 100.0, "Bathroom": 30.0}  # satisfied room non-zero
        airflow_limited = frozenset({"Bedroom 2"})
        new = balance.apply_cross_coupling(targets, rooms, MODE, SETPOINT_C, _settings(), airflow_limited)
        assert new["Bathroom"] == 0.0  # pushed closed
        assert new["Bedroom 2"] == 100.0  # laggard untouched
        # Input dict is not mutated (pure).
        assert targets["Bathroom"] == 30.0

    def test_airflow_limited_room_itself_not_closed(self):
        # The laggard is off-target (err > 0), so it is never a push target even
        # though the guard runs.
        rooms = [_room("Bedroom 2", temp_c=27.9, efficiency=0.017)]
        targets = {"Bedroom 2": 100.0}
        new = balance.apply_cross_coupling(
            targets, rooms, MODE, SETPOINT_C, _settings(), frozenset({"Bedroom 2"})
        )
        assert new["Bedroom 2"] == 100.0

    def test_no_push_when_no_airflow_limited_room(self):
        # Nothing is airflow-limited → no bottleneck to feed → satisfied room
        # keeps whatever aperture allocation gave it.
        rooms = [
            _room("Warm", temp_c=26.5, efficiency=0.05),
            _room("Bathroom", temp_c=25.7, efficiency=0.438),
        ]
        targets = {"Warm": 50.0, "Bathroom": 30.0}
        new = balance.apply_cross_coupling(targets, rooms, MODE, SETPOINT_C, _settings(), frozenset())
        assert new == targets

    def test_disabled_crosscoupling_does_not_push(self):
        # settings.crosscoupling == False disables the extra push entirely (R6.4).
        rooms = [
            _room("Bedroom 2", temp_c=27.9, efficiency=0.017),
            _room("Bathroom", temp_c=25.7, efficiency=0.438),
        ]
        targets = {"Bedroom 2": 100.0, "Bathroom": 30.0}
        new = balance.apply_cross_coupling(
            targets,
            rooms,
            MODE,
            SETPOINT_C,
            _settings(crosscoupling=False),
            frozenset({"Bedroom 2"}),
        )
        assert new["Bathroom"] == 30.0  # untouched
        assert new == targets

    def test_unsatisfied_non_limited_room_not_closed(self):
        # A room still needing conditioning (err > 0) but not airflow-limited
        # must NOT be closed by cross-coupling — only at/past-setpoint rooms are.
        rooms = [
            _room("Bedroom 2", temp_c=27.9, efficiency=0.017),  # limited laggard
            _room("Guest", temp_c=27.0, efficiency=0.033),  # err +0.9, throttled
        ]
        targets = {"Bedroom 2": 100.0, "Guest": 18.0}
        new = balance.apply_cross_coupling(
            targets, rooms, MODE, SETPOINT_C, _settings(), frozenset({"Bedroom 2"})
        )
        assert new["Guest"] == 18.0  # still needs air → left alone

    def test_inactive_room_not_touched(self):
        # Inactive rooms are held by the coordinator; cross-coupling ignores them.
        rooms = [
            _room("Bedroom 2", temp_c=27.9, efficiency=0.017),
            _room("OffSat", temp_c=24.0, efficiency=0.4, active=False),
        ]
        targets = {"Bedroom 2": 100.0, "OffSat": 30.0}
        new = balance.apply_cross_coupling(
            targets, rooms, MODE, SETPOINT_C, _settings(), frozenset({"Bedroom 2"})
        )
        assert new["OffSat"] == 30.0


# ===========================================================================
# A4 — cross-coupling never drops the combined below the floor (R6.2).
# The floor choke point owns the guarantee; here we prove the satisfied room
# pushed to 0 is NOT reopened by the floor (it re-pads the laggard instead).
# _Requirements: 6.2_
# ===========================================================================
class TestCrossCouplingFloorInteraction:
    def test_floor_does_not_reopen_pushed_satisfied_room(self):
        rooms = [
            _room("Need", temp_c=26.6, efficiency=0.02),  # err +0.5 (>0, eligible)
            _room("Sat", temp_c=25.0, efficiency=0.4),  # err -1.1 (satisfied)
        ]
        # Cross-coupling pushes Sat → 0 (Need is the airflow-limited laggard).
        # Start below the floor (combined 15 %) so the floor genuinely pads.
        targets = {"Need": 30.0, "Sat": 0.0}
        pushed = balance.apply_cross_coupling(
            targets, rooms, MODE, SETPOINT_C, _settings(), frozenset({"Need"})
        )
        assert pushed["Sat"] == 0.0
        # Now the floor (40 %) pads — it must lift the laggard, never reopen Sat.
        floored, binding = balance.apply_safety_floor(
            dict(pushed), rooms, _settings(safety_floor_pct=40.0, granularity=5)
        )
        assert binding is True
        assert floored["Sat"] == 0.0  # satisfied room stays closed
        assert balance.combined_open_pct(floored, _settings(safety_floor_pct=40.0)) >= 40.0


# ===========================================================================
# A4 — optional duct signals (R6.3).
# _Requirements: 6.3_
# ===========================================================================
class TestDuctSignals:
    def _scenario(self):
        rooms = [
            _room("Bedroom 2", temp_c=27.9, efficiency=0.017),
            _room("Bathroom", temp_c=25.7, efficiency=0.438),
        ]
        targets = {"Bedroom 2": 100.0, "Bathroom": 30.0}
        return rooms, targets, frozenset({"Bedroom 2"})

    def test_duct_indicating_no_airflow_vetoes_push(self):
        # Cooling but the duct is NOT delivering cold air (duct temp ≈ ambient)
        # and static pressure ≈ 0 → conditioned air is not flowing → a
        # cross-coupling move is pointless and must be vetoed.
        rooms, targets, limited = self._scenario()
        duct = balance.DuctSignals(duct_temp_c=26.0, duct_pressure_pa=0.0)
        new = balance.apply_cross_coupling(targets, rooms, MODE, SETPOINT_C, _settings(), limited, duct=duct)
        assert new["Bathroom"] == 30.0  # veto → unchanged
        assert new == targets

    def test_duct_confirming_airflow_allows_push(self):
        # Cold supply air well below setpoint → conditioned air confirmed
        # flowing → the push proceeds.
        rooms, targets, limited = self._scenario()
        duct = balance.DuctSignals(duct_temp_c=12.0, duct_pressure_pa=60.0)
        new = balance.apply_cross_coupling(targets, rooms, MODE, SETPOINT_C, _settings(), limited, duct=duct)
        assert new["Bathroom"] == 0.0  # push applied

    def test_absent_duct_signals_degrade_gracefully(self):
        # No duct signals at all → heuristic still applies (push proceeds).
        rooms, targets, limited = self._scenario()
        new_none = balance.apply_cross_coupling(
            targets, rooms, MODE, SETPOINT_C, _settings(), limited, duct=None
        )
        new_empty = balance.apply_cross_coupling(
            targets, rooms, MODE, SETPOINT_C, _settings(), limited, duct=balance.DuctSignals()
        )
        assert new_none["Bathroom"] == 0.0
        assert new_empty["Bathroom"] == 0.0

    def test_partial_duct_pressure_only_vetoes_when_no_flow(self):
        # Only a pressure signal, reading ~0 → no airflow → veto.
        rooms, targets, limited = self._scenario()
        duct = balance.DuctSignals(duct_pressure_pa=0.0)
        new = balance.apply_cross_coupling(targets, rooms, MODE, SETPOINT_C, _settings(), limited, duct=duct)
        assert new["Bathroom"] == 30.0  # vetoed

    def test_partial_duct_temp_only_confirms_flow(self):
        # Only a (cold) duct-temp signal → flow confirmed → push proceeds.
        rooms, targets, limited = self._scenario()
        duct = balance.DuctSignals(duct_temp_c=12.0)
        new = balance.apply_cross_coupling(targets, rooms, MODE, SETPOINT_C, _settings(), limited, duct=duct)
        assert new["Bathroom"] == 0.0


# ===========================================================================
# A4 — allocate() integrates the explicit guard (still pure & deterministic).
# _Requirements: 6.1, 6.4_
# ===========================================================================
class TestAllocateIntegration:
    def test_allocate_keeps_satisfied_closed_with_limited_room(self):
        # In the worked example Bedroom 2 is airflow-limited and the satisfied
        # Bathroom is at 0 %; with cross-coupling on, that holds.
        result = balance.allocate(_worked_rooms(), SETPOINT_C, MODE, _settings(crosscoupling=True))
        assert "Bedroom 2" in result.airflow_limited
        assert result.targets["Bathroom"] == 0.0

    def test_allocate_accepts_optional_duct_argument(self):
        # allocate threads an optional duct signal through to the guard without
        # changing the satisfied-room result (already 0 from A1).
        duct = balance.DuctSignals(duct_temp_c=12.0, duct_pressure_pa=60.0)
        result = balance.allocate(_worked_rooms(), SETPOINT_C, MODE, _settings(crosscoupling=True), duct=duct)
        assert result.targets["Bathroom"] == 0.0

    def test_allocate_deterministic_with_crosscoupling(self):
        first = balance.allocate(_worked_rooms(), SETPOINT_C, MODE, _settings(crosscoupling=True))
        second = balance.allocate(_worked_rooms(), SETPOINT_C, MODE, _settings(crosscoupling=True))
        assert first.targets == second.targets
        assert first.airflow_limited == second.airflow_limited
