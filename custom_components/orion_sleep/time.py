"""Time platform for Orion Sleep — per-person bedtime and wake up time.

These were sensors until the write route was measured on 2026-07-26.
A `time` entity is the honest shape now: the value is a wall-clock time
with no date, and it is settable.

Deliberately NOT `SensorDeviceClass.TIMESTAMP`, which needs a timezone
aware datetime. The API speaks `HH:mm` wall clock, and the bed carries its
own timezone, so inventing a date here would be wrong twice over.
"""

from __future__ import annotations

import logging
from datetime import time as dt_time

import voluptuous as vol
from homeassistant.components.time import TimeEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import entity_platform
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import util
from .coordinator import OrionDataUpdateCoordinator
from .entity import OrionBaseEntity

_LOGGER = logging.getLogger(__name__)

# (schedule field, entity key, label, icon)
#
# The entity key differs from the schedule field for wake up because the
# API returns `wakeup` while the entity has always been called
# `wakeup_time`. Keeping them distinct avoids a rename later.
_SCHEDULE_TIMES: tuple[tuple[str, str, str, str], ...] = (
    ("bedtime", "bedtime", "Bedtime", "mdi:bed-clock"),
    ("wakeup", "wakeup_time", "Wake Up Time", "mdi:alarm"),
)

SERVICE_OVERRIDE_SCHEDULE = "override_schedule"

# Service field -> schedule field. The four temperatures are exposed on the
# app's -10..+10 offset scale rather than raw Celsius, so the service reads
# the same way the sliders do. Conversion happens in the handler.
_OVERRIDE_OFFSETS: dict[str, str] = {
    "bedtime_temp_offset": "bedtime_temp",
    "phase_1_temp_offset": "phase_1_temp",
    "phase_2_temp_offset": "phase_2_temp",
    "wakeup_temp_offset": "wakeup_temp",
}
_OVERRIDE_FLAGS: tuple[str, ...] = (
    "bedtime_is_active",
    "wakeup_is_active",
    "auto_turn_off",
    "is_smart_temperature_active",
)

_OVERRIDE_SCHEMA = {
    vol.Optional("bedtime"): cv.time,
    vol.Optional("wakeup"): cv.time,
    **{
        vol.Optional(name): vol.All(vol.Coerce(int), vol.Range(min=-10, max=10))
        for name in _OVERRIDE_OFFSETS
    },
    **{vol.Optional(name): cv.boolean for name in _OVERRIDE_FLAGS},
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Orion Sleep time entities."""
    coordinator: OrionDataUpdateCoordinator = entry.runtime_data
    entities: list[TimeEntity] = []

    for device in coordinator.devices:
        device_id = device.get("id")
        if not device_id:
            continue
        for user_id in coordinator.schedule_user_ids():
            for field, key, label, icon in _SCHEDULE_TIMES:
                entities.append(
                    OrionScheduleTime(
                        coordinator, device_id, user_id, field, key, label, icon
                    )
                )

    async_add_entities(entities)

    # Registered on this platform so the service inherits entity targeting.
    # Pointing it at somebody's Bedtime entity is how the caller says whose
    # schedule to override, which beats asking for a raw Orion user id.
    platform = entity_platform.async_get_current_platform()
    platform.async_register_entity_service(
        SERVICE_OVERRIDE_SCHEDULE,
        _OVERRIDE_SCHEMA,
        "async_override_schedule",
    )


class OrionScheduleTime(OrionBaseEntity, TimeEntity):
    """One person's bedtime or wake up time.

    Writes carry an explicit ``user_id``, which the API honours, so one
    account sets both people's schedules. Measured 2026-07-26.
    """

    def __init__(
        self,
        coordinator: OrionDataUpdateCoordinator,
        device_id: str,
        user_id: str,
        field: str,
        key: str,
        label: str,
        icon: str,
    ) -> None:
        super().__init__(coordinator, device_id)
        self._user_id = user_id
        self._field = field
        self._attr_unique_id = util.schedule_unique_id(device_id, key, user_id)
        self._attr_icon = icon
        self._attr_name = f"{coordinator.display_name_for_user(user_id)} {label}"

    @property
    def available(self) -> bool:
        return super().available and self.coordinator.has_schedule_for_user(
            self._user_id
        )

    @property
    def native_value(self) -> dt_time | None:
        """Parse the API's `HH:mm` into a time, or None if malformed."""
        schedule = self.coordinator.get_today_schedule(self._user_id)
        if not schedule:
            return None
        return util.parse_schedule_time(schedule.get(self._field))

    async def async_set_value(self, value: dt_time) -> None:
        """Write this person's schedule for today's day-of-week."""
        day = self.coordinator.schedule_day_for_user(self._user_id)
        if day is None:
            raise HomeAssistantError(
                "Orion has not reported a usable schedule for this person yet"
            )
        # The API takes HH:mm only. Seconds are dropped rather than rounded,
        # because a schedule that fires at :00 is what the vendor supports
        # and silently shifting someone's bedtime by 30s would be worse.
        await self.coordinator.api_client.update_schedule_field(
            day=day,
            field=self._field,
            value=f"{value.hour:02d}:{value.minute:02d}",
            user_id=self._user_id,
        )
        await self.coordinator.async_request_refresh()

    async def async_override_schedule(self, **kwargs) -> None:
        """Override this person's schedule for today only.

        A different operation from setting the value on this entity. That
        edits the stored weekday row permanently. This leaves the stored
        rows alone and applies a one-day override, which is what "warmer
        just for tonight" actually means.

        The override route takes a flat multi-field body, unlike the
        permanent route which takes one field at a time, so every option
        given here goes out in a single request the way the vendor app
        sends it.
        """
        day = self.coordinator.schedule_day_for_user(self._user_id)
        if day is None:
            raise HomeAssistantError(
                "Orion has not reported a usable schedule for this person yet"
            )

        fields: dict = {}
        for name in ("bedtime", "wakeup"):
            value = kwargs.get(name)
            if value is not None:
                fields[name] = f"{value.hour:02d}:{value.minute:02d}"
        for name, schedule_field in _OVERRIDE_OFFSETS.items():
            offset = kwargs.get(name)
            if offset is None:
                continue
            celsius = self._offset_to_celsius(float(offset))
            if celsius is None:
                raise HomeAssistantError(f"Cannot map offset {offset} to a temperature")
            fields[schedule_field] = celsius
        for name in _OVERRIDE_FLAGS:
            value = kwargs.get(name)
            if value is not None:
                fields[name] = value

        if not fields:
            raise HomeAssistantError("Give at least one value to override")

        await self.coordinator.api_client.override_schedule(
            day=day, fields=fields, user_id=self._user_id
        )
        # The override response echoes pre-change values, so refetch rather
        # than trusting it. Measured 2026-07-26.
        await self.coordinator.async_request_refresh()
