from custom_components.hvac_vent_optimizer.api import FlairApi


def test_extract_devices_parses_minimal_payload():
    payload = {
        "data": [
            {"id": "vent1", "type": "vents", "attributes": {"name": "Living Room"}},
            {"id": "puck1", "type": "pucks", "attributes": {}},
            {"type": "vents", "attributes": {"name": "Missing"}},
        ]
    }
    devices = FlairApi._extract_devices(payload)
    assert len(devices) == 2
    assert devices[0]["id"] == "vent1"
    assert devices[0]["name"] == "Living Room"
    assert devices[1]["id"] == "puck1"
    assert devices[1]["name"] == "puck1"
