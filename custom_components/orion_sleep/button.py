"""Button platform for Orion Sleep — one-shot device actions.

Each button is *gated* on the device's `permissions.allowed_actions` but
*dispatched* to its own endpoint. Those are two different things, and
conflating them is what broke the first cut of this platform:

  `allowed_actions` is a **UI capability list** — the right question for
  "should this control exist?", the wrong answer for "what do I call?".
  `POST /v1/devices/{serial}/action` accepts only `reboot` and
  `forget_wifi` (measured 2026-07-26). Other write routes are unknown.

`split` and `swap` DO have real routes, found in the Orion Android v2.4.1
bundle (2026-07-26). They live under `/v1/sleep-configurations/`, take a
`user_id` body, and have nothing to do with the action endpoint. The earlier
404s came from firing the capability name at `/action`, which was the exact
mistake this module's opening paragraph warns about.

⚠️ `device_forget_wifi` and `device_deactivate` are permitted by the
account and deliberately NOT exposed. Forgetting WiFi strands the bed —
the network is the only path to it, there is no BLE surface and every TCP
port is closed — and deactivate unpairs it. Neither is recoverable from
Home Assistant. `device_reset` the server does not grant.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from orion_sleep_api import OrionApiClient

from . import helpers
from .coordinator import OrionDataUpdateCoordinator
from .entity import OrionBaseEntity
from .errors import orion_call

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, kw_only=True)
class OrionButtonDef:
    """A button, its display gate, and how it is actually invoked."""

    key: str
    name: str
    icon: str
    # Capability name in permissions.allowed_actions — the DISPLAY gate.
    gate: str
    # How to perform it. Buttons dispatch to three different routes with
    # three different identifiers, so every call gets both the serial and
    # the authenticated user id and takes whichever it needs.
    call: Callable[[OrionApiClient, str, str], Awaitable[dict]]
    # Disruptive or unproven actions stay out of the registry by default.
    enabled_default: bool = False


BUTTONS: tuple[OrionButtonDef, ...] = (
    OrionButtonDef(
        key="reboot",
        name="Reboot Control Tower",
        icon="mdi:restart",
        gate="device_reboot",
        # Bare "reboot" (not "device_reboot"), keyed as action_type,
        # addressed by SERIAL. All three were wrong in the first cut.
        call=lambda client, serial, user_id: client.device_action(
            device_serial=serial, action="reboot"
        ),
    ),
    OrionButtonDef(
        key="swap_sides",
        name="Swap Bed Sides",
        icon="mdi:swap-horizontal",
        gate="swap",
        # Its own sleep-configurations route, keyed by USER id, not serial.
        call=lambda client, serial, user_id: client.swap_user_sides(user_id),
        # Reversible: pressing again swaps back. Safe to have on hand.
        enabled_default=True,
    ),
    OrionButtonDef(
        key="split_zones",
        name="Split Zones",
        icon="mdi:arrow-split-vertical",
        gate="split",
        call=lambda client, serial, user_id: client.split_user_zones(user_id),
        # Left disabled: whether this toggles or only ever splits is
        # UNRESOLVED, and no field in the live payload reports the state,
        # so there is no way to see what a press did.
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
        for definition in BUTTONS:
            if definition.gate not in allowed:
                _LOGGER.debug(
                    "Orion device %s does not permit '%s'; button not created",
                    device_id, definition.gate,
                )
                continue
            entities.append(OrionActionButton(coordinator, device_id, definition))

    async_add_entities(entities)


class OrionActionButton(OrionBaseEntity, ButtonEntity):
    """Fires one device action. No state — the API exposes none for these."""

    _attr_entity_category = EntityCategory.CONFIG

    def __init__(
        self,
        coordinator: OrionDataUpdateCoordinator,
        device_id: str,
        definition: OrionButtonDef,
    ) -> None:
        super().__init__(coordinator, device_id)
        self._def = definition
        self._attr_name = definition.name
        self._attr_icon = definition.icon
        self._attr_unique_id = f"{device_id}_action_{definition.key}"
        self._attr_entity_registry_enabled_default = definition.enabled_default

    async def async_press(self) -> None:
        """Invoke this button's own endpoint."""
        _LOGGER.info(
            "Orion button '%s' pressed on device %s",
            self._def.key,
            helpers.short_id(self._device_id),
        )
        serial = self._get_device().get("serial_number")
        if not serial:
            raise HomeAssistantError(
                f"No serial_number for Orion device {self._device_id}"
            )
        user_id = self.coordinator.user_id
        if not user_id:
            raise HomeAssistantError(
                "Orion has not resolved the authenticated user yet"
            )
        # This module was the one write surface with no handler at all. It
        # never imported `errors`, so a vendor 500 or an expired refresh
        # token came out of `async_press` as a raw `OrionApiError` and Home
        # Assistant logged a traceback. `errors.py` documents itself as
        # existing to remove exactly that, and every other write path
        # already routes through it.
        async with orion_call(f"run {self._def.name.lower()}"):
            await self._def.call(self.coordinator.api_client, str(serial), user_id)
        await self.coordinator.async_request_refresh()
