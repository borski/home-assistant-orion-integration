"""One run must not produce a half-trusted partner journal.

`async_migrate_unique_ids` reads the partner rename pairs TWICE. Once in
`_reconcile_partner_journal`, which decides what to do with records a
previous run wrote, and once in the journalling loop below it, which
records pairs this run discovered. Both are statements about which
partner owns which unique_id, so both have to answer to the same rule.

They did not. The reconcile refused to trust an unconfirmed partner and
the loop never asked, so a single setup could stamp `stale` on every
pre-existing partner record and then write freshly discovered ones with
no flag at all. Same partner, same run, half the journal trusted.

That is not a cosmetic inconsistency. `async_revert_unique_ids` applies
records without `stale` and skips records with it, so it renames half of
one person's entities onto the role-keyed `{device}_partner_{key}` ids
2.x feeds from whoever is linked now, and only then raises
`partner_stale`. The registry has already been mutated by the time the
refusal arrives. Two people's heart rate and apnea history merge, via
the mechanism written to prevent it.

The state that gets there is ordinary. `_async_refresh_partner_identity`
runs twice inside one `async_config_entry_first_refresh`, once from
`_async_setup` and once from `_async_update_data`. The first can succeed
and the second can hit a dropped connection, which leaves `partner_user`
populated from the successful call while `partner_identity_confirmed`
goes false. `partner_mapping_valid` is deliberately left alone by that
handler, so the partner still resolves for the bed and pairs are still
produced.
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
    DOMAIN,
)
from custom_components.orion_sleep.migrations import _partner_recovery_renames
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


class FlakySecondFetchPartnerClient(FakeClient):
    """A partner whose SECOND identity fetch of the setup fails.

    The first fetch has to succeed, because that is what populates
    `partner_user` and lets `has_partner_for_device` resolve. A client
    that fails from the start produces an empty `partner_user`, no pairs,
    and therefore the safe path, which is the state this test is not
    about.
    """

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.user = {"id": PARTNER, "name": "Bo", "email": PARTNER_EMAIL}
        self.fetches = 0

    async def get_current_user(self) -> dict[str, Any]:
        self.calls.append("get_current_user")
        self.fetches += 1
        if self.fetches >= 2:
            raise OrionConnectionError("connection reset by peer")
        return dict(self.user)


def clients(primary: FakeClient, partner: FakeClient, ws_manager):
    """Patch both sessions, dispatching on the token they were handed.

    Deliberately not an alternating side_effect. Setup builds the primary
    first and the partner second, so a counter silently swaps the two the
    moment anything reloads and the test then asserts about the wrong
    account while still passing.
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
    """An established entry whose partner is linked AND recorded.

    `CONF_DEVICE_IDS` is pre-recorded on purpose, and it is load bearing
    rather than tidiness. Without it the first poll finds the bed list
    unrecorded, writes it, sets `topology_changed`, and reloads the entry.
    That reload runs `async_migrate_unique_ids` two more times against a
    registry that now has rows and a journal that now has records, and its
    first pass sweeps every partner record to `stale` before the loop can
    add anything. The mixed journal this test is about is real and is
    written to the entry, but the extra setup then tidies it away, so the
    assertion passes on a broken build.

    Recording the beds is also the more representative state. A household
    that has restarted even once has them, so this is an ordinary restart
    with a flaky partner rather than a first install.
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


async def test_an_unconfirmed_partner_never_leaves_half_the_journal_trusted(
    hass, ws_manager
):
    """The split-brain test.

    Fails against the pre-fix code with a mixed journal: the seeded record
    carries `stale` because the reconcile distrusted it, and the records
    the loop wrote in the same pass do not, because the loop was never
    given the same rule.
    """
    seeded = partner_record(PARTNER)
    entry = partner_entry(hass, journal=[seeded])
    partner = FlakySecondFetchPartnerClient()

    api, ws = clients(FakeClient(), partner, ws_manager)
    with api, ws:
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    # Asserted first. A partner fetch that took the whole setup down would
    # leave an untouched journal for an entirely unrelated reason, and
    # every assertion below would pass against a broken integration.
    assert entry.state is ConfigEntryState.LOADED
    assert partner.fetches >= 2, (
        "the second partner fetch never happened, so the state this test is "
        f"about was never reached: {partner.fetches} fetch(es)"
    )

    kept = partner_records(entry)
    assert kept, "the partner journal was wiped, which is a different bug"

    trusted = [record for record in kept if not record.get("stale")]
    assert not trusted, (
        "this run could not confirm which account the partner tokens belong "
        "to, yet it wrote partner records a revert will act on. A revert now "
        "renames these and skips the stale ones, so it half-applies the "
        "partner rename and mutates the registry before it refuses: "
        f"{trusted}"
    )


async def test_an_unconfirmed_partner_produces_no_rename_pairs_at_all(hass):
    """The veto itself, tested where it now lives.

    The behavioural test above is the one that matters, but it can only
    observe the journal that came out the far end. This pins the rule at
    its source, so moving the veto back downstream to a single consumer
    fails here even if some future caller happens to compensate.
    """

    entry = make_entry(hass)

    class Coordinator:
        partner_user = {"id": PARTNER}
        partner_identity_confirmed = False
        devices = [{"id": BED_A, "serial_number": SERIAL_A}]

        def has_partner_for_device(self, _device_id: str) -> bool:
            return True

    assert _partner_recovery_renames(entry, Coordinator()) == [], (
        "an unconfirmed partner emitted rename pairs. Every consumer of "
        "these pairs treats them as a claim about which partner owns an id, "
        "and this setup is not in a position to make that claim"
    )

    # The same coordinator, one attribute different, has to still work.
    # A veto that never lets anything through would pass the assertion
    # above while silently disabling the partner downgrade path entirely.
    Coordinator.partner_identity_confirmed = True
    assert _partner_recovery_renames(entry, Coordinator()), (
        "a confirmed partner produced no pairs, so nothing is journalled "
        "for them and a downgrade strands their history"
    )


async def test_a_coordinator_that_cannot_answer_is_not_a_yes(hass):
    """The `getattr` default, which used to be True.

    A coordinator carrying no `partner_identity_confirmed` at all is not
    a coordinator that confirmed the partner. The old default read the
    absence of the attribute as permission, which is the same fail-open
    shape as the flag's own initial value.
    """

    entry = make_entry(hass)

    class Coordinator:
        partner_user = {"id": PARTNER}
        devices = [{"id": BED_A, "serial_number": SERIAL_A}]

        def has_partner_for_device(self, _device_id: str) -> bool:
            return True

    assert _partner_recovery_renames(entry, Coordinator()) == [], (
        "a coordinator that does not carry the identity flag was treated as "
        "having confirmed the partner"
    )
