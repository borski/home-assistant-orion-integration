"""An unprepared downgrade has to fail closed.

3.0 shipped a documented downgrade procedure and no way to enforce it.
`revert_unique_ids` exists, the repair issue points at it, the README
explains it, and none of that stops somebody installing 2.x without
running it.

Home Assistant does have a mechanism: it refuses to load a config entry
whose stored version is higher than the running integration's. Both 2.x
and 3.0 shipped `VERSION = 1`, so the check could never fire. An
unprepared downgrade loaded normally, 2.x asked for unique ids that had
been renamed out from under it, found nothing, and built a second entity
for every key. History stayed on the 3.x rows and new data went to the
replacements.

So 3.1 stores version 2, and `revert_unique_ids` lowers it back to 1 as
part of preparing a downgrade. The guard does not block downgrades. It
blocks downgrades that skipped the step which makes them safe.
"""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import Context
from homeassistant.helpers import entity_registry as er

from custom_components.orion_sleep.config_flow import OrionSleepConfigFlow
from custom_components.orion_sleep.const import (
    CONF_UID_MIGRATION,
    CONF_UID_RECOVERY_ACTIVE,
    DOMAIN,
)
from tests_ha.conftest import make_entry


async def admin_context(hass) -> Context:
    user = await hass.auth.async_create_user("Admin", group_ids=["system-admin"])
    return Context(user_id=user.id)


async def revert(hass, entry) -> None:
    await hass.services.async_call(
        DOMAIN,
        "revert_unique_ids",
        {"config_entry_id": entry.entry_id, "confirm": True},
        blocking=True,
        context=await admin_context(hass),
    )


async def resume(hass, entry) -> None:
    await hass.services.async_call(
        DOMAIN,
        "resume_unique_ids",
        {"config_entry_id": entry.entry_id, "confirm": True},
        blocking=True,
        context=await admin_context(hass),
    )


async def test_a_2_x_entry_is_migrated_up_to_the_guarded_version(hass, patched):
    """The upgrade half.

    Every entry in the field stores version 1, so the guard only starts
    protecting anyone once they have been moved.
    """
    entry = make_entry(hass)
    assert entry.version == 1, "fixture did not start where a 2.x entry does"

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED
    assert entry.version == 2, (
        "the entry stayed on the version 2.x also uses, so installing 2.x "
        "would load it and split every entity's history"
    )


async def test_the_running_version_is_higher_than_the_entry_it_guards(hass, patched):
    """The guard itself, stated as the property that makes it work.

    Home Assistant compares `entry.version` against the config flow's
    `VERSION` and refuses when the stored one is higher. That is a fact
    about core rather than about this integration, so what is asserted
    here is the thing this integration controls: that 3.1 writes a
    version 2.x will reject.
    """
    entry = make_entry(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert OrionSleepConfigFlow.VERSION == 2
    assert entry.version > 1, (
        "a 2.x install runs VERSION = 1 and only refuses an entry stored "
        "ABOVE that. Storing 1 leaves the downgrade unguarded"
    )


async def test_preparing_a_downgrade_lowers_the_version_again(hass, patched):
    """The other end, and the reason the guard is not just an obstacle.

    Somebody who runs the documented action is doing the safe thing, and
    the entry has to be loadable by 2.x afterwards. A guard that blocked
    the prepared path too would only teach people to delete the entry.
    """
    entry = make_entry(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.data.get(CONF_UID_MIGRATION), "fresh install journalled nothing"
    assert entry.version == 2

    await revert(hass, entry)

    assert entry.version == 1, (
        "the entry stayed on a version 2.x refuses, so the supported "
        "downgrade path is blocked by the guard meant to protect it"
    )
    assert entry.data.get(CONF_UID_RECOVERY_ACTIVE), (
        "the revert did not latch, so this is not the prepared state"
    )


async def test_changing_your_mind_puts_the_guard_back(hass, patched):
    """Prepare, then resume.

    `resume_unique_ids` deliberately does not write the version itself.
    The reload it performs runs `async_migrate_entry`, which raises it,
    and a second copy of that rule in the resume handler is how the two
    ends of this drift apart.
    """
    entry = make_entry(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    await revert(hass, entry)
    assert entry.version == 1

    await resume(hass, entry)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED
    assert entry.version == 2, (
        "the household changed its mind and went back to 3.x, but the "
        "entry stayed on the version that lets 2.x take it unprepared"
    )
    # And the entities came back with it, which is what makes resuming
    # different from never having prepared.
    rows = er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)
    assert rows, "resuming left the entry with no entities at all"
