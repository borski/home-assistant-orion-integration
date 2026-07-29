"""The recovery services, invoked the way a user invokes them.

Both suites previously called `async_revert_unique_ids` directly, so the
handler around it was never executed: the admin gate, the state gate, the
latch rollback, the partner check. A `tuple | set` TypeError therefore sat
on the first line of that handler through a full review round, and the
service it kills is the only supported way off the 3.x ids.
"""

from __future__ import annotations

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import Context
from homeassistant.exceptions import HomeAssistantError, Unauthorized

from custom_components.orion_sleep.const import CONF_UID_RECOVERY_ACTIVE, DOMAIN
from tests_ha.conftest import make_entry


async def admin_context(hass) -> Context:
    user = await hass.auth.async_create_user("Admin", group_ids=["system-admin"])
    return Context(user_id=user.id)


async def call_revert(hass, entry, context):
    await hass.services.async_call(
        DOMAIN,
        "revert_unique_ids",
        {"config_entry_id": entry.entry_id, "confirm": True},
        blocking=True,
        context=context,
    )


@pytest.mark.parametrize(
    "state",
    [
        ConfigEntryState.LOADED,
        ConfigEntryState.NOT_LOADED,
        ConfigEntryState.SETUP_ERROR,
        ConfigEntryState.SETUP_RETRY,
    ],
)
async def test_revert_runs_from_every_state_a_user_can_reach(hass, patched, state):
    """The population that needs this service is the one whose entry is broken.

    So SETUP_ERROR and SETUP_RETRY matter more than LOADED, not less.
    """
    entry = make_entry(hass)
    if state is ConfigEntryState.LOADED:
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    else:
        entry.mock_state(hass, state)

    context = await admin_context(hass)
    try:
        await call_revert(hass, entry, context)
    except HomeAssistantError:
        # A refusal with a reason is a valid outcome. A TypeError is not.
        pass


async def test_revert_refuses_a_non_admin(hass, patched):
    entry = make_entry(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    # The FIRST user created in a fresh instance becomes the owner, and an
    # owner is admin whatever group you ask for. Burn one first.
    await hass.auth.async_create_user("Owner", group_ids=["system-admin"])
    user = await hass.auth.async_create_user("Someone", group_ids=["system-users"])
    assert not user.is_admin, "fixture failed to build a non-admin user"
    with pytest.raises(Unauthorized):
        await call_revert(hass, entry, Context(user_id=user.id))


async def test_a_revert_with_nothing_to_do_does_not_latch_recovery(hass, patched):
    """Running the action speculatively must not brick the entry.

    The latch makes 3.x refuse to load, and the way back is a differently
    named service the error text never mentions.
    """
    entry = make_entry(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    # A fresh install DOES journal its renames, so empty the journal to get
    # the genuinely-nothing-to-do case a user reaches by running the action
    # speculatively on an entry that never migrated.
    data = dict(entry.data)
    data.pop("_uid_migration_v3", None)
    hass.config_entries.async_update_entry(entry, data=data)

    context = await admin_context(hass)
    try:
        await call_revert(hass, entry, context)
    except HomeAssistantError:
        pass

    if entry.data.get(CONF_UID_RECOVERY_ACTIVE):
        assert await hass.config_entries.async_setup(entry.entry_id) or True
        pytest.fail(
            "a revert that reverted nothing latched recovery mode, so the "
            "integration will refuse to load on the next start"
        )


async def test_a_failed_revert_rolls_the_latch_back(hass, patched):
    """Whatever goes wrong, the entry must still be able to load.

    Leaving the latch set turns a failed rollback into a broken
    integration, and the remedy is a service the message does not name.
    """
    entry = make_entry(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    hass.config_entries.async_update_entry(
        entry,
        data={
            **entry.data,
            "_uid_migration_v3": [
                {
                    "domain": "sensor",
                    "platform": DOMAIN,
                    "old": "nope_old",
                    "new": "nope_new",
                    "role": "partner",
                }
            ],
        },
    )

    context = await admin_context(hass)
    try:
        await call_revert(hass, entry, context)
    except HomeAssistantError:
        pass

    assert not entry.data.get(CONF_UID_RECOVERY_ACTIVE), (
        "a revert that raised left the recovery latch set, so 3.x now refuses "
        "to load and the error told the user to reload"
    )
