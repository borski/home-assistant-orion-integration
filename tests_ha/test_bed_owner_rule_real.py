"""One bed-ownership rule, and a failure the user can act on.

`bed_owner(...) or min(rivals | {entry_id})` decides which config entry
owns a household's biometric history. It was written out twice, in
`migrations.async_migrate_unique_ids` and in
`coordinator._async_update_data`, each with its own longhand copy of the
same reasoning. Two entries that disagreed about who owned a bed would
both migrate it, over the same registry rows.

The coordinator's copy was also the one that mattered, in the worst way.
It runs during `async_config_entry_first_refresh`, which is before
`async_migrate_unique_ids` runs at all, so the migration's carefully
worded refusal was unreachable and the message people actually saw named
the owner and stopped there. It raised `UpdateFailed`, which becomes
`ConfigEntryNotReady` and SETUP_RETRY, so the entry re-ran the same
doomed refresh on a backoff forever. Retrying cannot resolve a conflict
only a human can resolve.
"""

from __future__ import annotations

import pathlib

from homeassistant.config_entries import ConfigEntryState
from homeassistant.helpers import entity_registry as er

from custom_components.orion_sleep import migrations
from custom_components.orion_sleep.const import CONF_DEVICE_IDS, DOMAIN
from tests_ha.conftest import ACCOUNT, BED_A, make_entry

COMPONENT = pathlib.Path(migrations.__file__).parent


def own_row(hass, unique_id: str, entry):
    return er.async_get(hass).async_get_or_create(
        "sensor", DOMAIN, unique_id, config_entry=entry
    )


async def test_the_ownership_expression_exists_once(hass):
    """Drift guard, and the only kind that actually holds here.

    Two behavioural tests passing says both copies agree today. It does
    not say there is one copy. This does, and it is what stops the next
    edit reintroducing a second one somewhere a test does not reach.
    """
    offenders = {
        path.name
        for path in COMPONENT.glob("*.py")
        for line in path.read_text().splitlines()
        if "bed_owner(" in line and "resolve_bed_owner" not in line
    }
    assert offenders <= {"migrations.py"}, (
        f"{offenders} calls bed_owner directly instead of resolve_bed_owner, "
        "so the rule deciding who owns a household's history now exists in "
        "more than one place"
    )


async def test_resolve_bed_owner_follows_the_registry_rows(hass):
    """Ownership is where the history is, not who booted first."""
    first = make_entry(hass, entry_id="entry-aaa", unique_id="acct-a")
    second = make_entry(hass, entry_id="entry-bbb", unique_id="acct-b")
    # The later entry_id, so a win here cannot be the tie-break winning
    # by accident. `min` would pick "entry-aaa".
    own_row(hass, f"{BED_A}_sleep_score", second)

    for entry in (first, second):
        assert (
            migrations.resolve_bed_owner(hass, entry.entry_id, {BED_A})
            == second.entry_id
        )


async def test_resolve_bed_owner_never_returns_none(hass):
    """The callers compare it to an entry_id, so None would silently pass.

    A fresh install owns no rows at all, which is exactly when the old
    `bed_owner` returns None and the fallback has to take over.
    """
    entry = make_entry(hass, entry_id="entry-solo")
    assert migrations.resolve_bed_owner(hass, entry.entry_id, set()) == "entry-solo"
    assert migrations.resolve_bed_owner(hass, entry.entry_id, {BED_A}) == "entry-solo"


async def test_the_losing_entry_fails_once_with_an_actionable_reason(hass, patched):
    """Not SETUP_RETRY, and not a message that stops at naming the owner.

    The entry that loses a bed cannot fix itself by trying again. It has
    to say what a human should do, and then stop.
    """
    winner = make_entry(hass, entry_id="entry-winner", unique_id="acct-winner")
    hass.config_entries.async_update_entry(
        winner, data={**winner.data, CONF_DEVICE_IDS: [BED_A]}
    )
    own_row(hass, f"{BED_A}_sleep_score", winner)

    loser = make_entry(hass, entry_id="entry-loser", unique_id=ACCOUNT)
    await hass.config_entries.async_setup(loser.entry_id)
    await hass.async_block_till_done()

    assert loser.state is ConfigEntryState.SETUP_ERROR, (
        "the losing entry is in "
        f"{loser.state}. SETUP_RETRY re-runs the same doomed refresh on a "
        "backoff forever, burying the one line that explains the fix"
    )
    reason = str(loser.reason or "")
    assert "entry-winner" in reason, f"the owner is not named: {reason!r}"
    assert "Remove whichever entry" in reason, (
        "the failure names the owner but never says what to do about it. "
        f"The migration's copy of this refusal does: {reason!r}"
    )


async def test_the_winning_entry_still_loads(hass, patched):
    """Positive control. Refusing must be scoped to the entry that lost."""
    winner = make_entry(hass, entry_id="entry-winner", unique_id=ACCOUNT)
    own_row(hass, f"{BED_A}_sleep_score", winner)
    rival = make_entry(hass, entry_id="entry-zzz", unique_id="acct-z")
    hass.config_entries.async_update_entry(
        rival, data={**rival.data, CONF_DEVICE_IDS: [BED_A]}
    )

    await hass.config_entries.async_setup(winner.entry_id)
    await hass.async_block_till_done()

    assert winner.state is ConfigEntryState.LOADED, (
        "the entry that holds this bed's registry rows was locked out by a "
        f"rival that holds none. reason={winner.reason!r}"
    )
