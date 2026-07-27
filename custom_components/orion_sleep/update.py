"""Update platform for Orion Sleep."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.update import UpdateEntity, UpdateEntityFeature
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from orion_sleep_api import OrionApiError

from .coordinator import OrionDataUpdateCoordinator
from .entity import OrionBaseEntity

_LOGGER = logging.getLogger(__name__)

# Home Assistant decides "update available" by string-comparing
# installed_version against latest_version. Orion only ever tells us
# `pending_update.is_available: true` and has never named the version it
# is offering, so when one is waiting we have nothing truthful to put in
# latest_version. This placeholder makes the two strings differ, which is
# what flips the entity.
#
# Every key the block might plausibly carry is checked first, so the day
# Orion starts naming versions this stops being reachable on its own.
_UNNAMED_VERSION = "Available"

_VERSION_KEYS: tuple[str, ...] = (
    "version",
    "target_version",
    "available_version",
    "firmware_version",
    "cb",
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the Orion Sleep firmware update entity."""
    coordinator: OrionDataUpdateCoordinator = entry.runtime_data
    entities: list[UpdateEntity] = []

    for device in coordinator.devices:
        device_id = device.get("id")
        if not device_id:
            continue
        entities.append(OrionFirmwareUpdateEntity(coordinator, device_id))

    async_add_entities(entities)


class OrionFirmwareUpdateEntity(OrionBaseEntity, UpdateEntity):
    """Control Tower firmware, as a real update entity.

    Supersedes the read-only `binary_sensor.*_firmware_update_available`.

    Confidence split, because the two halves are not equal:

    - READ is MEASURED. `status.firmware.cb` and
      `status.pending_update.is_available` both arrive on every live
      payload. Installed version currently reads 2.6.1 and no update has
      ever been advertised.
    - INSTALL is APP-DERIVED and has never been executed. See
      `OrionApiClient.trigger_firmware_update`.

    PROGRESS is supported but reports only a boolean. Nothing in the
    payload carries a percentage, so the bar is indeterminate.
    RELEASE_NOTES is not supported: there is no route for them.
    """

    _attr_supported_features = (
        UpdateEntityFeature.INSTALL | UpdateEntityFeature.PROGRESS
    )
    _attr_name = "Firmware"
    _attr_icon = "mdi:chip"

    def __init__(
        self, coordinator: OrionDataUpdateCoordinator, device_id: str
    ) -> None:
        super().__init__(coordinator, device_id)
        self._attr_unique_id = f"{device_id}_firmware_update"

    @property
    def installed_version(self) -> str | None:
        """Control board firmware currently running."""
        firmware = self.coordinator.firmware(self._device_id) or {}
        value = firmware.get("cb")
        return value if isinstance(value, str) and value.strip() else None

    @property
    def latest_version(self) -> str | None:
        """Version on offer, or the installed one when nothing is waiting.

        Returning installed_version is how an update entity says "up to
        date", and it is the same shape Lucid's integration uses for a
        vehicle reporting no pending software.
        """
        installed = self.installed_version
        if self.coordinator.pending_update_available(self._device_id) is not True:
            return installed

        pending = self.coordinator.pending_update_info(self._device_id) or {}
        for key in _VERSION_KEYS:
            value = pending.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return _UNNAMED_VERSION

    @property
    def in_progress(self) -> bool:
        """Whether the device says a flash is running."""
        return self.coordinator.firmware_update_in_progress(self._device_id)

    @property
    def update_percentage(self) -> None:
        """Always None. Orion reports a step name, never a percentage."""
        return None

    @property
    def available(self) -> bool:
        """Available once the device has reported a firmware version."""
        return super().available and self.installed_version is not None

    @property
    def extra_state_attributes(self) -> dict | None:
        """Surface the update step and interface board version."""
        attrs: dict[str, Any] = {}

        firmware = self.coordinator.firmware(self._device_id) or {}
        interface = firmware.get("ib")
        if isinstance(interface, str) and interface.strip():
            attrs["interface_board"] = interface

        info = self.coordinator.firmware_update_info(self._device_id) or {}
        for key in ("current_step", "result"):
            value = info.get(key)
            if value is not None:
                attrs[key] = value

        return attrs or None

    async def async_install(
        self, version: str | None, backup: bool, **kwargs: Any
    ) -> None:
        """Trigger the firmware update on the Control Tower.

        There is no rollback. Orion has no route to install a specific
        version either, so `version` is accepted for interface
        compatibility and deliberately ignored.
        """
        if self.latest_version == self.installed_version:
            raise HomeAssistantError("Orion is not advertising a firmware update")

        _LOGGER.warning(
            "Triggering Orion firmware update on %s (installed %s). "
            "This cannot be undone.",
            self._serial(),
            self.installed_version,
        )

        try:
            await self.coordinator.api_client.trigger_firmware_update(self._serial())
        except OrionApiError as err:
            raise HomeAssistantError(
                "Orion rejected the firmware update request"
            ) from err

        await self.coordinator.async_request_refresh()
