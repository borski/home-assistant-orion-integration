"""Pre-3.0 partner rows stop vouching for a partner who was replaced.

The journal eviction around this is correct. The leak sits beside it, and
every individual step in the sequence is a step something deliberately
decided to take.

1. A household upgrades from 2.x with partner A. Pre-3.0
   `{device}_partner_{key}` rows hold A's history. The forward migration
   leaves them alone on purpose, because a role-keyed row cannot prove
   which historical partner owns it.
2. The partner is replaced with B. `evict_partner_journal` drops A's
   rename records, correctly, because reverting them would hand A's
   entities to the id 2.x now feeds from B.
3. On the next reload B verifies. The journalling loop records a pair only
   when the OLD id is absent, and `{device}_partner_{key}` is still
   sitting there, so it records NOTHING for B.
4. `partner_unmapped` is computed as "a partner is linked AND no partner
   record exists AND no legacy partner rows exist". The legacy rows do
   exist, so the third clause is false and the refusal is suppressed. The
   revert reports itself ready for downgrade.
5. 2.x reads `{device}_partner_{key}`, which holds A's history, and writes
   B's heart rate and apnea into it.

The suppression is right for the household that upgraded and never
touched its partner, which is what it was written for. It is wrong the
moment the partner changes, and `_has_legacy_partner_rows` cannot tell
the difference on its own. A `_partner_` substring proves the row exists.
Nothing in the registry or the journal records who filled it, so the
config flow has to record the replacement at the moment it happens.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import Context
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er

from custom_components.orion_sleep.const import (
    CONF_ACCOUNT_ID,
    CONF_DEVICE_IDS,
    CONF_PARTNER_ACCESS_TOKEN,
    CONF_PARTNER_ACCOUNT_ID,
    CONF_PARTNER_AUTH_VALUE,
    CONF_PARTNER_DEVICE_SERIAL,
    CONF_PARTNER_EXPIRES_AT,
    CONF_PARTNER_REFRESH_TOKEN,
    CONF_PARTNER_REPLACED,
    DOMAIN,
)
from tests_ha.conftest import (
    ACCOUNT,
    BED_A,
    PARTNER,
    SERIAL_A,
    FakeClient,
    make_entry,
)

PARTNER_EMAIL = "bob@example.com"
PARTNER_TOKEN = "partner-at"
# The partner this household used to have. Their history is what the
# legacy rows contain, and they never appear in any profile response.
PREVIOUS_PARTNER = "33333333-3333-4333-8333-333333333333"


class PartnerClient(FakeClient):
    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.user = {"id": PARTNER, "name": "Bo", "email": PARTNER_EMAIL}


def clients(primary: FakeClient, partner: FakeClient, ws_manager):
    def _client(*_args: Any, **kwargs: Any) -> FakeClient:
        if kwargs.get("access_token") == PARTNER_TOKEN:
            return partner
        return primary

    return (
        patch("custom_components.orion_sleep.OrionApiClient", side_effect=_client),
        patch(
            "custom_components.orion_sleep.coordinator.OrionWebSocketManager",
            return_value=ws_manager,
        ),
    )


def partner_entry(hass, *, replaced: bool):
    """A partnered entry, optionally one whose partner has been changed.

    `CONF_DEVICE_IDS` is pre-recorded so the first poll does not rewrite
    the bed list and reload the entry mid-test.
    """
    return make_entry(
        hass,
        data={
            CONF_ACCOUNT_ID: ACCOUNT,
            CONF_DEVICE_IDS: [BED_A],
            CONF_PARTNER_ACCESS_TOKEN: PARTNER_TOKEN,
            CONF_PARTNER_REFRESH_TOKEN: "partner-rt",
            CONF_PARTNER_EXPIRES_AT: 9e12,
            CONF_PARTNER_DEVICE_SERIAL: SERIAL_A,
            CONF_PARTNER_AUTH_VALUE: PARTNER_EMAIL,
            CONF_PARTNER_ACCOUNT_ID: PARTNER,
            **({CONF_PARTNER_REPLACED: True} if replaced else {}),
        },
    )


def seed_legacy_partner_rows(hass, entry) -> list[str]:
    """The rows 2.x built for the PREVIOUS partner, still in the registry.

    Registered directly rather than by running a 2.x migration, because
    the shape is the whole point: a role-keyed id with no account in it,
    which is why nothing downstream can tell whose history it holds.

    EVERY partner key, not a representative sample. The journalling loop
    skips a pair only when the old id is already occupied, so seeding two
    keys leaves the other twenty-odd free to be journalled, a partner
    record then exists, and `partner_unmapped` is false for a reason that
    has nothing to do with the suppression under test. A real 2.x install
    built all of them, so seeding all of them is both the faithful state
    and the only one that reproduces the leak.

    Derived from the same description list the migration plans from,
    rather than a hand-written list of key names, so a new insight cannot
    quietly fall out of this fixture and re-open the gap.
    """
    from custom_components.orion_sleep.descriptions import (
        INSIGHT_SENSOR_DESCRIPTIONS,
    )

    registry = er.async_get(hass)
    created = []
    keys = [(d.key, "sensor") for d in INSIGHT_SENSOR_DESCRIPTIONS]
    keys.append(("session_active", "binary_sensor"))
    for key, domain in keys:
        row = registry.async_get_or_create(
            domain,
            DOMAIN,
            f"{BED_A}_partner_{key}",
            config_entry=entry,
            suggested_object_id=f"partner_{key}",
        )
        created.append(row.entity_id)
    return created


async def admin_context(hass) -> Context:
    user = await hass.auth.async_create_user("Admin", group_ids=["system-admin"])
    return Context(user_id=user.id)


async def revert(hass, entry):
    return await hass.services.async_call(
        DOMAIN,
        "revert_unique_ids",
        {"config_entry_id": entry.entry_id, "confirm": True},
        blocking=True,
        context=await admin_context(hass),
    )


async def test_a_replaced_partner_stops_legacy_rows_suppressing_the_refusal(
    hass, ws_manager
):
    """The finding.

    Against the pre-fix code this reports the downgrade ready, because the
    legacy rows suppress the refusal without anything asking whether the
    partner who filled them is still the partner.
    """
    entry = partner_entry(hass, replaced=True)
    seed_legacy_partner_rows(hass, entry)

    api, ws = clients(FakeClient(), PartnerClient(), ws_manager)
    with api, ws:
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        assert entry.state is ConfigEntryState.LOADED

        # The precondition the whole finding rests on. If a partner record
        # HAD been journalled for the current partner, the refusal would not
        # be suppressed by anything and this test would prove nothing.
        from custom_components.orion_sleep.const import CONF_UID_MIGRATION

        journalled = [
            record
            for record in (entry.data.get(CONF_UID_MIGRATION) or [])
            if record.get("role") == "partner"
        ]
        assert not journalled, (
            "a partner record was journalled for the current partner, so the "
            "legacy rows are not what is holding the revert together and this "
            f"test is not exercising the leak: {journalled}"
        )

        with pytest.raises(HomeAssistantError) as err:
            await revert(hass, entry)

    message = str(err.value).lower()
    assert "partner" in message, (
        f"the refusal has to be about the partner: {err.value}"
    )


async def test_the_refusal_names_the_rows_and_not_a_reload(
    hass, ws_manager, caplog
):
    """The remedy has to be one that can actually work.

    The generic unmapped message says to reload Orion and run the revert
    again. For this household that instruction can never succeed: the
    journalling loop skips the current partner for exactly as long as the
    legacy rows occupy the old ids, so every reload produces the same
    state. Telling them to reload is an infinite loop, with a two-person
    history merge at the end of it if they ever stop believing the message
    and install 2.x anyway.

    Asserted against the log `migrations` emits rather than the exception
    text. The exception is raised by `__init__.py`, which reads a single
    `partner_unmapped` boolean and cannot yet tell these two situations
    apart. Distinguishing the user-facing text needs a change in that
    file, which this pass does not own, so the distinct wording lives at
    the layer that actually knows.
    """
    entry = partner_entry(hass, replaced=True)
    seed_legacy_partner_rows(hass, entry)

    api, ws = clients(FakeClient(), PartnerClient(), ws_manager)
    with api, ws:
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        caplog.clear()
        with pytest.raises(HomeAssistantError):
            await revert(hass, entry)

    logged = "\n".join(
        record.message
        for record in caplog.records
        if record.name.endswith("migrations")
    )
    assert "previous partner" in logged.lower(), (
        "nothing told the household that the pre-3.0 rows hold the previous "
        f"partner's history, which is the whole finding: {logged}"
    )
    assert "_partner_" in logged, (
        "the message does not say which rows to delete, and the household "
        f"cannot act on it: {logged}"
    )
    assert "delete" in logged.lower(), (
        "the message does not name the one remedy that can work. Reloading "
        f"produces this same state forever: {logged}"
    )


async def test_an_untouched_partner_upgrade_is_still_suppressed(hass, ws_manager):
    """The other half, and the reason the suppression exists.

    A household that upgraded from 2.x and never touched its partner is in
    no danger at all. 2.x finds the legacy rows exactly where it left them,
    still holding the same person's history. Refusing here is what made the
    revert raise for every partnered household that upgraded, on a revert
    that had genuinely completed, so removing the suppression outright
    would trade one bug for a louder one.
    """
    entry = partner_entry(hass, replaced=False)
    seed_legacy_partner_rows(hass, entry)

    api, ws = clients(FakeClient(), PartnerClient(), ws_manager)
    with api, ws:
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        assert entry.state is ConfigEntryState.LOADED

        # No raise. The revert may report other things, but it must not
        # refuse over a partner whose rows still belong to them.
        await revert(hass, entry)


async def test_replacing_a_partner_records_that_it_happened(hass, ws_manager):
    """The marker's write path.

    The revert cannot derive this. Nothing in the registry or the journal
    survives to say a partner was swapped, so the config flow has to record
    it at the one moment anything knows.
    """
    from custom_components.orion_sleep.config_flow import OrionSleepOptionsFlow

    entry = partner_entry(hass, replaced=False)
    assert not entry.data.get(CONF_PARTNER_REPLACED)

    flow = OrionSleepOptionsFlow(entry)
    flow.hass = hass

    flow._write_partner_change(
        {
            **entry.data,
            CONF_PARTNER_ACCESS_TOKEN: "new-partner-at",
            CONF_PARTNER_ACCOUNT_ID: PREVIOUS_PARTNER,
        }
    )

    assert entry.data.get(CONF_PARTNER_REPLACED) is True, (
        "a partner was replaced and nothing recorded it, so the revert has "
        "no way to know the legacy rows hold somebody else's history"
    )


async def test_a_first_time_partner_link_is_not_a_replacement(hass, ws_manager):
    """The marker must not fire for a household linking its first partner.

    Setting it unconditionally would refuse the downgrade for every
    partnered household, which is the exact over-refusal the suppression
    was added to stop.
    """
    from custom_components.orion_sleep.config_flow import OrionSleepOptionsFlow

    entry = make_entry(
        hass,
        data={CONF_ACCOUNT_ID: ACCOUNT, CONF_DEVICE_IDS: [BED_A]},
    )

    flow = OrionSleepOptionsFlow(entry)
    flow.hass = hass

    flow._write_partner_change(
        {
            **entry.data,
            CONF_PARTNER_ACCESS_TOKEN: PARTNER_TOKEN,
            CONF_PARTNER_ACCOUNT_ID: PARTNER,
        }
    )

    assert not entry.data.get(CONF_PARTNER_REPLACED), (
        "linking a partner for the first time was recorded as a replacement, "
        "which refuses the downgrade for households in no danger"
    )
