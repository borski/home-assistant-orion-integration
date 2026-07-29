"""A refused revert must not have moved anything first.

`_handle_revert` used to evaluate every partner refusal on the
`RevertResult` it got back, which means `async_revert_unique_ids` had
already run. By the time the household read the refusal, rows had been
renamed and the journal had been rewritten. The message was correct about
a state that no longer existed.

For `partner_rows_outrank_journal` that is confusing. For `partner_stale`
it is dangerous, and the danger is the one this whole area exists to stop.
The revert applies non-stale records and skips stale ones. A journal
holding both, which is exactly the split-brain state
`_partner_recovery_renames` documents, therefore got a PARTIAL partner
rename and only then a refusal. Half of one person's entities land on the
role-keyed ids 2.x feeds from the other person. The instruction the
household is given at that moment describes a registry that has already
been half moved.

So the refusals moved to a preflight. `partner_revert_blockers` decides
all three from pure reads, `_handle_revert` calls it before the revert,
and a refused revert now leaves the registry byte for byte as it found it.

Every test here snapshots the registry rather than asserting on a count.
A count is satisfied by renaming two rows onto each other's ids, which is
the precise failure being guarded against.
"""

from __future__ import annotations

import logging
from typing import Any
from unittest.mock import patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import Context
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from orion_sleep_api import OrionConnectionError

from custom_components.orion_sleep.const import (
    CONF_PARTNER_ACCESS_TOKEN,
    CONF_UID_MIGRATION,
    CONF_UID_RECOVERY_ACTIVE,
    DOMAIN,
)
from custom_components.orion_sleep.migrations import PartnerRevertBlockers
from tests_ha.conftest import FakeClient, make_entry
from tests_ha.test_partner_replacement_message_real import (
    partner_entry as replaced_partner_entry,
)
from tests_ha.test_partner_replacement_message_real import (
    seed_legacy_partner_rows,
)
from tests_ha.test_partner_transient_real import (
    PARTNER,
    PartnerClient,
    clients,
    partner_entry,
    partner_record,
)

# The exact text the service raised before the preflight existed, copied
# from the pre-change `__init__.py` rather than re-derived from the
# builders it now calls. Asserting against the builders would pass if
# somebody rewrote both the builder and the test together, which is the
# one thing these two constants exist to catch. The whole point of the
# change is that a household sees no difference at all except that
# nothing moved.
STALE_MESSAGE = (
    "Orion could not confirm which account the partner tokens belong to "
    "on the last setup, so the partner's entity mappings were kept but "
    "not applied. Nothing has been lost. Run "
    "orion_sleep.resume_unique_ids to load 3.x again so the partner "
    "verifies, then run this once more before installing 2.x"
)
UNMAPPED_MESSAGE = (
    "A partner account is linked but no partner entity mappings were "
    "recorded, so a downgrade would strand the partner's history on ids "
    "2.x never asks for. Reload Orion with orion_sleep.resume_unique_ids "
    "so the partner verifies, then run this again"
)


def registry_snapshot(hass, entry) -> dict[str, str]:
    """Every unique_id this entry owns, keyed by the row that holds it.

    Keyed by entity_id on purpose. A revert moves unique_ids and leaves
    entity_ids alone, which is the entire reason renaming is safe, so this
    mapping changes if and only if a rename landed.

    A row count would not do. Reverting two records is capable of leaving
    the count identical while both people's history has swapped places,
    and that is the specific outcome this file is about.
    """
    return {
        row.entity_id: row.unique_id
        for row in er.async_entries_for_config_entry(
            er.async_get(hass), entry.entry_id
        )
    }


async def admin_context(hass) -> Context:
    user = await hass.auth.async_create_user("Admin", group_ids=["system-admin"])
    return Context(user_id=user.id)


async def call_revert(hass, entry) -> HomeAssistantError | None:
    """Run the action the way a user does, returning the refusal if any."""
    try:
        await hass.services.async_call(
            DOMAIN,
            "revert_unique_ids",
            {"config_entry_id": entry.entry_id, "confirm": True},
            blocking=True,
            context=await admin_context(hass),
        )
    except HomeAssistantError as err:
        return err
    return None


# ---------------------------------------------------------------------
# 1. The stale partner case. Nothing moves.
# ---------------------------------------------------------------------


async def test_a_stale_refusal_leaves_every_unique_id_where_it_was(
    hass, ws_manager
):
    """The finding. This fails against the pre-preflight code.

    Before the change, `async_revert_unique_ids` ran first and applied
    every non-stale record, so a household with primary records and a
    stale partner record had its whole primary set renamed back to the
    pre-3.0 ids and was then told to go and fix the partner. The refusal
    named a state that no longer described the registry.

    Breaks if `partner_revert_blockers` is called after
    `async_revert_unique_ids` instead of before it, or if the
    `blockers.partner_stale` raise in `_handle_revert` is removed and only
    the `result.partner_stale` one is left.
    """
    entry = partner_entry(hass, journal=[partner_record(PARTNER)])
    partner = PartnerClient(fail=OrionConnectionError("connection reset"))

    api, ws = clients(FakeClient(), partner, ws_manager)
    with api, ws:
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        assert entry.state is ConfigEntryState.LOADED, (
            "the partner fetch took setup down with it, so this test is "
            f"exercising a different failure. reason={entry.reason!r}"
        )

        before = registry_snapshot(hass, entry)
        assert before, "setup registered no entities, so nothing is under test"

        refusal = await call_revert(hass, entry)

    assert refusal is not None, "the stale partner did not refuse the revert"
    after = registry_snapshot(hass, entry)
    assert after == before, (
        "a revert that refused over a stale partner renamed rows before it "
        "refused. The household is being told to go and fix something while "
        "the registry has already been half moved: "
        f"{sorted(set(before.items()) ^ set(after.items()))}"
    )


async def test_a_stale_refusal_leaves_the_journal_byte_for_byte(hass, ws_manager):
    """The other half of "nothing moved", and it is separable.

    A revert could restore every unique_id and still have rewritten the
    journal, which is what `_write_journal` does on every pass. Losing the
    primary records there does not strand anything on its own, but it does
    mean the NEXT call sees a journal describing less work than actually
    remains, and every later message is built from that.

    Breaks if `async_revert_unique_ids` is reached on a refused run at
    all, since it writes `remaining + stale` unconditionally.
    """
    entry = partner_entry(hass, journal=[partner_record(PARTNER)])
    partner = PartnerClient(fail=OrionConnectionError("connection reset"))

    api, ws = clients(FakeClient(), partner, ws_manager)
    with api, ws:
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        before = list(entry.data.get(CONF_UID_MIGRATION) or [])
        assert before, "setup journalled nothing, so nothing is under test"
        assert any(not record.get("stale") for record in before), (
            "the journal holds only stale records, so a run that skipped "
            "everything would pass this test without proving anything"
        )

        refusal = await call_revert(hass, entry)

    assert refusal is not None, "the stale partner did not refuse the revert"
    assert (entry.data.get(CONF_UID_MIGRATION) or []) == before, (
        "a refused revert rewrote the journal, so the next call reasons "
        "about a smaller set of work than actually remains"
    )
    assert not entry.data.get(CONF_UID_RECOVERY_ACTIVE), (
        "a refused revert left the latch set, so 3.x now refuses to load"
    )


# ---------------------------------------------------------------------
# 2. The replacement case. Nothing moves.
# ---------------------------------------------------------------------


async def test_a_replacement_refusal_leaves_every_unique_id_where_it_was(
    hass, ws_manager
):
    """Same guarantee for `partner_rows_outrank_journal`.

    This household is told to delete entity registry rows, which is a
    destructive instruction they have to weigh. Handing it to them while
    their primary entities have silently moved to different unique_ids
    means the registry they go and inspect is not the one the message was
    written about.

    Breaks if the `blockers.partner_rows_outrank_journal` raise is dropped
    from the preflight, leaving only the post-revert copy that fires after
    `async_revert_unique_ids` has already applied the primary records.
    """
    entry = replaced_partner_entry(hass, replaced=True)
    seed_legacy_partner_rows(hass, entry)

    api, ws = clients(FakeClient(), PartnerClient(), ws_manager)
    with api, ws:
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        assert entry.state is ConfigEntryState.LOADED

        before = registry_snapshot(hass, entry)
        journal_before = list(entry.data.get(CONF_UID_MIGRATION) or [])
        assert journal_before, "setup journalled nothing, so nothing would move"

        refusal = await call_revert(hass, entry)

    assert refusal is not None, "the replaced partner did not refuse the revert"
    after = registry_snapshot(hass, entry)
    assert after == before, (
        "a revert that refused over a replaced partner renamed rows before "
        "it refused, so the entities named in the refusal are not the "
        "entities the household will find: "
        f"{sorted(set(before.items()) ^ set(after.items()))}"
    )
    assert (entry.data.get(CONF_UID_MIGRATION) or []) == journal_before, (
        "a refused revert rewrote the journal"
    )


# ---------------------------------------------------------------------
# 3. The messages are the ones the household already had.
# ---------------------------------------------------------------------


async def test_the_stale_refusal_says_exactly_what_it_used_to(hass, ws_manager):
    """The improvement has to be invisible except that nothing moved.

    A refusal raised from a new place is a chance to accidentally reword
    it, and the wording here is the product of two earlier passes that
    found the generic text sent people to relink an account that had never
    changed.

    Breaks if `_partner_stale_error` is reworded, or if the preflight
    raises some other error for a stale journal.
    """
    entry = partner_entry(hass, journal=[partner_record(PARTNER)])
    partner = PartnerClient(fail=OrionConnectionError("connection reset"))

    api, ws = clients(FakeClient(), partner, ws_manager)
    with api, ws:
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        refusal = await call_revert(hass, entry)

    assert str(refusal) == STALE_MESSAGE, (
        f"the stale refusal changed wording when it moved: {refusal}"
    )


async def test_the_replacement_refusal_says_exactly_what_it_used_to(
    hass, ws_manager
):
    """Same, for the message that names rows to delete.

    Asserted as substrings plus the seeded entity_ids rather than as one
    literal, because the row list is interpolated and its order comes from
    the registry.

    Breaks if `_partner_replacement_error` loses the deletion instruction,
    the reason deletion is worth its cost, or the row names.
    """
    entry = replaced_partner_entry(hass, replaced=True)
    seeded = seed_legacy_partner_rows(hass, entry)

    api, ws = clients(FakeClient(), PartnerClient(), ws_manager)
    with api, ws:
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        refusal = await call_revert(hass, entry)

    message = str(refusal)
    lowered = message.lower()
    assert "previous partner" in lowered, message
    assert "delete" in lowered, message
    assert "merge" in lowered, message
    assert any(entity_id in message for entity_id in seeded), (
        f"the refusal names no entity the household can go and find: {message}"
    )
    assert "resume_unique_ids" not in message, (
        "the refusal tells the household to reload, which is the one "
        f"instruction that can never clear this state: {message}"
    )


async def test_the_generic_unmapped_refusal_says_exactly_what_it_used_to(
    hass, ws_manager
):
    """The third message, driven through the preflight.

    Built by emptying the journal of partner records only. The old ids are
    free, so no legacy rows are in the way and `partner_unmapped` is the
    plain cause rather than the replacement one, and leaving the primary
    records in place keeps `pending_renames` non-zero so the refusal is
    reached at all.

    Breaks if `_partner_unmapped_error` is reworded, or if the preflight
    raises the replacement message for a household whose old ids are free.
    """
    entry = partner_entry(hass, journal=[partner_record(PARTNER)])

    api, ws = clients(FakeClient(), PartnerClient(), ws_manager)
    with api, ws:
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        journal = entry.data.get(CONF_UID_MIGRATION) or []
        primaries = [r for r in journal if r.get("role") != "partner"]
        assert primaries, "setup journalled no primary renames to keep"
        hass.config_entries.async_update_entry(
            entry, data={**entry.data, CONF_UID_MIGRATION: primaries}
        )
        await hass.async_block_till_done()

        refusal = await call_revert(hass, entry)

    assert str(refusal) == UNMAPPED_MESSAGE, (
        f"the generic unmapped refusal changed wording when it moved: {refusal}"
    )


async def test_the_generic_unmapped_refusal_also_moves_nothing(hass, ws_manager):
    """The behaviour change this item was opened to make deliberate.

    This is the one refusal that used to let renames land on purpose. It
    sat after `result.complete` and read a flag off the finished result, so
    every primary record was applied and the household was then told the
    partner had been stranded. A revert that previously did partial work
    now does none, which is the intended improvement rather than a side
    effect of moving the check.

    Breaks if the `blockers.partner_unmapped` raise is removed from the
    preflight and only the post-revert copy is left.
    """
    entry = partner_entry(hass, journal=[partner_record(PARTNER)])

    api, ws = clients(FakeClient(), PartnerClient(), ws_manager)
    with api, ws:
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        journal = entry.data.get(CONF_UID_MIGRATION) or []
        primaries = [r for r in journal if r.get("role") != "partner"]
        hass.config_entries.async_update_entry(
            entry, data={**entry.data, CONF_UID_MIGRATION: primaries}
        )
        await hass.async_block_till_done()

        before = registry_snapshot(hass, entry)
        refusal = await call_revert(hass, entry)

    assert refusal is not None, "the unmapped partner did not refuse the revert"
    after = registry_snapshot(hass, entry)
    assert after == before, (
        "the unmapped refusal still applied every primary record before "
        "raising, so the household is told their partner is stranded while "
        "their own entities have already moved: "
        f"{sorted(set(before.items()) ^ set(after.items()))}"
    )


# ---------------------------------------------------------------------
# 4. Positive controls. "Refuse always" is not a passing fix.
# ---------------------------------------------------------------------


async def test_an_unblocked_revert_still_does_the_whole_job(hass, patched):
    """The control that stops this change becoming an unconditional refusal.

    A preflight that returned "blocked" for everything would satisfy every
    test above. This one asserts the opposite direction: an entry with no
    partner at all still gets every recorded rename applied, the journal
    cleared, the latch set and the 2.x entry identity restored.

    Breaks if `partner_revert_blockers` reports a blocker for an entry with
    no partner token, or if the preflight raises before checking its own
    flags.
    """
    entry = make_entry(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    journal = list(entry.data.get(CONF_UID_MIGRATION) or [])
    assert journal, "a fresh install journalled nothing, so nothing can revert"
    before = registry_snapshot(hass, entry)
    wanted = {record["new"]: record["old"] for record in journal}
    assert set(wanted) & set(before.values()), (
        "no registry row holds a 3.x id from the journal, so a revert that "
        "did nothing at all would pass this test"
    )

    refusal = await call_revert(hass, entry)

    assert refusal is None, f"an entry with no partner was refused: {refusal}"
    after = registry_snapshot(hass, entry)
    for entity_id, unique_id in before.items():
        if unique_id in wanted:
            assert after[entity_id] == wanted[unique_id], (
                f"{entity_id} was not returned to the id 2.x asks for. It is "
                f"on {after[entity_id]} and 2.x will look for "
                f"{wanted[unique_id]}"
            )
    assert not entry.data.get(CONF_UID_MIGRATION), (
        "a completed revert left records behind, so the next call reports "
        "work still outstanding"
    )
    assert entry.data.get(CONF_UID_RECOVERY_ACTIVE), (
        "a completed revert did not latch, so 3.x will load again and "
        "re-apply the migration the user just undid"
    )
    assert entry.unique_id == "alice@example.com", (
        "a completed revert left the entry on its account id, so 2.x will "
        "not recognise it by the address it was set up with"
    )


async def test_a_partnered_entry_with_nothing_to_undo_is_still_a_no_op(
    hass, ws_manager, caplog
):
    """The gate that keeps the new refusal off households in no danger.

    `partner_unmapped` was deliberately never added to the "nothing
    recorded to undo" early return, because an entry whose journal is
    empty because nothing ever migrated would then be refused, and running
    the action speculatively is a supported thing to do.

    A preflight has no `reverted` to gate on, so moving the refusal ahead
    of the revert would have reintroduced exactly that, for every
    partnered household that ever ran the action out of curiosity.
    `pending_renames` restores the old gate rather than approximating it.

    Breaks if the `and blockers.pending_renames` clause is dropped from
    the preflight in `_handle_revert`.
    """
    entry = partner_entry(hass, journal=[partner_record(PARTNER)])

    api, ws = clients(FakeClient(), PartnerClient(), ws_manager)
    with api, ws:
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        data = dict(entry.data)
        data.pop(CONF_UID_MIGRATION, None)
        hass.config_entries.async_update_entry(entry, data=data)
        await hass.async_block_till_done()
        assert entry.data.get(CONF_PARTNER_ACCESS_TOKEN), (
            "the partner token went away with the journal, so this entry no "
            "longer reaches the unmapped branch at all"
        )

        with caplog.at_level(logging.INFO, logger="custom_components.orion_sleep"):
            refusal = await call_revert(hass, entry)

    assert refusal is None, (
        "a partnered entry with an empty journal was refused. Nothing has "
        f"migrated, so there is nothing to strand: {refusal}"
    )
    assert "Nothing changed" in caplog.text, (
        "the no-op path stopped saying plainly that it did nothing"
    )
    assert not entry.data.get(CONF_UID_RECOVERY_ACTIVE), (
        "a speculative run latched the entry, so 3.x now refuses to load"
    )


# ---------------------------------------------------------------------
# 5. One implementation of the rules, not two that agree today.
# ---------------------------------------------------------------------


async def test_the_revert_reports_the_blockers_it_was_given(hass, patched):
    """Structural. `async_revert_unique_ids` does not decide these itself.

    The flags are handed back doctored, on an entry that has no partner at
    all, and the result has to carry them unchanged. An implementation
    that recomputed any of the three from the journal or the registry
    would overwrite every one of them with False and fail here.

    This is the assertion that matters most in this file. Two
    implementations agreeing today is how `_is_partner_record` came to
    exist: `_read_journal` labelled unlabelled records via `_role_for`
    while the eviction tested `role == "partner"` directly, so an entire
    generation of journals walked past the guard. The rules being in one
    place is the property, not the rules currently matching.

    Breaks if `async_revert_unique_ids` computes `partner_unmapped`,
    `partner_stale`, `partner_rows_outrank_journal` or
    `legacy_partner_entity_ids` rather than copying them from
    `partner_revert_blockers`.
    """
    from custom_components.orion_sleep import migrations

    entry = make_entry(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    doctored = PartnerRevertBlockers(
        partner_stale=True,
        partner_unmapped=True,
        partner_rows_outrank_journal=True,
        legacy_partner_entity_ids=("sensor.invented_by_this_test",),
        pending_renames=7,
    )
    with patch.object(
        migrations, "partner_revert_blockers", return_value=doctored
    ) as spy:
        result = migrations.async_revert_unique_ids(hass, entry)

    assert spy.called, (
        "async_revert_unique_ids never consulted partner_revert_blockers, so "
        "the blocker rules exist in two places again"
    )
    assert result.partner_stale is True
    assert result.partner_unmapped is True
    assert result.partner_rows_outrank_journal is True
    assert result.legacy_partner_entity_ids == ("sensor.invented_by_this_test",), (
        "the result did not carry the row list it was handed, so something "
        "is still deriving it from the registry a second time"
    )


async def test_the_blockers_are_read_before_the_revert_runs(hass, ws_manager):
    """Structural. Order, asserted rather than inferred.

    Every "nothing moved" test above passes if the preflight merely agrees
    with the revert about a household that was going to be refused anyway.
    What makes them mean something is that the decision happens first. A
    reordering would leave those tests failing for a reason nobody could
    read off them, so the order gets its own assertion with its own name.

    Breaks if the `partner_revert_blockers` call in `_handle_revert` moves
    below `async_revert_unique_ids`, or is deleted.
    """
    import custom_components.orion_sleep as integration

    entry = partner_entry(hass, journal=[partner_record(PARTNER)])
    partner = PartnerClient(fail=OrionConnectionError("connection reset"))
    order: list[str] = []

    real_blockers = integration.partner_revert_blockers
    real_revert = integration.async_revert_unique_ids

    def spy_blockers(*args: Any, **kwargs: Any):
        order.append("preflight")
        return real_blockers(*args, **kwargs)

    def spy_revert(*args: Any, **kwargs: Any):
        order.append("revert")
        return real_revert(*args, **kwargs)

    api, ws = clients(FakeClient(), partner, ws_manager)
    with api, ws:
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        with (
            patch.object(integration, "partner_revert_blockers", spy_blockers),
            patch.object(integration, "async_revert_unique_ids", spy_revert),
        ):
            refusal = await call_revert(hass, entry)

    assert refusal is not None, "the stale partner did not refuse the revert"
    assert order == ["preflight"], (
        "the revert ran on a call that was always going to be refused. "
        f"Anything it renamed happened for nothing: {order}"
    )


async def test_the_preflight_never_writes_anything(hass, ws_manager):
    """The contract that makes calling it early safe at all.

    `partner_revert_blockers` runs before the decision to proceed, so a
    write inside it happens on runs that go on to refuse. That would put
    the mutation back exactly where nobody would look for it, and every
    snapshot test above would keep passing as long as the write was not a
    rename.

    Asserted by calling it directly and denying it every mutation route:
    the entity registry update, the config entry update, and
    `_write_journal`.

    `_write_journal` is denied by name and not only by effect, and that
    distinction was earned. It compares its result against `entry.data`
    and calls `async_update_entry` only when something actually differs,
    so a preflight that called it with the journal it had just read would
    write nothing and slip past a test that watched `async_update_entry`
    alone. The contract is that the preflight does not reach for a
    mutating API at all, because the next edit to that API is what turns a
    harmless call into a real write.

    Breaks if the preflight gains any call that renames a row, writes the
    journal, or updates the entry.
    """
    from custom_components.orion_sleep import migrations

    entry = replaced_partner_entry(hass, replaced=True)
    seed_legacy_partner_rows(hass, entry)

    api, ws = clients(FakeClient(), PartnerClient(), ws_manager)
    with api, ws:
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        registry = er.async_get(hass)
        with (
            patch.object(
                registry,
                "async_update_entity",
                side_effect=AssertionError("the preflight renamed a registry row"),
            ),
            patch.object(
                hass.config_entries,
                "async_update_entry",
                side_effect=AssertionError("the preflight wrote to the entry"),
            ),
            patch.object(
                migrations,
                "_write_journal",
                side_effect=AssertionError("the preflight called _write_journal"),
            ),
        ):
            blockers = migrations.partner_revert_blockers(hass, entry)

    assert blockers.partner_rows_outrank_journal, (
        "the preflight did not reach its own conclusion, so this test "
        "proved only that a no-op writes nothing"
    )
    assert blockers.legacy_partner_entity_ids, (
        "the replacement blocker fired with no rows to name"
    )


async def test_a_non_admin_is_still_refused_before_the_preflight(hass, ws_manager):
    """The new branch must not become an authorisation bypass.

    Adding a call ahead of everything else in a handler is exactly how an
    authorisation check gets stepped over. `_admin_entry` runs first and
    has to keep running first.

    Breaks if the preflight is hoisted above `_admin_entry`, or if
    `_require_admin` is dropped.
    """
    from homeassistant.exceptions import Unauthorized

    entry = partner_entry(hass, journal=[partner_record(PARTNER)])
    partner = PartnerClient(fail=OrionConnectionError("connection reset"))

    api, ws = clients(FakeClient(), partner, ws_manager)
    with api, ws:
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        # The first user in a fresh instance becomes the owner, and an
        # owner is admin whatever group you ask for. One is burned through
        # `admin_context` so this one is genuinely not.
        await admin_context(hass)
        user = await hass.auth.async_create_user(
            "Someone", group_ids=["system-users"]
        )
        assert not user.is_admin, "fixture failed to build a non-admin user"

        before = registry_snapshot(hass, entry)
        with pytest.raises(Unauthorized):
            await hass.services.async_call(
                DOMAIN,
                "revert_unique_ids",
                {"config_entry_id": entry.entry_id, "confirm": True},
                blocking=True,
                context=Context(user_id=user.id),
            )

    assert registry_snapshot(hass, entry) == before, (
        "a non-admin call still moved rows"
    )
