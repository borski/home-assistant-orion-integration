"""Select entities for the Orion bed.

One entity today: which side the bed faces. It sits on its own platform
rather than being folded into an existing one because it is the only
setting whose write goes to the device UUID instead of the serial, and
keeping that oddity in one file makes it hard to copy by accident.
"""

from __future__ import annotations

import logging
from typing import ClassVar

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from orion_sleep_api import util

from . import helpers
from .coordinator import OrionDataUpdateCoordinator
from .entity import OrionBaseEntity
from .errors import orion_call

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
        if not device_id:
            continue
        entities.append(OrionOrientationSelect(coordinator, device_id))

    # The app's temperature scale is one setting on the account, not one
    # per bed. Creating it inside the loop gave a two-bed household two
    # controls for the same value, and an unguarded id gave the first
    # device a unique_id of "None_temperature_display_unit".
    #
    # It still hangs off a device so it has somewhere to live in the
    # registry, but its IDENTITY is the config entry. Keying it on
    # whichever device happened to sort first meant removing that bed, or
    # the server returning them in a different order, silently minted a
    # replacement entity and orphaned the history of the old one.
    #
    # `coordinator.account_device_id` is now the single definition of
    # which bed that is, shared with every other account-scoped platform
    # and with `migrations._planned_renames`. This used to be a local
    # `min(...)` with a comment asking the migration to agree with it,
    # which is the arrangement that drifts.
    first = coordinator.account_device_id()
    if first:
        entities.append(
            OrionTemperatureDisplaySelect(coordinator, first, entry.entry_id)
        )

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
    # `ClassVar` because this is a mutable list shared by every instance of
    # the class, which ruff flags as RUF012. Annotating it is the assertion
    # that sharing is intended: the option set is a property of the vendor
    # API, not of one bed. Anything that mutated it in place would silently
    # change the options on every other bed's entity too.
    _attr_options: ClassVar[list[str]] = list(util.DEVICE_ORIENTATIONS)
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
        """Write a new orientation and refresh.

        Both arms go through `errors.orion_call` rather than being
        hand-rolled here. This method used to catch `OrionApiError` with a
        bespoke message and put NO guard at all on the API call for
        `ValueError`, which the client raises bare for input validation. A
        vendor 500 was handled and a client-side `ValueError` was not, so
        the second one reached the user as a traceback. `orion_call` covers
        both, and it now also routes an expired token to
        `ConfigEntryAuthFailed` instead of reporting it as a rejected
        orientation.

        The validation call sits INSIDE the context manager on purpose.
        `util.validate_device_orientation` raises `ValueError`, and
        `orion_call` surfaces a `ValueError` as its own message unchanged,
        so the user still reads "not a valid orientation" rather than
        "Orion could not ...".
        """
        async with orion_call("change the bed orientation"):
            util.validate_device_orientation(option)
            _LOGGER.warning(
                "Changing Orion bed orientation to %s. This decides which "
                "physical side each zone maps to and may re-attribute sleep "
                "data between sleepers",
                option,
            )
            await self.coordinator.api_client.set_device_orientation(
                self._device_id, option
            )
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
    # ClassVar for the same reason as the orientation options above.
    _attr_options: ClassVar[list[str]] = list(util.TEMPERATURE_DISPLAY_UNITS)

    def __init__(self, coordinator, device_id: str, entry_id: str) -> None:
        super().__init__(coordinator, device_id)
        # Through the shared helper, like every other account-scoped
        # entity. This was the first entity keyed on the entry and it
        # built the string by hand, which is why the migration had to
        # spell the same format out a second time.
        self._attr_unique_id = helpers.account_unique_id(
            entry_id, "temperature_display_unit"
        )
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
        """Write a new display scale and refresh.

        Reimplemented both of `orion_call`'s arms inline until now. Same
        two-branch shape, one message drifted from the other write paths,
        and it predated the `OrionAuthError` arm so an expired token here
        was reported as a rejected scale change.
        """
        # No warning log. This used to say the write "has not been
        # observed against the live API", which stopped being true and
        # then stayed in the code: `set_temperature_units` records
        # `display_unit` as MEASURED, set to `fahrenheit`, confirmed on
        # the server and restored to `relative`.
        #
        # A stale confidence note is worse than none. Everything else in
        # this integration uses that marker to decide whether a control is
        # safe to ship, so one that overstates the risk of a verified,
        # cosmetic, reversible write teaches readers to discount the ones
        # that mean it.
        #
        # `control_unit` on the same route genuinely has never been sent,
        # and this entity does not touch it.
        async with orion_call("change the app's temperature scale"):
            await self.coordinator.api_client.set_temperature_units(
                display_unit=option
            )
        await self.coordinator.async_request_refresh()
