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
    SCHEDULE_TEMPERATURE_FIELDS,
    auth_session_from_response,
    auth_tokens_from_session,
    describe_api_error,
    safe_api_error_code,
    should_refresh_token,
    validate_schedule_write,
    validate_session_delete_reason,
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

    async def swap_user_sides(self, user_id: str) -> dict:
        """POST /v1/sleep-configurations/user-swap — swap the two bed sides.

        Reassigns which zone each sleeper occupies. The app surfaces this
        as "Swap Sides" under a user's Quick Actions, so it is scoped to a
        user even though the effect is felt by both.

        Confidence: APP-DERIVED. Route and body read from the Orion Android
        v2.4.1 bundle (path at decompiled 675421, `user_id` body key at
        675430). Never executed.

        An earlier cut of this integration guessed that `swap` was an
        `action_type` on POST /v1/devices/{serial}/action because it appears
        in `permissions.allowed_actions`. That returned 404. `allowed_actions`
        is a UI capability list, not a dispatch vocabulary. This is the real
        route.
        """
        await self.ensure_valid_token()
        return await self._request(
            "POST",
            "/v1/sleep-configurations/user-swap",
            json_data={"user_id": user_id},
        )

    async def split_user_zones(self, user_id: str) -> dict:
        """POST /v1/sleep-configurations/user-split — split the bed zones.

        The app surfaces this as "Split Zones" beside "Swap Sides". Whether
        it toggles or only ever splits is UNRESOLVED: the bundle shows a
        single call site with no read-back of a combined/split flag, and no
        field in the measured live payload obviously carries that state.

        Confidence: APP-DERIVED. Path at decompiled 675512, `user_id` body
        key at 675521. Never executed.
        """
        await self.ensure_valid_token()
        return await self._request(
            "POST",
            "/v1/sleep-configurations/user-split",
            json_data={"user_id": user_id},
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

    async def start_thermal_relief(
        self,
        device_serial: str,
        zone_ids: list[str],
        duration_minutes: int,
        relief_type: str = "cool",
    ) -> dict:
        """PUT /v1/devices/{serial_number}/live/thermal-relief — hot flash relief.

        Temporarily overrides the schedule with maximum cooling on the
        named zones, then restores the previous state when it expires.
        The app calls this Hot Flash Relief and describes it as "pause
        schedules for temporary max cooling relief".

        **Confidence: MEASURED 2026-07-27.** Started and cancelled from
        the dashboard on a live bed. The side cooled, `thermal_relief`
        appeared on that zone, and cancelling restored the previous
        setpoint with no manual correction.

        Originally read from Orion Android v2.4.1 Hermes bytecode:
        `_startThermalRelief` builds the path at decompiled line 938590
        and dispatches a PUT at 938599. The caller assembles the body at
        1281908 to 1281910: `type`, `zones`, `duration_minutes`.

        Unlike every other route in this client, this one **legitimately
        takes multiple keys in one body.** It is the only multi-key
        device-live payload the app sends.

        `zone_ids` is a list because relief is per zone, which on a
        shared bed means per person. Cooling one side leaves the other
        untouched.

        `duration_minutes` is an integer. Only 30 has been sent. The
        app clamps its own picker to a `HOT_FLASH_DURATION_OPTIONS` set
        that lives in a separate bytecode module and was not resolved,
        so the exact menu values are UNRESOLVED. 30 appears as the clamp
        seed and the default is that array's index 1. Whether the server
        enforces a range at all is unknown, so this method does not
        impose one beyond requiring a positive integer.

        `relief_type` is `"cool"` at the only observed call site and the
        only value ever sent. No heating variant was found, which is an
        absence of evidence rather than evidence of absence.

        The server tracks relief state, confirmed on the wire: after a
        successful start, `zones[].thermal_relief` carries `end_time`,
        `previous_temp`, and `previous_on`. The restore is therefore
        server-side, which is what gives this write a safe undo path.
        The app's own settings copy says the same thing: "an active
        session's countdown stays visible even when this is off."

        On failure the app raises "Failed to start thermal relief"
        (decompiled 938602), so a non-2xx here is a real rejection
        rather than a silent no-op.
        """
        if not zone_ids:
            raise ValueError("start_thermal_relief requires at least one zone id")
        if isinstance(duration_minutes, bool) or not isinstance(duration_minutes, int):
            raise ValueError(
                f"duration_minutes must be an int, got {type(duration_minutes).__name__}"
            )
        if duration_minutes <= 0:
            raise ValueError(f"duration_minutes must be positive, got {duration_minutes}")

        await self.ensure_valid_token()
        return await self._request(
            "PUT",
            f"/v1/devices/{device_serial}/live/thermal-relief",
            json_data={
                "type": relief_type,
                "zones": list(zone_ids),
                "duration_minutes": duration_minutes,
            },
        )

    async def cancel_thermal_relief(
        self, device_serial: str, zone_ids: list[str]
    ) -> dict:
        """POST /v1/devices/{serial_number}/live/thermal-relief/cancel.

        Ends relief early on the named zones. The device restores the
        `previous_temp` and `previous_on` it stashed when relief began.

        **Confidence: MEASURED 2026-07-27.** Cancelled from the
        dashboard on a live bed. The zone returned to its prior setpoint
        on its own.

        `_cancelThermalRelief` builds the path at decompiled line 938680
        and POSTs `{"zones": [...]}` at 938689 to 938692. The caller maps
        zone objects to bare ids before passing them.
        """
        if not zone_ids:
            raise ValueError("cancel_thermal_relief requires at least one zone id")

        await self.ensure_valid_token()
        return await self._request(
            "POST",
            f"/v1/devices/{device_serial}/live/thermal-relief/cancel",
            json_data={"zones": list(zone_ids)},
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

    async def activate_device(self, device_serial: str, model: str) -> dict:
        """POST /v1/devices/{serial_number}/activate — pair/register a device.

        Takes the SERIAL, not the UUID. The Orion Android v2.4.1 bytecode
        passes `arg.serial` at decompiled line 924189. This method has no
        callers and has never been executed.
        """
        await self.ensure_valid_token()
        return await self._request(
            "POST",
            f"/v1/devices/{device_serial}/activate",
            json_data={"model": model},
        )

    async def deactivate_device(self, device_serial: str) -> dict:
        """POST /v1/devices/{serial_number}/deactivate — unpair a device.

        Takes the SERIAL, not the UUID (decompiled line 1095239). No
        callers, deliberately never wired to a control.
        """
        await self.ensure_valid_token()
        return await self._request("POST", f"/v1/devices/{device_serial}/deactivate")

    async def trigger_firmware_update(self, device_serial: str) -> dict:
        """POST /v1/devices/{serial_number}/update — start a firmware update.

        Confidence: APP-DERIVED. The route and its identifier were read
        out of the Orion Android v2.4.1 bytecode at decompiled line
        942957, where the app passes `serial_number`. It has never been
        executed from here.

        Takes the SERIAL, not the UUID. Every device route that was
        assumed to take the UUID has turned out to want the serial, and
        `/action` returned 404 until that was corrected.

        This is the one write in the integration with no save-and-restore
        path. A flash cannot be undone, and it cannot be provoked on
        demand either: `pending_update.is_available` has been false for
        the entire time we have been watching, so there is nothing to
        install until Orion actually ships one.
        """
        await self.ensure_valid_token()
        return await self._request("POST", f"/v1/devices/{device_serial}/update")

    async def delete_sleep_session(self, session_id: str, reason: str) -> dict:
        """DELETE /v1/sleep-sessions/{id} — permanently remove a session.

        **This is the only irreversible call in the integration.** There
        is no undo, no restore, and no route that lists deleted sessions.
        Everything else that writes to the bed can be put back; this
        cannot. Treat a wrong `session_id` as data loss.

        Confidence: APP-DERIVED. `_deleteSleepSession` builds the path at
        decompiled line 1106381 and issues a DELETE carrying the body in
        axios' `{data: ...}` config slot. The caller at 1423492 assembles
        that body as `{"reason": ...}`, and the two reasons its sheet
        offers are `not_real_session` (1423857) and `no_longer_needed`
        (1423877).

        The reason is validated against those two rather than passed
        through. A rejected reason should stop the call, not travel to a
        destructive endpoint to see what happens.

        Sessions belong to whichever account recorded them, so this must
        be called on the client that owns the session. Deleting a
        partner's session through the primary client has not been tested
        and should not be assumed to work or to fail safely.
        """
        if not isinstance(session_id, str) or not session_id.strip():
            raise ValueError("delete_sleep_session requires a session_id")
        validated = validate_session_delete_reason(reason)

        await self.ensure_valid_token()
        return await self._request(
            "DELETE",
            f"/v1/sleep-sessions/{session_id}",
            json_data={"reason": validated},
        )

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
        if field not in SCHEDULE_TEMPERATURE_FIELDS:
            raise ValueError(f"Unsupported Orion schedule temperature field: {field}")
        return await self.update_schedule_field(day, field, celsius, user_id=user_id)

    async def update_schedule_field(
        self,
        day: int,
        field: str,
        value: Any,
        user_id: str | None = None,
    ) -> dict:
        """Permanently change one field on one schedule day.

        PUT /v1/sleep-schedules with body
        {"schedules": [{"day": N, field: value}], "user_id": "..."}.

        Confidence: MEASURED. All ten writable fields were verified
        against the live API on 2026-07-26 with a backup-and-restore probe
        targeting a weekday that was not that day, so no test could change
        how the bed behaved that night.

        This is the PERMANENT edit path. It changes the stored weekday row
        and leaves `is_override_applied` and `override_date` alone. Its
        response body carries the new value, so a caller may trust it.

        Do NOT confuse this with `PUT /v1/sleep-schedules?action=override`,
        which applies a single-day override, stamps `override_date`, and
        returns a STALE response body. That route is a different operation
        and is deliberately not implemented here. See the Verification Log
        in AGENTS.md, 2026-07-26.

        Args:
            day: Day of week (0=Monday ... 6=Sunday).
            field: A member of _SCHEDULE_WRITABLE_FIELDS.
            value: Celsius float, "HH:mm" string, or bool, per the field.
            user_id: Orion user id to target. None means the token owner.

        Raises:
            ValueError: on an unknown field, an out-of-range day, or a value
                whose type does not match the field group.
        """
        validate_schedule_write(day, field, value)
        await self.ensure_valid_token()
        payload: dict[str, Any] = {"schedules": [{"day": day, field: value}]}
        if user_id:
            payload["user_id"] = user_id
        return await self._request("PUT", "/v1/sleep-schedules", json_data=payload)

    async def override_schedule(
        self,
        day: int,
        fields: dict[str, Any],
        user_id: str | None = None,
    ) -> dict:
        """Override one day's schedule without changing the stored rows.

        PUT /v1/sleep-schedules?action=override with a FLAT body:
        {"user_id": "...", "day": N, "bedtime": "23:30", ...}.

        Confidence: MEASURED 2026-07-26. Verified with backup and restore.

        This is a DIFFERENT OPERATION from update_schedule_field, not a
        variant of it. Three differences that matter:

        1. It leaves the seven stored weekday rows untouched and instead
           stamps `is_override_applied` and `override_date` on
           `today_sleep_schedule`. The change lasts one day.
        2. Its body is FLAT and accepts MANY fields at once, unlike the
           permanent route which nests a single-key object inside a
           `schedules` array. The vendor app builds one body with up to
           ten optional keys, so multi-field is the measured behaviour
           here even though it is not on the other route.
        3. Its response body is STALE. It echoes the pre-change values.
           A caller MUST refetch rather than trusting what comes back.

        No route to CLEAR an existing override has been found. The flag
        appears to reset when the date rolls over.

        Args:
            day: Day of week to override (0=Monday ... 6=Sunday).
            fields: One or more members of SCHEDULE_WRITABLE_FIELDS.
            user_id: Orion user id to target. None means the token owner.

        Raises:
            ValueError: if `fields` is empty, or any field or value is
                invalid for its group.
        """
        if not fields:
            raise ValueError("override_schedule requires at least one field")
        for field, value in fields.items():
            validate_schedule_write(day, field, value)

        await self.ensure_valid_token()
        payload: dict[str, Any] = {"day": day, **fields}
        if user_id:
            payload["user_id"] = user_id
        return await self._request(
            "PUT",
            "/v1/sleep-schedules",
            json_data=payload,
            params={"action": "override"},
        )
