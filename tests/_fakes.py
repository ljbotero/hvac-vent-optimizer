"""Lightweight fakes for Home Assistant objects used in tests.

Imported only after conftest has installed the stub modules.
"""
from __future__ import annotations

import asyncio
from typing import Any


class FakeState:
    def __init__(self, state: str, attributes: dict | None = None):
        self.state = state
        self.attributes = attributes or {}
        self.entity_id = None


class FakeStates:
    def __init__(self, mapping: dict[str, FakeState] | None = None):
        self._mapping = mapping or {}

    def get(self, entity_id: str):
        return self._mapping.get(entity_id)

    def set(self, entity_id: str, state: FakeState):
        state.entity_id = entity_id
        self._mapping[entity_id] = state


class _Units:
    def __init__(self, unit: str = "°C"):
        self.temperature_unit = unit


class FakeConfig:
    def __init__(self, unit: str = "°C", base_path: str = "/config"):
        self.units = _Units(unit)
        self._base = base_path

    def path(self, *parts: str) -> str:
        import os

        return os.path.join(self._base, *parts)

    def is_allowed_path(self, path: str) -> bool:
        return True


class FakeHass:
    def __init__(self, unit: str = "°C"):
        self.states = FakeStates()
        self.config = FakeConfig(unit)
        self.data: dict[str, Any] = {}
        self.created_tasks: list[asyncio.Task] = []

    def async_create_task(self, coro, *args, **kwargs):
        task = asyncio.ensure_future(coro)
        self.created_tasks.append(task)
        return task


class FakeEntry:
    def __init__(self, *, data: dict | None = None, options: dict | None = None,
                 entry_id: str = "entry1", title: str = "Test"):
        self.data = data or {}
        self.options = options or {}
        self.entry_id = entry_id
        self.title = title

    def add_update_listener(self, listener):
        return lambda: None

    def async_on_unload(self, func):
        return None


class FakeApi:
    """Records vent/room/structure mutations."""

    def __init__(self):
        self.set_vent_calls: list[tuple[str, int]] = []
        self.set_room_active_calls: list[tuple[str, bool]] = []
        self.set_structure_mode_calls: list[tuple[str, str]] = []
        self.set_setpoint_calls: list[tuple] = []
        self.set_vent_hook = None  # optional async callable(vent_id, percent)

    async def async_set_vent_position(self, vent_id: str, percent_open: int) -> None:
        if self.set_vent_hook is not None:
            await self.set_vent_hook(vent_id, percent_open)
        self.set_vent_calls.append((vent_id, int(percent_open)))

    async def async_set_room_active(self, room_id: str, active: bool) -> None:
        self.set_room_active_calls.append((room_id, bool(active)))

    async def async_set_structure_mode(self, structure_id: str, mode: str) -> None:
        self.set_structure_mode_calls.append((structure_id, mode))

    async def async_set_room_setpoint(self, room_id, set_point_c, hold_until=None) -> None:
        self.set_setpoint_calls.append((room_id, set_point_c, hold_until))
