"""The escape hatch has to apply itself.

`CONF_ALLOW_UNVERIFIED_ACCOUNT` exists for one lockout and one only.
Setup requires the profile to carry the address the entry was created
with. A profile carrying none of `email`, `phone` and `phone_number`
fails that, the reauth flow the failure launches applies the same test,
and the entry is stuck. That endpoint has been measured returning
`{"response": null}`, so the shape is observed rather than hypothetical.

The option was reachable from the options flow and did nothing on the
entry that needed it. Every other options change in this integration
applies through the update listener registered by `async_setup_entry`,
and that registration is the LAST statement of a SUCCESSFUL setup. A
locked-out entry never reaches it, so `entry.update_listeners` is empty,
the options write fires nothing, and the setting sits saved and
unapplied. The shipped remedy was a paragraph of `strings.json` telling
the household to go and reload by hand.

The fix is `hass.config_entries.async_schedule_reload` from the options
flow when the value actually changed. The alternatives were checked and
are closed: registering the listener early via `entry.async_on_unload` is
undone by `config_entries.py` calling `_async_process_on_unload` in the
`finally` of a failed setup, registering it bare leaks one listener per
SETUP_RETRY with nothing owning the unsubscribe, and
`OptionsFlowWithReload` refuses outright when `entry.update_listeners` is
non-empty, which it is.

The guard on an actual change is not cosmetic and the last tests here are
what hold it. This runs on EVERY options save, including a polling
interval edit on a healthy entry that already has a listener, and an
unguarded call would tear down and rebuild every per-device socket a
second time for nothing.
"""

from __future__ import annotations

from unittest.mock import patch

from homeassistant.config_entries import ConfigEntryState

from custom_components.orion_sleep.const import (
    CONF_ACCOUNT_ID,
    CONF_ALLOW_UNVERIFIED_ACCOUNT,
    CONF_INSIGHTS_DAYS,
    CONF_SCAN_INTERVAL,
)
from tests_ha.conftest import ACCOUNT, make_entry

# A profile with an id and no address at all. This is the exact shape
# that fails `profile_carries_address` in the branch where no account id
# has ever been recorded, which is the branch with no way out.
NO_ADDRESS_PROFILE = {"id": ACCOUNT}


async def locked_out(hass, client):
    """An entry in the lockout the escape hatch exists for.

    Deliberately built by making setup fail for the real reason rather
    than by forcing a state, because the whole finding is about what a
    FAILED setup leaves behind: no update listener.
    """
    client.user = dict(NO_ADDRESS_PROFILE)
    entry = make_entry(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is not ConfigEntryState.LOADED, (
        "the entry loaded, so the lockout this option exists for is not "
        "reproduced and every assertion below is meaningless"
    )
    assert not entry.update_listeners, (
        "a failed setup left an update listener registered, so the options "
        "write would reload the entry on its own and this test cannot tell "
        "whether the fix does anything"
    )
    return entry


async def set_options(hass, entry, **changes):
    """Drive the real options flow to a save."""
    flow = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        flow["flow_id"],
        {
            CONF_SCAN_INTERVAL: 300,
            CONF_INSIGHTS_DAYS: 7,
            "partner_action": "keep",
            "edit_aliases": False,
            **changes,
        },
    )
    assert result["type"] == "create_entry", result
    await hass.async_block_till_done()
    return result


async def test_turning_the_hatch_on_loads_the_entry_with_no_manual_reload(
    hass, patched, client, ws_manager
):
    """The finding.

    Against the pre-fix code the option is written and the entry stays in
    its failed state, because nothing was listening. The household is left
    with a setting that reads as applied and is not.
    """
    entry = await locked_out(hass, client)

    await set_options(hass, entry, **{CONF_ALLOW_UNVERIFIED_ACCOUNT: True})

    assert entry.options.get(CONF_ALLOW_UNVERIFIED_ACCOUNT) is True, (
        "the option was not even saved, so this test is measuring the "
        "wrong thing"
    )
    assert entry.state is ConfigEntryState.LOADED, (
        "the escape hatch was saved and the entry did not reload, so the "
        "one documented way out of this lockout still requires the "
        "household to know to go and reload the entry by hand"
    )


async def test_the_hatch_actually_records_the_account(
    hass, patched, client, ws_manager
):
    """Loading is not the point. Getting unstuck is.

    The option lets an entry with no recorded account id write the id the
    server just returned, which is what makes the NEXT boot take the
    recorded-id branch and stop needing the hatch at all. An entry that
    loads without recording anything is still stuck, just quietly.
    """
    entry = await locked_out(hass, client)

    await set_options(hass, entry, **{CONF_ALLOW_UNVERIFIED_ACCOUNT: True})

    assert entry.data.get(CONF_ACCOUNT_ID) == ACCOUNT, (
        "the entry loaded but recorded no account, so it depends on the "
        f"hatch forever: {entry.data.get(CONF_ACCOUNT_ID)}"
    )


async def test_turning_it_back_off_also_applies(hass, patched, client, ws_manager):
    """Both directions, because the strings tell the household to do this.

    `data_description` says to turn it back off afterwards. If only the
    on-transition reloads, that instruction leaves the entry running with
    the assertion relaxed while the UI shows it switched off.
    """
    entry = await locked_out(hass, client)
    await set_options(hass, entry, **{CONF_ALLOW_UNVERIFIED_ACCOUNT: True})
    assert entry.state is ConfigEntryState.LOADED

    # Now that an account id is recorded, the profile can stay addressless
    # and the entry still loads on the recorded-id branch, which the hatch
    # never reaches. That is the self-limiting property the coordinator
    # docstring claims, exercised rather than asserted.
    before = entry.runtime_data

    await set_options(hass, entry, **{CONF_ALLOW_UNVERIFIED_ACCOUNT: False})

    assert entry.options.get(CONF_ALLOW_UNVERIFIED_ACCOUNT) is False
    assert entry.state is ConfigEntryState.LOADED
    assert entry.runtime_data is not before, (
        "switching the hatch back off did not reload, so the entry keeps "
        "running with the setting the household believes they just cleared"
    )


async def test_an_unrelated_options_save_does_not_schedule_a_reload(
    hass, patched, client, ws_manager
):
    """The guard, on a healthy entry.

    This entry HAS an update listener, so the listener already reloads it
    on any options change. An unguarded `async_schedule_reload` here would
    reload it a second time for every polling interval edit, tearing down
    and rebuilding every per-device socket for nothing.

    Spied on `async_schedule_reload` specifically rather than on reload
    counts, because the listener's own reload is correct and expected and
    counting both together cannot tell them apart.
    """
    entry = make_entry(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    assert entry.update_listeners, "no listener, so this is not the healthy case"

    with patch.object(
        hass.config_entries, "async_schedule_reload"
    ) as scheduled:
        await set_options(hass, entry, **{CONF_SCAN_INTERVAL: 321})

    assert not scheduled.called, (
        "an options save that never touched the escape hatch scheduled a "
        "reload, which duplicates the one the update listener already does"
    )
    assert entry.runtime_data.options[CONF_SCAN_INTERVAL] == 321, (
        "the listener no longer applies an ordinary options change either"
    )


async def test_a_hatch_change_on_a_loaded_entry_is_left_to_the_listener(
    hass, patched, client, ws_manager
):
    """The listener guard, on the one save that would otherwise double up.

    The value guard cannot cover this, because here the value genuinely
    did change. A loaded entry already reloads on any options change
    through the listener `async_setup_entry` registers, so scheduling a
    second reload tears down and rebuilds every per-device socket again
    for nothing.

    The second half is the part that matters more. Declining to schedule
    must not mean declining to apply, or the guard has reintroduced the
    bug on exactly the entries that were never broken.
    """
    entry = make_entry(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.update_listeners, "no listener, so this is not the healthy case"
    before = entry.runtime_data

    with patch.object(
        hass.config_entries, "async_schedule_reload"
    ) as scheduled:
        await set_options(hass, entry, **{CONF_ALLOW_UNVERIFIED_ACCOUNT: True})

    assert not scheduled.called, (
        "a loaded entry was scheduled for a reload it was already getting "
        "from its own update listener, so every hatch toggle reloads twice"
    )
    assert entry.options.get(CONF_ALLOW_UNVERIFIED_ACCOUNT) is True
    assert entry.state is ConfigEntryState.LOADED
    assert entry.runtime_data is not before, (
        "declining to schedule a reload also stopped the change being "
        "applied, so the guard broke the healthy path it was protecting"
    )


async def test_the_first_save_on_an_entry_without_the_key_is_not_a_change(
    hass, patched, client, ws_manager
):
    """The value guard, with the listener guard deliberately out of the way.

    Run on a LOCKED OUT entry on purpose. A healthy entry is short
    circuited by the listener guard above, so asserting the value guard
    there proves nothing about the value guard: removing it entirely
    would leave that test green. Here there is no listener, so this
    assertion has only one thing holding it up.

    Every entry created before this option existed has no key for it, and
    that is the state under test. The comparison has to use the same
    default the form defaults the field from, or the very first options
    save on each of those entries reads as switching the hatch off and
    reloads a working entry for nothing.
    """
    entry = await locked_out(hass, client)
    assert CONF_ALLOW_UNVERIFIED_ACCOUNT not in entry.options

    with patch.object(
        hass.config_entries, "async_schedule_reload"
    ) as scheduled:
        await set_options(hass, entry, **{CONF_ALLOW_UNVERIFIED_ACCOUNT: False})

    assert not scheduled.called, (
        "the first options save on an entry predating this option was "
        "treated as switching it off, and reloaded"
    )


async def test_resaving_the_same_value_on_a_listenerless_entry_is_not_a_change(
    hass, patched, client, ws_manager
):
    """Re-saving without changing it is not a change.

    The field is `vol.Required` with a default read from the entry, so it
    is submitted on EVERY options save whether or not anybody touched it.
    Comparing presence instead of value would make every save a hatch
    change.

    The entry here is broken for a reason the hatch cannot fix, which is
    what keeps it listenerless while still holding the option switched on.
    A profile with no id is refused outright, deliberately and separately
    from the address check, because every person-scoped entity is keyed on
    that id.
    """
    client.user = {}
    entry = make_entry(hass)
    hass.config_entries.async_update_entry(
        entry, options={CONF_ALLOW_UNVERIFIED_ACCOUNT: True}
    )
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is not ConfigEntryState.LOADED
    assert not entry.update_listeners, "not the listenerless case"

    with patch.object(
        hass.config_entries, "async_schedule_reload"
    ) as scheduled:
        await set_options(hass, entry, **{CONF_ALLOW_UNVERIFIED_ACCOUNT: True})

    assert not scheduled.called, (
        "re-submitting the value the entry already held was treated as a "
        "change, so an unrelated options save reloads a broken entry on a "
        "loop"
    )


async def test_the_options_are_written_before_the_reload_runs(
    hass, patched, client, ws_manager
):
    """The ordering, which is the thing that silently made this useless.

    This was originally left to `OptionsFlowManager.async_finish_flow`,
    which writes the options once the flow step returns, on the reasoning
    that a task cannot begin before its scheduler yields. Measured, the
    reload ran FIRST. It set the entry up against the options as they were
    before the save, failed the identical account check, and left the
    hatch stored and unapplied, which looks exactly like the bug being
    fixed and passes every assertion about the option being saved.

    So the write is now done by the flow, immediately before the schedule.
    Asserted at the moment of scheduling rather than inferred from the end
    state, because the end state is also reachable by luck if some future
    Home Assistant happens to order things favourably. The contract is
    that the options are on the entry BEFORE anything is asked to reload
    against them.
    """
    entry = await locked_out(hass, client)

    seen: list[object] = []
    real = hass.config_entries.async_schedule_reload

    def _record(entry_id: str) -> None:
        seen.append(entry.options.get(CONF_ALLOW_UNVERIFIED_ACCOUNT))
        real(entry_id)

    with patch.object(
        hass.config_entries, "async_schedule_reload", side_effect=_record
    ):
        await set_options(hass, entry, **{CONF_ALLOW_UNVERIFIED_ACCOUNT: True})

    assert seen, "the reload was never scheduled"
    assert seen == [True], (
        "a reload was scheduled while the entry still held the old "
        "options. Whether it wins the race is not something this "
        f"integration controls: {seen}"
    )
    assert entry.state is ConfigEntryState.LOADED, (
        "the reload ran against options that did not yet contain the "
        "escape hatch, so setup failed the same check again and the "
        "setting is saved and unapplied"
    )
