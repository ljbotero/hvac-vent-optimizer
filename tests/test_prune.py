"""Fix #7: stale efficiency-model pruning must run on every update, not once."""
from __future__ import annotations


def _model():
    return {"cooling": {"baseline": 0.5, "offsets": [0.0, 0.0], "n": 3}}


def test_prune_removes_stale_models(make_coordinator):
    coord, *_ = make_coordinator()
    coord._efficiency_models = {"v1": _model(), "gone": _model()}
    coord._prune_stale_efficiency_models({"vents": {"v1": {}}})
    assert "gone" not in coord._efficiency_models
    assert "v1" in coord._efficiency_models


def test_prune_rearms_on_subsequent_calls(make_coordinator):
    """A vent removed after the first poll must still be pruned later."""
    coord, *_ = make_coordinator()
    coord._efficiency_models = {"v1": _model()}
    coord._prune_stale_efficiency_models({"vents": {"v1": {}}})

    # A new vent later disappears from the data.
    coord._efficiency_models["later_stale"] = _model()
    coord._prune_stale_efficiency_models({"vents": {"v1": {}}})
    assert "later_stale" not in coord._efficiency_models
