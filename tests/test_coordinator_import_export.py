import asyncio
from types import SimpleNamespace

import pytest

from custom_components.hvac_vent_optimizer.coordinator import FlairCoordinator, _coerce_rate
from custom_components.hvac_vent_optimizer.const import CONF_STRUCTURE_ID


def _make_coord():
    coord = FlairCoordinator.__new__(FlairCoordinator)
    coord.entry = SimpleNamespace(data={CONF_STRUCTURE_ID: "structure1"})
    coord._vent_rates = {"v1": {"cooling": 0.1, "heating": 0.2}}
    coord._max_rates = {"cooling": 0.5, "heating": 0.7}
    coord.data = {
        "vents": {
            "v1": {
                "id": "v1",
                "room": {"id": "room1", "attributes": {"name": "Office"}},
            },
            "v2": {
                "id": "v2",
                "room": {"id": "room2", "attributes": {"name": "Bedroom"}},
            },
        }
    }
    coord.async_set_updated_data = lambda data: None

    async def _noop():
        return None

    coord._async_save_state = _noop
    coord.async_request_refresh = _noop
    return coord


def test_build_efficiency_export_contains_rates():
    coord = _make_coord()
    payload = coord.build_efficiency_export()
    assert payload["exportMetadata"]["structureId"] == "structure1"
    assert payload["efficiencyData"]["globalRates"]["maxCoolingRate"] == 0.5
    assert payload["efficiencyData"]["roomEfficiencies"][0]["ventId"] == "v1"


def test_async_import_efficiency_matches_by_vent_id():
    coord = _make_coord()
    payload = {
        "efficiencyData": {
            "globalRates": {"maxCoolingRate": 0.9},
            "roomEfficiencies": [
                {"ventId": "v1", "coolingRate": 0.3, "heatingRate": 0.4},
            ],
        }
    }
    result = asyncio.run(coord.async_import_efficiency(payload))
    assert result["applied"] == 1
    assert coord._vent_rates["v1"]["cooling"] == 0.3
    assert coord._max_rates["cooling"] == 0.9


def test_async_import_efficiency_matches_by_room_id_and_name():
    coord = _make_coord()
    payload = {
        "efficiencyData": {
            "roomEfficiencies": [
                {"roomId": "room2", "coolingRate": 0.2},
                {"roomName": "Office", "heatingRate": 0.6},
            ],
        }
    }
    result = asyncio.run(coord.async_import_efficiency(payload))
    assert result["applied"] == 2
    assert coord._vent_rates["v2"]["cooling"] == 0.2
    assert coord._vent_rates["v1"]["heating"] == 0.6


def test_async_import_efficiency_invalid_payloads():
    coord = _make_coord()
    with pytest.raises(ValueError):
        asyncio.run(coord.async_import_efficiency("bad"))
    with pytest.raises(ValueError):
        asyncio.run(coord.async_import_efficiency({"efficiencyData": "bad"}))
    with pytest.raises(ValueError):
        asyncio.run(
            coord.async_import_efficiency({"efficiencyData": {"roomEfficiencies": {"a": 1}}})
        )


def test_coerce_rate_validation():
    assert _coerce_rate("1.2") == pytest.approx(1.2)
    assert _coerce_rate("-1") is None
    assert _coerce_rate("bad") is None
