"""Tests for balance.py synchronized-convergence allocation (Task 9.1 — TDD).

TESTS-FIRST: this file is written BEFORE ``allocate()`` and the
``RoomAllocInput`` / ``AllocResult`` / ``AllocSettings`` dataclasses exist
(Task 9.2 implements them). Until then every test here is EXPECTED TO FAIL —
collection-time / call-time ``AttributeError`` because the symbols are absent.
That failure is the point of the red step.

balance.py is a PURE module (no Home Assistant imports). It is loaded
standalone by absolute path so we never import the ``hvac_vent_optimizer``
package (whose __init__ pulls in Home Assistant, which is not installed in the
test environment). This mirrors the ``hvo_balance`` convention in
test_balance_classify.py.

    cd tests && python3 -m pytest test_balance_allocate.py -q
    python3 -m pytest tests/test_balance_allocate.py -q --import-mode=importlib

----------------------------------------------------------------------------
Worked example (design A1b — cooling, setpoint 26.1 °C, leak 0.1 everywhere)
----------------------------------------------------------------------------
Effective flow at aperture a:  flow_i(a) = leak + (1 - leak) * a   (flow(1)=1)
Predicted rate:                rate_i(a) = e_i * flow_i(a)

Because flow_i(1.0) = 1.0, rate_i(1.0) = e_i, so the full-open finish time is
    tau_i = err_i / rate_i(1.0) = err_i / e_i .

| Room     | T_i  | err_i | e_i   | tau_i = err_i / e_i        |
|----------|------|-------|-------|----------------------------|
| Bedroom 2  | 27.9 | 1.8   | 0.017 | 1.8/0.017   = 105.882 min  | <- bottleneck (max tau)
| Bedroom 3    | 27.7 | 1.6   | 0.020 | 1.6/0.020   =  80.000 min  |
| Bedroom 1   | 26.6 | 0.5   | 0.072 | 0.5/0.072   =   6.944 min  |
| Master   | 26.4 | 0.3   | 0.053 | 0.3/0.053   =   5.660 min  |
| Guest    | 27.0 | 0.9   | 0.033 | 0.9/0.033   =  27.273 min  |
| Bathroom | 25.7 | -0.4  | 0.438 | satisfied (25.7 <= 25.8)   | -> 0 %

tau* = 105.882 min (Bedroom 2) -> Bedroom 2 runs at 100 %.

Throttle the rest to finish at tau* (note required_flow_i simplifies to
tau_i/tau* because required_flow = (err/tau*)/e = (err/e)/tau* = tau_i/tau*):

  Bedroom 3 : required_flow = 80.000/105.882 = 0.755556
          a = (0.755556 - 0.1) / 0.9 = 0.655556 / 0.9 = 0.728395 -> 72.84 %
  Guest : required_flow = 27.273/105.882 = 0.257584
          a = (0.257584 - 0.1) / 0.9 = 0.157584 / 0.9 = 0.175094 -> 17.51 %
  Bedroom 1: required_flow = 6.944/105.882  = 0.065590 <= leak 0.1 -> a = 0 %
  Master: required_flow = 5.660/105.882  = 0.053460 <= leak 0.1 -> a = 0 %

Design A1b states the rounded targets as Bedroom 3 ~73 % and Guest ~18 %.

----------------------------------------------------------------------------
Test isolation / settings choices (documented per Task 9.1 instructions)
----------------------------------------------------------------------------
* ``safety_floor_pct = 0`` so the safety floor (A2, applied in allocate step 4)
  cannot pad apertures upward. This isolates the pure A1.1-A1.3 convergence
  numbers, so the worked-example pre-floor values above are exactly what
  ``allocate`` must return. (Floor behaviour is exercised by Task 10's tests.)
* ``crosscoupling = False`` so the explicit cross-coupling guard (A4) does not
  perturb targets; here it would only push the already-zero satisfied Bathroom,
  but disabling it keeps these assertions strictly about A1.
* ``granularity = 1`` so step-5 rounding does not blur the convergence numbers:
  72.84 -> 73 and 17.51 -> 18, matching design A1b. Rounding at the default
  granularity (5) is a separate concern left to Task 9.2's own tests.

Tolerance choice: assertions use ``pytest.approx(..., abs=ABS_TOL)`` with
``ABS_TOL = 1.0`` percentage point. That window simultaneously accommodates
(a) the precise pre-round values (72.84, 17.51), (b) granularity=1 rounding
(73, 18), and (c) the design's stated approximate figures (~73, ~18) — without
being so loose that a wrong room (e.g. 0 % or 100 %) could sneak through.
"""

from __future__ import annotations

import importlib.util
import pathlib
import sys

import pytest

_BALANCE_PATH = (
    pathlib.Path(__file__).resolve().parent.parent
    / "custom_components"
    / "hvac_vent_optimizer"
    / "balance.py"
)
_spec = importlib.util.spec_from_file_location("hvo_balance", _BALANCE_PATH)
balance = importlib.util.module_from_spec(_spec)
# Register before exec so dataclasses introspection (with `from __future__
# import annotations`) can resolve the module by name.
sys.modules[_spec.name] = balance
_spec.loader.exec_module(balance)


# ---------------------------------------------------------------------------
# Worked-example fixture data (design A1b).
# ---------------------------------------------------------------------------
MODE = "cooling"
SETPOINT_C = 26.1
LEAK = 0.1

# room_id -> (temp_c, efficiency e_i)
_ROOM_DATA: dict[str, tuple[float, float]] = {
    "Bedroom 2": (27.9, 0.017),
    "Bedroom 3": (27.7, 0.020),
    "Bedroom 1": (26.6, 0.072),
    "Master": (26.4, 0.053),
    "Guest": (27.0, 0.033),
    "Bathroom": (25.7, 0.438),
}

ABS_TOL = 1.0  # percentage points (see module docstring for rationale)


# ---------------------------------------------------------------------------
# Reference implementation of the design's allocation math, used to DERIVE the
# expected percentages from first principles (rather than hard-coding 73/18).
# Mirrors A1 steps 1-3 exactly.
# ---------------------------------------------------------------------------
def _flow(leak: float, a: float) -> float:
    return leak + (1.0 - leak) * a


def _err(temp_c: float) -> float:
    # cooling: error toward setpoint; >0 means still needs cooling.
    return temp_c - SETPOINT_C


def _is_satisfied(temp_c: float, hyst: float = balance.DEFAULT_HYSTERESIS_C) -> bool:
    return balance.has_reached_setpoint(MODE, SETPOINT_C, temp_c, hyst)


def _expected_targets() -> dict[str, float]:
    """Compute expected open % per room via design A1.1-A1.3."""
    # Step 1 - classify; satisfied rooms -> 0.
    unsatisfied = {rid: (temp, eff) for rid, (temp, eff) in _ROOM_DATA.items() if not _is_satisfied(temp)}
    # Step 2 - bottleneck horizon. rate_i(1.0) = e_i * flow(1.0) = e_i.
    taus = {rid: _err(temp) / (eff * _flow(LEAK, 1.0)) for rid, (temp, eff) in unsatisfied.items()}
    tau_star = max(taus.values())

    # Step 3 - throttle the rest to finish at tau*.
    targets: dict[str, float] = dict.fromkeys(_ROOM_DATA, 0.0)
    for rid, (temp, eff) in unsatisfied.items():
        required_rate = _err(temp) / tau_star
        required_flow = required_rate / eff
        a = (required_flow - LEAK) / (1.0 - LEAK)
        a = max(0.0, min(1.0, a))
        targets[rid] = a * 100.0
    return targets


def _bottleneck_tau() -> float:
    taus = {
        rid: _err(temp) / (eff * _flow(LEAK, 1.0))
        for rid, (temp, eff) in _ROOM_DATA.items()
        if not _is_satisfied(temp)
    }
    return max(taus.values())


def _make_rooms() -> list:
    """Build RoomAllocInput list (will raise AttributeError until Task 9.2)."""
    return [
        balance.RoomAllocInput(
            room_id=rid,
            temp_c=temp,
            active=True,
            efficiency=eff,
            leak=LEAK,
            current_open=0.0,
            vent_ids=(f"vent_{rid.lower()}",),
        )
        for rid, (temp, eff) in _ROOM_DATA.items()
    ]


def _make_settings():
    """AllocSettings isolating pure A1 convergence (see module docstring)."""
    return balance.AllocSettings(
        safety_floor_pct=0.0,
        granularity=1,
        crosscoupling=False,
        hysteresis_c=balance.DEFAULT_HYSTERESIS_C,
    )


def _allocate():
    return balance.allocate(_make_rooms(), SETPOINT_C, MODE, _make_settings())


# ---------------------------------------------------------------------------
# Sanity checks on the in-test reference math (these do NOT depend on
# allocate() existing, so they document the expected numbers regardless).
# ---------------------------------------------------------------------------
class TestReferenceMath:
    def test_bottleneck_is_bedroom_2(self):
        assert _bottleneck_tau() == pytest.approx(105.882, abs=0.01)

    def test_reference_targets_match_design(self):
        t = _expected_targets()
        assert t["Bedroom 2"] == pytest.approx(100.0, abs=ABS_TOL)
        assert t["Bedroom 3"] == pytest.approx(72.84, abs=0.05)
        assert t["Guest"] == pytest.approx(17.51, abs=0.05)
        assert t["Bedroom 1"] == 0.0
        assert t["Master"] == 0.0
        assert t["Bathroom"] == 0.0


# ---------------------------------------------------------------------------
# allocate() contract (Task 9.2 must satisfy these). RED until implemented.
# _Requirements: 1.1, 1.5, 2.1, 4.1, 4.3, 4.5, 19.2_
# ---------------------------------------------------------------------------
class TestAllocateApiExists:
    """The dataclasses + allocate() entry point must exist with the contract."""

    def test_dataclasses_and_allocate_exist(self):
        assert hasattr(balance, "RoomAllocInput")
        assert hasattr(balance, "AllocSettings")
        assert hasattr(balance, "AllocResult")
        assert hasattr(balance, "allocate")
        assert callable(balance.allocate)

    def test_allocate_returns_targets_for_every_room(self):
        result = _allocate()
        assert set(result.targets) == set(_ROOM_DATA)


class TestWorkedExample:
    """Design A1b numbers — the heart of Task 9.1."""

    def test_bedroom_2_is_the_bottleneck_and_runs_full_open(self):
        # Bedroom 2 has the largest tau_i (err/rate at full open) -> 100 %.
        result = _allocate()
        assert result.targets["Bedroom 2"] == pytest.approx(100.0, abs=ABS_TOL)

    def test_bedroom_3_throttled_to_about_73_percent(self):
        result = _allocate()
        expected = _expected_targets()["Bedroom 3"]
        # Derived value (72.84) ...
        assert result.targets["Bedroom 3"] == pytest.approx(expected, abs=ABS_TOL)
        # ... and cross-check against the design's stated ~73 %.
        assert result.targets["Bedroom 3"] == pytest.approx(73.0, abs=ABS_TOL)

    def test_guest_throttled_to_about_18_percent(self):
        result = _allocate()
        expected = _expected_targets()["Guest"]
        # Derived value (17.51) ...
        assert result.targets["Guest"] == pytest.approx(expected, abs=ABS_TOL)
        # ... and cross-check against the design's stated ~18 %.
        assert result.targets["Guest"] == pytest.approx(18.0, abs=ABS_TOL)

    def test_bedroom_1_and_master_go_to_zero_required_flow_below_leak(self):
        # required_flow_i <= leak (0.1) -> aperture clamps to 0 %.
        result = _allocate()
        assert result.targets["Bedroom 1"] == pytest.approx(0.0, abs=ABS_TOL)
        assert result.targets["Master"] == pytest.approx(0.0, abs=ABS_TOL)

    def test_bathroom_satisfied_goes_to_zero(self):
        # 25.7 <= 26.1 - 0.3 hysteresis -> satisfied -> 0 % (overshoot close).
        result = _allocate()
        assert result.targets["Bathroom"] == pytest.approx(0.0, abs=ABS_TOL)

    def test_all_targets_match_full_worked_example(self):
        result = _allocate()
        expected = _expected_targets()
        for rid, exp_pct in expected.items():
            assert result.targets[rid] == pytest.approx(exp_pct, abs=ABS_TOL), rid


class TestNoOvercooling:
    """R2.3 — no actively-conditioned room finishes before the bottleneck.

    The throttle makes every room with a commanded aperture > 0 arrive at the
    setpoint at (approximately) tau*, never earlier. Rooms whose required flow
    is below their leak (Bedroom 1/Master) are clamped to 0 % and may finish early
    purely on leakage — that is unavoidable (we cannot command below leak), so
    they are excluded from this guarantee.
    """

    def test_no_controlled_room_finishes_before_bottleneck(self):
        result = _allocate()
        tau_star = _bottleneck_tau()
        for rid, pct in result.targets.items():
            if pct <= 0.0:
                continue  # leak-pinned or satisfied -> not controllable
            finish = result.predicted_finish_min[rid]
            # Arrive together, never earlier than the bottleneck horizon.
            assert finish >= tau_star - 1.0, f"{rid} overcools (finishes {finish} < {tau_star})"

    def test_bottleneck_finishes_last(self):
        result = _allocate()
        tau_star = _bottleneck_tau()
        assert result.predicted_finish_min["Bedroom 2"] == pytest.approx(tau_star, abs=1.0)


class TestDeterminism:
    """R19.2 — identical inputs yield identical outputs (no hidden state)."""

    def test_allocation_is_deterministic(self):
        first = balance.allocate(_make_rooms(), SETPOINT_C, MODE, _make_settings())
        second = balance.allocate(_make_rooms(), SETPOINT_C, MODE, _make_settings())
        assert first.targets == second.targets
        assert first.predicted_finish_min == second.predicted_finish_min
        assert first.airflow_limited == second.airflow_limited
        assert first.floor_binding == second.floor_binding
