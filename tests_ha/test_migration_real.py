"""Migration behaviour against a real Home Assistant entity registry.

Every test here targets a defect the `ast` suite is structurally unable to
see, because each one is about what the code DOES rather than how it is
written.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.helpers import entity_registry as er

from custom_components.orion_sleep.const import (
    CONF_ACCOUNT_ID,
    CONF_DEVICE_IDS,
    DOMAIN,
)
from tests_ha.conftest import ACCOUNT, BED_A, BED_B, SERIAL_B, device, make_entry


def row(hass, unique_id: str, entry, *, domain: str = "sensor"):
    return er.async_get(hass).async_get_or_create(
        domain, DOMAIN, unique_id, config_entry=entry
    )


def orion_unique_ids(hass, entry_id: str) -> set[str]:
    registry = er.async_get(hass)
    return {
        e.unique_id
        for e in er.async_entries_for_config_entry(registry, entry_id)
    }


async def test_a_two_generation_registry_is_not_silently_accepted(hass, patched):
    """Upgrade, downgrade without reverting, come back.

    2.x does not find the 3.x ids, mints a second row per key on the old
    id, and writes real sleep data there. Coming back to 3.x leaves one
    person's history split across `sensor.x` and `sensor.x_2`, forever.

    Whatever the integration does here, it must not do it silently. The
    registry shape this produces is also the one `revert_unique_ids`
    cannot resolve, so a silent accept costs the user the rollback path
    as well as the history.
    """
    entry = make_entry(hass, data={CONF_DEVICE_IDS: [BED_A], CONF_ACCOUNT_ID: ACCOUNT})
    old_id = f"{BED_A}_sleep_score"
    new_id = f"{BED_A}_user_{ACCOUNT}_sleep_score"
    row(hass, new_id, entry)   # survived the upgrade
    row(hass, old_id, entry)   # minted by 2.x during the downgrade

    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    if entry.state is ConfigEntryState.LOADED:
        pytest.fail(
            "setup succeeded with both generations of sleep_score present, so "
            "one person's history is split across two entities and nothing "
            "told the user"
        )


@pytest.mark.xfail(
    reason=(
        "KNOWN DEFECT, no fix landed. A setup that fails still leaves its bed "
        "claim behind, and on the next start that claim locks out the entry "
        "which legitimately owns the bed. Withdrawing the claim in the setup "
        "teardown was tried and reverted: the coordinator re-publishes it on "
        "every attempt, so withdrawing changes entry data, which triggers "
        "another setup, which re-publishes. That reload loop is worse than "
        "the bug. The real fix is a deterministic owner among conflicting "
        "claims rather than first-writer-wins, which is a design change."
    ),
    strict=True,
)
async def test_a_failed_setup_withdraws_the_bed_claim_it_published(hass, patched):
    """A claim must not outlive the setup that published it.

    `async_setup_entry` writes the claim before the sibling barrier,
    deliberately, because if nobody wrote first two entries starting
    together would wait on each other forever. The consequence is that a
    setup which then fails has already published a claim to the bed, and
    `overlapping_entry_ids` cannot tell that apart from a claim held by a
    healthy entry. Left behind, it locks out whoever actually owns the bed.

    Forced here by making a step AFTER the write raise, because the
    coordinator's own overlap check is correctly ordered and would
    otherwise stop setup before the write is reached.
    """
    entry = make_entry(hass)
    boom = RuntimeError("something after the identity write")

    with patch(
        "custom_components.orion_sleep.async_migrate_unique_ids",
        side_effect=boom,
    ):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert entry.state is not ConfigEntryState.LOADED
    assert not entry.data.get(CONF_DEVICE_IDS), (
        "setup failed but left its bed claim behind. On the next restart "
        "that claim locks out the entry that legitimately owns the bed"
    )


async def test_two_entries_on_one_bed_do_not_lock_each_other_out(hass, patched):
    """The shape this integration exists to serve, as 2.x permitted it.

    Two people, one bed, one config entry each. 2.x had no cross-entry
    check, so this arrangement exists in the wild. Whatever 3.0 decides
    to do about it, exactly one of the two must be able to run.
    """
    a = make_entry(hass, entry_id="entry-a", unique_id="acct-a")
    b = make_entry(hass, entry_id="entry-b", unique_id="acct-b")

    # Setting up one entry makes Home Assistant set up every entry for the
    # domain, which is what happens at a real startup. Both run from here.
    await hass.config_entries.async_setup(a.entry_id)
    await hass.async_block_till_done()

    loaded = [e for e in (a, b) if e.state is ConfigEntryState.LOADED]
    assert loaded, f"neither entry loaded: a={a.state}, b={b.state}"

    # The invariant that decides whether the NEXT restart works. A claim is
    # what `overlapping_entry_ids` reads, so one left behind by an entry
    # that did not load is indistinguishable from one held by the entry
    # that did, and on the following start they lock each other out.
    for entry in (a, b):
        if entry.state is not ConfigEntryState.LOADED:
            assert not entry.data.get(CONF_DEVICE_IDS), (
                f"{entry.entry_id} is {entry.state} but still claims "
                f"{entry.data.get(CONF_DEVICE_IDS)}. That claim will lock out "
                "the entry that loaded correctly on the next restart"
            )


async def test_migration_preserves_entity_ids_and_mints_no_duplicates(hass, patched):
    """The invariant the whole migration exists to protect.

    Recorder history and long-term statistics key on entity_id. A rename
    that changes one, or that creates a `_2` twin, detaches history.
    """
    entry = make_entry(hass, data={CONF_DEVICE_IDS: [BED_A], CONF_ACCOUNT_ID: ACCOUNT})
    legacy = row(hass, f"{BED_A}_sleep_score", entry)
    before = legacy.entity_id

    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    registry = er.async_get(hass)
    rows = er.async_entries_for_config_entry(registry, entry.entry_id)
    moved = [e for e in rows if e.unique_id == f"{BED_A}_user_{ACCOUNT}_sleep_score"]
    assert moved, "the legacy row was never re-keyed"
    assert moved[0].entity_id == before, "entity_id changed, history is detached"
    assert not [e for e in rows if e.entity_id.endswith("_2")], "a duplicate was minted"


async def test_migration_is_idempotent_across_restarts(hass, patched):
    entry = make_entry(hass, data={CONF_DEVICE_IDS: [BED_A], CONF_ACCOUNT_ID: ACCOUNT})
    row(hass, f"{BED_A}_sleep_score", entry)

    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    first = orion_unique_ids(hass, entry.entry_id)

    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()

    assert orion_unique_ids(hass, entry.entry_id) == first
    assert entry.state is ConfigEntryState.LOADED


async def test_removing_a_bed_does_not_permanently_break_setup(hass, patched, client):
    """Selling a bed is a supported action, not a fatal condition."""
    client.devices = [device(BED_A, "AA11BB22CC33"), device(BED_B, SERIAL_B)]
    entry = make_entry(hass)

    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED

    client.devices = [device(BED_B, SERIAL_B)]
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED, (
        "removing one bed from a two-bed account broke setup permanently"
    )
