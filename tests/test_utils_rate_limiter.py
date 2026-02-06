import asyncio

import pytest

from custom_components.hvac_vent_optimizer.utils import AsyncRateLimiter


def test_rate_limiter_rejects_invalid_rate():
    with pytest.raises(ValueError):
        AsyncRateLimiter(0)


def test_rate_limiter_sleeps_when_called_too_fast(monkeypatch):
    limiter = AsyncRateLimiter(1.0)
    slept = []

    async def fake_sleep(delay):
        slept.append(delay)

    monkeypatch.setattr("custom_components.hvac_vent_optimizer.utils.asyncio.sleep", fake_sleep)

    import time

    limiter._next_time = time.monotonic() + 0.5
    asyncio.run(limiter.acquire())
    assert slept and slept[0] > 0
