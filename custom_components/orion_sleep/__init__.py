"""The Orion Sleep integration."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import (
    ConfigEntryError,
    ConfigEntryNotReady,
    HomeAssistantError,
    Unauthorized,
)
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from orion_sleep_api import OrionApiClient

from .const import (
    CONF_ACCESS_TOKEN,
    CONF_ACCOUNT_ID,
    CONF_DEVICE_IDS,
    CONF_EXPIRES_AT,
    CONF_PARTNER_ACCESS_TOKEN,
    CONF_PARTNER_EXPIRES_AT,
    CONF_PARTNER_REFRESH_TOKEN,
    CONF_REFRESH_TOKEN,
    CONF_UID_RECOVERY_ACTIVE,
    DOMAIN,
)
from .coordinator import OrionDataUpdateCoordinator
from .migrations import (
    async_migrate_entry_identity,
    async_migrate_unique_ids,
    async_revert_unique_ids,
    entry_identity_conflict,
    overlapping_entry_ids,
    unresolved_device_entries,
)

_LOGGER = logging.getLogger(__name__)

SERVICE_REVERT_UNIQUE_IDS = "revert_unique_ids"
SERVICE_RESUME_UNIQUE_IDS = "resume_unique_ids"
# States Home Assistant will accept an unload from.
_UNLOADABLE_STATES = (
    ConfigEntryState.LOADED,
    ConfigEntryState.SETUP_RETRY,
    ConfigEntryState.SETUP_ERROR,
)
SERVICE_REVERT_SCHEMA = vol.Schema(
    {
        vol.Required("config_entry_id"): str,
        vol.Required("confirm"): vol.All(bool, vol.In([True])),
    }
)

PLATFORMS: list[Platform] = [
    Platform.BUTTON,
    Platform.CLIMATE,
    Platform.NUMBER,
    Platform.SELECT,
    Platform.SENSOR,
    Platform.BINARY_SENSOR,
    Platform.SWITCH,
    Platform.TIME,
    Platform.UPDATE,
]


async def async_setup(hass: HomeAssistant, _config: dict[str, Any]) -> bool:
    """Register recovery independently of any entry's health."""
    _async_register_recovery_service(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Orion Sleep from a config entry."""
    if entry.data.get(CONF_UID_RECOVERY_ACTIVE):
        raise ConfigEntryError(
            "Orion entity ids were prepared for a downgrade. Install 2.x, or "
            "clear recovery mode before loading 3.x again"
        )
    session = async_get_clientsession(hass)
    api_client = OrionApiClient(
        session=session,
        access_token=entry.data[CONF_ACCESS_TOKEN],
        refresh_token=entry.data[CONF_REFRESH_TOKEN],
        expires_at=entry.data[CONF_EXPIRES_AT],
    )

    # Register token refresh callback to persist new tokens
    expected_primary_refresh_token = entry.data[CONF_REFRESH_TOKEN]

    def on_token_refresh(
        access_token: str, refresh_token: str, expires_at: float
    ) -> None:
        nonlocal expected_primary_refresh_token
        if entry.data.get(CONF_REFRESH_TOKEN) != expected_primary_refresh_token:
            return
        expected_primary_refresh_token = refresh_token
        hass.config_entries.async_update_entry(
            entry,
            data={
                **entry.data,
                CONF_ACCESS_TOKEN: access_token,
                CONF_REFRESH_TOKEN: refresh_token,
                CONF_EXPIRES_AT: expires_at,
            },
        )

    api_client.set_token_refresh_callback(on_token_refresh)

    partner_api_client: OrionApiClient | None = None
    if entry.data.get(CONF_PARTNER_ACCESS_TOKEN):
        expected_partner_refresh_token = entry.data.get(CONF_PARTNER_REFRESH_TOKEN, "")
        partner_api_client = OrionApiClient(
            session=session,
            access_token=entry.data[CONF_PARTNER_ACCESS_TOKEN],
            refresh_token=entry.data.get(CONF_PARTNER_REFRESH_TOKEN, ""),
            expires_at=entry.data.get(CONF_PARTNER_EXPIRES_AT, 0),
        )

        def on_partner_token_refresh(
            access_token: str, refresh_token: str, expires_at: float
        ) -> None:
            nonlocal expected_partner_refresh_token
            if (
                entry.data.get(CONF_PARTNER_REFRESH_TOKEN)
                != expected_partner_refresh_token
            ):
                return
            expected_partner_refresh_token = refresh_token
            hass.config_entries.async_update_entry(
                entry,
                data={
                    **entry.data,
                    CONF_PARTNER_ACCESS_TOKEN: access_token,
                    CONF_PARTNER_REFRESH_TOKEN: refresh_token,
                    CONF_PARTNER_EXPIRES_AT: expires_at,
                },
            )

        partner_api_client.set_token_refresh_callback(on_partner_token_refresh)

    coordinator = OrionDataUpdateCoordinator(
        hass,
        entry,
        api_client,
        partner_api_client=partner_api_client,
    )
    # The first refresh starts a WebSocket client per device. If it, or
    # the platform setup after it, raises, Home Assistant never calls
    # async_unload_entry, because as far as it is concerned setup never
    # succeeded. Nothing would stop those sockets, and they reconnect on
    # a backoff forever with no config entry left to own them. A reauth
    # loop would stack a fresh set on every attempt.
    platforms_loaded = False
    completed = False
    try:
        await coordinator.async_config_entry_first_refresh()
        entry.runtime_data = coordinator
        device_ids = sorted(
            {
                str(device["id"])
                for device in coordinator.devices
                if isinstance(device, dict) and device.get("id")
            }
        )
        identity_data = {
            CONF_DEVICE_IDS: device_ids,
            CONF_ACCOUNT_ID: coordinator.user_id,
        }
        if any(entry.data.get(key) != value for key, value in identity_data.items()):
            hass.config_entries.async_update_entry(
                entry,
                data={**entry.data, **identity_data},
            )
        unresolved = unresolved_device_entries(hass, entry.entry_id)
        if unresolved:
            raise ConfigEntryNotReady(
                "Waiting for every Orion entry that is still starting to name "
                "its beds before any history is migrated"
            )
        # Identity first, then entities. Both refuse to run into another
        # entry, but only this order fails before anything is renamed: the
        # account collision used to be discovered after every entity had
        # already moved. The two are independent, so ordering is free.
        # `async_migrate_unique_ids` opens with its own bed-overlap check,
        # which is why there is no third copy of either here.
        async_migrate_entry_identity(hass, entry, coordinator)
        async_migrate_unique_ids(hass, entry, coordinator)
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
        platforms_loaded = True
        # A fresh 3.0 install had no registry rows during the first pass.
        # Record the rows the platforms just created so downgrade recovery
        # works immediately rather than only after another restart.
        async_migrate_unique_ids(hass, entry, coordinator)
        completed = True
    finally:
        # NOT `except Exception`. CancelledError inherits from
        # BaseException, and Home Assistant cancels in-flight setup on
        # shutdown. The first refresh above is where the per-device sockets
        # start, so a cancellation there is exactly the leak this block
        # exists to prevent: sockets reconnecting on a backoff forever with
        # no config entry left to own them.
        if not completed:
            if platforms_loaded:
                await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
            await coordinator.async_shutdown()

    # Reload on options change
    # Registered AFTER the migration, deliberately. The migration records
    # its rename map into options, and a listener attached before that
    # would see the write as a user options change and reload the entry
    # mid-setup.
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))

    return True


async def _async_options_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload only when options changed, not when tokens were persisted."""
    coordinator: OrionDataUpdateCoordinator | None = getattr(
        entry, "runtime_data", None
    )
    if coordinator is not None and entry.options == coordinator.options:
        return
    if coordinator is not None and coordinator.reload_started:
        return
    if coordinator is not None:
        coordinator.reload_started = True
    try:
        reloaded = await hass.config_entries.async_reload(entry.entry_id)
        if not reloaded and coordinator is not None:
            coordinator.reload_started = False
    except Exception:
        if coordinator is not None:
            coordinator.reload_started = False
        raise


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        coordinator: OrionDataUpdateCoordinator | None = getattr(
            entry, "runtime_data", None
        )
        if coordinator is not None:
            # Close the live-device WebSockets cleanly (code 1001), matching
            # the Android app's background-shutdown behavior.
            await coordinator.async_shutdown()
    return unloaded


def _async_register_recovery_service(hass: HomeAssistant) -> None:
    """Expose the one-click undo for the 3.0 entity re-key.

    Domain-level and idempotent. Rolling back to 2.x leaves every re-keyed
    entity unavailable with its history stranded on it, because 2.x asks
    for ids that no longer exist and builds fresh ones instead. It must run
    under 3.x before Home Assistant is restarted into 2.x.
    """
    async def _admin_entry(call: ServiceCall) -> ConfigEntry:
        user = (
            await hass.auth.async_get_user(call.context.user_id)
            if call.context.user_id
            else None
        )
        if user is None or not user.is_admin:
            raise Unauthorized(call.context)

        entry = hass.config_entries.async_get_entry(call.data["config_entry_id"])
        if entry is None or entry.domain != DOMAIN:
            raise HomeAssistantError("Orion config entry not found")
        return entry

    async def _handle_revert(call: ServiceCall) -> None:
        entry = await _admin_entry(call)
        # Set the guard before unloading. A pending retry or an update
        # listener must not start the forward migration between unload and
        # the reverse registry transaction.
        hass.config_entries.async_update_entry(
            entry,
            data={**entry.data, CONF_UID_RECOVERY_ACTIVE: True},
        )
        # Also unload from SETUP_RETRY and SETUP_ERROR. Guarding on LOADED
        # alone left a pending retry timer armed and running against the
        # registry while the reverse transaction was in flight.
        if entry.state in _UNLOADABLE_STATES:
            unloaded = await hass.config_entries.async_unload(entry.entry_id)
            if not unloaded:
                raise HomeAssistantError("Could not unload Orion before recovery")

        result = async_revert_unique_ids(hass, entry)
        if not result.complete:
            raise HomeAssistantError(
                f"Orion recovery is incomplete: {result.remaining} entity "
                "mappings could not be returned to their pre-3.0 ids. Resolve "
                "the conflicting entity registry rows and run this again "
                "before installing 2.x"
            )
        if not result.identity_restored:
            # Cosmetic to 2.x, so it does not fail the run. Worth saying,
            # because the entry will not be recognised by its typed address.
            _LOGGER.warning(
                "Orion entities are ready for downgrade, but this entry kept "
                "its account-based identity because another entry already "
                "holds the address it was set up with"
            )
        _LOGGER.info(
            "Reverted %d Orion entities to their pre-3.0 ids. The entry is "
            "unloaded and ready for downgrade",
            result.reverted,
        )

    async def _handle_resume(call: ServiceCall) -> None:
        entry = await _admin_entry(call)
        device_ids = {str(value) for value in entry.data.get(CONF_DEVICE_IDS) or []}
        if overlapping_entry_ids(hass, entry.entry_id, device_ids):
            raise HomeAssistantError(
                "Cannot resume Orion 3.x while another entry owns the same bed"
            )
        account_id = str(entry.data.get(CONF_ACCOUNT_ID) or "")
        if account_id and entry_identity_conflict(hass, entry, account_id):
            raise HomeAssistantError(
                "Cannot resume Orion 3.x while another entry owns this account"
            )
        data = dict(entry.data)
        data.pop(CONF_UID_RECOVERY_ACTIVE, None)
        hass.config_entries.async_update_entry(entry, data=data)
        # Reload, not setup. `async_setup` refuses anything that is not
        # NOT_LOADED, and the likeliest way to reach this action is to
        # prepare a downgrade, restart Home Assistant, and change your
        # mind. That restart leaves the entry in SETUP_ERROR, where the
        # documented escape hatch used to raise OperationNotAllowed and
        # strand the entry with no supported way back.
        if entry.state is not ConfigEntryState.LOADED:
            if not await hass.config_entries.async_reload(entry.entry_id):
                raise HomeAssistantError("Orion could not resume 3.x setup")

    if not hass.services.has_service(DOMAIN, SERVICE_REVERT_UNIQUE_IDS):
        hass.services.async_register(
            DOMAIN,
            SERVICE_REVERT_UNIQUE_IDS,
            _handle_revert,
            schema=SERVICE_REVERT_SCHEMA,
        )
    if not hass.services.has_service(DOMAIN, SERVICE_RESUME_UNIQUE_IDS):
        hass.services.async_register(
            DOMAIN,
            SERVICE_RESUME_UNIQUE_IDS,
            _handle_resume,
            schema=SERVICE_REVERT_SCHEMA,
        )
