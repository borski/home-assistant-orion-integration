"""DataUpdateCoordinator for Orion Sleep."""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from . import live_state, util
from .api import OrionApiClient, OrionApiError, OrionAuthError, OrionConnectionError
from .const import (
    CONF_DISPLAY_ALIASES,
    CONF_INSIGHTS_DAYS,
    CONF_PARTNER_DEVICE_SERIAL,
    CONF_SCAN_INTERVAL,
    DEFAULT_COOLING_MINUTES,
    DEFAULT_INSIGHTS_DAYS,
    DEFAULT_SCAN_INTERVAL,
    MAX_COOLING_MINUTES,
    MIN_COOLING_MINUTES,
)
from .websocket import OrionWebSocketManager

_LOGGER = logging.getLogger(__name__)

OrionConfigEntry = ConfigEntry  # ConfigEntry[OrionDataUpdateCoordinator]


class OrionDataUpdateCoordinator(DataUpdateCoordinator[dict]):
    """Fetch data from Orion API."""

    config_entry: OrionConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        config_entry: OrionConfigEntry,
        api_client: OrionApiClient,
        partner_api_client: OrionApiClient | None = None,
    ) -> None:
        interval = config_entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
        super().__init__(
            hass,
            _LOGGER,
            name="orion_sleep",
            config_entry=config_entry,
            update_interval=timedelta(seconds=interval),
        )
        self.api_client = api_client
        self.partner_api_client = partner_api_client
        self.options = dict(config_entry.options)
        self.reload_started = False
        self.devices: list[dict] = []
        self.partner_devices: list[dict] = []
        # Live snapshots keyed by device id (UUID). Populated from
        # GET /v1/devices/{serial}/live on each poll AND from
        # live_device.{snapshot,update} frames on the per-device WebSocket.
        # The WS stream supersedes the polled state between polls, giving
        # realtime zone on/temp + status updates without waiting for the
        # next REST poll. Note that biometric-derived fields like
        # status.sensors.*.status_text (on-bed classification) lag the
        # real event by ~30s–1min because the topper itself is slow to
        # decide; the WS frame arrival is not the bottleneck there.
        self.live_devices: dict[str, dict] = {}
        # Per-zone rapid-cool window, in minutes. Chosen locally and held
        # here so the switch and its duration slider agree without either
        # importing the other. Not vendor state: the server is only ever
        # told the number at the moment cooling starts.
        self.rapid_cool_minutes: dict[str, int] = {}
        self.user: dict = {}
        self.user_id: str = ""
        self.partner_user: dict = {}
        self.partner_update_ok = False
        # User-facing display overrides keyed by immutable Orion user id.
        # Aliases only ever change friendly names. Unique ids and entity ids
        # are derived from device and zone ids, so renaming is non-breaking.
        self.display_aliases: dict[str, str] = util.clean_alias_map(
            self.options.get(CONF_DISPLAY_ALIASES)
        )
        self.partner_device_serial = str(config_entry.data.get(CONF_PARTNER_DEVICE_SERIAL, ""))
        self.partner_mapping_valid = bool(self.partner_device_serial)

        # Maps device serial_number -> UUID so the WS message handler
        # (which only knows the serial) can key into live_devices.
        self._serial_to_id: dict[str, str] = {}

        # Live WebSocket manager — one connection per device serial.
        self._ws_manager: OrionWebSocketManager = OrionWebSocketManager(
            hass=hass,
            session=async_get_clientsession(hass),
            api_client=api_client,
            on_message=self._handle_ws_message,
            on_state_change=self._handle_ws_state,
        )

    async def _async_setup(self) -> None:
        """Load one-time data: user profile, device list."""
        try:
            self.user = await self.api_client.get_current_user()
            self.user_id = self.user.get("id", "")
            self.devices = util.dedupe_devices_by_id(await self.api_client.list_devices())
        except OrionAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except (OrionApiError, OrionConnectionError) as err:
            raise UpdateFailed(f"Error fetching initial data: {err}") from err

        await self._async_refresh_partner_identity()

    async def _async_refresh_partner_identity(self) -> None:
        """Refresh partner profile and device visibility when configured."""
        if self.partner_api_client is None:
            return
        try:
            self.partner_user = await self.partner_api_client.get_current_user()
            self.partner_devices = util.dedupe_devices_by_id(
                await self.partner_api_client.list_devices()
            )
            self.partner_mapping_valid = (
                len(self.devices) == 1
                and len(self.partner_devices) == 1
                and self.devices[0].get("serial_number") == self.partner_device_serial
                and self.partner_devices[0].get("serial_number") == self.partner_device_serial
            )
            if not self.partner_mapping_valid:
                _LOGGER.warning(
                    "Partner device topology changed. Partner insights are disabled "
                    "until the account is relinked"
                )
        except OrionAuthError as err:
            self.partner_update_ok = False
            self.partner_mapping_valid = False
            _LOGGER.warning(
                "Partner authentication failed. Replace it in Orion options: %s",
                err,
            )
        except (OrionApiError, OrionConnectionError) as err:
            self.partner_update_ok = False
            self.partner_mapping_valid = False
            _LOGGER.warning("Failed to initialize partner account: %s", err)

    async def _async_update_data(self) -> dict:
        """Poll mutable state."""
        try:
            await self.api_client.ensure_valid_token()
        except OrionAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except (OrionApiError, OrionConnectionError) as err:
            raise UpdateFailed(f"Error refreshing token: {err}") from err

        # Carry the previous poll's payloads forward. A failed sub-fetch
        # logs and continues, so initialising these empty would blank every
        # dependent entity for a full scan interval after one transient 500.
        # `partner_insights` already did this. `schedules` and `insights`
        # did not, which is a real bug: nine schedule entities today, and
        # thirty-two once per-person schedules land.
        data: dict = {
            "schedules": (self.data or {}).get("schedules", {}),
            "insights": (self.data or {}).get("insights", {}),
            "partner_insights": (self.data or {}).get("partner_insights", {}),
        }

        # Re-fetch devices each poll so zone/user changes surface.
        try:
            self.devices = util.dedupe_devices_by_id(await self.api_client.list_devices())
        except OrionAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except (OrionApiError, OrionConnectionError) as err:
            _LOGGER.warning("Failed to refresh device list: %s", err)

        # Rebuild the serial -> UUID map and sync the WS connections to
        # the current device list. Starting the WS manager here (rather
        # than in _async_setup) means it survives account topology
        # changes (devices added/removed) without a full reload.
        self._serial_to_id = {
            d["serial_number"]: d["id"]
            for d in self.devices
            if d.get("serial_number") and d.get("id")
        }
        self._ws_manager.sync_to_serials(list(self._serial_to_id.keys()))

        # Fetch the live snapshot for each device (zone on/temp + status).
        # GET /v1/devices does NOT include the `on` field; GET /v1/devices/
        # {serial}/live does. The /live path uses serial_number, not UUID.
        #
        # We still poll /live even with the WS in place — the WS is best-
        # effort and the periodic REST fetch guarantees the entities have
        # fresh state if the socket ever drops between polls. When the WS
        # is healthy the coordinator state is kept up to date by
        # async_set_updated_data from _handle_ws_message, so users don't
        # wait for the next poll to see their toggles reflected.
        new_live: dict[str, dict] = {}
        for device in self.devices:
            dev_id = device.get("id")
            serial = device.get("serial_number")
            if not dev_id or not serial:
                continue
            # Keep any WS-provided state until the REST fetch replaces it
            # — this avoids a flash of stale data between polls.
            if dev_id in self.live_devices and self._ws_manager.is_fresh(serial):
                new_live[dev_id] = self.live_devices[dev_id]
                continue
            try:
                new_live[dev_id] = await self.api_client.get_live_device(serial)
            except OrionAuthError as err:
                raise ConfigEntryAuthFailed(str(err)) from err
            except (OrionApiError, OrionConnectionError) as err:
                _LOGGER.warning("Failed to fetch live state for %s: %s", serial, err)
                # Preserve whatever we already had rather than blanking it.
                if dev_id in self.live_devices:
                    new_live[dev_id] = self.live_devices[dev_id]
        self.live_devices = new_live

        try:
            data["schedules"] = await self.api_client.get_sleep_schedules()
        except OrionAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except (OrionApiError, OrionConnectionError) as err:
            _LOGGER.warning("Failed to fetch sleep schedules: %s", err)

        try:
            insights_days = self.config_entry.options.get(CONF_INSIGHTS_DAYS, DEFAULT_INSIGHTS_DAYS)
            data["insights"] = await self.api_client.get_insights(days=insights_days)
        except OrionAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except (OrionApiError, OrionConnectionError) as err:
            _LOGGER.warning("Failed to fetch insights: %s", err)

        if self.partner_api_client is not None:
            await self._async_refresh_partner_identity()
            try:
                data["partner_insights"] = await self.partner_api_client.get_insights(
                    days=insights_days
                )
                self.partner_update_ok = True
            except OrionAuthError as err:
                self.partner_update_ok = False
                _LOGGER.warning(
                    "Partner authentication failed. Replace it in Orion options: %s",
                    err,
                )
            except (OrionApiError, OrionConnectionError) as err:
                self.partner_update_ok = False
                _LOGGER.warning("Failed to fetch partner insights: %s", err)

        return data

    def get_latest_session(self) -> dict | None:
        """Get the most recent sleep session from insights data."""
        return util.latest_session(util.nested_mapping(self.data, "insights", "data"))

    def get_latest_session_for_zone(self, zone_id: str) -> dict | None:
        """Get the most recent sleep session for one device zone."""
        return util.latest_session_for_zone(
            util.nested_mapping(self.data, "insights", "data"), zone_id
        )

    def get_latest_partner_session(self, device_id: str) -> dict | None:
        """Get the newest partner session for an unambiguous shared bed."""
        if not self.has_partner_for_device(device_id):
            return None
        return util.latest_session(
            util.nested_mapping(self.data, "partner_insights", "data")
        )

    def get_latest_completed_session(self) -> dict | None:
        """Newest FINISHED session for the authenticated account.

        Separate from get_latest_session because a night in progress
        already carries an end_time. See util.latest_completed_session.
        """
        return util.latest_completed_session(
            util.nested_mapping(self.data, "insights", "data")
        )

    def get_latest_completed_partner_session(self, device_id: str) -> dict | None:
        """Newest FINISHED session for the linked partner account."""
        if not self.has_partner_for_device(device_id):
            return None
        return util.latest_completed_session(
            util.nested_mapping(self.data, "partner_insights", "data")
        )

    def session_active(self, session: dict | None) -> bool:
        """Whether the supplied session is currently running."""
        return util.session_in_progress(session)

    def known_users(self) -> list[dict[str, str]]:
        """Every Orion user visible to this entry, for the alias options form.

        Account objects are walked before device zones so the fuller
        `/v1/auth/me` copies win over the sparser embedded ones.
        """
        return util.collect_known_users(self.devices, [self.user, self.partner_user])

    def display_name_for_user(self, user_id: object) -> str:
        """Friendly name for one Orion user.

        Resolution order: configured alias, then whatever the vendor calls
        them, then a stable id-derived fallback. Never raises and never
        returns an empty string, because an entity with a blank name is
        worse than one with an ugly name.

        Affects friendly names ONLY. Unique ids are built from device and
        zone ids, never from a person, so renaming cannot orphan history.
        """
        if not isinstance(user_id, str) or not user_id:
            return "Unknown"
        alias = self.display_aliases.get(user_id)
        if isinstance(alias, str) and alias.strip():
            return alias.strip()
        for record in self.known_users():
            if record["id"] == user_id and record["name"]:
                return record["name"]
        return f"User {user_id[:8]}"

    def primary_name(self) -> str:
        """Display name for the authenticated account holder."""
        if self.user_id:
            return self.display_name_for_user(self.user_id)
        return util.orion_user_label(self.user) or "You"

    def partner_name(self) -> str:
        """Display name for the linked partner account."""
        partner_id = self.partner_user.get("id")
        if isinstance(partner_id, str) and partner_id:
            return self.display_name_for_user(partner_id)
        return util.orion_user_label(self.partner_user) or "Partner"

    def has_partner_for_device(self, device_id: str) -> bool:
        """Return whether the partner account was verified for this bed."""
        if self.partner_api_client is None or not self.partner_device_serial:
            return False
        primary = next((d for d in self.devices if d.get("id") == device_id), None)
        if not primary:
            return False
        serial = primary.get("serial_number")
        if not serial:
            return False
        return len(self.devices) == 1 and serial == self.partner_device_serial

    def get_today_schedule(self, user_id: str | None = None) -> dict | None:
        """Today's sleep schedule row for one Orion user.

        ``user_id=None`` means the authenticated user, which is what every
        caller meant before per-person entities existed. The API returns
        rows for every user on the bed in a single fetch with the primary
        token, so reading a partner's row costs no extra request.
        """
        target = user_id or self.user_id
        if not target:
            return None
        row = util.nested_mapping(self.data, "schedules", "today_sleep_schedule").get(
            target
        )
        return row if isinstance(row, dict) else None

    def schedule_user_ids(self) -> list[str]:
        """Orion user ids this integration owns a schedule for.

        Derived from account identity, NOT from the schedule response.
        A failed schedule fetch leaves that response empty, and building
        the entity list from it would silently create zero entities after
        one transient error, with no recovery until a reload.

        Deliberately an allowlist. If the server ever returns a third id
        (a guest, or a stale ex-partner) we do not spawn a full entity
        family for someone we can neither name nor reliably write to.
        """
        ids: list[str] = []
        if self.user_id:
            ids.append(self.user_id)

        partner_id = self.partner_user.get("id")
        if (
            isinstance(partner_id, str)
            and partner_id
            and partner_id != self.user_id
            and self.partner_mapping_valid
        ):
            ids.append(partner_id)
        return ids

    def has_schedule_for_user(self, user_id: str) -> bool:
        """Whether today's response carries a schedule row for this user.

        Read availability only. Reads use the primary token for everyone,
        so a partner whose own token has expired still has working
        schedule entities. That asymmetry is deliberate.
        """
        return self.get_today_schedule(user_id) is not None

    def schedule_day_for_user(self, user_id: str) -> int | None:
        """Today's day-of-week index from that person's own schedule row.

        Never computed locally. Devices carry their own ``timezone``, so a
        local weekday() would be wrong near midnight for anyone whose
        Home Assistant timezone differs from the bed's.
        """
        schedule = self.get_today_schedule(user_id)
        if not schedule:
            return None
        day = schedule.get("day")
        if isinstance(day, bool) or not isinstance(day, int):
            return None
        return day if day in range(7) else None

    # ── WebSocket integration ─────────────────────────────────────────

    @callback
    def _handle_ws_message(self, serial: str, msg_type: str, payload: dict[str, Any]) -> None:
        """Merge a ``live_device.{snapshot,update}`` frame into state.

        Called from the WS receive loop. Both event types carry the same
        payload shape, so we treat them identically: the payload IS the
        new live state for the device. We also extract the today's
        schedule timeline when present, since it arrives only via WS.
        """
        if msg_type not in ("live_device.snapshot", "live_device.update"):
            # Any new event type we haven't accounted for — log once so
            # we know to update openapi.yaml / AGENTS.md.
            _LOGGER.debug(
                "Orion WS unexpected event type=%s serial=%s keys=%s",
                msg_type,
                serial,
                list(payload.keys()),
            )
            return

        dev_id = self._serial_to_id.get(serial)
        if not dev_id:
            _LOGGER.debug("Orion WS message for unknown serial %s; ignoring", serial)
            return

        # Merge in place so any fields present in the prior snapshot that
        # aren't repeated in this frame are preserved. In practice the
        # server includes the full payload every time, so this is mostly
        # a belt-and-suspenders guard.
        previous = self.live_devices.get(dev_id, {})
        merged = {**previous, **payload}
        self.live_devices[dev_id] = merged

        # Stash the timeline (today's scheduled actions) on the coordinator
        # data so sensors can read it without polling /v1/sleep-schedules
        # more aggressively. Only live_device.update carries this field.
        if msg_type == "live_device.update" and "timeline" in payload:
            data = dict(self.data or {})
            timelines = dict(data.get("ws_timelines", {}))
            timelines[dev_id] = payload.get("timeline") or []
            data["ws_timelines"] = timelines
            self.async_set_updated_data(data)
        else:
            # Snapshot — no timeline, still push so entities re-render.
            # async_set_updated_data is a no-op if called with the same
            # dict reference, so build a shallow copy.
            data = dict(self.data or {})
            self.async_set_updated_data(data)

    @callback
    def apply_live_device(self, serial: str, payload: object) -> None:
        """Merge a live-device payload returned by a write into local state.

        `PUT /v1/devices/{serial}/live` returns the full live device
        object, so a successful write does not need a follow-up GET. The
        vendor app pipes the response straight into its store and we do
        the same. Same merge semantics as the WebSocket handler.
        """
        if not isinstance(payload, dict):
            return
        dev_id = self._serial_to_id.get(serial)
        if not dev_id:
            return
        previous = self.live_devices.get(dev_id, {})
        self.live_devices[dev_id] = {**previous, **payload}
        self.async_set_updated_data(dict(self.data or {}))

    @callback
    def _handle_ws_state(self, serial: str, state: str) -> None:
        """Log WS connection-state transitions for diagnostics."""
        _LOGGER.debug("Orion WS %s -> %s", serial, state)

    def ws_state(self, serial: str) -> str:
        """Return the current WS state for a device (for diagnostics)."""
        return self._ws_manager.state(serial)

    def ws_last_message_at(self, serial: str) -> float:
        """Monotonic timestamp of the most recent WS frame, or 0."""
        return self._ws_manager.last_message_at(serial)

    async def async_shutdown(self) -> None:
        """Stop the WS manager before the coordinator is disposed."""
        await self._ws_manager.async_stop()
        await super().async_shutdown()

    # ── Live per-sensor helpers (fed by the WebSocket stream) ─────────
    #
    # ``live_device.{snapshot,update}`` payloads expose two in-topper
    # sensors at ``status.sensors.sensor1`` and ``status.sensors.sensor2``.
    # The zone->sensor mapping (sensor1 ~ zone_a vs. zone_b) has not yet
    # been verified on the wire, so we key on the raw sensor name and let
    # the user map them to sides in their automations.
    #
    # Observed payload shape (see openapi.yaml WsSensor):
    #   status, status_text, heart_rate, breath_rate, sign_of_asleep,
    #   sign_of_wake_up, timestamp, uptime, is_working, firmware_version,
    #   hardware_version
    #
    # Observed status_text values: "left_bed" (nobody on the topper) and
    # "normal" (someone on it, readings tracking). heart_rate/breath_rate
    # use 255 as a "no reading yet" sentinel and 0 when the bed is empty.

    # Sentinel value the topper reports for HR/BR when it has no reading
    # yet (e.g. the first second or two after someone sits down).
    _SENSOR_SENTINEL = 255

    def _sensor_block(self, device_id: str, sensor_name: str) -> dict[str, Any] | None:
        """Return the raw sensor payload or None if not yet seen."""
        live = self.live_devices.get(device_id)
        if not live:
            return None
        sensors = (live.get("status") or {}).get("sensors") or {}
        block = sensors.get(sensor_name)
        if not isinstance(block, dict):
            return None
        return block

    def sensor_status_text(self, device_id: str, sensor_name: str) -> str | None:
        block = self._sensor_block(device_id, sensor_name)
        if not block:
            return None
        text = block.get("status_text")
        return text if isinstance(text, str) else None

    def sensor_is_on_bed(self, device_id: str, sensor_name: str) -> bool | None:
        """Return occupancy for one topper sensor.

        ``status_text == "left_bed"`` -> empty; any other value means a
        person is on the bed. If we've never seen a frame yet, return
        None so HA shows the sensor as unknown rather than guessing.
        """
        text = self.sensor_status_text(device_id, sensor_name)
        if text is None:
            return None
        return text != "left_bed"

    def sensor_heart_rate(self, device_id: str, sensor_name: str) -> int | None:
        """Return the live HR for one sensor, mapping sentinels to None.

        * ``0`` when the bed is empty -> None (the value would mislead
          automations looking at raw BPM).
        * ``255`` is the topper's "no reading yet" sentinel -> None.
        * Any other value is returned as-is.
        """
        block = self._sensor_block(device_id, sensor_name)
        if not block:
            return None
        hr = block.get("heart_rate")
        if not isinstance(hr, (int, float)):
            return None
        hr = int(hr)
        if hr == 0 or hr == self._SENSOR_SENTINEL:
            return None
        return hr

    def sensor_breath_rate(self, device_id: str, sensor_name: str) -> int | None:
        """Return the live breath rate for one sensor, with sentinel handling."""
        block = self._sensor_block(device_id, sensor_name)
        if not block:
            return None
        br = block.get("breath_rate")
        if not isinstance(br, (int, float)):
            return None
        br = int(br)
        if br == 0 or br == self._SENSOR_SENTINEL:
            return None
        return br

    def sensor_is_working(self, device_id: str, sensor_name: str) -> bool | None:
        block = self._sensor_block(device_id, sensor_name)
        if not block:
            return None
        val = block.get("is_working")
        return bool(val) if val is not None else None

    def sensor_diagnostics(
        self, device_id: str, sensor_name: str
    ) -> dict[str, Any] | None:
        """Raw per-sensor fields the entities otherwise hide or drop.

        Exists to diagnose a measured defect: the topper has been observed
        reporting ``status_text == "normal"`` on a side that was provably
        empty, so ``sensor_is_on_bed`` returns a false positive. The
        vendor's own app never reads ``status_text`` at all (zero hits
        across the decompiled bundle), so there is no upstream logic to
        copy and the replacement has to be designed from observation.

        ``heart_rate`` and ``breath_rate`` are returned RAW. The sensor
        entities map the ``0`` and ``255`` sentinels to None, which is
        right for automations but destroys the distinction between "empty
        bed" and "no reading yet" — exactly the distinction needed here.

        ``timestamp`` and ``uptime`` are deliberately excluded. They change
        on every frame (~2s), and attributes are written to the recorder,
        so including them would cost tens of thousands of rows a day to
        answer a question ``is_working`` already answers.
        """
        block = self._sensor_block(device_id, sensor_name)
        if not block:
            return None

        out: dict[str, Any] = {}
        for key in (
            "status",
            "is_working",
            "sign_of_asleep",
            "sign_of_wake_up",
        ):
            if key in block:
                out[key] = block[key]
        for key in ("heart_rate", "breath_rate"):
            if key in block:
                out[f"raw_{key}"] = block[key]
        return out or None

    def is_user_away(self, device_id: str) -> bool | None:
        """Check whether the authenticated user is away from this device.

        The server signals away mode by removing that user from the zone
        assignment returned by ``GET /v1/devices``. Other users may remain
        assigned, so checking whether any zone has any user gives the wrong
        answer on shared beds.

        This is **distinct from device power state** (``is_device_on``).
        The mattress can be powered off while the user is still present
        (e.g. outside the schedule window), so deriving away-mode from
        the power state produces a desynced switch and makes
        ``set_user_away(is_away=False)`` fail with
        ``400 "User has no previous device to return to"`` when the user
        was already present.
        """
        for device in self.devices:
            if device.get("id") != device_id:
                continue
            return util.user_is_away(device, self.user_id)
        return None

    def is_device_on(self, device_id: str) -> bool | None:
        """Check if the device is on.

        Reads the per-zone `on` field from the live snapshot
        (`GET /v1/devices/{serial}/live`). Returns True if any zone is
        on, False if all zones report off, and None if no live snapshot
        is available yet.
        """
        live = self.live_devices.get(device_id)
        if not live:
            return None
        zones = live.get("zones", [])
        if not zones:
            return None
        saw_any = False
        any_on = False
        for zone in zones:
            if "on" in zone:
                saw_any = True
                if zone.get("on"):
                    any_on = True
        return any_on if saw_any else None

    # ── Per-zone live state ───────────────────────────────────────────
    #
    # Two different zone lists live in the same snapshot and they do NOT
    # mean the same thing:
    #
    #   live["zones"][]           -> {id, on, temp}    temp is the SETPOINT
    #   live["status"]["zones"][] -> {id, temp,
    #                                 thermal_state}   temp is MEASURED
    #
    # Mixing them up silently reports the target as the actual, which
    # looks plausible on a dashboard and is wrong. Keep them separate.

    def zone_setpoint(self, device_id: str, zone_id: str) -> float | None:
        """Target temperature (°C) for one zone, from `live.zones[].temp`."""
        return live_state.zone_setpoint(self.live_devices.get(device_id), zone_id)

    def zone_measured_temp(self, device_id: str, zone_id: str) -> float | None:
        """Measured temperature (°C), from `live.status.zones[].temp`."""
        return live_state.zone_measured_temp(self.live_devices.get(device_id), zone_id)

    def zone_is_on(self, device_id: str, zone_id: str) -> bool | None:
        """Power state for one zone, from `live.zones[].on`."""
        return live_state.zone_is_on(self.live_devices.get(device_id), zone_id)

    def zone_thermal_relief(self, device_id: str, zone_id: str) -> dict | None:
        """Raw thermal-relief block for one zone, or None."""
        return live_state.zone_thermal_relief(self.live_devices.get(device_id), zone_id)

    def thermal_relief_until(self, device_id: str, zone_id: str):
        """When active hot flash relief on this zone ends, else None.

        Returns an aware datetime. Mirrors the app's own test: relief
        counts as running only when `end_time` is a finite number still
        in the future, so a stale block the server never cleared does
        not read as an active session.
        """
        relief = self.zone_thermal_relief(device_id, zone_id)
        end_ms = live_state.thermal_relief_end_ms(relief)
        if end_ms is None:
            return None
        ends = dt_util.utc_from_timestamp(end_ms / 1000)
        return ends if ends > dt_util.utcnow() else None

    def thermal_relief_active(self, device_id: str, zone_id: str) -> bool:
        """Whether hot flash relief is currently running on this zone."""
        return self.thermal_relief_until(device_id, zone_id) is not None

    def rapid_cool_duration(self, zone_id: str) -> int:
        """Minutes of cooling to request for this zone.

        Falls back to the default whenever nothing has been chosen yet,
        which is the state on a fresh install and after a restart if the
        slider has never been touched.
        """
        return util.clamp_cooling_minutes(
            self.rapid_cool_minutes.get(zone_id),
            DEFAULT_COOLING_MINUTES,
            MIN_COOLING_MINUTES,
            MAX_COOLING_MINUTES,
        )

    def set_rapid_cool_duration(self, zone_id: str, minutes: object) -> int:
        """Record the chosen window for a zone and return what was stored."""
        value = util.clamp_cooling_minutes(
            minutes, DEFAULT_COOLING_MINUTES, MIN_COOLING_MINUTES, MAX_COOLING_MINUTES
        )
        self.rapid_cool_minutes[zone_id] = value
        return value

    def zone_thermal_state(self, device_id: str, zone_id: str) -> str | None:
        """Raw `thermal_state` for one zone.

        Only ``"standby"`` has actually been observed on the wire; heating
        and cooling values are inferred from the field name and have never
        been captured. Callers must treat any unrecognised value as unknown
        rather than guessing a direction.
        """
        return live_state.zone_thermal_state(self.live_devices.get(device_id), zone_id)

    def device_zone_ids(self, device_id: str) -> list[str]:
        """Zone ids for a device, preferring the live snapshot.

        `GET /v1/devices` also carries a `zones[]`, but it is the
        *membership* list (zone -> user) and is not guaranteed to be
        ordered or populated the same way as the live runtime list.
        """
        live = self.live_devices.get(device_id) or {}
        ids = [z.get("id") for z in live.get("zones", []) or [] if z.get("id")]
        if ids:
            return ids
        for device in self.devices:
            if device.get("id") == device_id:
                return [z.get("id") for z in device.get("zones", []) or [] if z.get("id")]
        return []

    def zone_label(self, device_id: str, zone_id: str) -> str:
        """Human label for one zone, preferring the assigned user's name.

        Routes through `display_name_for_user` so a configured alias wins
        over the vendor's own first name. Falls back to the raw user
        object, then to a title-cased zone id.

        Deliberately NOT left/right. The device carries an `orientation`
        field, but the zone -> physical side mapping has never been
        verified against a split-occupancy capture, and a confidently
        mislabelled side is worse than a neutral one.
        """
        for device in self.devices:
            if device.get("id") != device_id:
                continue
            for zone in device.get("zones") or []:
                if not isinstance(zone, dict) or zone.get("id") != zone_id:
                    continue
                user = zone.get("user")
                if isinstance(user, dict):
                    user_id = user.get("id")
                    if isinstance(user_id, str) and user_id:
                        return self.display_name_for_user(user_id)
                    label = util.orion_user_label(user)
                    if label:
                        return label
                break
            break
        return zone_id.replace("_", " ").title()

    # ── Device-level capabilities and diagnostics ─────────────────────

    def device_allowed_actions(self, device_id: str) -> set[str]:
        """UI capabilities the server exposes for this account.

        Sourced from `permissions.allowed_actions` on `GET /v1/devices`.
        These values determine which controls the app renders. They are not
        accepted verbatim by the `/action` endpoint.
        """
        for device in self.devices:
            if device.get("id") == device_id:
                perms = device.get("permissions") or {}
                return set(perms.get("allowed_actions") or [])
        return set()

    def device_quiet_mode(self, device_id: str) -> bool | None:
        """Quiet-mode state from the live snapshot."""
        live = self.live_devices.get(device_id)
        if not live or "quiet_mode" not in live:
            return None
        return bool(live.get("quiet_mode"))

    def device_led_brightness(self, device_id: str) -> int | None:
        """LED brightness (0-100) from the live snapshot."""
        return live_state.led_brightness(self.live_devices.get(device_id))

    def firmware(self, device_id: str) -> dict | None:
        """Return device firmware details from the live snapshot."""
        return live_state.firmware(self.live_devices.get(device_id))

    def device_online(self, device_id: str) -> bool | None:
        """Server-reported reachability for this device."""
        return live_state.device_online(self.live_devices.get(device_id))

    def device_timeline(self, device_id: str) -> list:
        """Today's scheduled actions, as pushed over the WebSocket.

        Only `live_device.update` carries this, never the snapshot, so it
        is empty until the first update frame arrives after a reconnect.
        """
        timelines = util.nested_mapping(self.data, "ws_timelines")
        entries = timelines.get(device_id)
        return entries if isinstance(entries, list) else []

    def next_scheduled_action(self, device_id: str, user_id: str) -> dict | None:
        """The soonest upcoming timeline entry for one person."""
        return util.next_timeline_entry(
            self.device_timeline(device_id), user_id, dt_util.utcnow()
        )

    def pending_update_available(self, device_id: str) -> bool | None:
        """Return whether a firmware update is being advertised."""
        return live_state.pending_update_available(self.live_devices.get(device_id))

    def pending_update_info(self, device_id: str) -> dict | None:
        """Return the full pending-update block from the live snapshot."""
        return live_state.pending_update_info(self.live_devices.get(device_id))

    def firmware_update_info(self, device_id: str) -> dict | None:
        """Return the in-flight firmware update block."""
        return live_state.firmware_update_info(self.live_devices.get(device_id))

    def firmware_update_in_progress(self, device_id: str) -> bool:
        """Whether the device says a firmware update is running right now."""
        info = self.firmware_update_info(device_id) or {}
        return info.get("in_progress") is True

    def network_info(self, device_id: str) -> dict | None:
        """Return device network details from the live snapshot."""
        return live_state.network_info(self.live_devices.get(device_id))

    def wifi_rssi(self, device_id: str) -> int | None:
        """Return device Wi-Fi RSSI from the live snapshot."""
        return live_state.wifi_rssi(self.live_devices.get(device_id))

    def safety_info(self, device_id: str) -> dict | None:
        """Return device safety details from the live snapshot."""
        return live_state.safety_info(self.live_devices.get(device_id))

    def has_safety_error(self, device_id: str) -> bool | None:
        """Return whether the device reports a safety problem."""
        return live_state.safety_error(self.live_devices.get(device_id))
