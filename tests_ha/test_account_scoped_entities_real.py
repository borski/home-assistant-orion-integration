"""Account-scoped entities are built once, not once per bed.

Insights, schedules, the live session and the account configuration are
all returned per ACCOUNT. `/v2/insights` takes no device at all and
`/v1/sleep-schedules` is keyed on user id with no device in it, so an
entity built from either reads the same value no matter which bed it is
attached to.

They were built inside the per-device loop anyway. On a two-bed account
that produced two sleep scores, two HRVs, two apnea counts and two of
every schedule control, each pair reflecting one underlying value. A
night slept in one bed was recorded against the entities of both, which
is a biometric attribution error rather than cosmetic duplication, and
two schedule controls writing one stored row meant whichever the user
did not touch went stale until the next poll.

`migrations._planned_renames` knew about this and said so, describing the
3.0 change as "the half of that fix which does not need a device to test
against". This is the other half, and these are the tests that half was
missing.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.helpers import entity_registry as er

from custom_components.orion_sleep.const import (
    CONF_ACCOUNT_ID,
    CONF_DEVICE_IDS,
    CONF_UID_MIGRATION,
    DOMAIN,
)
from tests_ha.conftest import (
    ACCOUNT,
    BED_A,
    BED_B,
    ENTRY,
    SERIAL_A,
    SERIAL_B,
    FakeClient,
    device,
    make_entry,
)


class TwoBedClient(FakeClient):
    """One account, two Control Towers.

    The configuration this whole change is about, and the one the
    codebase has never had hardware for.
    """

    async def list_devices(self) -> list[dict[str, Any]]:
        return [device(BED_A, SERIAL_A), device(BED_B, SERIAL_B)]


@pytest.fixture
def two_beds(ws_manager):
    with (
        patch(
            "custom_components.orion_sleep.OrionApiClient",
            side_effect=lambda *a, **k: TwoBedClient(),
        ),
        patch(
            "custom_components.orion_sleep.coordinator.OrionWebSocketManager",
            return_value=ws_manager,
        ),
    ):
        yield


def unique_ids(hass, entry) -> list[str]:
    registry = er.async_get(hass)
    return [
        row.unique_id
        for row in er.async_entries_for_config_entry(registry, entry.entry_id)
    ]


async def test_two_beds_build_one_insight_family_not_two(hass, two_beds, caplog):
    """The finding.

    Against the pre-fix code this produces `bed-a_user_{account}_sleep_score`
    AND `bed-b_user_{account}_sleep_score`, both reading
    `coordinator.get_latest_session()`, which filters by nothing and
    returns the newest session on the account.

    The registry assertion and the log assertion catch different halves
    and both are needed. Keying these on the entry is what makes the
    duplicate impossible, so a platform that went back to building them
    inside the per-device loop would still leave ONE row in the registry.
    Home Assistant would reject the second as a duplicate unique_id and
    say so in the log, which is the only place that regression is
    visible.
    """
    entry = make_entry(
        hass, data={CONF_DEVICE_IDS: [BED_A, BED_B], CONF_ACCOUNT_ID: ACCOUNT}
    )
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED

    scores = [uid for uid in unique_ids(hass, entry) if uid.endswith("_sleep_score")]
    assert scores == [f"{ENTRY}_user_{ACCOUNT}_sleep_score"], (
        "the account's sleep score was built once per bed. Both entities "
        f"read the same account-wide session: {scores}"
    )

    duplicates = [
        record.message
        for record in caplog.records
        if "already registered" in record.message
        or "Platform orion_sleep does not generate unique IDs" in record.message
    ]
    assert not duplicates, (
        "an account-scoped entity was constructed more than once. It "
        "collapsed onto one registry row, so the duplication is invisible "
        f"except here: {duplicates}"
    )


async def test_two_beds_build_one_schedule_control_not_two(hass, two_beds):
    """The write-side half, which is worse than the read side.

    `PUT /v1/sleep-schedules` carries a user id and a weekday and no
    device, so two bedtime entities are two controls over one stored row.
    Setting one leaves the other showing the previous value until the
    next poll, and an automation reading the stale one acts on it.
    """
    entry = make_entry(
        hass, data={CONF_DEVICE_IDS: [BED_A, BED_B], CONF_ACCOUNT_ID: ACCOUNT}
    )
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    bedtimes = [uid for uid in unique_ids(hass, entry) if uid.endswith("_bedtime")]
    assert bedtimes == [f"{ENTRY}_user_{ACCOUNT}_bedtime"], (
        f"one stored schedule row got more than one control: {bedtimes}"
    )


async def test_account_level_settings_are_built_once(hass, two_beds):
    """Single values on `/v1/sleep-configurations`.

    Away mode used to be suppressed entirely on a multi-bed account,
    with a warning telling the household why they could not have it,
    because the only place to build it was the per-device loop. Now it is
    built once and the household gets the control.
    """
    entry = make_entry(
        hass, data={CONF_DEVICE_IDS: [BED_A, BED_B], CONF_ACCOUNT_ID: ACCOUNT}
    )
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    ids = unique_ids(hass, entry)
    for key in ("zone_split_mode", "away_mode", "temperature_display_unit"):
        matching = [uid for uid in ids if uid.endswith(f"_{key}")]
        assert matching == [f"{ENTRY}_{key}"], (
            f"{key} is one value on the account and got {len(matching)} "
            f"entities: {matching}"
        )


async def test_per_bed_entities_are_still_per_bed(hass, two_beds):
    """The other direction, which matters just as much.

    A power switch, a zone temperature and a topper heart rate genuinely
    belong to one bed. Collapsing those onto the entry would give a
    two-bed household one control for two beds, which is the same class
    of bug pointed the other way.
    """
    entry = make_entry(
        hass, data={CONF_DEVICE_IDS: [BED_A, BED_B], CONF_ACCOUNT_ID: ACCOUNT}
    )
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    ids = unique_ids(hass, entry)
    assert sorted(uid for uid in ids if uid.endswith("_power")) == sorted(
        [f"{BED_A}_power", f"{BED_B}_power"]
    ), "each bed needs its own power switch"
    assert f"{BED_A}_firmware_version" in ids and f"{BED_B}_firmware_version" in ids, (
        "firmware is per Control Tower and must not collapse onto the entry"
    )


async def test_a_3_0_row_is_rekeyed_in_place_and_keeps_its_history(hass, patched):
    """The upgrade path for anyone already on 3.0.

    3.0 moved these onto the person and left the bed in the prefix. The
    row has to move again, and the entity_id has to survive, because that
    is what recorder history and long-term statistics key on.
    """
    entry = make_entry(
        hass, data={CONF_DEVICE_IDS: [BED_A], CONF_ACCOUNT_ID: ACCOUNT}
    )
    registry = er.async_get(hass)
    existing = registry.async_get_or_create(
        "sensor",
        DOMAIN,
        f"{BED_A}_user_{ACCOUNT}_sleep_score",
        config_entry=entry,
    )
    before = existing.entity_id

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    rows = er.async_entries_for_config_entry(registry, entry.entry_id)
    moved = [r for r in rows if r.unique_id == f"{ENTRY}_user_{ACCOUNT}_sleep_score"]
    assert moved, "the 3.0 row was never moved off its per-bed id"
    assert moved[0].entity_id == before, (
        "entity_id changed, so recorder history and statistics are detached"
    )
    assert not [r for r in rows if r.entity_id.endswith("_2")], (
        "a duplicate was minted beside the original"
    )


async def test_the_downgrade_journal_still_points_at_the_2_x_id(hass, patched):
    """One hop, not two.

    The journal exists so `revert_unique_ids` can put every row back on
    the id 2.x asks for. A second record saying "3.1 came from 3.0" would
    make the revert walk backwards through two generations, and a
    two-generation registry is the one shape the revert documents itself
    as unable to resolve.

    So the re-key REWRITES the existing record's target and leaves its
    source alone.
    """
    entry = make_entry(
        hass, data={CONF_DEVICE_IDS: [BED_A], CONF_ACCOUNT_ID: ACCOUNT}
    )
    registry = er.async_get(hass)
    registry.async_get_or_create(
        "sensor",
        DOMAIN,
        f"{BED_A}_user_{ACCOUNT}_sleep_score",
        config_entry=entry,
    )

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    records = [
        record
        for record in (entry.data.get(CONF_UID_MIGRATION) or [])
        if record.get("new", "").endswith("_sleep_score")
    ]
    assert records, "nothing was journalled, so a downgrade strands this row"
    assert len(records) == 1, (
        f"more than one record describes one row, which is the two-generation "
        f"journal the revert cannot resolve: {records}"
    )
    assert records[0]["old"] == f"{BED_A}_sleep_score", (
        "the journal points at the 3.0 id rather than the 2.x one, so a "
        f"revert lands somewhere 2.x never asks for: {records[0]}"
    )
    assert records[0]["new"] == f"{ENTRY}_user_{ACCOUNT}_sleep_score"
