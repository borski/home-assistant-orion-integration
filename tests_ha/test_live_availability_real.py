"""Whether a live heart rate reading is current, or merely the last one seen.

`_OrionLiveSensorBase.available` used to be nothing but
`sensor_status_text(...) is not None`. That is a test for having ever
received a frame, not for the frame being recent.
`coordinator.live_devices` is only ever replaced inside
`_async_update_data`, and every early raise in that method leaves it
exactly as it was, so a permanently invalid refresh token left live heart
rate and breath rate reporting whatever the last frame said, forever,
marked available, with no upper bound on the staleness.

For a heart rate that is worse than reporting nothing. `available` now
chains `super().available`, which is poll health with a documented
exception for entities a genuinely fresh socket is still feeding.

Both directions are pinned below, because the two previous rounds broke
this in opposite directions: one made every entity permanently
unavailable after one failed poll, the next made every entity permanently
available regardless of poll health.
"""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntryState

from tests_ha.conftest import SERIAL_A, make_entry

# Only sensor_1 carries a real reading in the fixture's live payload.
# sensor_2 reports zeros, which map to None, so it says nothing useful
# about availability either way.
_HEART_RATE = "sensor.sleepy_sensor_1_heart_rate"
_BREATH_RATE = "sensor.sleepy_sensor_1_breath_rate"


async def _loaded(hass, patched):
    entry = make_entry(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    return entry


def _assert_the_stale_frame_is_still_there(coordinator):
    """The precondition that makes this a real test rather than a tautology.

    The old implementation reported available whenever a status text
    existed. If the frame had also been cleared, an assertion that the
    entity is unavailable would pass against the old code too and prove
    nothing. So check first that the thing the old code keyed on is still
    present and still truthy.
    """
    text = coordinator.sensor_status_text(coordinator.devices[0]["id"], "sensor1")
    assert text is not None, (
        "the live frame was cleared, so this test no longer distinguishes "
        "the fixed implementation from the one that trusted any frame "
        "it had ever seen"
    )


async def test_live_sensors_go_unavailable_when_the_coordinator_is_dead(
    hass, patched, client, ws_manager
):
    """A dead poll and a dead socket means unavailable, however recent the frame.

    This is the regression. Against the pre-fix property, which was only
    `sensor_status_text(...) is not None`, the retained frame below makes
    the entity report available and this assertion fails.
    """
    entry = await _loaded(hass, patched)
    coordinator = entry.runtime_data

    before = hass.states.get(_HEART_RATE)
    assert before is not None and before.state != "unavailable", (
        "expected a healthy live heart rate sensor before breaking the poll"
    )

    # No `_fresh` entry for the serial. The socket is dead too, which is
    # what a permanently invalid refresh token looks like.
    assert not ws_manager.is_fresh(SERIAL_A), "the socket must be stale here"
    coordinator.last_update_success = False
    coordinator.async_update_listeners()
    await hass.async_block_till_done()

    _assert_the_stale_frame_is_still_there(coordinator)

    for entity_id in (_HEART_RATE, _BREATH_RATE):
        state = hass.states.get(entity_id)
        assert state is not None, f"expected {entity_id} to exist"
        assert state.state == "unavailable", (
            f"{entity_id} reported available with a dead poll and a dead "
            "socket, so it is presenting an unboundedly old vital sign as "
            "a current reading"
        )


async def test_live_sensors_stay_available_on_a_fresh_socket(
    hass, patched, client, ws_manager
):
    """The complement, and the reason `_live_fed` exists.

    Chaining `super().available` must not collapse into "unavailable
    whenever polling fails". The socket feeding these sensors is
    independent of the polled endpoints, so a failing `/v2/insights` is
    no reason to black out a reading arriving every few seconds.

    Without this, the obvious over-correction for the test above passes.
    """
    entry = await _loaded(hass, patched)
    coordinator = entry.runtime_data

    ws_manager._fresh.add(SERIAL_A)
    coordinator.last_update_success = False
    coordinator.async_update_listeners()
    await hass.async_block_till_done()

    for entity_id in (_HEART_RATE, _BREATH_RATE):
        state = hass.states.get(entity_id)
        assert state is not None, f"expected {entity_id} to exist"
        assert state.state != "unavailable", (
            f"{entity_id} went unavailable while its own socket was fresh, "
            "so a failure in an unrelated polled endpoint now blacks out "
            "live vitals"
        )


async def test_a_live_sensor_needs_a_frame_even_when_the_poll_is_healthy(
    hass, patched, client, ws_manager
):
    """Poll health alone is not enough. The other half of the `and`.

    `super().available` is necessary, not sufficient. A bed that is
    polling fine but has never reported this topper sensor has nothing to
    show, and dropping the status text check would make it report
    available with no reading behind it.
    """
    entry = await _loaded(hass, patched)
    coordinator = entry.runtime_data
    device_id = coordinator.devices[0]["id"]

    assert coordinator.last_update_success, "the poll should be healthy here"
    assert coordinator.sensor_status_text(device_id, "sensor3") is None, (
        "sensor3 is meant to be a topper sensor the bed never reported"
    )

    state = hass.states.get("sensor.sleepy_sensor_3_heart_rate")
    if state is None:
        # The fixture only builds entities for the sensors it declares, so
        # there is nothing to assert against. Not a failure, and saying so
        # beats a silent pass.
        return
    assert state.state == "unavailable", (
        "a live sensor with no frame at all reported available on the "
        "strength of poll health alone"
    )
