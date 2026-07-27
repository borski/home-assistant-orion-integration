"""Select entities for the Orion bed.

One entity today: which side the bed faces. It sits on its own platform
rather than being folded into an existing one because it is the only
setting whose write goes to the device UUID instead of the serial, and
keeping that oddity in one file makes it hard to copy by accident.
"""

from __future__ import annotations

import logging

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import util
from .api import OrionApiError
from .coordinator import OrionDataUpdateCoordinator
from .entity import OrionBaseEntity

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Orion select entities."""
    coordinator: OrionDataUpdateCoordinator = entry.runtime_data
    entities: list[SelectEntity] = []
    for device in coordinator.devices:
        device_id = device.get("id")
        if device_id:
            entities.append(OrionOrientationSelect(coordinator, device_id))
        entities.append(OrionTemperatureDisplaySelect(coordinator, device_id))
    async_add_entities(entities)


class OrionOrientationSelect(OrionBaseEntity, SelectEntity):
    """Which physical side of the bed the device is oriented to.

    A single device-level value, not one per sleeper. The vendor app
    gives it a dedicated screen captioned "Update your side to fix your
    insight", which makes it the supported remedy when a night lands on
    the wrong person.

    The write is APP-DERIVED and has never been observed, so changing
    this is a deliberate experiment rather than routine configuration.
    It is filed as a config entity for that reason.
    """

    _attr_entity_category = EntityCategory.CONFIG
    _attr_icon = "mdi:bed-outline"
    _attr_options = list(util.DEVICE_ORIENTATIONS)
    _attr_translation_key = "device_orientation"

    def __init__(
        self, coordinator: OrionDataUpdateCoordinator, device_id: str
    ) -> None:
        super().__init__(coordinator, device_id)
        self._attr_unique_id = f"{device_id}_orientation"
        self._attr_name = "Bed Orientation"

    def _device(self) -> dict:
        for device in self.coordinator.devices:
            if device.get("id") == self._device_id:
                return device
        return {}

    @property
    def current_option(self) -> str | None:
        """Return the orientation the server reports, or None."""
        value = self._device().get("orientation")
        return value if value in util.DEVICE_ORIENTATIONS else None

    @property
    def available(self) -> bool:
        """Unavailable until the device reports an orientation we know."""
        return super().available and self.current_option is not None

    async def async_select_option(self, option: str) -> None:
        """Write a new orientation and refresh."""
        try:
            util.validate_device_orientation(option)
        except ValueError as err:
            raise HomeAssistantError(str(err)) from err

        _LOGGER.warning(
            "Changing Orion bed orientation to %s. This decides which "
            "physical side each zone maps to and may re-attribute sleep "
            "data between sleepers",
            option,
        )
        try:
            await self.coordinator.api_client.set_device_orientation(
                self._device_id, option
            )
        except OrionApiError as err:
            raise HomeAssistantError(f"Orion rejected the orientation change: {err}") from err
        await self.coordinator.async_request_refresh()


class OrionTemperatureDisplaySelect(OrionBaseEntity, SelectEntity):
    """Which temperature scale the Orion app shows.

    `relative` is the -10 to +10 offset ladder this integration already
    exposes as number entities. `fahrenheit` is the absolute scale. Both
    lookup tables ship on the device, so this changes what the phone app
    displays rather than anything the bed does.

    Account level, not per device. Changing it affects every bed on the
    account.
    """

    _attr_entity_category = EntityCategory.CONFIG
    _attr_icon = "mdi:thermometer-lines"
    _attr_options = list(util.TEMPERATURE_DISPLAY_UNITS)

    def __init__(self, coordinator, device_id: str) -> None:
        super().__init__(coordinator, device_id)
        self._attr_unique_id = f"{device_id}_temperature_display_unit"
        self._attr_name = "App Temperature Scale"

    @property
    def available(self) -> bool:
        return (
            super().available
            and self.coordinator.temperature_display_unit() in self._attr_options
        )

    @property
    def current_option(self) -> str | None:
        return self.coordinator.temperature_display_unit()

    async def async_select_option(self, option: str) -> None:
        _LOGGER.warning(
            "Changing the Orion app's temperature scale to %s. This write has "
            "not been observed against the live API.",
            option,
        )
        try:
            await self.coordinator.api_client.set_temperature_units(display_unit=option)
        except ValueError as err:
            raise HomeAssistantError(str(err)) from err
        except OrionApiError as err:
            raise HomeAssistantError(
                f"Orion rejected the temperature scale change: {err}"
            ) from err
        await self.coordinator.async_request_refresh()
