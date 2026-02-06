import asyncio
from types import SimpleNamespace

from custom_components.hvac_vent_optimizer.coordinator import FlairCoordinator
from custom_components.hvac_vent_optimizer.const import CONF_THERMOSTAT_ENTITY


def test_async_process_dab_groups_by_thermostat():
    coord = FlairCoordinator.__new__(FlairCoordinator)
    coord._get_vent_assignments = lambda: {
        "v1": {CONF_THERMOSTAT_ENTITY: "climate.a"},
        "v2": {CONF_THERMOSTAT_ENTITY: "climate.a"},
        "v3": {CONF_THERMOSTAT_ENTITY: "climate.b"},
    }
    called = []

    async def fake_group(thermo, vent_ids, data):
        called.append((thermo, tuple(sorted(vent_ids))))

    coord._async_process_thermostat_group = fake_group
    data = {"vents": {"v1": {}, "v2": {}, "v3": {}}}
    asyncio.run(coord._async_process_dab(data))
    assert ("climate.a", ("v1", "v2")) in called
    assert ("climate.b", ("v3",)) in called


def test_async_pre_adjust_uses_assignments():
    coord = FlairCoordinator.__new__(FlairCoordinator)
    coord.data = {"vents": {"v1": {}, "v2": {}}}
    coord._get_vent_assignments = lambda: {
        "v1": {CONF_THERMOSTAT_ENTITY: "climate.a"},
        "v2": {CONF_THERMOSTAT_ENTITY: "climate.b"},
    }
    called = {}

    async def fake_apply(thermo, action, vent_ids, data):
        called["args"] = (thermo, action, tuple(vent_ids))

    coord._async_apply_dab_adjustments = fake_apply
    asyncio.run(coord._async_pre_adjust("climate.a", "heating"))
    assert called["args"] == ("climate.a", "heating", ("v1",))
