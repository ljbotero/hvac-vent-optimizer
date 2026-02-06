import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from custom_components.hvac_vent_optimizer.coordinator import FlairCoordinator
from custom_components.hvac_vent_optimizer.const import CONF_CONTROL_STRATEGY


def _make_coord():
    coord = FlairCoordinator.__new__(FlairCoordinator)
    coord.entry = SimpleNamespace(options={CONF_CONTROL_STRATEGY: "hybrid"})
    coord._pending_finalize = {}
    coord._dab_state = {}
    coord._cycle_stats = {"climate.main": {"adjustments": 1, "movement": 10.0, "strategy": "hybrid"}}
    coord._vent_starting_temps = {"v1": 20.0}
    coord._vent_starting_open = {"v1": 50}
    coord._vent_rates = {"v1": {"heating": 0.1}}
    coord._max_rates = {"heating": 0.1, "cooling": 0.0}
    coord._max_running_minutes = {}
    coord._vent_models = {}
    coord._efficiency_models = {}
    coord._strategy_metrics = {}
    coord._get_room_temp = lambda vent_id, data: 22.0
    coord._get_room_name = lambda vent_id, data: "Office"
    coord._get_vent_attribute = lambda vent_id, data, attr: 50
    coord._get_thermostat_setpoint = lambda *_: 22.5
    coord._get_thermostat_target_raw = lambda *_: 22.5
    coord._maybe_log_efficiency_change = lambda *args, **kwargs: None
    coord._update_strategy_metrics = lambda *args, **kwargs: coord._strategy_metrics.update({"updated": True})

    async def _noop():
        return None

    coord._async_save_state = _noop
    coord.data = {"vents": {"v1": {"room": {"attributes": {"name": "Office"}}}}}
    return coord


def test_async_finalize_cycle_updates_rates_and_models():
    coord = _make_coord()
    now = datetime.now(timezone.utc)
    coord._dab_state["climate.main"] = {
        "mode": "heating",
        "started_cycle": now - timedelta(minutes=15),
        "started_running": now - timedelta(minutes=10),
        "samples": {
            "v1": [
                {"t": now - timedelta(minutes=8), "temp": 20.0, "aperture": 50, "duct": None},
                {"t": now - timedelta(minutes=1), "temp": 22.0, "aperture": 50, "duct": None},
            ]
        },
    }

    asyncio.run(coord._async_finalize_cycle("climate.main", "heating", ["v1"]))

    assert coord._vent_rates["v1"]["heating"] > 0
    assert "v1" in coord._vent_models
    assert coord._strategy_metrics.get("updated") is True
