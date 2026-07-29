"""A recorded serial is not a verified partner.

`partner_mapping_valid` gates whether the partner's biometric entities
are built at all, and it gates `_partner_recovery_renames`, which decides
which partner owns which unique_id in the downgrade journal. Two loads
that matter, both answered by one flag.

It used to initialise to `bool(partner_device_serial)`, which answers a
third question entirely: whether a serial is written in the config entry.
That is true from the moment a partner is linked and stays true forever,
including on a boot where the partner has never been fetched even once.
Its neighbour `partner_identity_confirmed` initialises to False with a
comment saying nothing has been confirmed before the first fetch, and the
two sat sixteen lines apart disagreeing.

The disagreement is reachable inside a single try block.
`_async_refresh_partner_identity` calls `get_current_user()` and then
`list_devices()`. When the first returns and the second throws,
`partner_user` is populated, neither assignment below runs, and the flag
still holds whatever the constructor invented. A partner nothing has
verified then answers yes to `has_partner_for_device`.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

from homeassistant.config_entries import ConfigEntryState
from orion_sleep_api import OrionConnectionError

from custom_components.orion_sleep.const import (
    CONF_ACCOUNT_ID,
    CONF_DEVICE_IDS,
    CONF_PARTNER_ACCESS_TOKEN,
    CONF_PARTNER_ACCOUNT_ID,
    CONF_PARTNER_AUTH_VALUE,
    CONF_PARTNER_DEVICE_SERIAL,
    CONF_PARTNER_EXPIRES_AT,
    CONF_PARTNER_REFRESH_TOKEN,
    CONF_UID_MIGRATION,
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


class HalfAnsweringPartnerClient(FakeClient):
    """Answers the profile call and drops the device call.

    Path (b). One try block, two requests, and the failure lands on the
    second. This is not exotic. It is one connection dying between two
    ordinary HTTP calls.
    """

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.user = {"id": PARTNER, "name": "Bo", "email": PARTNER_EMAIL}
        self.fail_devices = OrionConnectionError("connection reset by peer")


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


def partner_entry(hass, *, journal: list[dict[str, Any]] | None = None):
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
            **({CONF_UID_MIGRATION: journal} if journal else {}),
        },
    )


async def test_a_recorded_serial_alone_does_not_make_the_mapping_valid(
    hass, ws_manager
):
    """The constructor default, tested directly.

    Built by hand rather than through setup, because the whole point is
    the value the flag holds BEFORE any fetch has run. Anything that
    reaches the server overwrites it and hides the finding.

    Async purely so there is a running loop for the client session the
    coordinator builds. Nothing here awaits.
    """
    from custom_components.orion_sleep.coordinator import OrionDataUpdateCoordinator

    entry = partner_entry(hass)
    with patch(
        "custom_components.orion_sleep.coordinator.OrionWebSocketManager",
        return_value=ws_manager,
    ):
        coordinator = OrionDataUpdateCoordinator(
            hass, entry, FakeClient(), partner_api_client=FakeClient()
        )

    assert coordinator.partner_mapping_valid is False, (
        "a partner nothing has ever fetched was reported as validly mapped, "
        "purely because a device serial is written in the config entry"
    )
    assert (
        coordinator.partner_mapping_valid is coordinator.partner_identity_confirmed
    ), (
        "the two partner trust flags disagree before the first fetch. That "
        "pairing is what lets the migration produce partner rename pairs for "
        "a partner it cannot vouch for"
    )


async def test_a_partner_whose_device_call_fails_is_not_a_verified_partner(
    hass, ws_manager
):
    """Path (b), end to end.

    The profile call succeeds and the device call throws, so `partner_user`
    is populated and neither verdict assignment is reached. With the old
    fail-open default the entry finished setup believing the partner was
    validly mapped, on the strength of nothing but a recorded serial.

    The entry is asserted LOADED first. A partner fault must never take
    the primary account down with it, and an entry that failed setup would
    satisfy every assertion below for the wrong reason.
    """
    entry = partner_entry(hass)
    partner = HalfAnsweringPartnerClient()

    api, ws = clients(FakeClient(), partner, ws_manager)
    with api, ws:
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED
    coordinator = entry.runtime_data
    assert coordinator.partner_user.get("id") == PARTNER, (
        "the profile call was supposed to succeed, so this test is not "
        "exercising the half-answered fetch it claims to"
    )

    assert coordinator.partner_mapping_valid is False, (
        "the partner device list never arrived, so nothing established that "
        "these two accounts still share one bed, yet the mapping is reported "
        "valid. This flag gates whether the partner's biometric entities are "
        "built and which partner the downgrade journal names"
    )
    assert coordinator.has_partner_for_device(BED_A) is False, (
        "a partner this setup could not verify was accepted for the bed"
    )

    journalled = [
        record
        for record in (entry.data.get(CONF_UID_MIGRATION) or [])
        if record.get("role") == "partner"
    ]
    assert not [record for record in journalled if not record.get("stale")], (
        "partner rename records were written by a setup that never confirmed "
        "the partner. Applying these on a downgrade renames one person's "
        f"entities onto the ids 2.x feeds from another: {journalled}"
    )


async def test_a_transient_partner_failure_does_not_delete_the_partners_entities(
    hass, ws_manager
):
    """The guard rail on the other side of `partner_mapping_valid`.

    Tightening that flag is right, and there is one specific tightening
    that is catastrophic. Making it a read-only property returning
    `partner_topology_ok and partner_identity_confirmed` reads as pure
    tidying, passed every test in this suite when it was tried, and
    silently reintroduces the failure the OrionApiError handler in
    `_async_refresh_partner_identity` exists to prevent.

    Identity goes False on any transient partner error, deliberately, so
    that the migration does not mistake a dropped connection for proof the
    partner was replaced. `has_partner_for_device` gates entity
    CONSTRUCTION on `partner_mapping_valid`, so deriving that flag from
    identity means one unlucky 800ms during a restart removes a person's
    biometric entities from the system entirely. Measured, not assumed:
    the derived version built 0 partner entities here where this builds
    dozens.

    The first fetch succeeds and the second fails, which is the ordinary
    shape of a blip: `partner_user` is populated and stays populated, and
    the last SUCCESSFUL fetch established a valid mapping that no
    subsequent failure has disproved.
    """
    from tests_ha.test_partner_journal_split_real import (
        FlakySecondFetchPartnerClient,
    )

    entry = partner_entry(hass)
    partner = FlakySecondFetchPartnerClient()

    api, ws = clients(FakeClient(), partner, ws_manager)
    with api, ws:
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED
    coordinator = entry.runtime_data
    assert partner.fetches >= 2, "the second fetch never failed, so no blip"
    assert coordinator.partner_identity_confirmed is False, (
        "the transient failure was supposed to withdraw identity confirmation"
    )

    assert coordinator.partner_mapping_valid is True, (
        "one dropped connection withdrew a mapping verdict that a previous "
        "successful fetch established. Nothing disproved it, and this flag "
        "decides whether the partner's entities exist at all"
    )
    assert coordinator.has_partner_for_device(BED_A) is True, (
        "the bed stopped recognising its partner because of one failed "
        "request"
    )

    from homeassistant.helpers import entity_registry as er

    registry = er.async_get(hass)
    partner_rows = [
        row
        for row in er.async_entries_for_config_entry(registry, entry.entry_id)
        if PARTNER in row.unique_id
    ]
    assert partner_rows, (
        "no partner entities were built at all. A transient partner error "
        "must leave the person's entities in place and merely unavailable, "
        "because an entity that is never constructed takes its recorder "
        "history and every automation referencing it down with it"
    )
