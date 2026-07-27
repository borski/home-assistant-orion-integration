"""Async API client for Orion Sleep."""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import date, timedelta
from typing import Any, Callable

import aiohttp

from .const import API_BASE_URL
from .util import (
    auth_session_from_response,
    auth_tokens_from_session,
    describe_api_error,
    safe_api_error_code,
    should_refresh_token,
)

_SCHEDULE_TEMPERATURE_FIELDS = frozenset(
    {"bedtime_temp", "phase_1_temp", "phase_2_temp", "wakeup_temp"}
)

# Body keys accepted by PUT /v1/devices/{serial}/live besides `zones`.
# APP-DERIVED from Orion Android v2.4.1 bytecode. `water_fill` is a real
# fifth key (values `pour_water` and `unknown`) but drives a physical
# water-fill workflow, so it is deliberately not exposed as an entity.
_LIVE_SETTING_FIELDS = frozenset({"led_brightness", "quiet_mode"})

_LOGGER = logging.getLogger(__name__)


class OrionApiError(Exception):
    """General API error."""

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        error_code: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.error_code = error_code


class OrionAuthError(OrionApiError):
    """Authentication failure (401 / invalid tokens)."""


class OrionConnectionError(OrionApiError):
    """Network / connection error."""


class OrionApiClient:
    """Async API client for Orion Sleep."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        access_token: str | None = None,
        refresh_token: str | None = None,
        expires_at: float = 0,
    ) -> None:
        self._session = session
        self._access_token = access_token
        self._refresh_token = refresh_token
        self._expires_at = expires_at
        self._token_refresh_callback: Callable[[str, str, float], None] | None = None
        self._refresh_lock = asyncio.Lock()

    def set_token_refresh_callback(self, callback: Callable[[str, str, float], None]) -> None:
        """Register callback invoked when tokens are refreshed."""
        self._token_refresh_callback = callback

    # ── Internal helpers ──────────────────────────────────────────────

    def _url(self, path: str) -> str:
        return f"{API_BASE_URL}{path}"

    def _headers(self, with_auth: bool = True) -> dict[str, str]:
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if with_auth and self._access_token:
            headers["Authorization"] = f"Bearer {self._access_token}"
        return headers

    async def _request(
        self,
        method: str,
        path: str,
        *,
        with_auth: bool = True,
        json_data: dict | None = None,
        params: dict | None = None,
    ) -> Any:
        """Make an HTTP request and return parsed JSON."""
        url = self._url(path)
        headers = self._headers(with_auth=with_auth)

        try:
            async with self._session.request(
                method, url, headers=headers, json=json_data, params=params
            ) as resp:
                if resp.status == 401:
                    raise OrionAuthError(
                        "Authentication failed for Orion API request: 401",
                        status=resp.status,
                    )
                if not resp.ok:
                    error_code = await self._response_error_code(resp)
                    raise OrionApiError(
                        f"Orion API request failed: {method} {resp.status}",
                        status=resp.status,
                        error_code=error_code,
                    )
                if resp.content_length == 0:
                    return {}
                return await resp.json()
        except aiohttp.ClientError as err:
            raise OrionConnectionError(
                f"Orion API connection error: {type(err).__name__}"
            ) from None

    @staticmethod
    async def _response_error_code(resp: aiohttp.ClientResponse) -> str | None:
        """Extract only a stable, non-sensitive error identifier."""
        try:
            data = await resp.json(content_type=None)
        except (aiohttp.ContentTypeError, ValueError):
            return None
        if not isinstance(data, dict):
            return None
        return safe_api_error_code(data)

    # ── Auth methods (used by config_flow, no bearer token needed) ────

    async def request_auth_code(self, email: str | None = None, phone: str | None = None) -> bool:
        """POST /v1/auth/code — send a verification code."""
        body: dict[str, str] = {}
        if email:
            body["email"] = email
        if phone:
            body["phone"] = phone

        data = await self._request("POST", "/v1/auth/code", with_auth=False, json_data=body)
        channel = "email" if email else "phone" if phone else "unknown"
        success = bool(data.get("success", False)) if isinstance(data, dict) else False
        if success:
            _LOGGER.debug("Orion accepted the %s verification code request", channel)
        else:
            _LOGGER.warning(
                "Orion rejected the %s verification code request (%s)",
                channel,
                describe_api_error(data),
            )
        return success

    async def verify_auth_code(
        self,
        code: str,
        email: str | None = None,
        phone: str | None = None,
    ) -> dict:
        """POST /v1/auth/verify — returns session dict with tokens.

        Returns: {"access_token": ..., "refresh_token": ..., "expires_at": ...}
        """
        body: dict[str, str] = {"code": code}
        if email:
            body["email"] = email
        if phone:
            body["phone"] = phone

        data = await self._request("POST", "/v1/auth/verify", with_auth=False, json_data=body)

        tokens = auth_tokens_from_session(auth_session_from_response(data))
        if tokens is None:
            keys = sorted(data) if isinstance(data, dict) else []
            raise OrionAuthError(f"Unexpected verify response shape (top-level keys: {keys})")

        return tokens

    # ── Token management ──────────────────────────────────────────────

    def _token_expired(self, margin_seconds: int = 60) -> bool:
        """Return True if the access token is expired or about to expire."""
        return should_refresh_token(self._expires_at, time.time(), margin_seconds)

    async def ensure_valid_token(self) -> None:
        """Refresh the access token if it is expired or about to expire."""
        if not self._token_expired():
            return
        async with self._refresh_lock:
            if not self._token_expired():
                return
            await self._refresh_tokens()

    async def async_refresh_token(self, rejected_access_token: str | None = None) -> None:
        """Refresh once unless another task already replaced a rejected token."""
        async with self._refresh_lock:
            if rejected_access_token is not None and self._access_token != rejected_access_token:
                return
            await self._refresh_tokens()

    async def _refresh_tokens(self) -> None:
        """POST /v1/auth/refresh — refresh the access token."""
        if not self._refresh_token:
            raise OrionAuthError("No refresh token available")

        data = await self._request(
            "POST",
            "/v1/auth/refresh",
            with_auth=False,
            json_data={"refresh_token": self._refresh_token},
        )

        tokens = auth_tokens_from_session(
            auth_session_from_response(data, allow_top_level=True)
        )
        if tokens is None:
            keys = sorted(data) if isinstance(data, dict) else []
            raise OrionAuthError(f"Unexpected refresh response shape (top-level keys: {keys})")

        self._access_token = tokens["access_token"]
        self._refresh_token = tokens["refresh_token"]
        self._expires_at = tokens["expires_at"]

        if self._token_refresh_callback:
            self._token_refresh_callback(self._access_token, self._refresh_token, self._expires_at)

    # ── Data fetchers (all require valid token) ───────────────────────

    async def get_current_user(self) -> dict:
        """GET /v1/auth/me — current user profile.

        Returns: {"id": ..., "email": ..., "name": ..., ...}
        (unwrapped from response.response)
        """
        await self.ensure_valid_token()
        data = await self._request("GET", "/v1/auth/me")
        # Real shape: {"response": {user fields}, "success": true}
        return data.get("response", data)

    async def list_devices(self) -> list[dict]:
        """GET /v1/devices — list user's Orion devices.

        Real shape: {"response": {"devices": [...], "shared_with": [...]}, "success": true}
        Each device has: id, serial_number, name, model, type, capabilities,
        temperature_range, temperature_scale, zones, orientation, timezone,
        permissions, default_zone_id, shared_with
        """
        await self.ensure_valid_token()
        data = await self._request("GET", "/v1/devices")
        response = data.get("response", data)
        if isinstance(response, dict):
            return response.get("devices", [])
        if isinstance(response, list):
            return response
        return []

    async def get_sleep_schedules(self) -> dict:
        """GET /v1/sleep-schedules — sleep schedule configuration.

        Real shape: {"response": {"schedules": {<user_id>: [...]},
        "today_sleep_schedule": {<user_id>: {...}},
        "recommendations": {<user_id>: [...]}}, "success": true}
        """
        await self.ensure_valid_token()
        data = await self._request("GET", "/v1/sleep-schedules")
        return data.get("response", data)

    async def get_insights(self, days: int = 7) -> dict:
        """GET /v2/insights — sleep insights for date range.

        Real shape: {"user_id": "...", "data": {"YYYY-MM-DD": {date, score,
        sessions: [{session_id, zone_id, is_in_progress, sleep_summary,
        heart_rate, breath_rate, hrv, temperature, movement, ...}]}},
        "overview": {"YYYY-MM-DD": {"score": N}}}
        Note: NOT wrapped in "response" key.
        """
        await self.ensure_valid_token()
        today = date.today()
        params = {
            "from": (today - timedelta(days=days)).isoformat(),
            "to": today.isoformat(),
        }
        return await self._request("GET", "/v2/insights", params=params)

    # ── Actions ───────────────────────────────────────────────────────

    async def set_user_away(self, user_id: str, is_away: bool) -> dict:
        """POST /v1/sleep-configurations/user-away — toggle away/presence.

        is_away=True marks the user as away (presence override that also
        powers the mattress down); is_away=False marks present.

        NOTE: For direct power control, prefer `update_live_device_zones`
        (PUT /v1/devices/{id}/live) — it's the canonical power primitive
        per the OpenAPI spec. `set_user_away` is a presence/schedule
        override that happens to power the device down.

        The response returns the updated device list. When away, zones
        lose their user assignment; when present, users are re-assigned.
        """
        await self.ensure_valid_token()
        return await self._request(
            "POST",
            "/v1/sleep-configurations/user-away",
            json_data={"user_id": user_id, "is_away": is_away},
        )

    # ── Device live / metadata / action endpoints ─────────────────────

    async def get_live_device(self, device_serial: str) -> dict:
        """GET /v1/devices/{serial_number}/live — live runtime snapshot.

        Returns the per-device live state: zone on/off + temp, network,
        firmware, sensors, etc. Path uses `serial_number`, NOT the UUID.
        Response shape: {"response": {"serial_number", "zones": [{"id",
        "temp", "on"}, ...], "status": {...}, ...}, "success": true}.
        """
        await self.ensure_valid_token()
        data = await self._request("GET", f"/v1/devices/{device_serial}/live")
        return data.get("response", data)

    async def update_device(self, device_id: str, **fields: Any) -> dict:
        """PUT /v1/devices/{deviceId} — update device metadata.

        Accepts any subset of: name, orientation ("left"/"right"),
        timezone (IANA). Does NOT control power or temperature.
        """
        await self.ensure_valid_token()
        return await self._request("PUT", f"/v1/devices/{device_id}", json_data=fields)

    async def update_live_device_zones(self, device_serial: str, zones: list[dict]) -> dict:
        """PUT /v1/devices/{serial_number}/live — bulk update zone power/temp.

        This is the canonical power control endpoint. Each zone dict must
        include `id` and at least one of `on` (bool) or `temp` (float,
        Celsius for OSCT001-1).

        **Path uses `serial_number`, NOT the device UUID `id`.** Calling
        this with the UUID returns `403 "Device not found"`. Verified via
        `orion_info.py --power-on/--power-off`.

        Example:
            zones=[{"id": "zone_a", "on": True, "temp": 20.5},
                   {"id": "zone_b", "on": False}]
        """
        await self.ensure_valid_token()
        return await self._request(
            "PUT",
            f"/v1/devices/{device_serial}/live",
            json_data={"zones": zones},
        )

    async def update_live_device_setting(
        self, device_serial: str, field: str, value: Any
    ) -> dict:
        """PUT /v1/devices/{serial_number}/live — one device settings key.

        Same route and same identifier rule as `update_live_device_zones`.
        Only the body key differs.

        **Confidence: MEASURED. Both keys.**

        Verified against the live device on 2026-07-26, each key
        independently, each confirmed four ways: HTTP 200 echoing the new
        value, the device pushing it back over its own WebSocket, the
        matching Home Assistant entity changing, and the vendor's own app
        agreeing.

        `{"led_brightness": 30}` at 21:27. `{"quiet_mode": true}` at
        21:35. Both restored immediately after.

        `water_fill` is the one key on this route still APP-DERIVED. It
        is deliberately not exposed by this method.

        Both keys were found in the Orion Android v2.4.1 Hermes
        bytecode. `_updateLiveDevice` builds the path by literal
        concatenation at decompiled line 938407, the LED caller assigns
        `led_brightness` at 1083548, and the quiet mode caller assigns
        `quiet_mode` at 1083704.

        **One key per request.** All four call sites in the app construct
        a fresh empty object and assign exactly one property before
        dispatching. No code path merges keys, so multi-key on this route
        is untested by the vendor's own client. The `zones` array is the
        only thing the app ever batches.

        `device_led_brightness` is NOT an action name. It is a UI
        capability identifier that decides whether to render the slider.
        Three separate efforts fired it at `POST .../action` and were
        rejected. Do not try it a fourth time.

        Returns the full live-device object so the caller can update
        local state without a refetch, which is what the app does.
        """
        if field not in _LIVE_SETTING_FIELDS:
            raise ValueError(
                f"Unsupported live device setting {field!r}; "
                f"expected one of {sorted(_LIVE_SETTING_FIELDS)}"
            )
        await self.ensure_valid_token()
        return await self._request(
            "PUT",
            f"/v1/devices/{device_serial}/live",
            json_data={field: value},
        )

    async def update_live_device_zone(
        self,
        device_serial: str,
        zone_id: str,
        *,
        on: bool | None = None,
        temp: float | None = None,
    ) -> dict:
        """PUT /v1/devices/{serial_number}/live/zones/{zoneId} — single-zone update.

        Same identifier rule as `update_live_device_zones`: the path
        segment is the device's `serial_number`, not its UUID `id`.

        At least one of `on` or `temp` must be provided. `temp` is in the
        device's native unit (Celsius for OSCT001-1).
        """
        await self.ensure_valid_token()
        body: dict[str, Any] = {}
        if on is not None:
            body["on"] = on
        if temp is not None:
            body["temp"] = temp
        if not body:
            raise ValueError("update_live_device_zone requires `on` or `temp`")
        return await self._request(
            "PUT",
            f"/v1/devices/{device_serial}/live/zones/{zone_id}",
            json_data=body,
        )

    async def device_action(self, device_serial: str, action: str) -> dict:
        """POST /v1/devices/{serial_number}/action — perform device action.

        🔴 MEASURED 2026-07-26, against the live API: this endpoint accepts
        **only two** values, and it wants the BARE name::

            reboot        forget_wifi

        Anything else returns::

            400 {"success": false,
                 "error": "Invalid action_type. Must be \\"reboot\\" or \\"forget_wifi\\""}

        The 12-member `DeviceAllowedAction` enum and the `allowed_actions`
        array on `GET /v1/devices` are a UI capability list. They tell the
        app which controls to render, not what this endpoint accepts. The
        write routes for most of those capabilities remain undiscovered.

        `device_quiet_mode` and `device_led_brightness` are readable in the
        live snapshot but have **no discovered write path at all**.

        🔴 Takes the **serial_number**, NOT the UUID. Sending the UUID
        returns `404 {"success": false, "error": "Device not found"}` —
        the same identifier rule as the live endpoints, despite the spec
        naming this path parameter `deviceId`.

        Not a power endpoint either — power is `PUT .../live[/zones/{id}]`.
        """
        await self.ensure_valid_token()
        # The request key is `action_type`, NOT `action`. Proven by two
        # calls: `action="device_led_brightness"` and `action="reboot"`
        # returned the *same* "Invalid action_type" error — so the server
        # was never reading `action` at all. Sending `action` means this
        # endpoint has never worked.
        if action not in ("reboot", "forget_wifi"):
            raise ValueError(f"Unsupported Orion device action: {action}")
        body: dict[str, Any] = {"action_type": action}
        return await self._request("POST", f"/v1/devices/{device_serial}/action", json_data=body)

    async def activate_device(self, device_id: str, model: str) -> dict:
        """POST /v1/devices/{deviceId}/activate — pair/register a device."""
        await self.ensure_valid_token()
        return await self._request(
            "POST",
            f"/v1/devices/{device_id}/activate",
            json_data={"model": model},
        )

    async def deactivate_device(self, device_id: str) -> dict:
        """POST /v1/devices/{deviceId}/deactivate — unpair a device."""
        await self.ensure_valid_token()
        return await self._request("POST", f"/v1/devices/{device_id}/deactivate")

    async def trigger_firmware_update(self, device_id: str) -> dict:
        """POST /v1/devices/{deviceId}/update — trigger firmware update."""
        await self.ensure_valid_token()
        return await self._request("POST", f"/v1/devices/{device_id}/update")

    async def update_schedule_temperature(
        self,
        day: int,
        field: str,
        celsius: float,
        user_id: str | None = None,
    ) -> dict:
        """Update a single temperature field on a specific schedule day.

        PUT /v1/sleep-schedules with body
        {"schedules": [{"day": N, field: value}], "user_id": "..."}.

        Confidence: MEASURED.

        Partial updates work: only the specified field changes, every other
        field on that day is preserved.

        `user_id` targets ANOTHER person's schedule using this account's
        token, which is what the vendor app does (decompiled 673558, 673560).
        Measured 2026-07-26 21:43: writing the partner's `phase_1_temp` from
        the primary token moved the partner's value 16.7 -> 10 and left the
        primary's 17.5 untouched. Restored, and the full schedule blob came
        back byte-identical to a pre-write backup.

        Omit `user_id` to write the token owner's own schedule, which is the
        historical behaviour and remains correct.

        Args:
            day: Day of week (0=Monday ... 6=Sunday).
            field: One of bedtime_temp, phase_1_temp, phase_2_temp, wakeup_temp.
            celsius: Absolute Celsius value.
            user_id: Orion user id to target. None means the token owner.
        """
        if field not in _SCHEDULE_TEMPERATURE_FIELDS:
            raise ValueError(f"Unsupported Orion schedule temperature field: {field}")
        if day not in range(7):
            raise ValueError(f"Orion schedule day must be 0 through 6, got {day}")
        await self.ensure_valid_token()
        payload: dict[str, Any] = {"schedules": [{"day": day, field: celsius}]}
        if user_id:
            payload["user_id"] = user_id
        return await self._request("PUT", "/v1/sleep-schedules", json_data=payload)
