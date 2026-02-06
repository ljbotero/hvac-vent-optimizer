"""Shared helper utilities."""
from __future__ import annotations

from typing import Any
import asyncio
import time


class AsyncRateLimiter:
    """Simple async rate limiter enforcing a minimum interval between calls."""

    def __init__(self, rate_per_sec: float) -> None:
        if rate_per_sec <= 0:
            raise ValueError("rate_per_sec must be > 0")
        self._min_interval = 1.0 / rate_per_sec
        self._next_time = 0.0
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            now = time.monotonic()
            if now < self._next_time:
                await asyncio.sleep(self._next_time - now)
            self._next_time = max(now, self._next_time) + self._min_interval


def is_fahrenheit_unit(unit: str | None) -> bool:
    """Return True if the unit represents Fahrenheit."""
    if not unit:
        return False
    normalized = "".join(ch for ch in unit.lower() if ch.isascii())
    return normalized in {"f", "degf", "fahrenheit"}


def get_remote_sensor_id(room: dict[str, Any]) -> str | None:
    relationships = room.get("relationships") or {}
    remote_rel = relationships.get("remote-sensors") or {}
    data = remote_rel.get("data")
    if isinstance(data, list):
        if data:
            return data[0].get("id")
        return None
    if isinstance(data, dict):
        return data.get("id")
    return None
