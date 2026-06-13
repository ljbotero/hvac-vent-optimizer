"""Tests for the CRITICAL airflow-safety floor (Task 10.1 — TESTS-FIRST / TDD).

TESTS-FIRST: this file is written BEFORE ``combined_open_pct()`` and
``apply_safety_floor()`` exist (Task 10.2 implements them). Until then every
test that touches those symbols is EXPECTED TO FAIL — that failing red step is
the whole point. This is the single safety choke point every strategy routes
through (design A2, R3, decision D1), so the tests are deliberately thorough.

``balance.py`` is a PURE module (no Home Assistant imports). It is loaded
standalone by absolute path so we never import the ``hvac_vent_optimizer``
package (whose ``__init__`` pulls in Home Assistant, which is not installed in
the test environment). This mirrors the ``hvo_balance`` convention used by
test_balance_classify.py / test_balance_allocate.py / test_balance_edgecases.py.

    cd tests && python3 -m pytest test_balance_safety.py -q
    python3 -m pytest tests/test_balance_safety.py -q --import-mode=importlib

============================================================================
PINNED CONTRACT FOR TASK 10.2 (this file is the executable specification)
============================================================================

1. ``combined_open_pct(targets: dict[str, float], settings: AllocSettings) -> float``

   Returns the combined open percentage (0..100 scale) across *all* airflow
   devices on the thermostat, per design A2::

       combined = ( Σ targets_v
                    + conventional_vents * conventional_open_pct
                    + inactive_open_pct_sum )                      # held inactive (R3.7)
                  / ( n_smart + conventional_vents + inactive_count )

   * ``targets`` is keyed **per device** — each key counts as exactly ONE
     airflow device, so ``n_smart = len(targets)``. (``AllocSettings`` has no
     smart-vent count field, which fixes this interpretation.)
   * The numerator uses **commanded aperture only** — there is no ``leak``
     parameter, so leakage can never inflate the number and relax the floor
     (R25.9). A device commanded to 0 % contributes 0, not its leak fraction.
   * Conventional vents contribute ``conventional_vents * conventional_open_pct``
     (R3.6); held-open inactive vents contribute ``inactive_open_pct_sum`` over
     ``inactive_count`` devices (R3.7).
   * If the device count (denominator) is 0, returns ``0.0`` (no devices ⇒ no
     airflow obligation; degenerate guard).

2. ``apply_safety_floor(targets, rooms, settings) -> tuple[dict[str, float], bool]``
   ``apply_safety_floor(targets: dict[str, float],
                        rooms: list[RoomAllocInput],
                        settings: AllocSettings) -> tuple[dict[str, float], bool]``

   Raises apertures until ``combined_open_pct >= settings.safety_floor_pct``
   and returns ``(new_targets, floor_binding)``:

   * ``new_targets`` — per-room dict. In normal operation it contains exactly
     the active-room keys from the input ``targets`` (inactive rooms are held by
     the coordinator and do NOT appear). In the last-resort branch (3.9) the
     raised inactive room ids are added.
   * ``floor_binding`` — ``True`` iff any aperture had to be raised to meet the
     floor; ``False`` when the floor was already satisfied (targets unchanged).

   Guarantees (CRITICAL — R3):
   * **Only ever raises.** ``new_targets[r] >= targets[r]`` for every room — the
     floor never lowers an aperture (R3.3/R3.5).
   * **Never exceeds 100 %.** Every returned target is ``<= 100`` (R3.3).
   * **Never finishes below the floor** when capacity exists: the returned
     combined ``>= settings.safety_floor_pct`` (R3.1/R3.5). This file tests that
     ``apply_safety_floor`` *respects* ``settings.safety_floor_pct`` as given;
     clamping the configured value to the safe 20–90 % band is Task 10.2.
   * **Bias to need (R3.4).** Padding raises the *unsatisfied active room with
     the largest signed error and target < 100* first, then recomputes and
     repeats one ``settings.granularity`` increment at a time. Satisfied active
     rooms are NEVER reopened.
   * **Inactive is last resort (R3.9 / R19.3 / D1 > D4).** The floor is met
     using active rooms + conventional vents wherever physically possible;
     inactive rooms are only repositioned when active + conventional capacity is
     mathematically insufficient, and that branch **logs** its reason.

----------------------------------------------------------------------------
Device-count decision (pinned, R23 / Task 16)
----------------------------------------------------------------------------
Each **physical vent** counts individually in the floor math — a room with two
vents is two devices at the (shared) commanded %, i.e. use ``len(vent_ids)``.
``combined_open_pct`` counts one device per key, so ``apply_safety_floor``
EXPANDS each room into ``len(room.vent_ids)`` device entries (each carrying that
room's commanded %) before computing the combined. In the worked example every
room is single-vent, so the per-room target dict is already per-vent.

----------------------------------------------------------------------------
Need signal (pinned): RoomAllocInput.signed_error_c
----------------------------------------------------------------------------
``apply_safety_floor`` has no ``setpoint``/``mode`` argument (fixed 3-arg
signature), so the per-room *need* used for the R3.4 bias must travel on the
room. Task 10.2 adds an optional field ``signed_error_c: float = 0.0`` to
``RoomAllocInput`` — the signed error toward setpoint in the conditioning
direction (``> 0`` ⇒ still needs conditioning / not-yet-satisfied; ``<= 0`` ⇒
satisfied). The default keeps every existing Task 9 construction valid (and
``allocate`` ignores it — it derives its own error from ``setpoint_c`` + ``mode``).
A room is eligible for floor padding iff ``room.active and signed_error_c > 0``;
"largest error" is ``max(signed_error_c)`` among eligible rooms with target < 100.

----------------------------------------------------------------------------
NOTE on the design's "Guest 18→28 %" illustration (flagged discrepancy)
----------------------------------------------------------------------------
Design A1b says the floor pads "the highest-error rooms slightly (e.g., Guest
18→28 %)". Guest's error is only 0.9 °C — Tomas (1.6 °C) is the highest-error
room still below 100 % (Mariana at 1.8 °C is already pinned at 100 % and is not
eligible). R3.4 is explicit: bias toward the *largest error-to-setpoint*. These
tests therefore follow R3.4 (Tomas is padded first), treating the design's
"Guest" figure as a loose illustration. The requirement, not the example, is the
source of truth for this CRITICAL path. (Reported to the orchestrator.)
"""

from __future__ import annotations

import importlib.util
import logging
import pathlib
import sys

import pytest

_BALANCE_PATH = pathlib.Path(__file__).resolve().parent.parent / "custom_components" / "hvac_vent_optimizer" / "balance.py"
_spec = importlib.util.spec_from_file_location("hvo_balance", _BALANCE_PATH)
balance = importlib.util.module_from_spec(_spec)
# Register before exec so dataclass introspection (with `from __future__ import
# annotations`) can resolve the module by name.
sys.modules[_spec.name] = balance
_spec.loader.exec_module(balance)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _room(
    room_id: str,
    *,
    signed_error_c: float,
    active: bool = True,
    vent_ids: tuple[str, ...] | None = None,
    leak: float = 0.1,
    efficiency: float = 0.05,
):
    """Build a RoomAllocInput for the floor tests.

    Only ``room_id``, ``active``, ``vent_ids`` and the pinned ``signed_error_c``
    field matter to ``apply_safety_floor``; ``temp_c`` / ``efficiency`` / ``leak``
    are filled with plausible values for completeness. ``temp_c`` is kept
    consistent with the cooling sign convention (temp above setpoint ⇒ positive
    error) so the room is internally coherent.

    Constructing with ``signed_error_c`` will raise ``TypeError`` until Task 10.2
    adds the field — that is an expected red-step failure mode for this file.
    """
    if vent_ids is None:
        vent_ids = (f"vent_{room_id.lower()}",)
    return balance.RoomAllocInput(
        room_id=room_id,
        temp_c=26.1 + signed_error_c,
        active=active,
        efficiency=efficiency,
        leak=leak,
        current_open=0.0,
        vent_ids=vent_ids,
        signed_error_c=signed_error_c,
    )


def _settings(**overrides):
    """AllocSettings with floor defaults; overridable per test."""
    base = dict(
        safety_floor_pct=40.0,
        conventional_vents=0,
        conventional_open_pct=50.0,
        inactive_open_pct_sum=0.0,
        inactive_count=0,
        granularity=5,
    )
    base.update(overrides)
    return balance.AllocSettings(**base)


# Worked-example data (design A1b — cooling, setpoint 26.1 °C).
# Pre-floor targets are the gran-5 rounded output of allocate() (see
# test_balance_allocate.py): Mariana 100, Tomas 75, Guest 20, rest 0.
_WORKED_SIGNED_ERR = {
    "Mariana": 1.8,
    "Tomas": 1.6,
    "Guest": 0.9,
    "Matias": 0.5,
    "Master": 0.3,
    "Bathroom": -0.4,  # satisfied (below setpoint)
}
_WORKED_BASE_TARGETS = {
    "Mariana": 100.0,
    "Tomas": 75.0,
    "Guest": 20.0,
    "Matias": 0.0,
    "Master": 0.0,
    "Bathroom": 0.0,
}


def _worked_rooms():
    return [
        _room(rid, signed_error_c=err) for rid, err in _WORKED_SIGNED_ERR.items()
    ]


# ===========================================================================
# 0. Contract surface exists (clean red until Task 10.2).
# ===========================================================================
class TestContractExists:
    def test_combined_open_pct_exists(self):
        assert hasattr(balance, "combined_open_pct")
        assert callable(balance.combined_open_pct)

    def test_apply_safety_floor_exists(self):
        assert hasattr(balance, "apply_safety_floor")
        assert callable(balance.apply_safety_floor)

    def test_room_alloc_input_carries_signed_error(self):
        # Task 10.2 adds the optional need-signal field used by the R3.4 bias.
        room = _room("X", signed_error_c=1.0)
        assert room.signed_error_c == 1.0


# ===========================================================================
# 1. combined_open_pct — the device-averaged formula (design A2).
# _Requirements: 3.1, 3.6, 3.7_
# ===========================================================================
class TestCombinedOpenPct:
    def test_worked_example_is_about_39_percent(self):
        # Smart targets 100+73+18 = 191 over 6 smart devices, plus 4 conventional
        # @50 % (=200), over 6+4 = 10 devices: (191 + 200) / 10 = 39.1 %.
        targets = {
            "Mariana": 100.0,
            "Tomas": 73.0,
            "Guest": 18.0,
            "Matias": 0.0,
            "Master": 0.0,
            "Bathroom": 0.0,
        }
        settings = _settings(conventional_vents=4, conventional_open_pct=50.0)
        combined = balance.combined_open_pct(targets, settings)
        assert combined == pytest.approx((191.0 + 200.0) / 10.0, abs=1e-6)
        assert combined == pytest.approx(39.1, abs=0.5)
        assert combined < 40.0  # below the floor → A2 must pad (next tests)

    def test_each_key_counts_as_one_device(self):
        # n_smart = len(targets); plain average when no conventional/inactive.
        targets = {"a": 100.0, "b": 0.0, "c": 50.0}
        combined = balance.combined_open_pct(targets, _settings())
        assert combined == pytest.approx((100.0 + 0.0 + 50.0) / 3.0, abs=1e-6)

    def test_conventional_vents_counted(self):
        # 1 smart @ 60 + 4 conventional @ 50 over 5 devices = (60 + 200)/5 = 52.
        targets = {"a": 60.0}
        settings = _settings(conventional_vents=4, conventional_open_pct=50.0)
        assert balance.combined_open_pct(targets, settings) == pytest.approx(52.0, abs=1e-6)

    def test_held_inactive_vents_counted(self):
        # 1 smart @ 40 + 2 inactive held summing 100 over 1+2 = 3 devices.
        targets = {"a": 40.0}
        settings = _settings(inactive_count=2, inactive_open_pct_sum=100.0)
        assert balance.combined_open_pct(targets, settings) == pytest.approx(140.0 / 3.0, abs=1e-6)

    def test_commanded_only_zero_target_contributes_zero(self):
        # R25.9: leakage is invisible here — a 0 % command is 0, not the leak %.
        targets = {"a": 0.0, "b": 0.0}
        assert balance.combined_open_pct(targets, _settings()) == 0.0

    def test_empty_with_no_devices_returns_zero(self):
        # Degenerate guard: no devices at all → 0.0, never a ZeroDivisionError.
        assert balance.combined_open_pct({}, _settings()) == 0.0

    def test_empty_smart_with_conventional_only(self):
        # No smart vents but 4 conventional @ 50 → 200 / 4 = 50 %.
        settings = _settings(conventional_vents=4, conventional_open_pct=50.0)
        assert balance.combined_open_pct({}, settings) == pytest.approx(50.0, abs=1e-6)


# ===========================================================================
# 2. apply_safety_floor — no-op when the floor is already met.
# _Requirements: 3.3_
# ===========================================================================
class TestFloorAlreadyMet:
    def test_no_padding_when_above_floor(self):
        targets = {"A": 100.0, "B": 50.0}  # combined 75 % >= 40 %
        rooms = [_room("A", signed_error_c=1.0), _room("B", signed_error_c=0.5)]
        new, binding = balance.apply_safety_floor(dict(targets), rooms, _settings())
        assert binding is False
        assert new == targets  # unchanged

    def test_exactly_at_floor_is_not_binding(self):
        # Two devices averaging exactly 40 % → already satisfied, no raise.
        targets = {"A": 80.0, "B": 0.0}
        rooms = [_room("A", signed_error_c=1.0), _room("B", signed_error_c=0.5)]
        new, binding = balance.apply_safety_floor(dict(targets), rooms, _settings())
        assert binding is False
        assert new == targets


# ===========================================================================
# 3. apply_safety_floor — bias toward the largest-error room (R3.4).
# _Requirements: 3.1, 3.2, 3.3, 3.4_
# ===========================================================================
class TestBiasTowardNeed:
    def test_largest_error_room_padded_first(self):
        # Two equal-target unsatisfied rooms; only the larger-error one is raised.
        # combined = (20+20)/2 = 20 < 40. Raising "High" alone to 60 gives
        # (60+20)/2 = 40 → floor met without touching "Low".
        targets = {"High": 20.0, "Low": 20.0}
        rooms = [
            _room("High", signed_error_c=2.0),
            _room("Low", signed_error_c=0.5),
        ]
        new, binding = balance.apply_safety_floor(dict(targets), rooms, _settings())
        assert binding is True
        assert new["Low"] == 20.0  # smaller error → never touched
        assert new["High"] == pytest.approx(60.0, abs=1e-6)
        assert balance.combined_open_pct(new, _settings()) >= 40.0

    def test_satisfied_active_room_is_never_reopened(self):
        # "Sat" is satisfied (error <= 0) and sits at 0 %. The floor must reach
        # 40 % via the unsatisfied "Need" room, leaving "Sat" closed (R3.4).
        targets = {"Need": 0.0, "Sat": 0.0}
        rooms = [
            _room("Need", signed_error_c=1.5),
            _room("Sat", signed_error_c=-0.4),
        ]
        new, binding = balance.apply_safety_floor(dict(targets), rooms, _settings())
        assert binding is True
        assert new["Sat"] == 0.0  # satisfied → stays closed
        assert new["Need"] > 0.0
        assert balance.combined_open_pct(new, _settings()) >= 40.0

    def test_padding_never_exceeds_100_and_falls_through_to_next_room(self):
        # floor 80 %. "A" (bigger error) maxes at 100 % but cannot reach 80 %
        # alone over 2 devices, so the next-largest-error "B" is padded too.
        targets = {"A": 95.0, "B": 0.0}
        rooms = [
            _room("A", signed_error_c=2.0),
            _room("B", signed_error_c=1.0),
        ]
        settings = _settings(safety_floor_pct=80.0)
        new, binding = balance.apply_safety_floor(dict(targets), rooms, settings)
        assert binding is True
        assert new["A"] == 100.0  # clamped, never above 100
        assert new["A"] >= targets["A"] and new["B"] >= targets["B"]  # only raises
        assert new["B"] == pytest.approx(60.0, abs=1e-6)  # (100+60)/2 = 80
        assert balance.combined_open_pct(new, settings) >= 80.0

    def test_respects_configured_floor_value(self):
        # Same base (combined 20 %); the floor we pad to follows settings exactly
        # (no clamping in 10.1 — that is 10.2). floor 30 → High 40; floor 40 → 60.
        targets = {"High": 20.0, "Low": 20.0}
        rooms = [_room("High", signed_error_c=2.0), _room("Low", signed_error_c=0.5)]

        new30, b30 = balance.apply_safety_floor(dict(targets), rooms, _settings(safety_floor_pct=30.0))
        assert b30 is True
        assert balance.combined_open_pct(new30, _settings(safety_floor_pct=30.0)) == pytest.approx(30.0, abs=1e-6)
        assert new30["High"] == pytest.approx(40.0, abs=1e-6)

        new40, b40 = balance.apply_safety_floor(dict(targets), rooms, _settings(safety_floor_pct=40.0))
        assert b40 is True
        assert balance.combined_open_pct(new40, _settings(safety_floor_pct=40.0)) == pytest.approx(40.0, abs=1e-6)


# ===========================================================================
# 4. apply_safety_floor — the design A1b/A2 worked example end-to-end.
# _Requirements: 3.1, 3.2, 3.3, 3.4_
# ===========================================================================
class TestWorkedExample:
    """combined ≈ 39.5 % (gran-5 targets + 4 conventional @50) → pad to 40 %."""

    def _settings(self):
        return _settings(conventional_vents=4, conventional_open_pct=50.0, granularity=5)

    def test_base_combined_below_floor(self):
        # Sanity: the pre-floor combined really is below 40 %.
        combined = balance.combined_open_pct(_WORKED_BASE_TARGETS, self._settings())
        assert combined == pytest.approx(39.5, abs=1e-6)
        assert combined < 40.0

    def test_floor_pads_to_40_and_binds(self):
        new, binding = balance.apply_safety_floor(
            dict(_WORKED_BASE_TARGETS), _worked_rooms(), self._settings()
        )
        assert binding is True
        assert balance.combined_open_pct(new, self._settings()) >= 40.0

    def test_bottleneck_stays_full_open_and_satisfied_stays_closed(self):
        new, _ = balance.apply_safety_floor(
            dict(_WORKED_BASE_TARGETS), _worked_rooms(), self._settings()
        )
        assert new["Mariana"] == 100.0          # bottleneck, can't pad past 100
        assert new["Bathroom"] == 0.0           # satisfied → never reopened (R3.4)

    def test_highest_error_eligible_room_is_padded(self):
        # Eligible = active, signed_error > 0, base target < 100.
        # Mariana(1.8) is at 100 → ineligible; Tomas(1.6) is the highest-error
        # eligible room, so Tomas is padded (75 → 80). Following R3.4, NOT Guest.
        new, _ = balance.apply_safety_floor(
            dict(_WORKED_BASE_TARGETS), _worked_rooms(), self._settings()
        )
        assert new["Tomas"] == pytest.approx(80.0, abs=1e-6)
        # The lower-error rooms are left exactly where allocation put them.
        assert new["Guest"] == 20.0
        assert new["Matias"] == 0.0
        assert new["Master"] == 0.0

    def test_floor_only_ever_raises_and_clamps_at_100(self):
        new, _ = balance.apply_safety_floor(
            dict(_WORKED_BASE_TARGETS), _worked_rooms(), self._settings()
        )
        for rid, base in _WORKED_BASE_TARGETS.items():
            assert new[rid] >= base, f"{rid} was lowered by the floor"
            assert new[rid] <= 100.0, f"{rid} exceeds 100 %"


# ===========================================================================
# 5. apply_safety_floor — multi-vent device counting (len(vent_ids)).
# _Requirements: 3.6, 3.7_
# ===========================================================================
class TestPerVentDeviceCount:
    def test_each_physical_vent_counts_individually(self):
        # Room "Twin" has 2 vents at 0 %; room "Solo" has 1 vent at 100 %.
        #  - per-vent counting (3 devices): combined = 100/3 = 33.3 % < 40 → BIND
        #  - per-room counting (2 devices): combined = 100/2 = 50 % >= 40 → no bind
        # Asserting it binds proves each physical vent is counted (len(vent_ids)).
        targets = {"Twin": 0.0, "Solo": 100.0}
        rooms = [
            _room("Twin", signed_error_c=1.5, vent_ids=("t1", "t2")),
            _room("Solo", signed_error_c=2.0, vent_ids=("s1",)),
        ]
        new, binding = balance.apply_safety_floor(dict(targets), rooms, _settings())
        assert binding is True, "per-room counting would not bind; must count each vent"
        assert new["Solo"] == 100.0  # already maxed
        assert new["Twin"] > 0.0     # padded to lift the 3-device average to 40 %


# ===========================================================================
# 6. apply_safety_floor — inactive rooms are last resort (R3.9 / R19.3 / D1>D4).
# _Requirements: 3.7, 3.9, 19.3_
# ===========================================================================
class TestInactiveLastResort:
    def test_inactive_not_reopened_when_active_plus_conventional_suffice(self):
        # R19.3: an unsatisfied active room + conventional capacity can reach the
        # floor, so the held-open inactive rooms must NOT be repositioned.
        targets = {"Need": 0.0}
        rooms = [
            _room("Need", signed_error_c=1.5),
            _room("InactiveA", signed_error_c=0.0, active=False, vent_ids=("ia",)),
            _room("InactiveB", signed_error_c=0.0, active=False, vent_ids=("ib",)),
        ]
        # 2 inactive vents held at 0 %, plus 1 conventional @ 50 % to help.
        settings = _settings(
            conventional_vents=1,
            conventional_open_pct=50.0,
            inactive_count=2,
            inactive_open_pct_sum=0.0,
        )
        new, binding = balance.apply_safety_floor(dict(targets), rooms, settings)
        assert binding is True
        # Inactive rooms are held by the coordinator — never in the result here.
        assert "InactiveA" not in new
        assert "InactiveB" not in new
        assert set(new) == {"Need"}
        # The floor was reached using the active room + conventional alone.
        assert balance.combined_open_pct(new, settings) >= 40.0

    def test_inactive_raised_only_as_last_resort_and_logs(self, caplog):
        # Pathological config: 1 active room (1 vent, maxes at 100), no
        # conventional, 2 held-closed inactive vents. Over 3 devices the most
        # active capacity can deliver is 100/3 = 33.3 % < 40 → the floor is
        # mathematically unreachable without reopening inactive vents (R3.9).
        targets = {"Need": 100.0}
        rooms = [
            _room("Need", signed_error_c=2.0, vent_ids=("n1",)),
            _room("InactiveA", signed_error_c=0.0, active=False, vent_ids=("ia",)),
            _room("InactiveB", signed_error_c=0.0, active=False, vent_ids=("ib",)),
        ]
        settings = _settings(
            conventional_vents=0,
            inactive_count=2,
            inactive_open_pct_sum=0.0,
        )
        with caplog.at_level(logging.INFO):
            new, binding = balance.apply_safety_floor(dict(targets), rooms, settings)

        assert binding is True
        assert new["Need"] == 100.0  # active capacity exhausted first
        # Last resort: at least one inactive vent reopened to reach the floor.
        reopened = [r for r in ("InactiveA", "InactiveB") if new.get(r, 0.0) > 0.0]
        assert reopened, "inactive rooms must be reopened when nothing else can meet the floor"
        # The last-resort branch must log its reason (R3.9).
        logged = " ".join(rec.getMessage().lower() for rec in caplog.records)
        assert ("inactive" in logged) or ("last resort" in logged) or ("floor" in logged), (
            "last-resort inactive reopen must be logged"
        )


# ===========================================================================
# 7. apply_safety_floor — leakage never relaxes the floor (R25.9).
# _Requirements: 3.1, 3.5_
# ===========================================================================
class TestCommandedApertureOnly:
    def test_high_leak_does_not_relax_floor(self):
        # Single active room commanded to 0 % with a large leak (0.35). The floor
        # is on COMMANDED aperture (0 %), so combined = 0 % < 30 % and the floor
        # must pad. If leakage (≈35 %) were (wrongly) counted, combined would read
        # 35 % >= 30 % and the floor would NOT bind. Asserting it binds proves the
        # floor ignores leakage (R25.9).
        targets = {"Need": 0.0}
        rooms = [_room("Need", signed_error_c=1.5, leak=0.35)]
        settings = _settings(safety_floor_pct=30.0)
        new, binding = balance.apply_safety_floor(dict(targets), rooms, settings)
        assert binding is True
        assert new["Need"] >= 30.0
        assert balance.combined_open_pct(new, settings) >= 30.0
