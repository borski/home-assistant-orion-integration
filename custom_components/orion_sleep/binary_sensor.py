"""Binary sensor platform for Orion Sleep."""

from __future__ import annotations

import logging
import time
from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import helpers
from .const import OCCUPANCY_HOLD_SECONDS
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
        # CONFIGURED, not verified. Same split, same reasoning, as the
        # partner insight sensors in `sensor.py`. Gating CONSTRUCTION on the
        # trust predicate meant one failed partner fetch at cold start
        # deleted this entity from Home Assistant entirely rather than
        # marking it unavailable, and nothing rebuilt it until a manual
        # reload. `OrionPartnerSessionActiveBinarySensor.available` still
        # requires `has_partner_for_device`, so an unverified partner gets
        # an entity that exists and reports `unavailable`.
        if coordinator.has_partner_configured_for_device(device_id):
            entities.append(OrionPartnerSessionActiveBinarySensor(coordinator, device_id))
        for sensor_name in _TOPPER_SENSORS:
            entities.append(OrionSensorOnBedBinarySensor(coordinator, device_id, sensor_name))
        for zone_id in coordinator.device_zone_ids(device_id):
            entities.append(
                OrionThermalReliefBinarySensor(coordinator, device_id, zone_id)
            )
        entities.append(OrionDeviceOnlineBinarySensor(coordinator, device_id))
        entities.append(OrionSafetyProblemBinarySensor(coordinator, device_id))
        entities.append(OrionServerOccupancyBinarySensor(coordinator, device_id))
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
        self._attr_unique_id = helpers.person_unique_id(
            device_id,
            "session_active",
            coordinator.user_id,
            legacy=f"{device_id}_session_active",
        )
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
        # `partner_entity_key_id` rather than `partner_user["id"]`. This
        # entity is now built on runs where no partner fetch has succeeded,
        # and an empty id would send `person_unique_id` to its 2.x
        # role-keyed legacy fallback, which the registry would then hold
        # forever while the account-keyed id got minted as a second entity
        # on the next healthy boot. See `coordinator.partner_entity_key_id`.
        self._attr_unique_id = helpers.person_unique_id(
            device_id,
            "session_active",
            coordinator.partner_entity_key_id(),
            legacy=f"{device_id}_partner_session_active",
        )

    @property
    def name(self) -> str:
        """The partner's display name, recomputed rather than frozen.

        A property for the same reason the one on
        `OrionPartnerInsightSensor` is. This entity can be constructed
        before any partner fetch has succeeded, when `partner_name()` can
        only answer "Partner", and the name has to correct itself once a
        later poll learns who the partner actually is. Freezing it at
        construction would need a reload to fix.
        """
        return f"{self.coordinator.partner_name()} Sleep Session"

    def _session(self) -> dict | None:
        return self.coordinator.get_latest_partner_session(self._device_id)

    @property
    def available(self) -> bool:
        # `has_partner_for_device` already returns False unless
        # `partner_mapping_valid`, so the conjunct that used to sit here
        # decided nothing. It was removed rather than left alone because
        # a redundant check reads as a necessary one, and the next
        # partner-gated entity gets written by copying this line.
        return super().available and self.coordinator.has_partner_for_device(
            self._device_id
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

    **The raw field flaps, so this holds a reading before believing it.**
    The topper repeatedly reports someone arriving and leaving within a
    minute or two on a bed nobody is near, and both pads tend to do it in
    lockstep, which is the tell that it is the topper guessing rather
    than two people moving. Over one measured night the pads produced 16
    such episodes, every one shorter than four minutes, against a single
    real episode of nearly nine hours. Nothing fell in between.

    So a change has to persist for ``OCCUPANCY_HOLD_SECONDS`` before it
    is published. That costs a few minutes of latency on a signal the
    vendor already delays by 30 to 60 seconds, and buys an entity that
    can actually be automated against. The undelayed value stays visible
    as the ``raw`` attribute so the filter can be audited rather than
    trusted.

    This does not claim to fix every false positive. A long spurious
    reading would survive the hold untouched. It removes the short ones,
    which measurably are all of them so far.
    """

    _attr_device_class = BinarySensorDeviceClass.OCCUPANCY
    _attr_icon = "mdi:bed-outline"

    # Socket-fed, exactly like the heart rate and breath rate sensors that
    # read the same frames. Without this the base class returns False the
    # moment a poll fails, which would take occupancy down while the
    # socket is still delivering it.
    _live_fed = True

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
        self._settled: bool | None = None
        self._candidate: bool | None = None
        self._candidate_since: float | None = None

    def _raw(self) -> bool | None:
        return self.coordinator.sensor_is_on_bed(self._device_id, self._sensor_name)

    @callback
    def _handle_coordinator_update(self) -> None:
        """Promote a raw reading once it has held long enough."""
        raw = self._raw()
        now = time.monotonic()

        if raw is None:
            self._candidate = None
            self._candidate_since = None
        elif self._settled is None:
            # First reading after a restart. There is no history to
            # filter against, and refusing to answer for five minutes
            # would be worse than trusting the bed, so adopt it.
            self._settled = raw
            self._candidate = None
            self._candidate_since = None
        elif raw == self._settled:
            # Flapped back before it was believed. Nothing to publish.
            self._candidate = None
            self._candidate_since = None
        elif raw != self._candidate:
            self._candidate = raw
            self._candidate_since = now
        elif (
            self._candidate_since is not None
            and now - self._candidate_since >= OCCUPANCY_HOLD_SECONDS
        ):
            self._settled = raw
            self._candidate = None
            self._candidate_since = None

        super()._handle_coordinator_update()

    @property
    def is_on(self) -> bool | None:
        return self._settled

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose the unfiltered reading so the hold can be audited."""
        attrs: dict[str, Any] = {
            "raw": self._raw(),
            "hold_seconds": OCCUPANCY_HOLD_SECONDS,
        }
        if self._candidate is not None and self._candidate_since is not None:
            attrs["pending"] = self._candidate
            attrs["pending_for_seconds"] = int(
                time.monotonic() - self._candidate_since
            )
        return attrs

    @property
    def available(self) -> bool:
        """A reading this sensor has actually delivered, and a live source.

        The `super().available` half is the important one and it used to be
        missing. `live_devices` is deliberately carried forward across a
        failed poll, so the payload check alone is satisfied forever by one
        frame received once. With the account logged out and the socket
        unable to reconnect, this sensor kept publishing whatever the bed
        last said, with no upper bound on how old that was.

        Presence is the worst state to freeze. The biometric sensors on the
        identical data already chain through the base class for this exact
        reason, and this one being written as a bare payload check is what
        left it out of that fix.

        The comment this replaces claimed the check reported available
        "even if the individual sensor hasn't reported yet". It never did:
        `sensor_status_text` is per sensor, so an unreported sensor was
        already unavailable. The code and the comment disagreed and the
        code was right.
        """
        return (
            super().available
            and self.coordinator.sensor_status_text(
                self._device_id, self._sensor_name
            )
            is not None
        )


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
        self._attr_unique_id = helpers.schedule_unique_id(
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


class OrionServerOccupancyBinarySensor(OrionBaseEntity, BinarySensorEntity):
    """Whether Orion's servers think this person is in bed.

    Deliberately a second, separate entity rather than a replacement for
    the topper's own occupancy reading. The two disagree, and the
    disagreement is the point.

    The per-pad sensors derive occupancy from `status_text`, a field the
    Orion app never reads. This one comes from `/v1/sleep-session`, which
    the app does read. Where they differ, this is the better bet, and
    having both visible is what will eventually show how the topper gets
    it wrong.

    Covers the authenticated account only. The route reports for whoever
    holds the token, so a linked partner would need her own fetch.
    """

    _attr_device_class = BinarySensorDeviceClass.OCCUPANCY
    _attr_icon = "mdi:bed-outline"

    def __init__(self, coordinator, device_id: str) -> None:
        super().__init__(coordinator, device_id)
        # `server_says_in_bed()` reads the authenticated user's session,
        # so this belongs to that person rather than to the bed.
        self._attr_unique_id = helpers.person_unique_id(
            device_id,
            "server_in_bed",
            coordinator.user_id,
            legacy=f"{device_id}_server_in_bed",
        )
        self._attr_name = f"{coordinator.primary_name()} In Bed (Orion)"

    @property
    def available(self) -> bool:
        return super().available and self.coordinator.server_says_in_bed() is not None

    @property
    def is_on(self) -> bool | None:
        return self.coordinator.server_says_in_bed()

    @property
    def extra_state_attributes(self) -> dict:
        session = self.coordinator.live_session()
        return {
            "in_bed_start": session.get("in_bed_start"),
            "in_bed_end": session.get("in_bed_end"),
            # No session_id. Attributes go into the recorder's
            # state_attributes table and into every backup, and this file
            # already states that policy for the same identifier: the
            # session-listing service exists so an id can be found without
            # ever recording one forever for a lookup done once.
            "zone_id": session.get("zone_id") or None,
        }
