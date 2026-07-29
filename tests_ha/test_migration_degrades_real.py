"""A declined rename must cost the user history, never availability.

The migration refuses to rename an entity onto a unique_id something
else already holds, and that refusal is correct: forcing it merges two
entities' recorder history under one identity. What was not correct was
the price. The refusal raised `ConfigEntryError` from
`async_setup_entry`, before `async_forward_entry_setups`, so one squatted
id took down all nine platforms. Every climate control, every biometric
sensor, and every automation referencing any of them went unavailable and
stayed that way, with the only documented recovery being to hand-delete
entity registry rows named in a log line.

The registry decision is unchanged here. What changed is that the entity
which could not move stays where it is and keeps working, and the
conflict is reported as a repair issue instead of a dead integration.

The registry shape these tests build is the one the code documents
producing on itself: upgrade to 3.x, downgrade to 2.x without running
the revert action, then upgrade again. 2.x cannot find the 3.x ids, so it
mints a second row per key on the old id, and coming back to 3.x finds
both generations present.
"""

from __future__ import annotations

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import Context
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.setup import async_setup_component

from custom_components.orion_sleep import ISSUE_UNIQUE_ID_CONFLICT
from custom_components.orion_sleep.const import (
    CONF_ACCOUNT_ID,
    CONF_DEVICE_IDS,
    CONF_UID_RECOVERY_ACTIVE,
    DOMAIN,
)
from tests_ha.conftest import ACCOUNT, BED_A, make_entry

LEGACY_ID = f"{BED_A}_sleep_score"
ACCOUNT_ID = f"{BED_A}_user_{ACCOUNT}_sleep_score"


def issue_for(hass, entry) -> ir.IssueEntry | None:
    return ir.async_get(hass).async_get_issue(
        DOMAIN, f"{ISSUE_UNIQUE_ID_CONFLICT}_{entry.entry_id}"
    )


def rows(hass, entry) -> dict[str, str]:
    """unique_id to entity_id for everything this entry owns."""
    registry = er.async_get(hass)
    return {
        row.unique_id: row.entity_id
        for row in er.async_entries_for_config_entry(registry, entry.entry_id)
    }


def two_generation_entry(hass):
    """An entry carrying both the 2.x and the 3.x row for one key."""
    entry = make_entry(hass, data={CONF_DEVICE_IDS: [BED_A], CONF_ACCOUNT_ID: ACCOUNT})
    registry = er.async_get(hass)
    # Survived the upgrade. This is the row the migration wants to move
    # the legacy one onto, so it is what does the blocking.
    registry.async_get_or_create("sensor", DOMAIN, ACCOUNT_ID, config_entry=entry)
    # Minted by 2.x during the downgrade, because it could not find the id
    # above and built a fresh entity instead.
    registry.async_get_or_create("sensor", DOMAIN, LEGACY_ID, config_entry=entry)
    return entry


async def test_a_declined_rename_still_loads_every_platform(hass, patched):
    """The whole point. A blocked id must not black out the integration.

    Breaks if `async_setup_entry` goes back to letting the migration's
    refusal propagate, whatever exception class it uses, because the
    platforms never load and none of these entities exist.
    """
    entry = two_generation_entry(hass)

    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED, (
        "one squatted unique_id refused the whole integration. reason="
        f"{entry.reason!r}"
    )

    # Not just "setup returned true". Assert the platforms actually built
    # entities, across more than one of them, because a setup that loaded
    # and forwarded nothing is the same outage with a healthier looking
    # config entry page.
    built = {
        row.domain
        for row in er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)
    }
    assert {"climate", "sensor", "binary_sensor", "switch"} <= built, (
        f"platforms did not build their entities, only got {sorted(built)}"
    )

    # And one of them is actually serving a state. The climate entity is
    # the one a user notices first, and it is the one that controls a bed
    # somebody is currently lying in.
    live = hass.states.get("climate.sleepy_alex_climate")
    assert live is not None, "the climate entity was never created"
    assert live.state != "unavailable", (
        "the bed's climate control loaded but reports unavailable"
    )


async def test_a_declined_rename_raises_a_repair_issue(hass, patched):
    """Degrading must not mean going quiet.

    A user whose history is stranded on a bed-keyed id needs to be told,
    in the UI, while they can still act on it. A log warning is not that.

    Breaks if `_async_report_unique_id_conflicts` stops being called, if
    the issue id stops being per-entry, or if the migration stops raising
    `UnmigratedEntities` on a decline.
    """
    entry = two_generation_entry(hass)

    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    issue = issue_for(hass, entry)
    assert issue is not None, (
        "an entity was left on a 2.x id and nothing said so in the UI"
    )
    assert issue.translation_key == ISSUE_UNIQUE_ID_CONFLICT
    assert issue.severity is ir.IssueSeverity.WARNING
    assert issue.is_fixable is False

    placeholders = issue.translation_placeholders or {}
    # Names the BLOCKER as well as the row being moved. Reporting only the
    # source is what sent users to delete the entity that still worked,
    # taking its history with it, while the actual squatter stayed.
    conflicts = placeholders.get("conflicts", "")
    entity_ids = rows(hass, entry)
    assert entity_ids[LEGACY_ID] in conflicts, "the blocked entity is not named"
    assert entity_ids[ACCOUNT_ID] in conflicts, "the blocking entity is not named"


async def test_a_declined_rename_is_not_forced_through(hass, patched):
    """The half of the old behaviour that was right.

    Degrading is about availability, not about relaxing the registry
    rule. If this ever starts passing by merging the two rows, one
    person's sleep history has been silently filed under another id.

    Breaks if the migration starts deleting or overwriting the blocker to
    make room for the rename.
    """
    entry = two_generation_entry(hass)

    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    present = rows(hass, entry)
    assert LEGACY_ID in present, "the 2.x row was destroyed to clear the conflict"
    assert ACCOUNT_ID in present, "the 3.x row was destroyed to clear the conflict"


async def test_a_later_clean_setup_clears_the_issue(hass, patched):
    """A repair that outlives the problem trains users to ignore repairs.

    Breaks if `_async_report_unique_id_conflicts` stops calling
    `async_delete_issue` on the empty path, or if the issue id is
    computed differently on the create and the delete.
    """
    entry = two_generation_entry(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert issue_for(hass, entry) is not None, "the fixture never raised the issue"

    # Resolve it the way a user does, by removing the row that has no
    # entity behind it. The legacy row is the orphan 2.x minted, so with
    # it gone there is no source for the blocked rename and the next
    # setup has nothing left to decline.
    registry = er.async_get(hass)
    registry.async_remove(rows(hass, entry)[LEGACY_ID])

    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED
    assert issue_for(hass, entry) is None, (
        "the conflict was resolved but the repair issue is still showing"
    )


async def test_resume_clears_the_latch_for_a_non_owner_entry(hass, patched):
    """The second dead end, in the action that exists to escape the first.

    `resume_unique_ids` is the documented way out of recovery mode. While
    the latch is set, `async_setup_entry` refuses and `revert_unique_ids`
    has nothing left to undo, so this action refusing too left the entry
    with no supported way back at all.

    It refused on bed ownership, which `async_migrate_unique_ids` already
    checks from the same rule, with a message that says what to do about
    it. So the check was not adding enforcement, only a worse error, and
    it latched the entry behind that error permanently.

    Breaks if the ownership block returns to `_handle_resume`: it raises
    before the latch is popped, so `CONF_UID_RECOVERY_ACTIVE` survives.
    """
    # The owner. Never set up, so it cannot take the account identity out
    # from under the latched entry, but it holds the registry rows, which
    # is what `bed_owner` reads and therefore what decides ownership.
    owner = make_entry(
        hass,
        entry_id="entry-owner",
        unique_id="acct-owner",
        data={CONF_DEVICE_IDS: [BED_A]},
    )
    er.async_get(hass).async_get_or_create(
        "sensor", DOMAIN, ACCOUNT_ID, config_entry=owner
    )
    # The non-owner, latched for a downgrade it changed its mind about.
    # Already carries the account unique_id, so identity migration is a
    # no-op and the bed check is unambiguously what it runs into.
    latched = make_entry(
        hass,
        entry_id="entry-latched",
        unique_id=ACCOUNT,
        data={CONF_DEVICE_IDS: [BED_A], CONF_UID_RECOVERY_ACTIVE: True},
    )
    # What a restart into 3.x with the latch already set actually does.
    # Both entries attempt setup and both fail, which is the state the
    # user is looking at when they reach for this action. It also
    # registers the recovery services, which live on `async_setup` rather
    # than on any entry loading, precisely so they survive this.
    assert await async_setup_component(hass, DOMAIN, {})
    await hass.async_block_till_done()
    assert latched.state is not ConfigEntryState.LOADED

    user = await hass.auth.async_create_user("Admin", group_ids=["system-admin"])
    try:
        await hass.services.async_call(
            DOMAIN,
            "resume_unique_ids",
            {"config_entry_id": latched.entry_id, "confirm": True},
            blocking=True,
            context=Context(user_id=user.id),
        )
    except HomeAssistantError as err:
        # A reload that then fails its own migration is a valid outcome.
        # The terse ownership refusal this test exists about is not, and
        # it is the one that used to fire before the latch was cleared.
        assert "holds this bed's entity history" not in str(err), (
            "resume refused on bed ownership again, so the entry is latched "
            "behind an error that tells it to do nothing"
        )

    assert not latched.data.get(CONF_UID_RECOVERY_ACTIVE), (
        "resume left the recovery latch set, so setup still refuses, revert "
        "still has nothing to undo, and this action is still the only way "
        "out. The entry is stuck exactly where it started"
    )

    # And the conflict is still enforced by the setup path, which names
    # the entry that actually holds the history. Without this the test
    # would also pass on a `_handle_resume` that cleared the latch and
    # then never reloaded, which is not an escape, only a quieter latch.
    #
    # Asserted on the owner's entry_id rather than on a specific message,
    # deliberately. Setup currently reports this from `coordinator.py`,
    # whose copy of the ownership rule raises `UpdateFailed` during the
    # first refresh and therefore gets there before
    # `async_migrate_unique_ids` does. Pinning the migration's wording
    # here would make this test fail the day that ordering changes, over
    # something it is not about. Before the fix `reason` is still the
    # latch message, which names nobody, so this stays load-bearing.
    assert latched.state is not ConfigEntryState.LOADED
    assert owner.entry_id in (latched.reason or ""), (
        "the reload never happened, or setup stopped naming the entry that "
        f"holds the bed. reason={latched.reason!r}"
    )


async def test_resume_is_still_admin_only(hass, patched):
    """The gate moved to `helpers._require_admin`, it did not disappear.

    Breaks if the `_require_admin` call is dropped from `_admin_entry`,
    which is exactly the kind of thing that happens when an inline copy
    of a rule is deleted in favour of a shared one.
    """
    from homeassistant.exceptions import Unauthorized

    entry = make_entry(hass, data={CONF_UID_RECOVERY_ACTIVE: True})
    assert await async_setup_component(hass, DOMAIN, {})
    await hass.async_block_till_done()
    # The first user in a fresh instance becomes the owner, and an owner
    # is admin whatever group you ask for. Burn one first.
    await hass.auth.async_create_user("Owner", group_ids=["system-admin"])
    user = await hass.auth.async_create_user("Someone", group_ids=["system-users"])
    assert not user.is_admin, "fixture failed to build a non-admin user"

    with pytest.raises(Unauthorized):
        await hass.services.async_call(
            DOMAIN,
            "resume_unique_ids",
            {"config_entry_id": entry.entry_id, "confirm": True},
            blocking=True,
            context=Context(user_id=user.id),
        )
    assert entry.data.get(CONF_UID_RECOVERY_ACTIVE), (
        "a non-admin cleared the recovery latch"
    )
