"""Flair API client."""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from typing import Any

import aiohttp
import logging

from .utils import AsyncRateLimiter

_LOGGER = logging.getLogger(__name__)


class FlairApiError(Exception):
    """Base Flair API error."""


class FlairApiAuthError(FlairApiError):
    """Authentication failure with Flair API."""


class FlairApi:
    """Minimal Flair API client with token management."""

    BASE_URL = "https://api.flair.co"
    _SCOPES_FULL = (
        "vents.view vents.edit structures.view structures.edit "
        "pucks.view pucks.edit rooms.view rooms.edit"
    )
    _SCOPES_BASE = "vents.view vents.edit structures.view structures.edit pucks.view pucks.edit"

    def __init__(self, session: aiohttp.ClientSession, client_id: str, client_secret: str) -> None:
        self._session = session
        self._client_id = client_id
        self._client_secret = client_secret
        self._access_token: str | None = None
        self._token_expires_at: datetime | None = None
        self._auth_lock = asyncio.Lock()
        self._missing_pressure_logged: set[str] = set()
        self._basic_limiter = AsyncRateLimiter(4.0)
        self._search_limiter = AsyncRateLimiter(1.0)

    async def async_authenticate(self) -> None:
        """Authenticate with Flair API using client credentials."""
        async with self._auth_lock:
            if self._access_token and self._token_expires_at:
                if datetime.now(timezone.utc) < self._token_expires_at:
                    return

            async def _request_token(scope: str) -> dict[str, Any]:
                payload = {
                    "client_id": self._client_id,
                    "client_secret": self._client_secret,
                    "scope": scope,
                    "grant_type": "client_credentials",
                }

                try:
                    await self._basic_limiter.acquire()
                    async with self._session.post(
                        f"{self.BASE_URL}/oauth2/token",
                        data=payload,
                        headers={
                            "Accept": "application/json",
                            "Content-Type": "application/x-www-form-urlencoded",
                        },
                        timeout=aiohttp.ClientTimeout(total=10),
                    ) as resp:
                        if resp.status in {401, 403}:
                            raise FlairApiAuthError("Invalid Flair credentials")
                        body = await resp.text()
                        if resp.status >= 400:
                            raise FlairApiError(
                                f"Authentication failed: HTTP {resp.status}: {body}"
                            )
                        if not body:
                            return {}
                        try:
                            return json.loads(body)
                        except json.JSONDecodeError:
                            return {}
                except asyncio.TimeoutError as err:
                    raise FlairApiError("Authentication request timed out") from err
                except aiohttp.ClientError as err:
                    raise FlairApiError(f"Authentication request failed: {err}") from err

            try:
                data = await _request_token(self._SCOPES_FULL)
            except FlairApiError as err:
                message = str(err)
                if "invalid_scope" in message:
                    data = await _request_token(self._SCOPES_BASE)
                else:
                    raise

            token = data.get("access_token")
            if not token:
                raise FlairApiAuthError("Missing access_token in response")

            self._access_token = token
            expires_in = int(data.get("expires_in", 3600))
            self._token_expires_at = datetime.now(timezone.utc) + timedelta(seconds=expires_in - 60)

    def _get_rate_limiter(self, path: str) -> AsyncRateLimiter:
        # Flair documents different limits; treat any search endpoint as "search".
        if "search" in path:
            return self._search_limiter
        return self._basic_limiter

    async def _async_request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        await self._get_rate_limiter(path).acquire()
        await self.async_authenticate()
        headers = kwargs.pop("headers", {})
        headers["Authorization"] = f"Bearer {self._access_token}"
        headers.setdefault("Accept", "application/vnd.api+json")

        async def _do_request() -> aiohttp.ClientResponse:
            return await self._session.request(
                method,
                f"{self.BASE_URL}{path}",
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=10),
                **kwargs,
            )

        async with await _do_request() as resp:
            if resp.status in {401, 403}:
                self._access_token = None
                raise FlairApiAuthError("Flair token expired or unauthorized")
            if resp.status == 429:
                retry_after = resp.headers.get("Retry-After")
                try:
                    wait_for = float(retry_after) if retry_after else 1.0
                except ValueError:
                    wait_for = 1.0
                await asyncio.sleep(wait_for)
                async with await _do_request() as retry_resp:
                    if retry_resp.status in {401, 403}:
                        self._access_token = None
                        raise FlairApiAuthError("Flair token expired or unauthorized")
                    if retry_resp.status >= 400:
                        body = await retry_resp.text()
                        raise FlairApiError(
                            f"Flair API error: HTTP {retry_resp.status}: {body}"
                        )
                    try:
                        return await retry_resp.json()
                    except (aiohttp.ContentTypeError, json.JSONDecodeError) as err:
                        body = await retry_resp.text()
                        raise FlairApiError(
                            f"Flair API non-JSON response: HTTP {retry_resp.status}: {body}"
                        ) from err
            if resp.status >= 400:
                body = await resp.text()
                raise FlairApiError(f"Flair API error: HTTP {resp.status}: {body}")
            try:
                return await resp.json()
            except (aiohttp.ContentTypeError, json.JSONDecodeError) as err:
                body = await resp.text()
                raise FlairApiError(
                    f"Flair API non-JSON response: HTTP {resp.status}: {body}"
                ) from err

    async def async_get_structures(self) -> list[dict[str, str]]:
        """Return a list of structures with id and name."""
        data = await self._async_request("GET", "/api/structures")
        structures = []
        for item in data.get("data", []) or []:
            name = (item.get("attributes") or {}).get("name") or item.get("id")
            structures.append({"id": item.get("id"), "name": name})
        return [s for s in structures if s.get("id")]

    async def async_get_vents(self, structure_id: str) -> list[dict[str, Any]]:
        """Return raw vent payloads for a structure."""
        data = await self._async_request("GET", f"/api/structures/{structure_id}/vents")
        return self._extract_devices(data)

    async def async_get_pucks(self, structure_id: str) -> list[dict[str, Any]]:
        """Return raw puck payloads for a structure."""
        data = await self._async_request("GET", f"/api/structures/{structure_id}/pucks")
        return self._extract_devices(data)

    async def async_get_vent_reading(self, vent_id: str) -> dict[str, Any]:
        """Return vent current-reading attributes."""
        data = await self._async_request("GET", f"/api/vents/{vent_id}/current-reading")
        payload = data.get("data")
        if isinstance(payload, list):
            if not payload:
                return {}
            payload = payload[0]
        attrs = (payload or {}).get("attributes", {}) or {}
        if "duct-pressure" not in attrs and vent_id not in self._missing_pressure_logged:
            self._missing_pressure_logged.add(vent_id)
            _LOGGER.debug(
                "Vent %s current-reading missing duct-pressure. Keys=%s Payload=%s",
                vent_id,
                list(attrs.keys()),
                payload,
            )
        return attrs

    async def async_get_vent_room(self, vent_id: str) -> dict[str, Any]:
        """Return vent room data."""
        data = await self._async_request("GET", f"/api/vents/{vent_id}/room")
        return data.get("data") or {}

    async def async_get_puck_reading(self, puck_id: str) -> dict[str, Any]:
        """Return puck current-reading attributes."""
        data = await self._async_request("GET", f"/api/pucks/{puck_id}/current-reading")
        payload = data.get("data")
        if isinstance(payload, list):
            if not payload:
                return {}
            payload = payload[0]
        return (payload or {}).get("attributes", {}) or {}

    async def async_get_remote_sensor_reading(self, sensor_id: str) -> dict[str, Any]:
        """Return remote sensor current-reading attributes."""
        try:
            data = await self._async_request("GET", f"/api/remote-sensors/{sensor_id}/current-reading")
        except FlairApiError:
            data = await self._async_request("GET", f"/api/remote-sensors/{sensor_id}/sensor-readings")

        payload = data.get("data")
        if isinstance(payload, list):
            if not payload:
                return {}
            payload = payload[0]
        return (payload or {}).get("attributes", {}) or {}

    async def async_get_puck_room(self, puck_id: str) -> dict[str, Any]:
        """Return puck room data."""
        data = await self._async_request("GET", f"/api/pucks/{puck_id}/room")
        return data.get("data") or {}

    async def async_set_vent_position(self, vent_id: str, percent_open: int) -> None:
        """Set vent position (0-100)."""
        payload = {
            "data": {
                "type": "vents",
                "attributes": {"percent-open": int(percent_open)},
            }
        }
        await self._async_request("PATCH", f"/api/vents/{vent_id}", json=payload)

    async def async_set_room_active(self, room_id: str, active: bool) -> None:
        """Set room active/away state."""
        payload = {
            "data": {
                "type": "rooms",
                "attributes": {"active": bool(active)},
            }
        }
        await self._async_request("PATCH", f"/api/rooms/{room_id}", json=payload)

    async def async_set_structure_mode(self, structure_id: str, mode: str) -> None:
        """Set structure mode (auto/manual)."""
        payload = {
            "data": {
                "type": "structures",
                "attributes": {"mode": mode},
            }
        }
        await self._async_request("PATCH", f"/api/structures/{structure_id}", json=payload)

    async def async_set_room_setpoint(
        self, room_id: str, set_point_c: float, hold_until: str | datetime | None = None
    ) -> None:
        """Set room set point in Celsius with optional hold-until timestamp."""
        attributes: dict[str, Any] = {"set-point-c": float(set_point_c)}
        if hold_until:
            if isinstance(hold_until, datetime):
                attributes["hold-until"] = hold_until.isoformat()
            else:
                attributes["hold-until"] = hold_until
        payload = {
            "data": {
                "type": "rooms",
                "attributes": attributes,
            }
        }
        await self._async_request("PATCH", f"/api/rooms/{room_id}", json=payload)

    @staticmethod
    def _extract_devices(data: dict[str, Any]) -> list[dict[str, Any]]:
        devices = []
        for item in data.get("data", []) or []:
            if not item.get("id"):
                continue
            attributes = item.get("attributes") or {}
            name = attributes.get("name") or item.get("id")
            devices.append(
                {
                    "id": item.get("id"),
                    "name": name,
                    "type": item.get("type"),
                    "attributes": attributes,
                }
            )
        return devices
