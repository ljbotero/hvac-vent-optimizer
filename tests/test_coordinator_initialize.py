import asyncio
from types import SimpleNamespace

from custom_components.hvac_vent_optimizer.coordinator import FlairCoordinator


class _Store:
    def __init__(self, payload):
        self.payload = payload

    async def async_load(self):
        return self.payload


def test_async_initialize_loads_state():
    coord = FlairCoordinator.__new__(FlairCoordinator)
    coord._store = _Store(
        {
            "vent_rates": {"v1": {"heating": 0.2}},
            "max_rates": {"heating": 0.9},
            "max_running_minutes": {"climate.main": 15.0},
            "vent_models": {"v1": {"heating": {"n": 1}}},
            "strategy_metrics": {"hybrid": {"cycles": 1}},
        }
    )
    coord._vent_rates = {}
    coord._max_rates = {"heating": 0.0, "cooling": 0.0}
    coord._max_running_minutes = {}
    coord._vent_models = {}
    coord._strategy_metrics = {}

    asyncio.run(coord.async_initialize())
    assert coord._vent_rates["v1"]["heating"] == 0.2
    assert coord._max_rates["heating"] == 0.9
    assert coord._max_running_minutes["climate.main"] == 15.0
    assert coord._vent_models["v1"]["heating"]["n"] == 1
    assert coord._strategy_metrics["hybrid"]["cycles"] == 1


def test_async_initialize_no_data():
    coord = FlairCoordinator.__new__(FlairCoordinator)
    coord._store = _Store(None)
    coord._vent_rates = {"v1": {"heating": 0.2}}
    asyncio.run(coord.async_initialize())
    assert coord._vent_rates["v1"]["heating"] == 0.2
