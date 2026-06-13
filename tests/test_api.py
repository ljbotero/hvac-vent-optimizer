"""Fix #9: a 401/403 mid-request should trigger one token refresh + retry."""
from __future__ import annotations

import json

import aiohttp
import pytest

from hvac_vent_optimizer.api import FlairApi, FlairApiAuthError


class FakeResp:
    def __init__(self, status, *, json_data=None, text="", headers=None):
        self.status = status
        self._json = json_data
        self._text = text
        self.headers = headers or {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def text(self):
        return self._text

    async def json(self):
        if self._json is None:
            raise aiohttp.ContentTypeError()
        return self._json


class FakeSession:
    def __init__(self, token_payload, request_responses):
        self._token_payload = token_payload
        self._responses = list(request_responses)
        self.request_calls = []
        self.post_calls = []

    def post(self, url, **kwargs):
        self.post_calls.append(url)
        return FakeResp(200, text=json.dumps(self._token_payload))

    async def request(self, method, url, **kwargs):
        self.request_calls.append((method, url))
        return self._responses.pop(0)


def _make_api(request_responses):
    session = FakeSession({"access_token": "tok", "expires_in": 3600}, request_responses)
    api = FlairApi(session, "cid", "secret")

    # Neutralize real rate-limiter sleeps for fast tests.
    class _NoLimit:
        async def acquire(self):
            return None

    api._basic_limiter = _NoLimit()
    api._search_limiter = _NoLimit()
    return api, session


@pytest.mark.asyncio
async def test_success_first_try_no_retry():
    api, session = _make_api([FakeResp(200, json_data={"ok": 1})])
    result = await api._async_request("GET", "/api/structures")
    assert result == {"ok": 1}
    assert len(session.request_calls) == 1


@pytest.mark.asyncio
async def test_401_triggers_reauth_and_retry():
    api, session = _make_api([FakeResp(401), FakeResp(200, json_data={"ok": 2})])
    result = await api._async_request("GET", "/api/structures")
    assert result == {"ok": 2}
    assert len(session.request_calls) == 2          # retried once
    assert len(session.post_calls) == 2             # token refreshed after 401


@pytest.mark.asyncio
async def test_persistent_401_raises_after_one_retry():
    api, session = _make_api([FakeResp(401), FakeResp(403)])
    with pytest.raises(FlairApiAuthError):
        await api._async_request("GET", "/api/structures")
    assert len(session.request_calls) == 2          # exactly one retry, no infinite loop


@pytest.mark.asyncio
async def test_429_still_retries_once():
    api, session = _make_api([
        FakeResp(429, headers={"Retry-After": "0"}),
        FakeResp(200, json_data={"ok": 3}),
    ])
    result = await api._async_request("GET", "/api/structures")
    assert result == {"ok": 3}
    assert len(session.request_calls) == 2
