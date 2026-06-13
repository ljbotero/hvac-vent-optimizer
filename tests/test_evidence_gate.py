"""R15.6 evidence-gate tests for the ``balance`` strategy (Task 26).

This is the **ship gate** that decides whether ``balance`` may become the
default strategy (Task 27 / R17.1). It builds canned, deterministic scenarios
that are faithful to the documented data analysis — the *Mariana-pinned* case
(design worked example A1b), the *Bathroom-overcooled* case, and a *mixed*
full-house case — and runs the offline closed-loop simulator (R15) to compare
``balance`` against ``dab`` on the **same** scenario.

R15.6 requires ALL of, across the representative scenarios:

* (a) average active-room spread reduced by **≥ 30 %** versus ``dab``;
* (b) maximum spread **no worse** than ``dab``;
* (c) total vent movements **≤ 110 %** of ``dab`` (ideally lower).

Like the sibling pure-module tests, this file loads ``simulator``/``balance``
standalone by path (no Home Assistant import).

    python3 -m pytest tests/test_evidence_gate.py -q --import-mode=importlib

Current status (see ``docs/quality-baseline.md`` for the full recorded tables):
the **spread** criteria of the gate are NOT met. Against the
*Task-14-overshoot-fixed* ``dab`` the ``balance`` strategy is essentially tied
on avg spread (within a few percent, and marginally worse on max spread on two
scenarios because it runs slightly longer), while using far fewer vent
movements (16-93 % of ``dab``). Criterion (c) passes decisively; criteria (a)
and (b) do not.

**Ship decision (Task 27).** The homeowner was shown this result and the
root-cause analysis — the original "~30 % lower spread" target was framed
against the *legacy, pre-overshoot-fix* ``dab``; once ``dab`` itself is bugfixed
the remaining headroom is movement, not spread — and explicitly chose to ship
``balance`` as the default **on the movement win**, waiving the spread gate. So
this module:

* keeps the full R15.6 gate **executable and visible** as
  :func:`r156_ship_gate_report` and asserts the criteria that genuinely hold
  today (movement ≤ 110 %, safety floor, determinism, table rendering);
* records, via :func:`test_r156_spread_gate_waived`, that the spread criteria
  (a)+(b) are knowingly unmet and were waived by the homeowner — so the suite
  documents the accepted decision instead of a misleading
  ``xfail(strict=True)`` that implies ``balance`` is still blocked.

    python3 -m pytest tests/test_evidence_gate.py -q --import-mode=importlib
"""

from __future__ import annotations

import importlib.util
import pathlib
import sys

import pytest

# --- Load the pure modules standalone (no HA) ------------------------------
_ROOT = pathlib.Path(__file__).resolve().parent.parent / "custom_components" / "hvac_vent_optimizer"


def _load(name: str):
    path = _ROOT / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"hvo_{name}", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


balance = _load("balance")
simulator = _load("simulator")


# ---------------------------------------------------------------------------
# Representative scenario builders (faithful to the documented data analysis)
# ---------------------------------------------------------------------------
# Learned cooling efficiencies from the data analysis (°C/min at full flow).
EFF = {
    "mariana": 0.017,  # slow bottleneck
    "tomas": 0.020,
    "guest": 0.033,
    "master": 0.053,  # two vents (R23)
    "matias": 0.072,
    "bathroom": 0.438,  # very fast → overcools
}
LEAK = 0.1
SETPOINT_C = 26.1


def _settings():
    """Representative floor settings: the documented 4 conventional vents @50 %.

    The real install carries conventional (non-smart) vents that provide
    baseline airflow (design A2 — "4 conventional @50 % → combined over 10
    devices"). Modelling them keeps the airflow-safety floor feasible for
    ``balance`` exactly as in production; the default ``AllocSettings`` has none,
    which would make the floor mathematically unreachable once every smart room
    satisfies.
    """
    return balance.AllocSettings(
        safety_floor_pct=40.0, conventional_vents=4, conventional_open_pct=50.0
    )


def _room(room_id: str, temp_c: float, drift: float = 0.0):
    """Build a scenario room with the documented efficiency + saturating curve.

    ``drift`` is an intuitive "drift away from setpoint" magnitude (heat ingress
    in cooling); 0 means no passive drift. Master is the documented two-vent
    room (R23).
    """
    extra = {"vent_ids": ("master_a", "master_b")} if room_id == "master" else {}
    return simulator.RoomScenario(
        room_id=room_id,
        temp_c=temp_c,
        efficiency=EFF[room_id],
        leak=LEAK,
        idle_drift=simulator.drift_away_from_setpoint(drift, "cooling"),
        curve=simulator.representative_saturating_curve(LEAK),
        **extra,
    )


def scenario_mariana_pinned():
    """Design worked example A1b: Mariana hot + slow, Bathroom pre-overcooled."""
    return simulator.Scenario(
        rooms=[
            _room("mariana", 27.9),
            _room("tomas", 27.7),
            _room("guest", 27.0),
            _room("master", 26.4),
            _room("matias", 26.6),
            _room("bathroom", 25.7),
        ],
        setpoint_c=SETPOINT_C,
        mode="cooling",
        horizon_min=900.0,
        seed=11,
        settings=_settings(),
    )


def scenario_bathroom_overcooled():
    """The fast Bathroom sits below setpoint while the slow rooms run hot."""
    return simulator.Scenario(
        rooms=[
            _room("mariana", 27.6),
            _room("tomas", 27.2),
            _room("guest", 26.9),
            _room("bathroom", 25.5),
        ],
        setpoint_c=SETPOINT_C,
        mode="cooling",
        horizon_min=900.0,
        seed=12,
        settings=_settings(),
    )


def scenario_mixed():
    """Full house, realistic wide spread (~1.8 °C) with mild heat ingress."""
    return simulator.Scenario(
        rooms=[
            _room("mariana", 28.0, drift=0.004),
            _room("tomas", 27.6, drift=0.004),
            _room("guest", 27.1, drift=0.004),
            _room("master", 26.8, drift=0.004),
            _room("matias", 26.7, drift=0.004),
            _room("bathroom", 26.2, drift=0.004),
        ],
        setpoint_c=SETPOINT_C,
        mode="cooling",
        horizon_min=900.0,
        seed=13,
        settings=_settings(),
    )


SCENARIOS = {
    "Mariana-pinned": scenario_mariana_pinned,
    "Bathroom-overcooled": scenario_bathroom_overcooled,
    "Mixed": scenario_mixed,
}

# Gate thresholds (R15.6).
AVG_SPREAD_REDUCTION_MIN = 30.0  # % lower than dab
MOVES_RATIO_MAX = 110.0  # % of dab's total moves


# ---------------------------------------------------------------------------
# Gate metric helper
# ---------------------------------------------------------------------------
def _gate_metrics(builder) -> dict:
    """Run ``dab`` and ``balance`` on one scenario and compute the R15.6 metrics."""
    cmp = simulator.compare(builder(), ["dab", "balance"], to_stdout=False)
    dab = cmp.results["dab"]
    bal = cmp.results["balance"]
    avg_reduction_pct = (
        (dab.avg_spread - bal.avg_spread) / dab.avg_spread * 100.0
        if dab.avg_spread
        else 0.0
    )
    moves_ratio_pct = (
        bal.total_moves / dab.total_moves * 100.0 if dab.total_moves else 0.0
    )
    return {
        "table": cmp.table,
        "dab": dab,
        "balance": bal,
        "avg_reduction_pct": avg_reduction_pct,
        "max_no_worse": bal.max_spread <= dab.max_spread + 1e-9,
        "moves_ratio_pct": moves_ratio_pct,
    }


# ---------------------------------------------------------------------------
# Properties that genuinely hold today (movement + safety + determinism)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("name", list(SCENARIOS))
def test_scenarios_are_deterministic(name):
    """Each canned scenario is reproducible run-to-run (R15.5)."""
    builder = SCENARIOS[name]
    a = simulator.run(builder(), strategy="balance")
    b = simulator.run(builder(), strategy="balance")
    assert a.avg_spread == pytest.approx(b.avg_spread)
    assert a.max_spread == pytest.approx(b.max_spread)
    assert a.total_moves == b.total_moves


@pytest.mark.parametrize("name", list(SCENARIOS))
def test_balance_movement_within_gate(name):
    """R15.6(c): ``balance`` never exceeds 110 % of ``dab``'s vent movements."""
    m = _gate_metrics(SCENARIOS[name])
    assert m["moves_ratio_pct"] <= MOVES_RATIO_MAX, (
        f"{name}: balance moved {m['balance'].total_moves} vs dab "
        f"{m['dab'].total_moves} ({m['moves_ratio_pct']:.1f}% > {MOVES_RATIO_MAX}%)"
    )


@pytest.mark.parametrize("name", list(SCENARIOS))
def test_safety_floor_never_violated(name):
    """``balance`` keeps combined open ≥ 40 % every step in sim (R15.2/R3).

    The evaluated strategy must never starve airflow. (``dab`` can structurally
    dip below the floor near end-of-cycle once every smart room satisfies and
    only conventional vents remain — a separate floor-feasibility concern owned
    by Task 10, not this gate, and one ``balance`` avoids by converging rooms
    together rather than letting fast rooms close early.)
    """
    result = simulator.run(SCENARIOS[name](), strategy="balance")
    assert result.min_combined_open_pct >= 40.0 - 1e-6, (
        f"{name}/balance: combined open dropped to "
        f"{result.min_combined_open_pct:.2f}% < 40% floor"
    )


def test_comparison_tables_render():
    """Each scenario renders a deterministic side-by-side table (R15.3)."""
    for name in SCENARIOS:
        m = _gate_metrics(SCENARIOS[name])
        assert "balance" in m["table"] and "dab" in m["table"]
        assert "avg_spread" in m["table"] and "total_moves" in m["table"]


# ---------------------------------------------------------------------------
# The R15.6 ship gate — kept executable and visible (see module docstring).
#
# The spread criteria (a)+(b) are NOT met today; the homeowner waived them and
# shipped balance on the movement win (Task 27). Rather than a strict xfail that
# implies balance is blocked, we (1) expose the full gate as a report helper and
# (2) assert the accepted decision: movement passes, spread is knowingly waived.
# ---------------------------------------------------------------------------
def r156_ship_gate_report() -> dict:
    """Evaluate ALL three R15.6 criteria across every scenario (no asserts).

    Returns a per-scenario dict of pass/fail booleans + metrics so the gate
    stays runnable and inspectable. This is the canonical place to re-check the
    gate if the algorithm is later improved (at which point ``balance`` could be
    promoted on merit rather than on the movement waiver).
    """
    report: dict = {}
    for name in SCENARIOS:
        m = _gate_metrics(SCENARIOS[name])
        report[name] = {
            "avg_reduction_pct": m["avg_reduction_pct"],
            "avg_pass": m["avg_reduction_pct"] >= AVG_SPREAD_REDUCTION_MIN,
            "max_no_worse": m["max_no_worse"],
            "moves_ratio_pct": m["moves_ratio_pct"],
            "moves_pass": m["moves_ratio_pct"] <= MOVES_RATIO_MAX,
        }
    return report


def test_r156_movement_criterion_passes_all_scenarios():
    """R15.6(c) holds for every scenario — the basis for the ship decision."""
    report = r156_ship_gate_report()
    failures = [n for n, r in report.items() if not r["moves_pass"]]
    assert not failures, (
        "balance must not exceed 110% of dab's movements: " + ", ".join(failures)
    )


def test_r156_spread_gate_waived():
    """Document that the spread criteria (a)+(b) are knowingly unmet + waived.

    This is intentionally an assertion about the *current* state of the world,
    not a latent failure: the homeowner accepted shipping ``balance`` as default
    on the movement win with the spread gate waived (Task 27 decision, recorded
    in ``docs/quality-baseline.md``). If a future algorithm change makes the
    spread gate pass, THIS test will fail and should be replaced by promoting
    ``balance`` on merit (and updating the docs).
    """
    report = r156_ship_gate_report()
    spread_unmet = [
        n for n, r in report.items() if not (r["avg_pass"] and r["max_no_worse"])
    ]
    assert spread_unmet, (
        "R15.6 spread criteria now PASS for all scenarios — the movement-only "
        "waiver is no longer needed. Promote balance on merit and update "
        "docs/quality-baseline.md + this test."
    )
