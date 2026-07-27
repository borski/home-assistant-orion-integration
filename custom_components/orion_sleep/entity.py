"""Base entity for Orion Sleep."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DEFAULT_RELATIVE_TEMP_TABLE, DOMAIN
from .coordinator import OrionDataUpdateCoordinator

_LOGGER = logging.getLogger(__name__)

# The vendor app debounces per body key so a slider drag produces one
# request instead of dozens, then holds a short lock after a successful
# write so its own store is not immediately overwritten. Both behaviours
# are APP-DERIVED from Orion Android v2.4.1 (`DEBOUNCE_INTERVAL` and
# `POST_API_LOCK_DELAY`). The exact constants are not readable from the
# bytecode, so these are our own conservative values.
LIVE_WRITE_DEBOUNCE_SECONDS = 0.6
LIVE_WRITE_LOCK_SECONDS = 5.0


class OrionBaseEntity(CoordinatorEntity[OrionDataUpdateCoordinator]):
    """Base entity for Orion Sleep."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: OrionDataUpdateCoordinator,
        device_id: str,
    ) -> None:
        super().__init__(coordinator)
        self._device_id = device_id

    @property
    def device_info(self) -> DeviceInfo:
        """Return device info for this entity."""
        device = self._get_device()
        return DeviceInfo(
            identifiers={(DOMAIN, self._device_id)},
            name=device.get("name", "Orion Sleep"),
            manufacturer="Orion Longevity",
            model=device.get("model", "Orion Sleep"),
            serial_number=device.get("serial_number"),
        )

    def _get_device(self) -> dict:
        """Find the device dict from the coordinator's device list."""
        for d in self.coordinator.devices:
            if d.get("id") == self._device_id:
                return d
        return {}

    def _serial(self) -> str:
        """Serial number. The live endpoints reject the UUID with a 403."""
        serial = self._get_device().get("serial_number")
        if not serial:
            raise HomeAssistantError(
                f"No serial_number for Orion device {self._device_id}; "
                "cannot address the live device endpoint"
            )
        return str(serial)

    def _get_relative_temp_table(self) -> list[dict[str, float]]:
        """Get the device's temperature_scale.relative lookup table.

        Falls back to the default table if not available.
        """
        device = self._get_device()
        table = device.get("temperature_scale", {}).get("relative")
        if table and isinstance(table, list) and len(table) > 0:
            return table
        return DEFAULT_RELATIVE_TEMP_TABLE

    def _celsius_to_offset(self, celsius: float | None) -> float | None:
        """Convert absolute Celsius to app-style offset using the device's table."""
        if celsius is None:
            return None
        table = self._get_relative_temp_table()
        best = min(table, key=lambda e: abs(e["out"] - celsius))
        return best["in"]

    def _offset_to_celsius(self, offset: float) -> float | None:
        """Convert app-style offset to absolute Celsius using the device's table."""
        table = self._get_relative_temp_table()
        for entry in table:
            if entry["in"] == offset:
                return entry["out"]
        # Nearest match fallback
        best = min(table, key=lambda e: abs(e["in"] - offset))
        return best["out"]


class OrionLiveSettingMixin:
    """Optimistic, debounced writes for `PUT /v1/devices/{serial}/live` keys.

    Mirrors what the vendor app does, because a naive implementation
    fights itself. Without the debounce a single slider drag fires dozens
    of PUTs. Without the post-write lock the next coordinator poll or
    WebSocket frame stomps the value the user just set and the control
    visibly snaps back.

    Behaviour, in order: stash the prior value for rollback, update local
    state optimistically, hold a lock so reads return the optimistic value,
    debounce the network call, then on success feed the returned live
    device object straight into the coordinator. On failure, roll back and
    ask the coordinator for the truth.

    Confidence on the underlying route: APP-DERIVED. Nobody has watched
    one of these requests succeed.
    """

    # Class-level defaults so this mixes in without an __init__ that has
    # to cooperate with CoordinatorEntity's constructor chain.
    _live_field: str = ""
    _live_pending: Any = None
    _live_previous: Any = None
    _live_optimistic: Any = None
    _live_optimistic_until: float = 0.0
    _live_flush_task: asyncio.Task | None = None

    def _live_display_value(self, actual: Any) -> Any:
        """Return the optimistic value while locked, otherwise the real one."""
        if time.monotonic() < self._live_optimistic_until:
            return self._live_optimistic
        return actual

    async def _async_write_live_setting(self, value: Any, current: Any) -> None:
        """Queue a debounced write, updating local state immediately."""
        if not self._live_field:
            raise HomeAssistantError("No Orion live setting field configured")
        # Only capture the rollback target on the first write of a burst,
        # so dragging a slider reverts to where it started rather than to
        # the previous intermediate position.
        if time.monotonic() >= self._live_optimistic_until:
            self._live_previous = current
        self._live_pending = value
        self._live_optimistic = value
        self._live_optimistic_until = time.monotonic() + LIVE_WRITE_LOCK_SECONDS
        self.async_write_ha_state()

        if self._live_flush_task is not None and not self._live_flush_task.done():
            self._live_flush_task.cancel()
        self._live_flush_task = self.hass.async_create_task(
            self._async_flush_live_setting()
        )

    async def _async_flush_live_setting(self) -> None:
        """Send the pending value after the debounce window closes."""
        try:
            await asyncio.sleep(LIVE_WRITE_DEBOUNCE_SECONDS)
        except asyncio.CancelledError:
            return

        value = self._live_pending
        try:
            serial = self._serial()
            response = await self.coordinator.api_client.update_live_device_setting(
                serial, self._live_field, value
            )
        except Exception as err:  # noqa: BLE001 - background task, must not escape
            # Roll back rather than leaving the control showing a value the
            # device never accepted. This runs in a background task, so an
            # exception here would only reach the log anyway.
            _LOGGER.warning(
                "Orion %s update failed, reverting: %s",
                self._live_field,
                type(err).__name__,
            )
            self._live_optimistic = self._live_previous
            self._live_optimistic_until = 0.0
            self.async_write_ha_state()
            await self.coordinator.async_request_refresh()
            return

        # The PUT returns the full live device object. Use it instead of
        # refetching, which is what the app does.
        payload = response.get("response") if isinstance(response, dict) else None
        self.coordinator.apply_live_device(serial, payload)
        # Keep the lock briefly so an in-flight poll started before the
        # write cannot land afterwards and undo it.
        self._live_optimistic_until = time.monotonic() + LIVE_WRITE_LOCK_SECONDS
        self.async_write_ha_state()

    async def async_will_remove_from_hass(self) -> None:
        """Cancel any pending write when the entity goes away."""
        if self._live_flush_task is not None and not self._live_flush_task.done():
            self._live_flush_task.cancel()
        await super().async_will_remove_from_hass()
