"""What a WebSocket frame is and is not evidence of.

A frame proves one bed's live state is fresh. It proves nothing about
`/v2/insights`, the schedule endpoint, the live session, or the account
still being authenticated. Two rounds got this wrong in opposite
directions: one made every entity permanently unavailable after a single
failed poll, the next made every entity permanently available regardless
of poll health.
"""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntryState
from homeassistant.helpers.update_coordinator import UpdateFailed
from orion_sleep_api import OrionApiError

from tests_ha.conftest import SERIAL_A, make_entry


async def _loaded(hass, patched):
    entry = make_entry(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    return entry


async def test_a_push_does_not_erase_why_the_poll_failed(hass, patched, client, ws_manager):
    """`last_exception` is the only record of the cause.

    Home Assistant reads it when reporting a failed refresh. A socket
    frame arriving two seconds later must not delete it, or the reason a
    setup or a poll failed is gone before anyone can see it.
    """
    entry = await _loaded(hass, patched)
    coordinator = entry.runtime_data

    client.fail_insights = OrionApiError("vendor 500")
    coordinator.async_set_updated_data  # noqa: B018 - documents the contrast
    coordinator.last_exception = UpdateFailed("poll blew up")
    coordinator.last_update_success = False

    coordinator._async_push_without_rescheduling(dict(coordinator.data or {}))

    assert coordinator.last_exception is not None, (
        "a pushed frame erased the record of why the poll failed"
    )


async def test_a_push_does_not_declare_the_whole_poll_healthy(
    hass, patched, client, ws_manager
):
    """A frame from one bed says nothing about insights or schedules."""
    entry = await _loaded(hass, patched)
    coordinator = entry.runtime_data
    coordinator.last_update_success = False

    coordinator._async_push_without_rescheduling(dict(coordinator.data or {}))

    assert coordinator.last_update_success is False, (
        "a live frame marked the entire coordinator healthy, so every "
        "insight and schedule entity reports available on stale data"
    )


async def test_live_entities_stay_available_on_a_fresh_socket(
    hass, patched, client, ws_manager
):
    """The failure the push fix exists to prevent, still prevented.

    A poll failure must not black out entities the socket is still
    feeding, even though it correctly blacks out the ones it is not.
    """
    entry = await _loaded(hass, patched)
    coordinator = entry.runtime_data
    ws_manager._fresh.add(SERIAL_A)
    coordinator.last_update_success = False
    coordinator.async_update_listeners()
    await hass.async_block_till_done()

    live = hass.states.get("climate.sleepy_alice_example_com_climate")
    assert live is not None, "expected a live climate entity"
    assert live.state != "unavailable", (
        "a live-fed entity went unavailable while its socket was fresh"
    )


async def test_poll_fed_entities_do_go_unavailable_on_a_failed_poll(
    hass, patched, client, ws_manager
):
    """The other half. Insight sensors are not socket-fed."""
    entry = await _loaded(hass, patched)
    coordinator = entry.runtime_data
    ws_manager._fresh.add(SERIAL_A)
    coordinator.last_update_success = False
    coordinator.async_update_listeners()
    await hass.async_block_till_done()

    score = hass.states.get("sensor.sleepy_alice_example_com_sleep_score")
    assert score is not None, "expected a sleep score sensor"
    assert score.state == "unavailable", (
        "an insight sensor reported available while its poll was failing, so "
        "it is presenting stale data as current"
    )
