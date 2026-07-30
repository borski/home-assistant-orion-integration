"""Climate platform for Orion Sleep.

One climate entity per *zone*, not per device. A dual-zone bed genuinely
runs two independent setpoints — the live snapshot shows them diverging —
so collapsing them into a single entity throws away half the control
surface.

All reads and writes go through the live runtime endpoints:

    GET /v1/devices/{serial}/live                  setpoint + measured
    PUT /v1/devices/{serial}/live/zones/{zoneId}   on / temp

These are the endpoints the app itself drives and they have been observed
on the wire. The previous device-level entity wrote through
`PUT /v1/sleep-configurations/temperature`, an unverified legacy route
that is no longer part of the measured API contract. Its `turn_off` was
also a no-op.
"""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant.components.climate import (
    ClimateEntity,
    ClimateEntityFeature,
    HVACAction,
    HVACMode,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_TEMPERATURE, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import entity_platform
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import helpers
from .const import (
    DEFAULT_COOLING_MINUTES,
    MAX_COOLING_MINUTES,
    MIN_COOLING_MINUTES,
)
from .coordinator import OrionDataUpdateCoordinator
from .entity import OrionBaseEntity
from .errors import orion_call

_LOGGER = logging.getLogger(__name__)

SERVICE_START_COOLING = "start_cooling"
SERVICE_STOP_COOLING = "stop_cooling"

# Only "standby" has ever been captured on the wire. The heating/cooling
# spellings are guesses from the field name, so they are listed here but
# anything unrecognised deliberately falls through to None ("unknown")
# rather than being rendered as a confident direction.
_THERMAL_STATE_TO_ACTION: dict[str, HVACAction] = {
    "standby": HVACAction.IDLE,
    "idle": HVACAction.IDLE,
    "off": HVACAction.OFF,
    "heating": HVACAction.HEATING,
    "heat": HVACAction.HEATING,
    "cooling": HVACAction.COOLING,
    "cool": HVACAction.COOLING,
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up one climate entity per zone, per device."""
    coordinator: OrionDataUpdateCoordinator = entry.runtime_data
    entities: list[OrionZoneClimateEntity] = []

    for device in coordinator.devices:
        device_id = device.get("id")
        if not device_id:
            continue

        zone_ids = coordinator.device_zone_ids(device_id)
        if not zone_ids:
            # No live snapshot yet and no membership list. Skipping is
            # correct: inventing a synthetic zone would create an entity
            # whose unique_id can never match the real one later.
            _LOGGER.warning(
                "Orion device %s exposed no zones; no climate entity created. "
                "This resolves itself once the first live snapshot arrives",
                helpers.short_id(device_id),
            )
            continue

        for zone_id in zone_ids:
            entities.append(OrionZoneClimateEntity(coordinator, device_id, zone_id))

    async_add_entities(entities)

    # Hot flash relief is per zone, and the climate entities are the only
    # per-zone things in the integration. Registering here means the
    # caller picks a side by naming a person's climate entity instead of
    # handling raw zone ids.
    #
    # `admin=False` on both, and stated rather than omitted. These change a
    # temperature for a few minutes and nothing else, which is the same
    # authority the climate entity already hands to anyone who can see it.
    # Gating them would mean a non-admin can set the bed to 45 degrees
    # through `climate.set_temperature` but cannot cool it back down, which
    # is a worse outcome than the one it prevents.
    #
    # Registered through `helpers.async_register_entity_service` rather than
    # the raw platform method so that decision is visible HERE. These two
    # used to call `platform.async_register_entity_service` directly, and
    # the reasoning above lived only in a comment inside
    # `tests_ha/test_entity_service_auth_real.py`. A reader in this file saw
    # an unexplained deviation from the pattern every other registration in
    # the integration follows and had no way to tell "considered and
    # allowed" apart from "nobody thought about it".
    platform = entity_platform.async_get_current_platform()
    helpers.async_register_entity_service(
        platform,
        SERVICE_START_COOLING,
        {
            vol.Optional("duration_minutes", default=DEFAULT_COOLING_MINUTES): vol.All(
                cv.positive_int,
                vol.Range(min=MIN_COOLING_MINUTES, max=MAX_COOLING_MINUTES),
            )
        },
        "async_start_cooling",
        admin=False,
    )
    helpers.async_register_entity_service(
        platform,
        SERVICE_STOP_COOLING,
        {},
        "async_stop_cooling",
        admin=False,
    )


class OrionZoneClimateEntity(OrionBaseEntity, ClimateEntity):
    """A single temperature zone (one side of the bed)."""

    # Fed by the live-device stream, so a fresh socket keeps it
    # available even while a polled endpoint is failing.
    _live_fed = True

    _attr_hvac_modes = [HVACMode.HEAT_COOL, HVACMode.OFF]
    _attr_supported_features = (
        ClimateEntityFeature.TARGET_TEMPERATURE
        | ClimateEntityFeature.TURN_ON
        | ClimateEntityFeature.TURN_OFF
    )
    # The API is Celsius-native; HA converts for display. Reporting the
    # native unit is what makes °F households render correctly.
    _attr_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_target_temperature_step = 0.5
    _enable_turn_on_off_backwards_compat = False

    def __init__(
        self,
        coordinator: OrionDataUpdateCoordinator,
        device_id: str,
        zone_id: str,
    ) -> None:
        super().__init__(coordinator, device_id)
        self._zone_id = zone_id
        self._attr_unique_id = f"{device_id}_climate_{zone_id}"
        self._attr_name = f"{self._zone_label()} Climate"

        temp_range = self._get_device().get("temperature_range", {})
        self._attr_min_temp = float(temp_range.get("min", 10))
        self._attr_max_temp = float(temp_range.get("max", 45))

    def _zone_label(self) -> str:
        """Human label for this zone. See `coordinator.zone_label`."""
        return self.coordinator.zone_label(self._device_id, self._zone_id)

    @property
    def available(self) -> bool:
        """Available only when a live snapshot actually contains this zone."""
        return (
            super().available
            and self.coordinator.zone_is_on(self._device_id, self._zone_id) is not None
        )

    @property
    def current_temperature(self) -> float | None:
        """Measured zone temperature (`status.zones[].temp`)."""
        return self.coordinator.zone_measured_temp(self._device_id, self._zone_id)

    @property
    def target_temperature(self) -> float | None:
        """Zone setpoint (`zones[].temp`)."""
        return self.coordinator.zone_setpoint(self._device_id, self._zone_id)

    @property
    def hvac_mode(self) -> HVACMode:
        """HEAT_COOL when the zone is powered, OFF otherwise."""
        return (
            HVACMode.HEAT_COOL
            if self.coordinator.zone_is_on(self._device_id, self._zone_id)
            else HVACMode.OFF
        )

    @property
    def hvac_action(self) -> HVACAction | None:
        """What the zone is doing right now, when the device says so."""
        if not self.coordinator.zone_is_on(self._device_id, self._zone_id):
            return HVACAction.OFF
        state = self.coordinator.zone_thermal_state(self._device_id, self._zone_id)
        if state is None:
            return None
        return _THERMAL_STATE_TO_ACTION.get(state.lower())

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Zone id, raw thermal state, and physical side.

        `side` (left/right) is here for side-anchored controllers like a
        bedside dial, which think in sides rather than in whichever person
        sleeps there. Rather than rename this person-named entity (which would
        break the naming the rest of the integration is built on and every
        dashboard that references it), the side rides as an attribute. A dial
        or automation resolves "the left climate" with a template over this
        attribute, and it keeps working when the sleepers swap sides because
        the CONF_ZONE_LEFT option moves with the bed, not with the person.
        The key is omitted, not set to None, when the zone is neither
        configured side.
        """
        attrs: dict[str, Any] = {
            "zone_id": self._zone_id,
            "thermal_state": self.coordinator.zone_thermal_state(
                self._device_id, self._zone_id
            ),
        }
        side = self.coordinator.zone_side(self._zone_id)
        if side is not None:
            attrs["side"] = side
        return attrs

    async def async_set_temperature(self, **kwargs: Any) -> None:
        """Set this zone's target temperature."""
        temp = kwargs.get(ATTR_TEMPERATURE)
        if temp is None:
            return
        async with orion_call("change the temperature"):
            await self.coordinator.api_client.update_live_device_zone(
                device_serial=self._serial(),
                zone_id=self._zone_id,
                temp=float(temp),
            )
        await self.coordinator.async_request_refresh()

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        """Turn this zone on or off."""
        if hvac_mode == HVACMode.OFF:
            await self.async_turn_off()
        else:
            await self.async_turn_on()

    async def async_turn_on(self) -> None:
        """Power this zone on, leaving its setpoint untouched."""
        async with orion_call("change the temperature"):
            await self.coordinator.api_client.update_live_device_zone(
                device_serial=self._serial(),
                zone_id=self._zone_id,
                on=True,
            )
        await self.coordinator.async_request_refresh()

    async def async_turn_off(self) -> None:
        """Power this zone off.

        NOTE: this is a *runtime* off. The sleep schedule is a separate
        object and will power the zone back on at its next scheduled
        action — see `_handle_schedule_on_turn_off`.
        """
        async with orion_call("change the temperature"):
            await self.coordinator.api_client.update_live_device_zone(
                device_serial=self._serial(),
                zone_id=self._zone_id,
                on=False,
            )
        await self._handle_schedule_on_turn_off()
        await self.coordinator.async_request_refresh()

    async def _handle_schedule_on_turn_off(self) -> None:
        """Reconcile a manual zone-off against tonight's sleep schedule.

        POLICY: passthrough (chosen 2026-07-26). Turning a zone off powers
        it down immediately and does NOT touch the schedule, so the next
        scheduled action powers it back on. This matches the vendor app,
        and it keeps a climate card from silently mutating a schedule the
        other sleeper also depends on.

        Consequence to be aware of: "I turned it off and it came back on
        at bedtime" is expected behaviour, not a bug.

        To change the policy later, everything needed is already here:
            self.coordinator.get_today_schedule()  -> today's schedule dict,
                carrying `bedtime_is_active` / `wakeup_is_active`
            self.coordinator.api_client            -> PUT /v1/sleep-schedules
                body: {"schedules": [{"day": N, <field>: <value>}]}
                (partial updates work — only the named field changes)
        Note the schedule is per-USER, not per-zone, so a schedule edit
        made from one zone's card affects that user's whole schedule.
        """
        return

    async def async_start_cooling(self, duration_minutes: int) -> None:
        """Start hot flash relief on this zone only.

        Overrides the schedule with maximum cooling for the requested
        window, then the device restores what it was doing. The other
        side of the bed is untouched.

        MEASURED 2026-07-27. It is the one route in this integration that
        legitimately sends several keys in one body, because that is what
        the vendor's own client does.
        """
        # No `except ValueError` around this. `errors.orion_call` already
        # catches `ValueError` and re-raises it as `HomeAssistantError`, so
        # an outer handler here can never run. It looked like belt and
        # braces and was dead code, which is worse than nothing: the next
        # reader copies it to a fifth call site believing it does something.
        async with orion_call("start rapid cooling"):
            await self.coordinator.api_client.start_thermal_relief(
                self._serial(), [self._zone_id], duration_minutes
            )

        await self.coordinator.async_request_refresh()

    async def async_stop_cooling(self) -> None:
        """End hot flash relief early on this zone.

        The device restores the `previous_temp` and `previous_on` it
        stashed when relief started. Safe to call when nothing is
        running: the server treats it as a no-op rather than an error.
        """
        async with orion_call("stop rapid cooling"):
            await self.coordinator.api_client.cancel_thermal_relief(
                self._serial(), [self._zone_id]
            )

        await self.coordinator.async_request_refresh()
