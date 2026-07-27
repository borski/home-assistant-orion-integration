"""Number platform for Orion Sleep — adjustable temperature offsets."""

from __future__ import annotations

import logging

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .coordinator import OrionDataUpdateCoordinator
from .entity import OrionBaseEntity, OrionLiveSettingMixin

_LOGGER = logging.getLogger(__name__)

# (key, translation_key, icon, schedule_field)
OFFSET_NUMBER_DEFS: tuple[tuple[str, str, str, str], ...] = (
    ("bedtime_temp_offset", "bedtime_temp_offset", "mdi:thermometer", "bedtime_temp"),
    (
        "phase_1_temp_offset",
        "phase_1_temp_offset",
        "mdi:thermometer-chevron-down",
        "phase_1_temp",
    ),
    (
        "phase_2_temp_offset",
        "phase_2_temp_offset",
        "mdi:thermometer-chevron-up",
        "phase_2_temp",
    ),
    (
        "wakeup_temp_offset",
        "wakeup_temp_offset",
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

    for device in coordinator.devices:
        device_id = device.get("id")
        if not device_id:
            continue
        for key, trans_key, icon, field in OFFSET_NUMBER_DEFS:
            entities.append(
                OrionTempOffsetNumber(
                    coordinator, device_id, key, trans_key, icon, field
                )
            )
        entities.append(OrionLedBrightnessNumber(coordinator, device_id))

    async_add_entities(entities)


class OrionTempOffsetNumber(OrionBaseEntity, NumberEntity):
    """Adjustable temperature offset for a schedule phase.

    Displays and accepts values in the app's offset scale (-10 to +10).
    When the user sets a value, the offset is converted to absolute Celsius
    via the device's non-linear lookup table and sent to the API as a
    schedule update for today's day-of-week.
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
        translation_key: str,
        icon: str,
        schedule_field: str,
    ) -> None:
        super().__init__(coordinator, device_id)
        self._attr_unique_id = f"{device_id}_{key}"
        self._attr_translation_key = translation_key
        self._attr_icon = icon
        self._schedule_field = schedule_field

    @property
    def native_value(self) -> float | None:
        """Return the current offset value from today's schedule."""
        schedule = self.coordinator.get_today_schedule()
        if not schedule:
            return None
        celsius = schedule.get(self._schedule_field)
        return self._celsius_to_offset(celsius)

    async def async_set_native_value(self, value: float) -> None:
        """Set the temperature offset — converts to Celsius and updates schedule."""
        celsius = self._offset_to_celsius(value)
        if celsius is None:
            _LOGGER.error("Could not convert offset %s to Celsius", value)
            return

        schedule = self.coordinator.get_today_schedule()
        if not schedule:
            _LOGGER.error("No schedule available to update")
            return

        day = schedule.get("day")
        if day is None:
            _LOGGER.error("No day field in today's schedule")
            return

        await self.coordinator.api_client.update_schedule_temperature(
            day=day,
            field=self._schedule_field,
            celsius=celsius,
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
