"""A partner the server did not answer about is not a partner who changed.

The downgrade journal maps 3.x unique_ids back to the ids 2.x asks for,
and the partner half of it used to be deleted on every migration pass,
unconditionally. The stated reason was sound. 2.x has exactly one
role-keyed `{device}_partner_{key}` row per key and feeds it from
whichever partner account is linked at the time, so reverting a record
that names a REPLACED partner puts that person's rows on the id the
current partner's heart rate then lands on.

What the delete could not do was tell "replaced" from "unreachable".
Both arrive at the migration as an unverified partner, and
`async_migrate_unique_ids` runs twice per setup. So a restart that
happened to land inside a brief network interruption discarded the
household's entire partner rollback record, announced it with one
warning, and never rebuilt it: the next revert reported the partner
unmapped and refused outright.

These tests pin the split. A replacement this setup can PROVE still
evicts. Anything short of proof keeps the records and marks them stale,
which withholds the revert without withholding the data.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import Context
from homeassistant.exceptions import HomeAssistantError
from orion_sleep_api import OrionConnectionError

from custom_components.orion_sleep.const import (
    CONF_ACCOUNT_ID,
    CONF_PARTNER_ACCESS_TOKEN,
    CONF_PARTNER_ACCOUNT_ID,
    CONF_PARTNER_AUTH_VALUE,
    CONF_PARTNER_DEVICE_SERIAL,
    CONF_PARTNER_EXPIRES_AT,
    CONF_PARTNER_REFRESH_TOKEN,
    CONF_UID_MIGRATION,
    CONF_UID_RECOVERY_ACTIVE,
    DOMAIN,
)
from tests_ha.conftest import (
    ACCOUNT,
    BED_A,
    ENTRY,
    PARTNER,
    SERIAL_A,
    FakeClient,
    make_entry,
)

PARTNER_EMAIL = "bob@example.com"
PARTNER_TOKEN = "partner-at"
# The partner this household used to have. Only ever appears in a seeded
# journal record, never in a profile, which is what makes it "previous".
PREVIOUS_PARTNER = "33333333-3333-4333-8333-333333333333"


class PartnerClient(FakeClient):
    """The linked partner's own session, optionally unreachable.

    A real subclass rather than a Mock for the same reason `FakeClient`
    is one. A method this test does not model should fail loudly instead
    of returning something truthy.
    """

    def __init__(self, fail: Exception | None = None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.user = {"id": PARTNER, "name": "Bo", "email": PARTNER_EMAIL}
        self.fail_user = fail

    async def get_current_user(self) -> dict[str, Any]:
        self.calls.append("get_current_user")
        if self.fail_user is not None:
            raise self.fail_user
        return dict(self.user)


def clients(primary: FakeClient, partner: FakeClient, ws_manager):
    """Patch both sessions, dispatching on the token they were handed.

    Deliberately NOT an alternating side_effect. `async_setup_entry`
    builds the primary client and then the partner client, so a counter
    works right up until anything reloads or an extra client is built,
    at which point the two swap and the test starts asserting about the
    wrong account while still passing.
    """

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


def partner_entry(hass, *, journal: list[dict[str, Any]] | None = None):
    """An entry whose partner is linked AND recorded.

    `CONF_PARTNER_ACCOUNT_ID` is what makes the identity check decidable
    rather than merely unverified, so leaving it out would give every
    test here the unverified outcome and the eviction half would never
    run.
    """
    return make_entry(
        hass,
        data={
            CONF_ACCOUNT_ID: ACCOUNT,
            CONF_PARTNER_ACCESS_TOKEN: PARTNER_TOKEN,
            CONF_PARTNER_REFRESH_TOKEN: "partner-rt",
            CONF_PARTNER_EXPIRES_AT: 9e12,
            CONF_PARTNER_DEVICE_SERIAL: SERIAL_A,
            CONF_PARTNER_AUTH_VALUE: PARTNER_EMAIL,
            CONF_PARTNER_ACCOUNT_ID: PARTNER,
            **({CONF_UID_MIGRATION: journal} if journal else {}),
        },
    )


def partner_record(user_id: str, key: str = "sleep_score") -> dict[str, Any]:
    return {
        "domain": "sensor",
        "platform": DOMAIN,
        "old": f"{BED_A}_partner_{key}",
        "new": f"{ENTRY}_user_{user_id}_{key}",
        "role": "partner",
    }


def partner_records(entry) -> list[dict[str, Any]]:
    return [
        record
        for record in (entry.data.get(CONF_UID_MIGRATION) or [])
        if record.get("role") == "partner"
    ]


async def admin_context(hass) -> Context:
    user = await hass.auth.async_create_user("Admin", group_ids=["system-admin"])
    return Context(user_id=user.id)


async def test_an_unreachable_partner_does_not_wipe_the_journal(hass, ws_manager):
    """The highest-value test in this file.

    One dropped connection during one restart used to be enough to
    destroy the only description of where this partner's entities came
    from. There is no self-healing path afterwards: the records are gone,
    so the next revert reports the partner unmapped and refuses, and the
    household is told to reload and try again forever.

    The entry is asserted LOADED first. A partner fetch that took setup
    down with it would empty the journal for a completely different
    reason and this assertion would pass on a broken integration.
    """
    seeded = partner_record(PARTNER)
    entry = partner_entry(hass, journal=[seeded])
    partner = PartnerClient(fail=OrionConnectionError("no route to host"))

    api, ws = clients(FakeClient(), partner, ws_manager)
    with api, ws:
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED
    assert "get_current_user" in partner.calls, "the partner was never fetched"

    kept = partner_records(entry)
    assert kept, (
        "a transient partner fetch failure deleted every partner rename "
        "record. The downgrade path for that person is now gone and nothing "
        "rebuilds it"
    )
    assert [r for r in kept if r["new"] == seeded["new"]], (
        f"the seeded record did not survive: {kept}"
    )
    assert all(r.get("stale") for r in kept), (
        "records kept across an unverified partner must be marked stale, or "
        "a revert will apply mappings this setup could not vouch for"
    )


async def test_a_verified_partner_replacement_still_evicts_the_previous_partner(
    hass, ws_manager
):
    """The other half of the split, and the reason the delete existed.

    Retaining everything would be a safe-looking change that reopened the
    original hole. When the setup CAN prove who the partner is, a record
    naming somebody else has to go.
    """
    seeded = partner_record(PREVIOUS_PARTNER)
    entry = partner_entry(hass, journal=[seeded])

    api, ws = clients(FakeClient(), PartnerClient(), ws_manager)
    with api, ws:
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED
    surviving = partner_records(entry)
    assert not [r for r in surviving if PREVIOUS_PARTNER in r["new"]], (
        "a record naming the previous partner survived a setup that proved "
        "the partner is somebody else. Reverting it hands their entities to "
        f"the id 2.x now feeds from the current partner: {surviving}"
    )
    assert [r for r in surviving if PARTNER in r["new"]], (
        "the current partner was verified but no records were rebuilt for "
        "them, so a downgrade would strand their history"
    )
    assert not any(r.get("stale") for r in surviving), (
        "records rebuilt from a verified partner must not be marked stale, "
        "or the revert refuses forever"
    )


async def test_a_stale_partner_record_blocks_the_revert_and_survives_it(
    hass, ws_manager
):
    """Refusing is only safe because nothing is thrown away when it does.

    A stale record must not be applied, because it may name the previous
    partner. It must also not be dropped, because it is the only copy.
    The service therefore has to refuse AND leave the journal intact, and
    it has to say which of the two partner problems this is: the generic
    "no mappings were recorded" text sends the user to relink an account
    that never changed.
    """
    entry = partner_entry(hass, journal=[partner_record(PARTNER)])
    partner = PartnerClient(fail=OrionConnectionError("connection reset"))

    api, ws = clients(FakeClient(), partner, ws_manager)
    with api, ws:
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        assert partner_records(entry), "nothing stale to test with"

        with pytest.raises(HomeAssistantError) as err:
            await hass.services.async_call(
                DOMAIN,
                "revert_unique_ids",
                {"config_entry_id": entry.entry_id, "confirm": True},
                blocking=True,
                context=await admin_context(hass),
            )

    assert "could not confirm" in str(err.value).lower(), (
        "the refusal has to name the actual problem. Telling this user no "
        "partner mappings were recorded points them at a partner change that "
        f"did not happen: {err.value}"
    )
    assert partner_records(entry), (
        "the revert refused and then discarded the records it refused over, "
        "so the next run has nothing left to refuse about and will report a "
        "clean success"
    )
    assert not entry.data.get(CONF_UID_RECOVERY_ACTIVE), (
        "a refused revert left the latch set, so 3.x now refuses to load"
    )
