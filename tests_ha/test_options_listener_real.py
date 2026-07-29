"""The options listener must never reload an entry it cannot account for.

`async_update_entry` does not await update listeners. It dispatches them
as tasks. So the listener registered by `async_setup_entry` can run at a
moment the caller never intended, and the moment that matters is inside
`_handle_revert`.

That handler sets `CONF_UID_RECOVERY_ACTIVE` before unloading, precisely
so nothing restarts the forward migration in the gap. The write that sets
the guard is what schedules this listener. The listener then had to
survive running after the unload, and it did not: every early return was
conditioned on `coordinator is not None`, and Home Assistant clears
`runtime_data` the moment an entry leaves LOADED. A None coordinator
therefore fell through all three guards into `async_reload`, racing
`async_revert_unique_ids` over the same registry rows.

Landing before the `finally` pops the latch makes setup raise and leaves
the entry broken. Landing after it makes setup succeed and run the
forward migration on top of a half finished revert, producing the
two-generation registry the migration documents as the one shape a revert
cannot resolve.

The last test here is a positive control and is not optional. Both
guards can be satisfied by a listener that never reloads anything, which
would break every options change in the integration while turning this
file green.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

from homeassistant.config_entries import ConfigEntryState

from custom_components.orion_sleep import _async_options_updated
from custom_components.orion_sleep.const import (
    CONF_INSIGHTS_DAYS,
    CONF_SCAN_INTERVAL,
    CONF_UID_RECOVERY_ACTIVE,
)
from tests_ha.conftest import make_entry


async def loaded_entry(hass):
    entry = make_entry(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    return entry


def reload_spy(hass):
    return patch.object(
        hass.config_entries, "async_reload", new=AsyncMock(return_value=True)
    )


async def test_the_listener_is_actually_registered(hass, patched):
    """Otherwise every assertion below is about a function nothing calls."""
    entry = await loaded_entry(hass)
    assert _async_options_updated in entry.update_listeners


async def test_a_latched_entry_is_never_reloaded_by_the_listener(hass, patched):
    """The exact state `_handle_revert` creates on its second statement.

    Latch set, entry unloaded, `runtime_data` cleared by Home Assistant.
    A reload from here runs `async_setup_entry` against rows the reverse
    registry transaction is renaming.
    """
    entry = await loaded_entry(hass)
    hass.config_entries.async_update_entry(
        entry, data={**entry.data, CONF_UID_RECOVERY_ACTIVE: True}
    )
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert getattr(entry, "runtime_data", None) is None, (
        "Home Assistant no longer clears runtime_data on unload, so this "
        "test is not reproducing the race any more"
    )

    with reload_spy(hass) as reload:
        await _async_options_updated(hass, entry)

    assert not reload.called, (
        "the options listener reloaded an entry the recovery service had "
        "latched and unloaded, so setup now races the reverse registry "
        "transaction over the same rows"
    )


async def test_an_unloaded_entry_is_never_reloaded_by_the_listener(hass, patched):
    """The same guard without the latch, because it is a separate hole.

    Anything that unloads an entry owns what happens next. A listener
    that reloads it because it cannot find a coordinator is inventing a
    decision from missing information.
    """
    entry = await loaded_entry(hass)
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    with reload_spy(hass) as reload:
        await _async_options_updated(hass, entry)

    assert not reload.called, (
        "the options listener reloaded an entry that was already unloaded, "
        "purely because runtime_data was missing"
    )


async def test_a_real_options_change_still_reloads(hass, patched):
    """Positive control. Failing closed must not mean failing always."""
    entry = await loaded_entry(hass)
    before = entry.runtime_data

    hass.config_entries.async_update_entry(
        entry,
        options={**entry.options, CONF_SCAN_INTERVAL: 321, CONF_INSIGHTS_DAYS: 5},
    )
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED
    assert entry.runtime_data is not before, (
        "changing the scan interval no longer reloads the entry, so the "
        "fail-closed guards have disabled the listener entirely"
    )
    assert entry.runtime_data.options[CONF_SCAN_INTERVAL] == 321
