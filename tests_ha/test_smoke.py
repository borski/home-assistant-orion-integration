"""Does the harness actually load the integration."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntryState

from tests_ha.conftest import make_entry


async def test_the_integration_sets_up_against_real_home_assistant(hass, patched):
    entry = make_entry(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED
    entities = [s for s in hass.states.async_all() if "sleepy" in s.entity_id]
    assert entities, "no Orion entities were created"
