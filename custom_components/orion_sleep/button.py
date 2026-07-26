"""Button platform for Orion Sleep — one-shot device actions.

Wraps `POST /v1/devices/{deviceId}/action` for the actions that have no
readable state, so a stateful entity would have to lie about them.

⚠️ Two deliberate omissions. `device_forget_wifi` and `device_deactivate`
are in the account's allowed_actions but are **NOT** exposed here:
forgetting WiFi drops the bed off the network (and the only path to it
is the network — see the project notes on the BLE dead end), and
deactivate unpairs it from the account. Neither is recoverable from
Home Assistant. `device_reset` is not exposed either; the server does
not grant it.

⚠️ The `value` payload for each action is UNVERIFIED. `openapi.yaml`
documents it only as "action-specific payload (e.g. brightness level)",
so the shapes below are the best reading of the enum, not observed
traffic. A rejected action surfaces as a 400 and changes nothing.
"""

from __future__ import annotations

import logging

from homeassistant.components.button import ButtonEntity, ButtonEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .coordinator import OrionDataUpdateCoordinator
from .entity import OrionBaseEntity

_LOGGER = logging.getLogger(__name__)

# (description, api action name). The entity only gets created when the
# action name is present in the device's own permissions.allowed_actions.
_BUTTONS: tuple[tuple[ButtonEntityDescription, str], ...] = (
    (
        ButtonEntityDescription(
            key="reboot",
            name="Reboot Control Tower",
            icon="mdi:restart",
            entity_category=EntityCategory.CONFIG,
        ),
        "device_reboot",
    ),
    (
        ButtonEntityDescription(
            key="split_zones",
            name="Split Zones",
            icon="mdi:call-split",
            entity_category=EntityCategory.CONFIG,
        ),
        "split",
    ),
    (
        ButtonEntityDescription(
            key="swap_sides",
            name="Swap Sides",
            icon="mdi:swap-horizontal",
            entity_category=EntityCategory.CONFIG,
        ),
        "swap",
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Create a button per permitted device action."""
    coordinator: OrionDataUpdateCoordinator = entry.runtime_data
    entities: list[OrionActionButton] = []

    for device in coordinator.devices:
        device_id = device.get("id")
        if not device_id:
            continue
        allowed = coordinator.device_allowed_actions(device_id)
        for description, action in _BUTTONS:
            if action not in allowed:
                _LOGGER.debug(
                    "Orion device %s does not permit '%s'; button not created",
                    device_id, action,
                )
                continue
            entities.append(
                OrionActionButton(coordinator, device_id, description, action)
            )

    async_add_entities(entities)


class OrionActionButton(OrionBaseEntity, ButtonEntity):
    """Fires one device action. No state — the API exposes none for these."""

    def __init__(
        self,
        coordinator: OrionDataUpdateCoordinator,
        device_id: str,
        description: ButtonEntityDescription,
        action: str,
    ) -> None:
        super().__init__(coordinator, device_id)
        self.entity_description = description
        self._action = action
        self._attr_unique_id = f"{device_id}_action_{description.key}"

    async def async_press(self) -> None:
        """Send the action. Uses the device UUID, not the serial."""
        _LOGGER.info(
            "Orion device action '%s' on %s", self._action, self._device_id
        )
        await self.coordinator.api_client.device_action(
            device_id=self._device_id,
            action=self._action,
        )
        await self.coordinator.async_request_refresh()
