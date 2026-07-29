"""Number platform for Orion Sleep — adjustable temperature offsets."""

from __future__ import annotations

import logging

from homeassistant.components.number import NumberEntity, NumberMode, RestoreNumber
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import helpers
from .const import (
    RAPID_COOL_SLIDER_MAX,
    RAPID_COOL_SLIDER_MIN,
    RAPID_COOL_SLIDER_STEP,
)
from .coordinator import OrionDataUpdateCoordinator
from .entity import OrionBaseEntity, OrionLiveSettingMixin
from .errors import orion_call

_LOGGER = logging.getLogger(__name__)

# (key, label, icon, schedule_field)
#
# Labels are used imperatively rather than via translation_key, because the
# person's display name has to lead and translations cannot interpolate a
# runtime value.
OFFSET_NUMBER_DEFS: tuple[tuple[str, str, str, str], ...] = (
    ("bedtime_temp_offset", "Bedtime Offset", "mdi:thermometer", "bedtime_temp"),
    (
        "phase_1_temp_offset",
        "Asleep Phase 1 Offset",
        "mdi:thermometer-chevron-down",
        "phase_1_temp",
    ),
    (
        "phase_2_temp_offset",
        "Asleep Phase 2 Offset",
        "mdi:thermometer-chevron-up",
        "phase_2_temp",
    ),
    (
        "wakeup_temp_offset",
        "Wake Up Offset",
        "mdi:thermometer-alert",
        "wakeup_temp",
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Orion Sleep number entities."""
    coordinator: OrionDataUpdateCoordinator = entry.runtime_data
    entities: list[NumberEntity] = []

    # The overnight temperature curve is stored on the schedule row, so it
    # is per person and per account rather than per bed. See `time.py`.
    account_device_id = coordinator.account_device_id()
    if account_device_id:
        for user_id in coordinator.schedule_user_ids():
            for key, label, icon, field in OFFSET_NUMBER_DEFS:
                entities.append(
                    OrionTempOffsetNumber(
                        coordinator, account_device_id, key, label, icon, field, user_id
                    )
                )

    for device in coordinator.devices:
        device_id = device.get("id")
        if not device_id:
            continue
        for zone_id in coordinator.device_zone_ids(device_id):
            entities.append(
                OrionRapidCoolDurationNumber(coordinator, device_id, zone_id)
            )
        entities.append(OrionLedBrightnessNumber(coordinator, device_id))

    async_add_entities(entities)


class OrionTempOffsetNumber(OrionBaseEntity, NumberEntity):
    """One person's adjustable temperature offset for a schedule phase.

    Displays and accepts values in the app's offset scale (-10 to +10).
    Setting a value converts the offset to absolute Celsius through the
    device's non-linear lookup table and writes today's schedule row.

    Reading NEVER writes. The lookup table snaps to the nearest rung, and
    real schedules carry off-table values (an observed phase_1_temp was 16.7
    against a ladder of 16 and 17.5). A read-then-write round trip would
    silently rewrite those.

    Writes carry an explicit ``user_id``, which the API honours, so one
    account can set both people's temperatures. Verified 2026-07-26.
    """

    _attr_native_min_value = -10
    _attr_native_max_value = 10
    _attr_native_step = 1
    _attr_mode = NumberMode.SLIDER

    def __init__(
        self,
        coordinator: OrionDataUpdateCoordinator,
        device_id: str,
        key: str,
        label: str,
        icon: str,
        schedule_field: str,
        user_id: str,
    ) -> None:
        super().__init__(coordinator, device_id)
        self._user_id = user_id
        self._schedule_field = schedule_field
        self._attr_unique_id = helpers.account_schedule_unique_id(
            coordinator.config_entry.entry_id, key, user_id
        )
        self._attr_icon = icon
        self._attr_name = f"{coordinator.display_name_for_user(user_id)} {label}"

    @property
    def available(self) -> bool:
        """Only available once this person's schedule row is present."""
        return super().available and self.coordinator.has_schedule_for_user(
            self._user_id
        )

    @property
    def native_value(self) -> float | None:
        """Return the current offset value from today's schedule."""
        schedule = self.coordinator.get_today_schedule(self._user_id)
        if not schedule:
            return None
        return self._celsius_to_offset(schedule.get(self._schedule_field))

    async def async_set_native_value(self, value: float) -> None:
        """Convert the offset to Celsius and write this person's schedule."""
        celsius = self._offset_to_celsius(value)
        if celsius is None:
            raise HomeAssistantError(
                f"Cannot map offset {value} to a temperature on this device"
            )

        day = self.coordinator.schedule_day_for_user(self._user_id)
        if day is None:
            raise HomeAssistantError(
                "Orion has not reported a usable schedule for this person yet"
            )

        async with orion_call("save that schedule change"):
            await self.coordinator.api_client.update_schedule_field(
                day=day,
                field=self._schedule_field,
                value=celsius,
                user_id=self._user_id,
            )
        await self.coordinator.async_request_refresh()


class OrionLedBrightnessNumber(OrionLiveSettingMixin, OrionBaseEntity, NumberEntity):
    """Control Tower LED brightness, 0 to 100.

    Confidence: APP-DERIVED. The write route was read out of the Orion
    Android v2.4.1 bytecode and has never been observed executing. It is
    the same `PUT /v1/devices/{serial}/live` the power switch already uses
    successfully, with `led_brightness` as the body key instead of `zones`.

    The read-only `sensor.*_led_brightness` stays. A Number entity does
    not produce long-term statistics, so the sensor keeps owning history
    while this owns control.
    """

    # Fed by the live-device stream, so a fresh socket keeps it
    # available even while a polled endpoint is failing.
    _live_fed = True

    _attr_native_min_value = 0
    _attr_native_max_value = 100
    _attr_native_step = 1
    _attr_mode = NumberMode.SLIDER
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_icon = "mdi:brightness-6"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_name = "LED Brightness"
    _live_field = "led_brightness"

    def __init__(
        self, coordinator: OrionDataUpdateCoordinator, device_id: str
    ) -> None:
        super().__init__(coordinator, device_id)
        self._attr_unique_id = f"{device_id}_led_brightness"

    def _reported_value(self) -> float | None:
        raw = self.coordinator.device_led_brightness(self._device_id)
        return None if raw is None else float(raw)

    @property
    def native_value(self) -> float | None:
        """Brightness, preferring an in-flight optimistic value."""
        return self._live_display_value(self._reported_value())

    @property
    def available(self) -> bool:
        """Only available once the device has reported a brightness."""
        return super().available and self._reported_value() is not None

    async def async_set_native_value(self, value: float) -> None:
        """Queue a debounced brightness write.

        The app applies `Math.round()` before sending, so we send an int.
        """
        await self._async_write_live_setting(
            int(round(value)), self._reported_value()
        )


class OrionRapidCoolDurationNumber(OrionBaseEntity, RestoreNumber):
    """How long this side's Rapid Cool switch should run for.

    A local preference, not device state. Nothing is sent to Orion when
    this changes: the number is only read at the moment the switch is
    turned on, and the bed is told the window once, up front.

    That is why it restores rather than polls. The server does report a
    running window's `end_time`, but only while one is running, so there
    is nothing to read back the rest of the time and no way to learn what
    was last chosen except by remembering it.

    Bounds here are narrower than `orion_sleep.start_cooling`, which
    still accepts 1 to 240. The slider is the thing someone nudges half
    asleep, so it is capped where a mistake stays comfortable. The
    service is the escape hatch.
    """

    _attr_icon = "mdi:timer-sand"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_native_min_value = RAPID_COOL_SLIDER_MIN
    _attr_native_max_value = RAPID_COOL_SLIDER_MAX
    _attr_native_step = RAPID_COOL_SLIDER_STEP
    _attr_native_unit_of_measurement = UnitOfTime.MINUTES
    _attr_mode = NumberMode.SLIDER

    def __init__(
        self,
        coordinator: OrionDataUpdateCoordinator,
        device_id: str,
        zone_id: str,
    ) -> None:
        super().__init__(coordinator, device_id)
        self._zone_id = zone_id
        self._attr_unique_id = f"{device_id}_{zone_id}_rapid_cool_duration"
        self._attr_name = (
            f"{coordinator.zone_label(device_id, zone_id)} Rapid Cool Duration"
        )

    async def async_added_to_hass(self) -> None:
        """Restore the last chosen window and hand it to the coordinator."""
        await super().async_added_to_hass()
        last = await self.async_get_last_number_data()
        if last is not None and last.native_value is not None:
            self.coordinator.set_rapid_cool_duration(
                self._zone_id, last.native_value
            )

    @property
    def native_value(self) -> float:
        """The window the switch will request, in minutes."""
        return float(self.coordinator.rapid_cool_duration(self._zone_id))

    async def async_set_native_value(self, value: float) -> None:
        """Record a new window. Does not touch a running cooling session."""
        self.coordinator.set_rapid_cool_duration(self._zone_id, value)
        self.async_write_ha_state()
