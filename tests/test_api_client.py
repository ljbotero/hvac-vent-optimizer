import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from custom_components.hvac_vent_optimizer.api import FlairApi, FlairApiAuthError, FlairApiError


class _FakeResponse:
    def __init__(self, status, payload):
        self.status = status
        self._payload = payload
        self.headers = {}

    async def json(self):
        return self._payload

    async def text(self):
        if isinstance(self._payload, str):
            return self._payload
        if isinstance(self._payload, dict):
            import json

            return json.dumps(self._payload)
        return str(self._payload)

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _FakeSession:
    def __init__(self, response):
        self.response = response
        self.last_headers = None
        self.last_request = None
        self.post_calls = []

    def post(self, url, **kwargs):
        self.post_calls.append((url, kwargs))
        self.last_request = ("POST", url, kwargs)
        return self.response

    async def request(self, method, url, **kwargs):
        self.last_request = (method, url, kwargs)
        self.last_headers = kwargs.get("headers", {})
        return self.response


class _SequencedSession(_FakeSession):
    def __init__(self, responses):
        self.responses = list(responses)
        super().__init__(self.responses[0])

    def post(self, url, **kwargs):
        self.post_calls.append((url, kwargs))
        self.last_request = ("POST", url, kwargs)
        return self.responses.pop(0)


class _SequencedRequestSession(_FakeSession):
    def __init__(self, responses):
        self.responses = list(responses)
        super().__init__(self.responses[0])

    async def request(self, method, url, **kwargs):
        self.last_request = (method, url, kwargs)
        self.last_headers = kwargs.get("headers", {})
        return self.responses.pop(0)


def test_authenticate_success_sets_token():
    response = _FakeResponse(200, {"access_token": "abc", "expires_in": 3600})
    session = _FakeSession(response)
    api = FlairApi(session, "id", "secret")

    asyncio.run(api.async_authenticate())
    assert api._access_token == "abc"
    assert api._token_expires_at is not None
    assert session.last_request[2]["headers"]["Content-Type"] == "application/x-www-form-urlencoded"


def test_authenticate_invalid_credentials():
    response = _FakeResponse(401, {})
    session = _FakeSession(response)
    api = FlairApi(session, "id", "secret")

    with pytest.raises(FlairApiAuthError):
        asyncio.run(api.async_authenticate())


def test_authenticate_error_body_includes_message():
    response = _FakeResponse(400, "invalid_client")
    session = _FakeSession(response)
    api = FlairApi(session, "id", "secret")

    with pytest.raises(FlairApiError) as err:
        asyncio.run(api.async_authenticate())
    assert "invalid_client" in str(err.value)


def test_authenticate_timeout_raises_flair_error():
    class _TimeoutSession(_FakeSession):
        def post(self, url, **kwargs):
            raise asyncio.TimeoutError

    session = _TimeoutSession(_FakeResponse(200, {}))
    api = FlairApi(session, "id", "secret")

    with pytest.raises(FlairApiError):
        asyncio.run(api.async_authenticate())


def test_authenticate_retries_on_invalid_scope():
    responses = [
        _FakeResponse(400, '{"error": "invalid_scope"}'),
        _FakeResponse(200, {"access_token": "abc", "expires_in": 3600}),
    ]
    session = _SequencedSession(responses)
    api = FlairApi(session, "id", "secret")

    asyncio.run(api.async_authenticate())
    assert api._access_token == "abc"
    assert len(session.post_calls) == 2


def test_authenticate_skips_when_token_valid():
    response = _FakeResponse(200, {"access_token": "abc", "expires_in": 3600})
    session = _FakeSession(response)
    api = FlairApi(session, "id", "secret")
    api._access_token = "cached"
    api._token_expires_at = datetime.now(timezone.utc) + timedelta(minutes=5)

    asyncio.run(api.async_authenticate())
    assert api._access_token == "cached"
    assert session.last_request is None


def test_async_request_adds_auth_headers():
    response = _FakeResponse(200, {"data": []})
    session = _FakeSession(response)
    api = FlairApi(session, "id", "secret")
    api._access_token = "token"
    api._token_expires_at = datetime.now(timezone.utc) + timedelta(minutes=5)

    result = asyncio.run(api._async_request("GET", "/api/test"))
    assert result == {"data": []}
    assert session.last_headers["Authorization"] == "Bearer token"
    assert session.last_headers["Accept"] == "application/vnd.api+json"


def test_async_request_unauthorized_resets_token():
    response = _FakeResponse(401, {})
    session = _FakeSession(response)
    api = FlairApi(session, "id", "secret")
    api._access_token = "token"
    api._token_expires_at = datetime.now(timezone.utc) + timedelta(minutes=5)

    with pytest.raises(FlairApiAuthError):
        asyncio.run(api._async_request("GET", "/api/test"))
    assert api._access_token is None


def test_async_get_structures_parses_names():
    api = FlairApi(_FakeSession(_FakeResponse(200, {})), "id", "secret")

    async def fake_request(method, path, **kwargs):
        return {
            "data": [
                {"id": "1", "attributes": {"name": "Home"}},
                {"id": "2", "attributes": {}},
                {"id": None, "attributes": {"name": "Skip"}},
            ]
        }

    api._async_request = fake_request
    structures = asyncio.run(api.async_get_structures())
    assert structures == [{"id": "1", "name": "Home"}, {"id": "2", "name": "2"}]


def test_set_room_setpoint_payload():
    api = FlairApi(_FakeSession(_FakeResponse(200, {})), "id", "secret")
    calls = {}

    async def fake_request(method, path, **kwargs):
        calls["method"] = method
        calls["path"] = path
        calls["json"] = kwargs.get("json")
        return {}

    api._async_request = fake_request
    asyncio.run(api.async_set_room_setpoint("room-1", 22.5, "2024-01-01T00:00:00Z"))

    assert calls["method"] == "PATCH"
    assert calls["path"] == "/api/rooms/room-1"
    assert calls["json"]["data"]["attributes"]["set-point-c"] == 22.5
    assert calls["json"]["data"]["attributes"]["hold-until"] == "2024-01-01T00:00:00Z"


def test_async_get_vent_reading_handles_list_payload_and_missing_pressure():
    api = FlairApi(_FakeSession(_FakeResponse(200, {})), "id", "secret")

    async def fake_request(method, path, **kwargs):
        return {"data": [{"attributes": {"system-voltage": 2.8}}]}

    api._async_request = fake_request
    attrs = asyncio.run(api.async_get_vent_reading("vent1"))
    assert attrs["system-voltage"] == 2.8
    assert "vent1" in api._missing_pressure_logged


def test_async_get_puck_reading_handles_empty_list():
    api = FlairApi(_FakeSession(_FakeResponse(200, {})), "id", "secret")

    async def fake_request(method, path, **kwargs):
        return {"data": []}

    api._async_request = fake_request
    attrs = asyncio.run(api.async_get_puck_reading("puck1"))
    assert attrs == {}


def test_async_get_remote_sensor_reading_fallback():
    api = FlairApi(_FakeSession(_FakeResponse(200, {})), "id", "secret")
    calls = {"count": 0}

    async def fake_request(method, path, **kwargs):
        calls["count"] += 1
        if calls["count"] == 1:
            raise FlairApiError("fail")
        return {"data": {"attributes": {"occupied": True}}}

    api._async_request = fake_request
    attrs = asyncio.run(api.async_get_remote_sensor_reading("sensor1"))
    assert attrs["occupied"] is True


def test_set_structure_mode_payload():
    api = FlairApi(_FakeSession(_FakeResponse(200, {})), "id", "secret")
    calls = {}

    async def fake_request(method, path, **kwargs):
        calls["method"] = method
        calls["path"] = path
        calls["json"] = kwargs.get("json")
        return {}

    api._async_request = fake_request
    asyncio.run(api.async_set_structure_mode("struct-1", "manual"))
    assert calls["path"] == "/api/structures/struct-1"
    assert calls["json"]["data"]["attributes"]["mode"] == "manual"


def test_set_room_active_payload():
    api = FlairApi(_FakeSession(_FakeResponse(200, {})), "id", "secret")
    calls = {}

    async def fake_request(method, path, **kwargs):
        calls["method"] = method
        calls["path"] = path
        calls["json"] = kwargs.get("json")
        return {}

    api._async_request = fake_request
    asyncio.run(api.async_set_room_active("room-2", True))
    assert calls["path"] == "/api/rooms/room-2"
    assert calls["json"]["data"]["attributes"]["active"] is True


def test_async_request_error_status():
    response = _FakeResponse(500, {})
    session = _FakeSession(response)
    api = FlairApi(session, "id", "secret")
    api._access_token = "token"
    api._token_expires_at = datetime.now(timezone.utc) + timedelta(minutes=5)

    with pytest.raises(FlairApiError):
        asyncio.run(api._async_request("GET", "/api/test"))


def test_async_request_retries_on_429(monkeypatch):
    responses = [
        _FakeResponse(429, {"error": "rate_limited"}),
        _FakeResponse(200, {"data": []}),
    ]
    session = _SequencedRequestSession(responses)
    api = FlairApi(session, "id", "secret")
    api._access_token = "token"
    api._token_expires_at = datetime.now(timezone.utc) + timedelta(minutes=5)

    slept = []

    async def fake_sleep(delay):
        slept.append(delay)

    monkeypatch.setattr("custom_components.hvac_vent_optimizer.api.asyncio.sleep", fake_sleep)
    result = asyncio.run(api._async_request("GET", "/api/test"))
    assert result == {"data": []}
    assert slept


def test_async_request_non_json_response_raises():
    class _BadResponse(_FakeResponse):
        async def json(self):
            from aiohttp import ContentTypeError

            raise ContentTypeError(request_info=None, history=None, message="bad")

    response = _BadResponse(200, "not-json")
    session = _FakeSession(response)
    api = FlairApi(session, "id", "secret")
    api._access_token = "token"
    api._token_expires_at = datetime.now(timezone.utc) + timedelta(minutes=5)

    with pytest.raises(FlairApiError):
        asyncio.run(api._async_request("GET", "/api/test"))


def test_async_request_retry_non_json_raises():
    class _BadResponse(_FakeResponse):
        async def json(self):
            from aiohttp import ContentTypeError

            raise ContentTypeError(request_info=None, history=None, message="bad")

    responses = [
        _FakeResponse(429, {"error": "rate_limited"}),
        _BadResponse(200, "not-json"),
    ]
    session = _SequencedRequestSession(responses)
    api = FlairApi(session, "id", "secret")
    api._access_token = "token"
    api._token_expires_at = datetime.now(timezone.utc) + timedelta(minutes=5)

    with pytest.raises(FlairApiError):
        asyncio.run(api._async_request("GET", "/api/test"))
