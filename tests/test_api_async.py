import asyncio
from unittest.mock import MagicMock

from custom_components.hvac_vent_optimizer.api import FlairApi, FlairApiError


def test_remote_sensor_reading_current():
    api = FlairApi(MagicMock(), "id", "secret")

    async def fake_request(method, path, **kwargs):
        return {"data": {"attributes": {"occupied": True}}}

    api._async_request = fake_request
    result = asyncio.run(api.async_get_remote_sensor_reading("sensor-1"))
    assert result["occupied"] is True


def test_remote_sensor_reading_fallback():
    api = FlairApi(MagicMock(), "id", "secret")

    calls = []

    async def fake_request(method, path, **kwargs):
        calls.append(path)
        if path.endswith("/current-reading"):
            raise FlairApiError("404")
        return {"data": [{"attributes": {"occupied": False}}]}

    api._async_request = fake_request
    result = asyncio.run(api.async_get_remote_sensor_reading("sensor-2"))
    assert calls[0].endswith("/current-reading")
    assert calls[1].endswith("/sensor-readings")
    assert result["occupied"] is False


def test_vent_reading_handles_list_payload():
    api = FlairApi(MagicMock(), "id", "secret")

    async def fake_request(method, path, **kwargs):
        return {"data": [{"attributes": {"duct-pressure": 1.2}}]}

    api._async_request = fake_request
    result = asyncio.run(api.async_get_vent_reading("vent-1"))
    assert result["duct-pressure"] == 1.2


def test_puck_reading_handles_list_payload():
    api = FlairApi(MagicMock(), "id", "secret")

    async def fake_request(method, path, **kwargs):
        return {"data": [{"attributes": {"current-temperature-c": 21.5}}]}

    api._async_request = fake_request
    result = asyncio.run(api.async_get_puck_reading("puck-1"))
    assert result["current-temperature-c"] == 21.5


def test_rate_limiter_selection():
    api = FlairApi(MagicMock(), "id", "secret")
    assert api._get_rate_limiter("/api/structures") is api._basic_limiter
    assert api._get_rate_limiter("/api/vents/search") is api._search_limiter
