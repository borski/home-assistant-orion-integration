"""Binary sensor platform for Orion Sleep."""

from __future__ import annotations

import logging

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import util
from .coordinator import OrionDataUpdateCoordinator
from .entity import OrionBaseEntity

_LOGGER = logging.getLogger(__name__)


# Sensors exposed on every ``live_device.{snapshot,update}`` payload.
# Mapping to zone_a/zone_b isn't verified yet; we expose the raw names
# the server uses so the user can build their own side mapping.
_TOPPER_SENSORS: tuple[str, ...] = ("sensor1", "sensor2")



async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Orion Sleep binary sensor entities."""
    coordinator: OrionDataUpdateCoordinator = entry.runtime_data
    entities: list[BinarySensorEntity] = []

    for device in coordinator.devices:
        device_id = device.get("id")
        if not device_id:
            continue
        entities.append(OrionSessionActiveBinarySensor(coordinator, device_id))
        if coordinator.has_partner_for_device(device_id):
            entities.append(OrionPartnerSessionActiveBinarySensor(coordinator, device_id))
        for sensor_name in _TOPPER_SENSORS:
            entities.append(OrionSensorOnBedBinarySensor(coordinator, device_id, sensor_name))
        for zone_id in coordinator.device_zone_ids(device_id):
            entities.append(
                OrionThermalReliefBinarySensor(coordinator, device_id, zone_id)
            )
        entities.append(OrionFirmwareUpdateBinarySensor(coordinator, device_id))
        entities.append(OrionDeviceOnlineBinarySensor(coordinator, device_id))
        entities.append(OrionSafetyProblemBinarySensor(coordinator, device_id))
        for user_id in coordinator.schedule_user_ids():
            entities.append(
                OrionScheduleOverrideBinarySensor(coordinator, device_id, user_id)
            )

    async_add_entities(entities)


class OrionSessionActiveBinarySensor(OrionBaseEntity, BinarySensorEntity):
    """Binary sensor indicating if a sleep session is active.

    Determined by checking if the latest session in insights has
    is_in_progress == True.

    We intentionally do NOT set a device_class here. Using
    BinarySensorDeviceClass.RUNNING shows "Running / Not running" which
    is confusing for sleep tracking. Instead we rely on translation_key
    to provide "Asleep / Not asleep" state labels.
    """

    # translation_key is retained even though _attr_name overrides the
    # displayed name: it is still what resolves the "Asleep / Not asleep"
    # STATE labels. Dropping it would silently fall back to On / Off.
    _attr_translation_key = "sleep_session_active"
    _attr_icon = "mdi:bed"

    def __init__(
        self,
        coordinator: OrionDataUpdateCoordinator,
        device_id: str,
    ) -> None:
        super().__init__(coordinator, device_id)
        self._attr_unique_id = f"{device_id}_session_active"
        # Named for the account holder, matching the insight sensors.
        # This tracks one person's session, not the bed as a whole.
        self._attr_name = f"{coordinator.primary_name()} Sleep Session"

    def _session(self) -> dict | None:
        return self.coordinator.get_latest_session()

    @property
    def is_on(self) -> bool | None:
        """Return True if a sleep session is currently active."""
        return self.coordinator.session_active(self._session())


class OrionPartnerSessionActiveBinarySensor(OrionSessionActiveBinarySensor):
    """Whether the linked partner account is mid-session."""

    def __init__(
        self,
        coordinator: OrionDataUpdateCoordinator,
        device_id: str,
    ) -> None:
        super().__init__(coordinator, device_id)
        self._attr_unique_id = f"{device_id}_partner_session_active"
        self._attr_name = f"{coordinator.partner_name()} Sleep Session"

    def _session(self) -> dict | None:
        return self.coordinator.get_latest_partner_session(self._device_id)

    @property
    def available(self) -> bool:
        return (
            super().available
            and self.coordinator.has_partner_for_device(self._device_id)
            and self.coordinator.partner_mapping_valid
        )


class OrionSensorOnBedBinarySensor(OrionBaseEntity, BinarySensorEntity):
    """Per-topper-sensor occupancy detector.

    Drives off the WebSocket ``status.sensors.<sensor_name>.status_text``
    field: ``"left_bed"`` means empty, any other value (observed:
    ``"normal"``) means the sensor detects a person.

    The WS frames themselves arrive in realtime, but the topper's own
    classification of on-bed vs. left-bed is slow: observed latency is
    roughly 30 s to 1 minute after sitting down or getting up before
    ``status_text`` transitions. Heart-rate/breath-rate updates are
    faster since those come straight off the sensor.

    The two sensors (``sensor1`` / ``sensor2``) correspond to the two
    measurement pads in the topper. Their mapping to ``zone_a`` /
    ``zone_b`` has not been verified against a split-occupancy capture,
    so entities are named per sensor rather than per side.
    """

    _attr_device_class = BinarySensorDeviceClass.OCCUPANCY
    _attr_icon = "mdi:bed-outline"

    def __init__(
        self,
        coordinator: OrionDataUpdateCoordinator,
        device_id: str,
        sensor_name: str,
    ) -> None:
        super().__init__(coordinator, device_id)
        self._sensor_name = sensor_name
        self._attr_translation_key = f"{sensor_name}_on_bed"
        self._attr_unique_id = f"{device_id}_{sensor_name}_on_bed"

    @property
    def is_on(self) -> bool | None:
        return self.coordinator.sensor_is_on_bed(self._device_id, self._sensor_name)

    @property
    def available(self) -> bool:
        # Report available whenever we have a live payload at all,
        # even if the individual sensor hasn't reported yet.
        return self.coordinator.sensor_status_text(self._device_id, self._sensor_name) is not None


class OrionThermalReliefBinarySensor(OrionBaseEntity, BinarySensorEntity):
    """Whether hot flash relief is currently cooling one side of the bed.

    Relief is per zone, so on a shared bed this is per person. One side
    can be cooling while the other holds its schedule.

    No `device_class`. `RUNNING` renders "Running / Not running", and
    `COLD` inverts the sense (it means "too cold"). A plain On / Off with
    a clear name reads better than either.
    """

    def __init__(
        self,
        coordinator: OrionDataUpdateCoordinator,
        device_id: str,
        zone_id: str,
    ) -> None:
        super().__init__(coordinator, device_id)
        self._zone_id = zone_id
        self._attr_unique_id = f"{device_id}_{zone_id}_thermal_relief"
        self._attr_icon = "mdi:snowflake-thermometer"
        self._attr_name = f"{coordinator.zone_label(device_id, zone_id)} Cooling"

    @property
    def is_on(self) -> bool:
        """True while relief is running and has not expired."""
        return self.coordinator.thermal_relief_active(self._device_id, self._zone_id)

    @property
    def extra_state_attributes(self) -> dict:
        """Expose when relief ends and what the bed will restore.

        `previous_temp` and `previous_on` are the schedule state the
        device stashed when relief began, so they answer "what happens
        when this finishes" without needing a second lookup.
        """
        ends = self.coordinator.thermal_relief_until(self._device_id, self._zone_id)
        if ends is None:
            return {}

        attrs: dict = {"ends_at": ends.isoformat()}
        relief = self.coordinator.zone_thermal_relief(self._device_id, self._zone_id)
        if isinstance(relief, dict):
            for key in ("type", "previous_temp", "previous_on"):
                if key in relief:
                    attrs[key] = relief[key]
        return attrs


class OrionFirmwareUpdateBinarySensor(OrionBaseEntity, BinarySensorEntity):
    """Whether the Control Tower is advertising a firmware update.

    Deliberately a binary_sensor and NOT an `update` entity. An `update`
    entity wants both `installed_version` and `latest_version`. Installed
    is available at `status.firmware.cb`, but nothing in the live payload
    carries the AVAILABLE version, so `latest_version` would have to be
    fabricated. Triggering the update is also unmodelled:
    `POST /v1/devices/{serial}/update` exists in the vendor app but has
    never been executed from here.
    """

    _attr_translation_key = "firmware_update_available"
    _attr_device_class = BinarySensorDeviceClass.UPDATE
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: OrionDataUpdateCoordinator, device_id: str) -> None:
        super().__init__(coordinator, device_id)
        self._attr_unique_id = f"{device_id}_firmware_update_available"

    @property
    def available(self) -> bool:
        return (
            super().available
            and self.coordinator.pending_update_available(self._device_id) is not None
        )

    @property
    def is_on(self) -> bool | None:
        return self.coordinator.pending_update_available(self._device_id)

    @property
    def extra_state_attributes(self) -> dict | None:
        pending = self.coordinator.pending_update_info(self._device_id)
        if not pending:
            return None
        return {
            key: value
            for key, value in pending.items()
            if key != "is_available" and value is not None
        } or None


class OrionScheduleOverrideBinarySensor(OrionBaseEntity, BinarySensorEntity):
    """Whether a single-day override is currently applied for this person.

    Distinct from the schedule itself. ``PUT /v1/sleep-schedules?action=override``
    changes today's values without touching the seven weekday rows, and
    stamps ``is_override_applied`` plus ``override_date``. Diagnostic
    because it explains a surprising bedtime rather than being something
    to act on.
    """

    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self,
        coordinator: OrionDataUpdateCoordinator,
        device_id: str,
        user_id: str,
    ) -> None:
        super().__init__(coordinator, device_id)
        self._user_id = user_id
        self._attr_unique_id = util.schedule_unique_id(
            device_id, "is_override_applied", user_id
        )
        self._attr_icon = "mdi:calendar-edit"
        self._attr_name = (
            f"{coordinator.display_name_for_user(user_id)} Schedule Override"
        )

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
        value = schedule.get("is_override_applied")
        return value if isinstance(value, bool) else None

    @property
    def extra_state_attributes(self) -> dict | None:
        schedule = self.coordinator.get_today_schedule(self._user_id)
        if not schedule:
            return None
        attrs = {
            "override_date": schedule.get("override_date"),
            "override_available": schedule.get("is_override_available"),
        }
        return {k: v for k, v in attrs.items() if v is not None} or None


class OrionSafetyProblemBinarySensor(OrionBaseEntity, BinarySensorEntity):
    """Binary problem entity for Control Tower safety errors."""

    _attr_translation_key = "safety_problem"
    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: OrionDataUpdateCoordinator, device_id: str) -> None:
        super().__init__(coordinator, device_id)
        self._attr_unique_id = f"{device_id}_safety_problem"

    @property
    def is_on(self) -> bool | None:
        return self.coordinator.has_safety_error(self._device_id)

    @property
    def extra_state_attributes(self) -> dict | None:
        safety = self.coordinator.safety_info(self._device_id)
        if not safety:
            return None
        attrs = {
            "error_codes": safety.get("error_codes"),
            "error_descriptions": safety.get("error_descriptions"),
        }
        return {key: value for key, value in attrs.items() if value} or None


class OrionDeviceOnlineBinarySensor(OrionBaseEntity, BinarySensorEntity):
    """Whether the server considers the topper reachable.

    Distinct from the Live Connection sensor, which reports OUR WebSocket
    link to Orion. This is the server's own view of the hardware, so the
    two can legitimately disagree: our socket can drop while the bed is
    fine, and the bed can drop while our socket is healthy.
    """

    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_translation_key = "device_online"

    def __init__(self, coordinator: OrionDataUpdateCoordinator, device_id: str) -> None:
        super().__init__(coordinator, device_id)
        self._attr_unique_id = f"{device_id}_device_online"

    @property
    def available(self) -> bool:
        return super().available and self.coordinator.device_online(self._device_id) is not None

    @property
    def is_on(self) -> bool | None:
        return self.coordinator.device_online(self._device_id)
