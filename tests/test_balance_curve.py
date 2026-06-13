"""Curve-aware allocation + knee-based airflow-limited (Task 32 — TDD).

TESTS-FIRST: written BEFORE ``balance.allocate`` is taught about the learned
non-linear :class:`learning.VentCurve`. Until Task 32 implements it these tests
are RED:

* ``RoomAllocInput`` has no ``curve`` field yet (``TypeError`` on construction);
* ``balance.closing_airflow_cost`` does not exist yet (``AttributeError``);
* the bottleneck is commanded at 100 % (linear) instead of its ``knee`` (< 100 %);
* airflow-limited detection keys off ``>= 100 - margin`` instead of the knee.

Both pure modules import nothing from Home Assistant, so they are loaded
standalone by absolute path (the ``hvo_*`` convention used across the
``test_balance_*`` / ``test_learning_*`` suites). ``allocate`` consumes the
curve purely by duck-typing (``flow`` / ``inverse`` / ``knee``), so the module
identity of the standalone-loaded ``VentCurve`` does not matter.

----------------------------------------------------------------------------
Design references
----------------------------------------------------------------------------
* A1 step 2: ``tau_i = err_i / rate_i(knee_i)`` — finish time at the EFFECTIVE-
  MAX airflow (the knee), not at 100 %. The bottleneck is commanded at its knee.
* A1 step 3: ``a_i = aperture_i( clamp(required_flow_i, leak_i, flow_i(knee_i)) )``
  — invert the learned curve (R25.13).
* A3: room ``i`` is airflow-limited iff ``a_i >= knee_i - margin`` AND
  ``err_i > error_c``. The knee may be well below 100 % (e.g. 50 %), so a room
  can be airflow-limited at 50 %.
* A4: closing a satisfied room from ABOVE its knee down TO the knee is a
  ~zero-airflow-cost move (airflow is flat above the knee).
"""

from __future__ import annotations

import importlib.util
import pathlib
import sys

import pytest

_ROOT = pathlib.Path(__file__).resolve().parent.parent / "custom_components" / "hvac_vent_optimizer"


def _load(mod_name: str, file_name: str):
    spec = importlib.util.spec_from_file_location(mod_name, _ROOT / file_name)
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


learning = _load("hvo_learning_curve", "learning.py")
balance = _load("hvo_balance_curve", "balance.py")


MODE = balance.MODE_COOLING
SETPOINT_C = 26.1
LEAK = 0.1


# ---------------------------------------------------------------------------
# Curve fixtures
# ---------------------------------------------------------------------------
def _sat_curve(leak: float = LEAK):
    """A saturating learned curve whose airflow plateaus at the 50 % knee.

    ``flow(100%) == 1`` and ``flow`` is flat from 50 % upward, so
    ``knee == 50`` (smallest breakpoint reaching ``(1 - KNEE_EPS) * full``).
    Counts sum to 24 (>= ``MODEL_MIN_N``) so the curve is TRUSTED — the read
    methods use the learned saturating shape, not the near-linear seed.
    """
    data = {
        "breakpoints": [0, 5, 10, 20, 35, 50, 75, 100],
        "flow": [leak, 0.45, 0.65, 0.82, 0.93, 1.0, 1.0, 1.0],
        "counts": [3, 3, 3, 3, 3, 3, 3, 3],
    }
    return learning.VentCurve.from_dict(data)


def _room(room_id, temp_c, efficiency, *, curve=None, leak=LEAK, active=True, vent_ids=None):
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
        curve=curve,
    )


def _settings(**overrides):
    base = {
        "safety_floor_pct": 0.0,
        "granularity": 1,
        "crosscoupling": False,
        "hysteresis_c": balance.DEFAULT_HYSTERESIS_C,
        "airflow_limited_margin_pct": 5.0,
        "airflow_limited_error_c": 0.5,
    }
    base.update(overrides)
    return balance.AllocSettings(**base)


# ===========================================================================
# Contract surface
# ===========================================================================
class TestContractExists:
    def test_room_alloc_input_accepts_curve(self):
        r = _room("Solo", SETPOINT_C + 1.0, 0.02, curve=_sat_curve())
        assert r.curve is not None

    def test_curve_defaults_to_none(self):
        r = balance.RoomAllocInput(
            room_id="Solo",
            temp_c=27.0,
            active=True,
            efficiency=0.02,
            leak=LEAK,
            current_open=0.0,
            vent_ids=("v",),
        )
        assert r.curve is None

    def test_closing_airflow_cost_exists(self):
        assert hasattr(balance, "closing_airflow_cost")
        assert callable(balance.closing_airflow_cost)


# ===========================================================================
# A1 step 2/3 — knee-aware bottleneck + curve inversion.
# _Requirements: 1.1, 4.1, 4.3, 25.12, 25.13
# ===========================================================================
class TestKneeAwareBottleneck:
    def test_bottleneck_commanded_at_knee_not_full_open(self):
        # A single saturating room: it is the bottleneck. The horizon uses
        # rate_i(knee_i), and the room is commanded at its knee (50 %), NOT 100 %
        # — beyond the knee airflow barely rises, so opening further is wasted.
        curve = _sat_curve()
        knee = curve.knee()
        assert knee == 50  # fixture sanity
        rooms = [_room("Solo", SETPOINT_C + 2.0, 0.02, curve=curve)]
        result = balance.allocate(rooms, SETPOINT_C, MODE, _settings())
        assert result.targets["Solo"] == pytest.approx(float(knee), abs=1.0)
        assert result.targets["Solo"] < 100.0

    def test_throttled_room_uses_curve_inverse_clamped(self):
        # Slow bottleneck fixes tau*; the faster room is throttled to the
        # aperture the LEARNED CURVE inverse maps its required flow to, clamped
        # to [leak, flow(knee)].
        curve = _sat_curve()
        knee_frac = curve.knee() / 100.0
        flow_knee = curve.flow(curve.knee())
        leak = curve.flow(0.0)

        slow = _room("Slow", SETPOINT_C + 2.0, 0.02, curve=curve)  # err 2.0
        fast = _room("Fast", SETPOINT_C + 1.96, 0.028, curve=curve)  # err 1.96
        result = balance.allocate([slow, fast], SETPOINT_C, MODE, _settings())

        # tau* set by the slow room at its knee airflow.
        tau_star = 2.0 / (0.02 * flow_knee)
        required_flow = (1.96 / tau_star) / 0.028
        clamped = min(max(required_flow, leak), flow_knee)
        expected_aperture = curve.inverse(clamped)
        expected_aperture = max(0.0, min(knee_frac * 100.0, expected_aperture))

        assert result.targets["Fast"] == pytest.approx(expected_aperture, abs=1.0)
        # The slow bottleneck sits at its knee.
        assert result.targets["Slow"] == pytest.approx(float(curve.knee()), abs=1.0)

    def test_required_flow_below_leak_closes_to_zero(self):
        # A fast room whose required flow is below the closed-vent leak finishes
        # on leakage alone → 0 % (same rule as the linear model, via the curve).
        curve = _sat_curve()
        slow = _room("Slow", SETPOINT_C + 3.0, 0.015, curve=curve)
        fast = _room("Fast", SETPOINT_C + 0.6, 0.4, curve=curve)
        result = balance.allocate([slow, fast], SETPOINT_C, MODE, _settings())
        assert result.targets["Fast"] == pytest.approx(0.0, abs=1.0)


# ===========================================================================
# A3 — knee-based airflow-limited detection (knee may be < 100 %).
# _Requirements: 5.1, 6.1
# ===========================================================================
class TestKneeBasedAirflowLimited:
    def test_room_at_knee_below_100_is_airflow_limited(self):
        # The bottleneck is pinned at its 50 % knee yet still 2 °C off-target.
        # Under knee-based detection it IS airflow-limited even though its
        # commanded aperture (50 %) is far below 100 % — opening further is
        # pointless. (Under the old >= 100-margin rule it would be missed.)
        curve = _sat_curve()
        rooms = [_room("Solo", SETPOINT_C + 2.0, 0.02, curve=curve)]
        result = balance.allocate(rooms, SETPOINT_C, MODE, _settings())
        assert result.targets["Solo"] < 95.0  # below the legacy linear threshold
        assert "Solo" in result.airflow_limited

    def test_room_below_knee_not_airflow_limited(self):
        # A throttled room sitting well below its knee is NOT airflow-limited.
        curve = _sat_curve()
        slow = _room("Slow", SETPOINT_C + 2.0, 0.02, curve=curve)
        fast = _room("Fast", SETPOINT_C + 1.96, 0.028, curve=curve)
        result = balance.allocate([slow, fast], SETPOINT_C, MODE, _settings())
        assert result.targets["Fast"] < curve.knee() - 5.0
        assert "Fast" not in result.airflow_limited

    def test_at_knee_but_on_target_not_flagged(self):
        # At/above the knee but within error_c of setpoint → not airflow-limited.
        curve = _sat_curve()
        rooms = [_room("Solo", SETPOINT_C + 0.4, 0.02, curve=curve)]
        result = balance.allocate(rooms, SETPOINT_C, MODE, _settings(airflow_limited_error_c=0.5))
        assert "Solo" not in result.airflow_limited


# ===========================================================================
# A4 — closing above the knee is ~zero airflow cost (battery-saving move).
# _Requirements: 6.1, 25.12, 25.13
# ===========================================================================
class TestClosingAirflowCost:
    def test_above_knee_to_knee_is_zero_cost(self):
        curve = _sat_curve()
        knee = float(curve.knee())
        cost = balance.closing_airflow_cost(curve, 90.0, knee)
        assert cost == pytest.approx(0.0, abs=1e-6)

    def test_knee_to_zero_redirects_real_airflow(self):
        curve = _sat_curve()
        knee = float(curve.knee())
        cost = balance.closing_airflow_cost(curve, knee, 0.0)
        # flow(knee)=1.0, flow(0)=leak → cost ≈ 1 - leak.
        assert cost == pytest.approx(1.0 - LEAK, abs=0.05)

    def test_cost_never_negative(self):
        curve = _sat_curve()
        # "Closing" to a more-open position is not a cost.
        assert balance.closing_airflow_cost(curve, 10.0, 50.0) == 0.0

    def test_crosscoupling_pushes_above_knee_satisfied_room_to_zero(self):
        # With a laggard airflow-limited at its knee, a satisfied room sitting
        # above its knee is collapsed to 0 % to redirect air to the laggard
        # (the above-knee→knee part is free, the knee→0 part does the work).
        curve = _sat_curve()
        laggard = _room("Laggard", SETPOINT_C + 2.0, 0.02, curve=curve)
        satisfied = _room("Sat", SETPOINT_C - 1.0, 0.4, curve=curve)
        targets = {"Laggard": float(curve.knee()), "Sat": 90.0}
        new = balance.apply_cross_coupling(
            targets,
            [laggard, satisfied],
            MODE,
            SETPOINT_C,
            _settings(crosscoupling=True),
            frozenset({"Laggard"}),
        )
        assert new["Sat"] == 0.0
        assert new["Laggard"] == pytest.approx(float(curve.knee()), abs=1.0)


# ===========================================================================
# Backward-compat: curve=None must reproduce the linear leak model exactly.
# _Requirements: 1.1, 4.1, 4.3
# ===========================================================================
class TestLinearFallback:
    def test_curve_none_bottleneck_full_open(self):
        rooms = [_room("Solo", SETPOINT_C + 2.0, 0.02, curve=None, leak=0.1)]
        result = balance.allocate(rooms, SETPOINT_C, MODE, _settings())
        # Linear model: knee == 100 → bottleneck driven to full open.
        assert result.targets["Solo"] == pytest.approx(100.0, abs=1.0)
        assert "Solo" in result.airflow_limited

    def test_curve_none_matches_worked_example_tomas(self):
        # The Task 9 worked example (linear leak 0.1) must still hold with the
        # optional curve field absent.
        data = {
            "Mariana": (27.9, 0.017),
            "Tomas": (27.7, 0.020),
            "Guest": (27.0, 0.033),
            "Bathroom": (25.7, 0.438),
        }
        rooms = [_room(rid, t, e, curve=None) for rid, (t, e) in data.items()]
        result = balance.allocate(rooms, SETPOINT_C, MODE, _settings())
        assert result.targets["Mariana"] == pytest.approx(100.0, abs=1.0)
        assert result.targets["Tomas"] == pytest.approx(73.0, abs=1.5)
        assert result.targets["Bathroom"] == 0.0


class TestDeterminism:
    def test_curve_allocation_is_deterministic(self):
        curve = _sat_curve()
        rooms = [
            _room("Slow", SETPOINT_C + 2.0, 0.02, curve=curve),
            _room("Fast", SETPOINT_C + 1.0, 0.05, curve=curve),
        ]
        first = balance.allocate(rooms, SETPOINT_C, MODE, _settings())
        second = balance.allocate(rooms, SETPOINT_C, MODE, _settings())
        assert first.targets == second.targets
        assert first.airflow_limited == second.airflow_limited
