"""Switch platform for Orion Sleep."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import helpers
from .coordinator import OrionDataUpdateCoordinator
from .entity import OrionBaseEntity, OrionLiveSettingMixin

# Per-person schedule booleans. (schedule field, label, icon)
#
# All four were measured against the live API on 2026-07-26 by writing to a
# weekday row that was not that day, so nothing about that night could
# change. See the Verification Log in AGENTS.md.
_SCHEDULE_FLAGS: tuple[tuple[str, str, str], ...] = (
    ("bedtime_is_active", "Bedtime Enabled", "mdi:bed-clock"),
    ("wakeup_is_active", "Wake Up Enabled", "mdi:alarm"),
    ("auto_turn_off", "Automatic Turn Off", "mdi:power-sleep"),
    ("is_smart_temperature_active", "Smart Temperature", "mdi:auto-fix"),
)

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Orion Sleep switch entities."""
    coordinator: OrionDataUpdateCoordinator = entry.runtime_data
    entities: list[SwitchEntity] = []

    for device in coordinator.devices:
        device_id = device.get("id")
        if not device_id:
            continue
        entities.append(OrionPowerSwitch(coordinator, device_id))
        entities.append(OrionQuietModeSwitch(coordinator, device_id))
        for zone_id in coordinator.device_zone_ids(device_id):
            entities.append(OrionRapidCoolSwitch(coordinator, device_id, zone_id))
        for user_id in coordinator.schedule_user_ids():
            for field, label, icon in _SCHEDULE_FLAGS:
                entities.append(
                    OrionScheduleFlagSwitch(
                        coordinator, device_id, user_id, field, label, icon
                    )
                )
        if len(coordinator.devices) == 1:
            entities.append(OrionAwayModeSwitch(coordinator, device_id))

    if len(coordinator.devices) > 1:
        _LOGGER.warning(
            "Away Mode is unavailable because the Orion account has multiple devices"
        )

    async_add_entities(entities)


class OrionPowerSwitch(OrionBaseEntity, SwitchEntity):
    """Switch to turn the Orion mattress topper on/off.

    Uses the canonical power primitive `PUT /v1/devices/{serial}/live` to
    set all zones on/off in one call. This is distinct from Away Mode,
    which is a presence/schedule override.

    State is derived from each zone's `on` field in the live snapshot.
    """

    _attr_translation_key = "power"
    _attr_icon = "mdi:power"

    def __init__(
        self,
        coordinator: OrionDataUpdateCoordinator,
        device_id: str,
    ) -> None:
        super().__init__(coordinator, device_id)
        self._attr_unique_id = f"{device_id}_power"

    @property
    def is_on(self) -> bool | None:
        """Return True if the device is on."""
        return self.coordinator.is_device_on(self._device_id)

    def _device(self) -> dict | None:
        """Return the device dict for this entity, or None."""
        for device in self.coordinator.devices:
            if device.get("id") == self._device_id:
                return device
        return None

    async def _set_power(self, on: bool) -> None:
        """Send on=<bool> to every zone via PUT /v1/devices/{serial}/live."""
        device = self._device()
        if not device:
            raise HomeAssistantError("Orion device is unavailable")
        # The /live endpoints use serial_number in the path, NOT the UUID.
        serial = device.get("serial_number")
        zone_ids = [z.get("id") for z in device.get("zones", []) if z.get("id")]
        if not serial or not zone_ids:
            raise HomeAssistantError("Orion device has no controllable zones")
        await self.coordinator.api_client.update_live_device_zones(
            device_serial=serial,
            zones=[{"id": zid, "on": on} for zid in zone_ids],
        )
        await self.coordinator.async_request_refresh()

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn on the device via PUT /v1/devices/{serial}/live."""
        await self._set_power(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn off the device via PUT /v1/devices/{serial}/live."""
        await self._set_power(False)


class OrionAwayModeSwitch(OrionBaseEntity, SwitchEntity):
    """Switch to control the user's away mode.

    When away mode is ON, the authenticated user is marked as away and
    their zone assignment is removed.

    When away mode is OFF, the user is marked as present and the device
    resumes normal operation.

    Away mode is **distinct from the Power switch**. The mattress can be
    powered off without the user being marked away (e.g. the schedule's
    turn_off action just ran), so this switch reads the authoritative
    signal from ``zones[*].user`` on ``/v1/devices``. The authenticated
    user being absent from every zone means away. Deriving away from
    device power, or from another user's assignment, would desync the switch and cause
    ``POST /v1/sleep-configurations/user-away`` to return
    ``400 "User has no previous device to return to"`` when a click
    results in a no-op toggle.
    """

    _attr_translation_key = "away_mode"
    _attr_icon = "mdi:home-export-outline"

    def __init__(
        self,
        coordinator: OrionDataUpdateCoordinator,
        device_id: str,
    ) -> None:
        super().__init__(coordinator, device_id)
        self._attr_unique_id = f"{device_id}_away_mode"

    @property
    def is_on(self) -> bool | None:
        """Return True if the user is currently marked away."""
        return self.coordinator.is_user_away(self._device_id)

    @property
    def available(self) -> bool:
        """Available only while the account has exactly one Orion device."""
        return super().available and len(self.coordinator.devices) == 1

    async def _set_away(self, is_away: bool) -> None:
        """Call set_user_away, tolerating the no-op 400 from the server.

        The Orion API returns ``400 "User has no previous device to
        return to"`` when called with ``is_away=False`` on a user who's
        already present. Swallow that specific error so a redundant
        toggle (e.g. after an automation re-asserts state) isn't a hard
        failure in the HA UI.
        """
        from orion_sleep_api import OrionApiError

        if len(self.coordinator.devices) != 1:
            raise HomeAssistantError(
                "Away Mode is unavailable for Orion accounts with multiple devices"
            )
        if not self.coordinator.user_id:
            raise HomeAssistantError("Orion user is unavailable")

        try:
            await self.coordinator.api_client.set_user_away(
                user_id=self.coordinator.user_id,
                is_away=is_away,
            )
        except OrionApiError as err:
            if err.error_code == "user_already_present":
                _LOGGER.debug(
                    "set_user_away(%s) was a no-op; server state already matched",
                    is_away,
                )
            else:
                raise
        await self.coordinator.async_request_refresh()

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Enable away mode (mark user as away, device stops)."""
        await self._set_away(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Disable away mode (mark user as present, device resumes)."""
        await self._set_away(False)


class OrionQuietModeSwitch(OrionLiveSettingMixin, OrionBaseEntity, SwitchEntity):
    """Control Tower quiet mode.

    Confidence: APP-DERIVED. Same `PUT /v1/devices/{serial}/live` route the
    power switch already uses successfully, with `quiet_mode` as the body
    key. Read out of the Orion Android v2.4.1 bytecode at decompiled line
    1083704, never observed executing.

    Supersedes the read-only `binary_sensor.*_quiet_mode`, which the
    codebase created because no write path was known at the time.
    """

    _attr_icon = "mdi:volume-off"
    _attr_name = "Quiet Mode"
    _attr_entity_category = EntityCategory.CONFIG
    _live_field = "quiet_mode"

    def __init__(
        self, coordinator: OrionDataUpdateCoordinator, device_id: str
    ) -> None:
        super().__init__(coordinator, device_id)
        self._attr_unique_id = f"{device_id}_quiet_mode"

    def _reported_state(self) -> bool | None:
        return self.coordinator.device_quiet_mode(self._device_id)

    @property
    def is_on(self) -> bool | None:
        """Quiet mode state, preferring an in-flight optimistic value."""
        return self._live_display_value(self._reported_state())

    @property
    def available(self) -> bool:
        """Only available once the device has reported a quiet mode state."""
        return super().available and self._reported_state() is not None

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Enable quiet mode."""
        await self._async_write_live_setting(True, self._reported_state())

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Disable quiet mode."""
        await self._async_write_live_setting(False, self._reported_state())


class OrionRapidCoolSwitch(OrionBaseEntity, SwitchEntity):
    """One person's rapid cooling, as a toggle.

    The app calls this Hot Flash Relief: it pauses the schedule and drives
    that side to maximum cooling for a fixed window, then restores the
    previous setpoint on its own.

    A switch rather than a pair of buttons because the bed tracks this
    server-side. `zones[].thermal_relief` carries `end_time`,
    `previous_temp` and `previous_on`, and the app's own settings copy
    says an active countdown stays visible even when the feature is
    hidden. So there is real state to reflect, and turning the switch off
    is a genuine cancel rather than a second fire-and-forget button.

    Confidence: MEASURED 2026-07-27. Toggled on and back off against a
    live bed. The side cooled, `thermal_relief` populated, and the
    cancel restored the previous setpoint without intervention.
    Originally read out of the Orion Android v2.4.1 bytecode, start at
    decompiled line 938590 and cancel at 938680.
    """

    _attr_icon = "mdi:snowflake"

    def __init__(
        self,
        coordinator: OrionDataUpdateCoordinator,
        device_id: str,
        zone_id: str,
    ) -> None:
        super().__init__(coordinator, device_id)
        self._zone_id = zone_id
        self._attr_unique_id = f"{device_id}_{zone_id}_rapid_cool"
        self._attr_name = (
            f"{coordinator.zone_label(device_id, zone_id)} Rapid Cool"
        )

    @property
    def is_on(self) -> bool:
        """True while the server reports an unexpired cooling window."""
        return self.coordinator.thermal_relief_active(self._device_id, self._zone_id)

    @property
    def extra_state_attributes(self) -> dict | None:
        """Expose the window it will request, when it ends, what it restores.

        `duration_minutes` is reported whether or not cooling is running,
        because it is the answer to "what happens if I flip this", which
        is worth knowing before flipping it rather than after.
        """
        attrs: dict[str, Any] = {
            "duration_minutes": self.coordinator.rapid_cool_duration(self._zone_id)
        }
        relief = self.coordinator.zone_thermal_relief(self._device_id, self._zone_id)
        if not relief:
            return attrs

        ends = self.coordinator.thermal_relief_until(self._device_id, self._zone_id)
        if ends is not None:
            attrs["ends_at"] = ends.isoformat()
        for key in ("type", "previous_temp", "previous_on"):
            value = relief.get(key)
            if value is not None:
                attrs[key] = value
        return attrs

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Start cooling this side for the window chosen on its slider."""
        minutes = self.coordinator.rapid_cool_duration(self._zone_id)
        try:
            await self.coordinator.api_client.start_thermal_relief(
                self._serial(), [self._zone_id], minutes
            )
        except ValueError as err:
            raise HomeAssistantError(str(err)) from err
        await self.coordinator.async_request_refresh()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Cancel cooling on this side and restore the previous setpoint."""
        try:
            await self.coordinator.api_client.cancel_thermal_relief(
                self._serial(), [self._zone_id]
            )
        except ValueError as err:
            raise HomeAssistantError(str(err)) from err
        await self.coordinator.async_request_refresh()


class OrionScheduleFlagSwitch(OrionBaseEntity, SwitchEntity):
    """One person's schedule boolean, as a real control.

    Writes go through the authenticated account carrying an explicit
    ``user_id``, which the API honours, so one login sets both people's
    schedules. All four fields were measured on 2026-07-26.

    Always writes today's row, read from that person's own schedule rather
    than computed locally. Devices carry their own timezone, so a local
    weekday() would be wrong near midnight for anyone whose Home Assistant
    timezone differs from the bed's.
    """

    def __init__(
        self,
        coordinator: OrionDataUpdateCoordinator,
        device_id: str,
        user_id: str,
        field: str,
        label: str,
        icon: str,
    ) -> None:
        super().__init__(coordinator, device_id)
        self._user_id = user_id
        self._field = field
        self._attr_unique_id = helpers.schedule_unique_id(device_id, field, user_id)
        self._attr_icon = icon
        self._attr_name = f"{coordinator.display_name_for_user(user_id)} {label}"

    @property
    def available(self) -> bool:
        return super().available and self.coordinator.has_schedule_for_user(
            self._user_id
        )

    @property
    def is_on(self) -> bool | None:
        schedule = self.coordinator.get_today_schedule(self._user_id)
        if not schedule:
            return None
        value = schedule.get(self._field)
        return value if isinstance(value, bool) else None

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._write(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._write(False)

    async def _write(self, value: bool) -> None:
        day = self.coordinator.schedule_day_for_user(self._user_id)
        if day is None:
            raise HomeAssistantError(
                "Orion has not reported a usable schedule for this person yet"
            )
        await self.coordinator.api_client.update_schedule_field(
            day=day,
            field=self._field,
            value=value,
            user_id=self._user_id,
        )
        await self.coordinator.async_request_refresh()
