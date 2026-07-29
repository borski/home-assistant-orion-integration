"""A journal holding only stale records must not report "nothing to undo".

Stale partner records count in NEITHER `RevertResult.reverted` nor
`RevertResult.remaining`. That is deliberate and argued on the dataclass:
nothing is blocking them, so counting them as remaining would send the
user hunting for a squatted registry row that does not exist.

The consequence was that `_handle_revert` opened with

    if not (result.reverted or result.remaining):

so a journal containing only stale records satisfied that condition and
returned, which made the `partner_stale` branch further down unreachable
in exactly the case it was written for.

The sequence that lands there is ordinary.

1. A restart happens during a network interruption. The partner's account
   cannot be confirmed, so the partner records are kept and marked stale
   rather than evicted.
2. The journal is left holding stale partner records and nothing that
   can be applied. Historically the revert action produced that state
   itself, because it renamed the primary records and rewrote the journal
   as `remaining + stale` BEFORE raising over the partner. Refusals are
   decided from pure reads now and change nothing, so `stale_only_journal`
   narrows the journal explicitly instead. The state is unchanged and so
   is everything below it.
3. The user runs the action against that journal.

At step 3 they were told "No recorded Orion renames to undo. Nothing
changed, and nothing is prepared for a downgrade." The partner's only
rollback record was being withheld at that exact moment. Told nothing is
withheld while something is, which is the failure this project treats as
worst-case, and in flat contradiction to the message the same action gave
them at step 2.

Nothing here asserts on the number of primary records or on which
entities moved. Those belong to `test_partner_transient_real.py`. This
file is only about what the SECOND call says.
"""

from __future__ import annotations

import logging

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import Context
from homeassistant.exceptions import HomeAssistantError
from orion_sleep_api import OrionConnectionError

from custom_components.orion_sleep.const import (
    CONF_UID_MIGRATION,
    CONF_UID_RECOVERY_ACTIVE,
    DOMAIN,
)
from tests_ha.conftest import FakeClient
from tests_ha.test_partner_transient_real import (
    PARTNER,
    PartnerClient,
    clients,
    partner_entry,
    partner_record,
    partner_records,
)


async def admin_context(hass) -> Context:
    user = await hass.auth.async_create_user("Admin", group_ids=["system-admin"])
    return Context(user_id=user.id)


async def call_revert(hass, entry) -> HomeAssistantError | None:
    """Run the action the way a user does, returning the refusal if any.

    Returns rather than raises so a test can assert on the difference
    between "refused with a reason" and "reported success", which is the
    whole subject of this file.
    """
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


async def stale_only_journal(hass, ws_manager):
    """Reach step 3 above by actually walking steps 1 and 2.

    The stale marking is deliberately not seeded by hand. It is produced
    by a real setup against a partner the fake client refuses to answer
    for, so a change that stopped marking records stale at all fails here
    loudly instead of leaving a hand-written flag propping the file up.

    The stale-only NARROWING is now explicit, and it used to be a side
    effect. `_handle_revert` evaluated every partner refusal on the
    returned `RevertResult`, which meant `async_revert_unique_ids` had
    already run: it applied the primary records, skipped the stale partner
    ones, and rewrote the journal as `remaining + stale`. The primaries
    disappeared on their own and this fixture simply asserted that they
    had.

    That was the bug, not the contract. A refused revert now decides
    everything from pure reads before anything moves, so it leaves the
    journal exactly as it found it. Step 2 is still driven for real,
    because "the first call refuses and changes nothing" is worth
    asserting right here, and the primaries are then dropped explicitly to
    build the state step 3 is about. Reaching it by way of a partial
    rename was never the point of this file.
    """
    entry = partner_entry(hass, journal=[partner_record(PARTNER)])
    partner = PartnerClient(fail=OrionConnectionError("connection reset"))

    api, ws = clients(FakeClient(), partner, ws_manager)
    with api, ws:
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        assert entry.state is ConfigEntryState.LOADED, (
            "the partner fetch took setup down with it, so this fixture is "
            f"building a different failure entirely. reason={entry.reason!r}"
        )

        before = list(entry.data.get(CONF_UID_MIGRATION) or [])
        first = await call_revert(hass, entry)

    assert first is not None, "the first revert did not refuse over the partner"
    assert "could not confirm" in str(first).lower(), (
        f"the first revert refused for some other reason: {first}"
    )

    journal = entry.data.get(CONF_UID_MIGRATION) or []
    assert journal, "the first revert discarded the journal it refused over"
    assert journal == before, (
        "a refused revert rewrote the journal. It decides every partner "
        "refusal from pure reads before anything moves, so a difference "
        f"here means renames landed on a run that then refused: {journal}"
    )

    stale_only = [record for record in journal if record.get("stale")]
    assert stale_only, "setup marked nothing stale, so there is no state to build"
    data = dict(entry.data)
    data[CONF_UID_MIGRATION] = stale_only
    hass.config_entries.async_update_entry(entry, data=data)
    await hass.async_block_till_done()
    return entry


async def test_a_stale_only_revert_refuses_instead_of_reporting_nothing_to_undo(
    hass, ws_manager, caplog
):
    """The finding. This is the test that must fail before the fix.

    The user is being told nothing is withheld at the moment the
    partner's only rollback record is withheld. If they believe it and
    install 2.x, the partner's history is stranded on ids 2.x never asks
    for.

    Breaks if `result.partner_stale` is removed from the early-return
    condition in `_handle_revert`, which is the pre-fix code exactly.
    """
    entry = await stale_only_journal(hass, ws_manager)

    with caplog.at_level(logging.INFO, logger="custom_components.orion_sleep"):
        second = await call_revert(hass, entry)

    assert second is not None, (
        "a second revert against a stale-only journal reported success. The "
        "partner's mappings are still being withheld and nothing said so"
    )
    assert "could not confirm" in str(second).lower(), (
        "the refusal has to name the partner problem. Any other wording sends "
        f"this user to fix something that is not broken: {second}"
    )
    assert "No recorded Orion renames to undo" not in caplog.text, (
        "the action claimed there was nothing to undo while holding the "
        "partner's only rollback record. It also contradicts the refusal the "
        "same action gave on the previous call"
    )


async def test_a_stale_only_revert_does_not_claim_readiness_for_downgrade(
    hass, ws_manager, caplog
):
    """The half of the message that costs data if believed.

    Separate from the test above because the two failures are separable.
    A future shape could stop saying "nothing to undo" and still fall
    through to the shared success log, which is the exact regression
    `test_speculative_revert_real.py` was written about for the empty
    journal case.

    Breaks if any path with `partner_stale` set reaches the tail log at
    the end of `_handle_revert`, meaning `prepared` was set True on a run
    that withheld records.
    """
    entry = await stale_only_journal(hass, ws_manager)

    with caplog.at_level(logging.INFO, logger="custom_components.orion_sleep"):
        await call_revert(hass, entry)

    assert "ready for downgrade" not in caplog.text, (
        "a revert holding stale partner records told the user the entry was "
        "ready for 2.x. Installing 2.x on that advice strands the partner's "
        "history"
    )


async def test_a_stale_only_revert_keeps_the_records_and_the_latch_off(
    hass, ws_manager
):
    """Refusing is only safe because nothing is thrown away when it does.

    Two independent ways the new refusal could be worse than the old
    silence. Discarding the records would make the NEXT call report a
    clean success against a journal that no longer knows it was
    distrusted. Leaving the latch set would mean 3.x refuses to load, and
    the escape from that is a differently named action the refusal does
    mention but which the user now has to find.

    Breaks if the `partner_stale` raise moves outside the `try`, or if
    `prepared` is set before it, either of which skips the latch
    rollback in the `finally`.
    """
    entry = await stale_only_journal(hass, ws_manager)

    await call_revert(hass, entry)

    assert partner_records(entry), (
        "the refusal discarded the records it refused over, so the next run "
        "has nothing left to refuse about and will report a clean success"
    )
    assert all(record.get("stale") for record in partner_records(entry)), (
        "the records survived but lost their stale marking, so the next "
        "revert will apply mappings no setup ever vouched for"
    )
    assert not entry.data.get(CONF_UID_RECOVERY_ACTIVE), (
        "a refused revert left the latch set, so 3.x now refuses to load"
    )


async def test_an_empty_journal_still_reports_nothing_to_undo(hass, patched, caplog):
    """Negative control. The early return must still exist.

    Widening the condition must not turn a genuinely empty journal into a
    refusal. Running the action speculatively on an entry that never
    migrated is a supported thing to do, and `test_speculative_revert_real`
    covers the damage that path used to cause. This asserts the fix did
    not simply delete the branch.

    Breaks if the early return is removed outright rather than having
    `partner_stale` added to its condition.
    """
    from tests_ha.conftest import make_entry

    entry = make_entry(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    data = dict(entry.data)
    data.pop(CONF_UID_MIGRATION, None)
    hass.config_entries.async_update_entry(entry, data=data)
    await hass.async_block_till_done()

    with caplog.at_level(logging.INFO, logger="custom_components.orion_sleep"):
        refusal = await call_revert(hass, entry)

    assert refusal is None, (
        f"an empty journal now refuses instead of reporting a no-op: {refusal}"
    )
    assert "Nothing changed" in caplog.text, (
        "the no-op path stopped saying plainly that it did nothing"
    )


async def test_a_stale_partner_outranks_an_incomplete_primary_revert(
    hass, ws_manager
):
    """The reordering, asserted on the message the user actually gets.

    When a run is both incomplete and carrying stale partner records, the
    incomplete message used to win. That message talks only about
    conflicting registry rows and never mentions that a second, unrelated
    set of mappings was also withheld, so the user resolves the conflict,
    runs the action again, and only then learns about the partner. Two
    withholdings reported one per attempt, with the first message
    implying it had named the only obstacle.

    Built by blocking one primary record's target id with a row the
    revert cannot move, which is what produces `remaining > 0`.

    Breaks if the `result.complete` check is moved back above the
    `result.partner_stale` check.
    """
    entry = partner_entry(hass, journal=[partner_record(PARTNER)])
    partner = PartnerClient(fail=OrionConnectionError("connection reset"))

    api, ws = clients(FakeClient(), partner, ws_manager)
    with api, ws:
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        assert entry.state is ConfigEntryState.LOADED

        journal = entry.data.get(CONF_UID_MIGRATION) or []
        primaries = [r for r in journal if not r.get("stale")]
        assert primaries, "setup journalled no primary renames to block"

        # Squat the pre-3.0 id one primary record wants to move back to.
        # A revert onto an occupied id is refused rather than forced, for
        # the same reason the forward migration refuses, so this record
        # lands in `remaining` and the run reports incomplete.
        from homeassistant.helpers import entity_registry as er

        blocked = primaries[0]
        er.async_get(hass).async_get_or_create(
            blocked["domain"],
            blocked["platform"],
            blocked["old"],
            config_entry=entry,
        )

        refusal = await call_revert(hass, entry)

    assert refusal is not None, "a run that was both incomplete and stale succeeded"
    text = str(refusal).lower()
    assert "could not confirm" in text, (
        "the incomplete-primary message won, so this user is never told that "
        "their partner's mappings were also withheld. They will resolve the "
        f"registry conflict and discover the partner problem next time: {refusal}"
    )


async def test_the_revert_service_is_still_admin_only_on_the_stale_path(
    hass, ws_manager
):
    """The new branch must not become an authorisation bypass.

    Widening an early-return condition changes which code a caller
    reaches, and this handler's first act is `_require_admin`. Asserting
    it on the specific path this change opened is cheap.

    Breaks if `_require_admin` is dropped from `_admin_entry`, or if the
    stale branch is ever reached before the admin check.
    """
    from homeassistant.exceptions import Unauthorized

    entry = await stale_only_journal(hass, ws_manager)

    # The first user in a fresh instance becomes the owner, and an owner
    # is admin whatever group you ask for. `stale_only_journal` already
    # burned one through `admin_context`, so this one is genuinely not.
    user = await hass.auth.async_create_user("Someone", group_ids=["system-users"])
    assert not user.is_admin, "fixture failed to build a non-admin user"

    with pytest.raises(Unauthorized):
        await hass.services.async_call(
            DOMAIN,
            "revert_unique_ids",
            {"config_entry_id": entry.entry_id, "confirm": True},
            blocking=True,
            context=Context(user_id=user.id),
        )
    assert partner_records(entry), "a non-admin call still touched the journal"
