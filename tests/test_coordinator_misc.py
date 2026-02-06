from types import SimpleNamespace

from custom_components.hvac_vent_optimizer.coordinator import FlairCoordinator


def _make_coord():
    coord = FlairCoordinator.__new__(FlairCoordinator)
    coord._vent_rates = {}
    coord._initial_efficiency_percent = 50
    coord._notify_efficiency_changes = False
    coord._log_efficiency_changes = False
    coord.hass = SimpleNamespace()
    coord.data = {"vents": {"v1": {"room": {"attributes": {"name": "Office"}}}}}
    return coord


def test_initial_rate_and_clamp():
    coord = _make_coord()
    coord._initial_efficiency_percent = 120
    assert coord._initial_rate() == 1.0
    assert coord._ensure_initial_rate("v1", "heating") == 1.0
    assert coord._vent_rates["v1"]["heating"] == 1.0

    coord._initial_efficiency_percent = -10
    assert coord._initial_rate() == 0.0


def test_maybe_log_efficiency_change_short_circuit():
    coord = _make_coord()
    coord._notify_efficiency_changes = False
    coord._log_efficiency_changes = False
    coord._maybe_log_efficiency_change("v1", "heating", 0.1, 0.2)


def test_maybe_log_efficiency_change_with_logbook(monkeypatch):
    coord = _make_coord()
    calls = {"notify": 0, "log": 0}
    coord._notify_efficiency_changes = True
    coord._log_efficiency_changes = True

    monkeypatch.setattr(
        "custom_components.hvac_vent_optimizer.coordinator.persistent_notification.async_create",
        lambda *args, **kwargs: calls.__setitem__("notify", calls["notify"] + 1),
    )
    monkeypatch.setattr(
        "custom_components.hvac_vent_optimizer.coordinator.logbook.async_log_entry",
        lambda *args, **kwargs: calls.__setitem__("log", calls["log"] + 1),
    )
    coord._maybe_log_efficiency_change("v1", "heating", 0.1, 0.2)
    assert calls["notify"] == 1
    assert calls["log"] == 1
