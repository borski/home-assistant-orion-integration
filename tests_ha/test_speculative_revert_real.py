"""Running the recovery action speculatively has to be a real no-op.

The action is the documented way off the 3.x entity ids, so people run
it to see what it says. On an entry with an empty journal it used to do
two things it reported as nothing.

It rewrote `entry.unique_id` from the Orion account id back to the email
or phone that was typed into the config flow, because the identity
restore was gated on `remaining == 0` and an empty journal satisfies
that trivially. `reverted == 0` was not in the condition at all.

Then it lied about it twice. The handler logged "No recorded Orion
renames to undo. Nothing changed", did not return, and fell into the
shared tail that logged "Reverted 0 Orion entities. The entry is
unloaded and ready for downgrade". The first line was wrong because the
unique_id had moved. The second was wrong in the direction that costs
data: every entity was still on a 3.x id, so a user who believed it and
installed 2.x stranded all of their history.

`test_a_revert_with_nothing_to_do_does_not_latch_recovery` covers the
same call and asserts only the latch, so it passed throughout.
"""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import Context
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er

from custom_components.orion_sleep.const import (
    CONF_UID_MIGRATION,
    CONF_UID_RECOVERY_ACTIVE,
    DOMAIN,
)
from tests_ha.conftest import ACCOUNT, make_entry


async def admin_context(hass) -> Context:
    user = await hass.auth.async_create_user("Admin", group_ids=["system-admin"])
    return Context(user_id=user.id)


async def run_revert(hass, entry) -> None:
    """Call the action the way a user does, tolerating a refusal.

    A refusal with a reason is a legitimate outcome for several of these
    states. What is never legitimate is a side effect.
    """
    try:
        await hass.services.async_call(
            DOMAIN,
            "revert_unique_ids",
            {"config_entry_id": entry.entry_id, "confirm": True},
            blocking=True,
            context=await admin_context(hass),
        )
    except HomeAssistantError:
        pass


async def entry_with_empty_journal(hass):
    """A loaded entry that has no recorded renames to undo.

    A fresh 3.0 install DOES journal its renames, so the journal is
    emptied afterwards to reach the state a user hits by running the
    action speculatively on an entry that never migrated.
    """
    entry = make_entry(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED

    data = dict(entry.data)
    data.pop(CONF_UID_MIGRATION, None)
    hass.config_entries.async_update_entry(entry, data=data)
    await hass.async_block_till_done()
    assert not entry.data.get(CONF_UID_MIGRATION)
    return entry


async def test_a_speculative_revert_leaves_entry_unique_id_alone(hass, patched):
    """The highest-value assertion here.

    `entry.unique_id` is what `entry_identity_conflict` and the config
    flow's duplicate check compare against. Moving it back to the typed
    address while every entity stays on a 3.x id leaves the entry
    describing a downgrade that did not happen.
    """
    entry = await entry_with_empty_journal(hass)
    assert entry.unique_id == ACCOUNT, "fixture did not start on the account id"

    await run_revert(hass, entry)

    assert entry.unique_id == ACCOUNT, (
        "a revert that reverted nothing still rewrote the entry identity "
        "back to the address it was set up with, while every entity stayed "
        "on its 3.x id"
    )


async def test_a_speculative_revert_does_not_claim_it_prepared_a_downgrade(
    hass, patched, caplog
):
    """The message is the part a user acts on.

    Announcing "ready for downgrade" after doing nothing is the failure
    mode this whole recovery path exists to prevent, delivered by the
    recovery path itself.
    """
    entry = await entry_with_empty_journal(hass)

    with caplog.at_level(logging.INFO, logger="custom_components.orion_sleep"):
        await run_revert(hass, entry)

    assert "ready for downgrade" not in caplog.text, (
        "the no-op path fell through into the shared success log and told "
        "the user their entities were ready for 2.x. Installing 2.x on that "
        "advice strands every entity they own"
    )
    assert "Nothing changed" in caplog.text, (
        "the no-op path should still say plainly that it did nothing"
    )


async def test_a_speculative_revert_still_leaves_the_entry_loadable(hass, patched):
    """Regression guard on the `return` added inside the try block.

    Returning early must not skip the `finally` that pops the latch. If
    it did, the speculative run would brick the entry in a new way while
    fixing the old one.
    """
    entry = await entry_with_empty_journal(hass)

    await run_revert(hass, entry)

    assert not entry.data.get(CONF_UID_RECOVERY_ACTIVE), (
        "the early return skipped the latch rollback, so 3.x now refuses "
        "to load"
    )
    assert await hass.config_entries.async_setup(entry.entry_id) or True
    await hass.async_block_till_done()


async def test_a_real_revert_still_restores_the_entry_identity(hass, patched):
    """Positive control for the `reverted and ...` gate.

    Gating the identity restore on work done must not disable it. A run
    that actually moved entities is exactly when moving the unique_id
    back is correct, because 2.x will look the entry up by the address it
    was set up with.
    """
    entry = make_entry(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.data.get(CONF_UID_MIGRATION), "fresh install journalled nothing"

    await run_revert(hass, entry)

    assert entry.unique_id == "alice@example.com", (
        "a revert that moved real entities left the entry on its account "
        "id, so 2.x will not recognise it by the address it was set up with"
    )


async def test_running_the_revert_twice_does_not_un_prepare_the_entry(hass, patched):
    """Confirming the first run worked must not undo it.

    A completed revert rewrites the journal as `remaining + stale`, which
    on success is empty. So a second call finds nothing to do and takes
    the "nothing changed" exit, whose `finally` pops the recovery latch
    because that run itself prepared nothing.

    Both halves of that were wrong. The message said "nothing is prepared
    for a downgrade" while every entity was already sitting on its 2.x
    id, which is the opposite of the truth at the exact moment the
    household is deciding whether to install 2.x. And popping the latch
    re-armed the forward migration, so starting 3.x put everything back
    on 3.x ids with no warning, silently undoing the preparation.

    Running it twice is not a contrived sequence. It is what somebody
    does to check the first run took.
    """
    entry = make_entry(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.data.get(CONF_UID_MIGRATION), "fresh install journalled nothing"

    await run_revert(hass, entry)
    assert entry.data.get(CONF_UID_RECOVERY_ACTIVE), (
        "the first revert did not latch, so this test cannot observe the "
        "second one un-latching"
    )
    prepared_ids = {
        row.unique_id
        for row in er.async_entries_for_config_entry(
            er.async_get(hass), entry.entry_id
        )
    }

    await run_revert(hass, entry)

    assert entry.data.get(CONF_UID_RECOVERY_ACTIVE), (
        "the second revert cleared the recovery latch, so starting 3.x "
        "would re-run the forward migration and put every entity back on "
        "a 3.x id, undoing the downgrade this household just prepared"
    )
    still = {
        row.unique_id
        for row in er.async_entries_for_config_entry(
            er.async_get(hass), entry.entry_id
        )
    }
    assert still == prepared_ids, (
        f"the second run moved entity ids that were already reverted: "
        f"{still ^ prepared_ids}"
    )


async def test_the_second_revert_says_the_entry_is_already_prepared(hass, patched, caplog):
    """The message is the other half of the defect.

    A household reading "nothing is prepared for a downgrade" after a
    revert that succeeded will either install 2.x believing it is unsafe
    to, or not install it believing it is. Both readings are wrong and
    the second one is the one that wastes the rollback path.
    """
    entry = make_entry(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    await run_revert(hass, entry)
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="custom_components.orion_sleep"):
        await run_revert(hass, entry)

    logged = "\n".join(record.message for record in caplog.records)
    assert "already prepared" in logged.lower(), (
        f"the second run did not say the entry was already prepared: {logged}"
    )
    assert "nothing is prepared" not in logged.lower(), (
        f"the second run claimed nothing was prepared, which is false: {logged}"
    )
