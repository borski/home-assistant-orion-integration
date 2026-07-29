"""`resume_unique_ids` is the way out, so it must not have dead ends.

While `CONF_UID_RECOVERY_ACTIVE` is set, `async_setup_entry` refuses and
`revert_unique_ids` has nothing left to undo. This action is the only
remaining move. Anything it refuses BEFORE popping the latch is
permanent, because the refusal leaves the entry in the exact state whose
only documented escape is the action that just refused.

That happened twice. A bed-ownership check was removed for it, and an
account-identity check with the same shape was left behind and did the
same thing to a different population: any entry whose Orion account is
also claimed by a sibling entry. `async_migrate_entry_identity` already
performs that check during the reload, after the latch is gone, with a
message that says to remove the duplicate.
"""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntryDisabler, ConfigEntryState
from homeassistant.core import Context
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.orion_sleep.const import (
    CONF_ACCOUNT_ID,
    CONF_AUTH_METHOD,
    CONF_AUTH_VALUE,
    CONF_UID_RECOVERY_ACTIVE,
    DOMAIN,
)
from tests_ha.conftest import ACCOUNT, make_entry


def account_rival(hass) -> MockConfigEntry:
    """A sibling that owns this Orion account id and nothing else.

    DISABLED on purpose. Setting up a config entry sets up every entry in
    its domain, so an enabled sibling loads, claims the shared bed, and
    the bed-ownership check in the coordinator's first refresh fires
    before the account check is ever reached. That would leave this file
    silently testing the wrong conflict.
    """
    entry = MockConfigEntry(
        domain=DOMAIN,
        entry_id="entry-rival",
        unique_id=ACCOUNT,
        disabled_by=ConfigEntryDisabler.USER,
        data={
            CONF_AUTH_METHOD: "email",
            CONF_AUTH_VALUE: "someone.else@example.com",
        },
    )
    entry.add_to_hass(hass)
    return entry


async def admin_context(hass) -> Context:
    user = await hass.auth.async_create_user("Admin", group_ids=["system-admin"])
    return Context(user_id=user.id)


async def call_resume(hass, entry) -> Exception | None:
    """Run the action, returning the failure instead of raising it.

    Whether it refuses is secondary here. What matters is that the latch
    is gone afterwards either way, because the latch is what makes a
    refusal permanent.
    """
    try:
        await hass.services.async_call(
            DOMAIN,
            "resume_unique_ids",
            {"config_entry_id": entry.entry_id, "confirm": True},
            blocking=True,
            context=await admin_context(hass),
        )
    except Exception as err:  # noqa: BLE001
        return err
    return None


async def test_a_latched_entry_whose_account_is_claimed_can_still_escape(
    hass, patched
):
    """The second dead end, in the state that reaches it.

    A sibling entry holds this entry's Orion account id. The removed
    check fired on exactly this shape, raised before the latch was
    popped, and left the entry latched forever.
    """
    account_rival(hass)
    latched = make_entry(
        hass,
        entry_id="entry-latched",
        unique_id="alice@example.com",
        data={CONF_ACCOUNT_ID: ACCOUNT, CONF_UID_RECOVERY_ACTIVE: True},
    )

    await hass.config_entries.async_setup(latched.entry_id)
    await hass.async_block_till_done()
    assert latched.state is ConfigEntryState.SETUP_ERROR, (
        "the latch is supposed to make 3.x refuse to load, so this test is "
        "not starting from the state it claims to test"
    )

    await call_resume(hass, latched)

    assert not latched.data.get(CONF_UID_RECOVERY_ACTIVE), (
        "resume refused an entry whose account is claimed elsewhere and did "
        "so before popping the latch, so the only way out of recovery mode "
        "is the action that just refused. The entry is stuck permanently"
    )


async def test_the_conflict_is_still_reported_by_the_migration(hass, patched):
    """Deleting the check must not delete the enforcement.

    `async_migrate_entry_identity` runs during the reload and refuses
    there instead, with a message that names the remedy. The entry still
    does not load, which is correct. It is simply no longer trapped.
    """
    account_rival(hass)
    latched = make_entry(
        hass,
        entry_id="entry-latched",
        unique_id="alice@example.com",
        data={CONF_ACCOUNT_ID: ACCOUNT, CONF_UID_RECOVERY_ACTIVE: True},
    )
    await hass.config_entries.async_setup(latched.entry_id)
    await hass.async_block_till_done()

    await call_resume(hass, latched)
    await hass.async_block_till_done()

    assert latched.state is not ConfigEntryState.LOADED, (
        "an entry whose Orion account is owned by a sibling loaded anyway, "
        "so removing the duplicate check removed the enforcement with it"
    )
    assert "duplicate entry" in str(latched.reason or ""), (
        "the account conflict is no longer reported by the migration, so "
        f"nothing tells the user what to remove: {latched.reason!r}"
    )


async def test_resume_still_works_for_an_entry_with_no_conflict(hass, patched):
    """Positive control. The action still has to actually resume."""
    latched = make_entry(
        hass,
        entry_id="entry-solo",
        unique_id=ACCOUNT,
        data={CONF_ACCOUNT_ID: ACCOUNT, CONF_UID_RECOVERY_ACTIVE: True},
    )
    await hass.config_entries.async_setup(latched.entry_id)
    await hass.async_block_till_done()
    assert latched.state is ConfigEntryState.SETUP_ERROR

    assert await call_resume(hass, latched) is None
    await hass.async_block_till_done()

    assert latched.state is ConfigEntryState.LOADED, (
        f"resume did not bring the entry back. reason={latched.reason!r}"
    )
    assert not latched.data.get(CONF_UID_RECOVERY_ACTIVE)
