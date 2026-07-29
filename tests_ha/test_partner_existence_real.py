"""A partner who exists in the config is a partner whose entities exist.

The bug these pin is the mirror image of the one
`test_partner_transient_real.py` covers, and it survived that whole wave
because the two look like the same thing and are not.

That wave fixed the WARM case. An entry whose partner had already been
fetched successfully kept its verdict, and its entities, across a later
dropped connection. What it could not fix from where it stood is the COLD
case, because on a cold start there is no previous verdict to preserve.
If the very FIRST partner fetch of a run failed, `partner_user` stayed
empty and `partner_mapping_valid` stayed False, and `sensor.py` and
`binary_sensor.py` gated entity CONSTRUCTION on that. So one failed HTTP
request at boot meant the partner's sleep score, heart rate, HRV and
apnea entities did not exist in Home Assistant. Not unavailable. Absent.
Every dashboard card and automation naming them broke, with nothing in
the log connecting that to an 800ms blip, and no recovery short of
reloading the entry by hand.

The fix separates two questions that had been folded into one predicate.

  EXISTENCE is a fact about configuration. Partner tokens and a partner
  serial are written on the config entry, so this household has a partner
  and their entities should exist. `has_partner_configured_for_device`
  answers this and gates construction.

  TRUST is a fact about this session. The last successful fetch
  established that these accounts still share one bed and that the
  partner is the one we recorded. `has_partner_for_device` answers this,
  is unchanged, and gates availability, data, and every journal record.

The hazard the split has to keep closed is that an unverified partner's
id must never reach a downgrade journal record or a rename. 2.x has
exactly one role-keyed row per partner key, so a record naming the wrong
partner hands the previous partner's entities to the current one and two
people's biometrics merge under one identity. That veto lives in
`migrations._partner_recovery_renames`, which refuses to emit anything
unless `coordinator.partner_identity_confirmed` is true. Nothing here
touches that flag, and
`test_an_unverified_partner_is_never_journalled` asserts it directly on
the journal rather than trusting the trace.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

from homeassistant.config_entries import ConfigEntryState
from homeassistant.helpers import entity_registry as er
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


# Every partner insight sensor and the partner session binary sensor are
# keyed `{device}_user_{partner}_{key}`. Matching on the account id alone
# is enough to separate them from the primary's, and it deliberately does
# NOT match the 2.x role-keyed `{device}_partner_{key}` shape, so a fix
# that minted legacy ids would not satisfy these assertions.
def partner_rows(hass, entry) -> list[Any]:
    registry = er.async_get(hass)
    return [
        row
        for row in er.async_entries_for_config_entry(registry, entry.entry_id)
        if f"user_{PARTNER}_" in row.unique_id
    ]


def legacy_partner_rows(hass, entry) -> list[Any]:
    """Rows on the 2.x role-keyed ids `person_unique_id` falls back to.

    Watched separately because the failure mode of building entities
    before identity resolves is not "no entities", it is "entities on the
    wrong id". Those would be held by the registry forever, and the next
    boot that reached the server would mint the account-keyed ids as a
    SECOND set, splitting one person's history across two entities with
    no way back.
    """
    registry = er.async_get(hass)
    return [
        row
        for row in er.async_entries_for_config_entry(registry, entry.entry_id)
        if f"{BED_A}_partner_" in row.unique_id
    ]


class ColdFailPartnerClient(FakeClient):
    """A partner account the server does not answer about, until told to.

    `fail_user` covers the FIRST fetch and every fetch after it until a
    test clears it, which is what makes this a cold start rather than the
    warm blip `FlakySecondFetchPartnerClient` models.
    `_async_refresh_partner_identity` runs twice per setup, once from
    `_async_setup` and once from the first poll, so a client that only
    failed once would be verified before the platforms were ever set up
    and none of these tests would exercise anything.
    """

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.user = {"id": PARTNER, "name": "Bo", "email": PARTNER_EMAIL}
        self.fail_user: Exception | None = OrionConnectionError("no route to host")
        self.fetches = 0

    async def get_current_user(self) -> dict[str, Any]:
        self.calls.append("get_current_user")
        self.fetches += 1
        if self.fail_user is not None:
            raise self.fail_user
        return dict(self.user)


def clients(primary: FakeClient, partner: FakeClient, ws_manager):
    """Patch both sessions, dispatching on the token they were handed.

    Deliberately not an alternating side_effect, for the reason spelled
    out in `test_partner_transient_real.clients`. A counter silently swaps
    the two accounts the moment anything builds an extra client.
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


def partner_entry(hass, *, account_id: str | None = PARTNER, journal=None):
    """An entry whose partner is linked, and by default recorded.

    `account_id=None` drops `CONF_PARTNER_ACCOUNT_ID` to model an entry
    linked before that key existed. Those entries have no durable answer
    to "which account", which is the one case existence-based
    construction deliberately does not cover.
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
            **({CONF_PARTNER_ACCOUNT_ID: account_id} if account_id else {}),
            **({CONF_UID_MIGRATION: journal} if journal else {}),
        },
    )


async def test_a_failed_first_partner_fetch_still_builds_the_partners_entities(
    hass, ws_manager
):
    """The bug, stated directly.

    BREAKS IF: `sensor.py` or `binary_sensor.py` goes back to gating
    construction on `has_partner_for_device`, or
    `has_partner_configured_for_device` starts requiring
    `partner_mapping_valid`, `partner_identity_confirmed`, or a non-empty
    `partner_user`. Any of those reintroduces the absent-entity failure.

    The entry is asserted LOADED first. A partner fetch that took setup
    down with it would produce zero entities for a completely different
    reason and every assertion below would pass on a broken integration.
    """
    entry = partner_entry(hass)
    partner = ColdFailPartnerClient()

    api, ws = clients(FakeClient(), partner, ws_manager)
    with api, ws:
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED
    coordinator = entry.runtime_data
    assert partner.fetches >= 1, "the partner was never fetched, so nothing failed"
    assert coordinator.partner_user == {}, (
        "the partner profile arrived after all, so this test is not "
        "exercising the cold failure it claims to"
    )
    assert coordinator.partner_mapping_valid is False, (
        "nothing verified this partner, so the trust predicate must say no"
    )

    rows = partner_rows(hass, entry)
    assert rows, (
        "one failed partner fetch at cold start built ZERO partner entities. "
        "The partner's sleep, heart rate, HRV and apnea entities do not "
        "exist in Home Assistant, so every card and automation naming them "
        "is broken, and nothing rebuilds them without a manual reload"
    )
    assert not legacy_partner_rows(hass, entry), (
        "partner entities were built on the 2.x role-keyed ids rather than "
        "the recorded account id. The registry holds those forever, and the "
        "next healthy boot mints the account-keyed ids as a second set, "
        f"splitting one person's history in two: {legacy_partner_rows(hass, entry)}"
    )

    # Existence is not trust. These have to be present AND silent.
    states = [hass.states.get(row.entity_id) for row in rows]
    assert all(state is not None for state in states), (
        "a partner entity was registered but never wrote a state"
    )
    assert all(state.state == "unavailable" for state in states), (
        "an unverified partner's entities reported a value. `unavailable` is "
        "the whole point of constructing them: they exist, and this run "
        "cannot speak for them. Presenting data would be worse than the "
        f"original bug: {[(s.entity_id, s.state) for s in states]}"
    )


async def test_a_later_poll_makes_them_available_without_a_reload(hass, ws_manager):
    """Constructed-but-unavailable is only useful if it self-heals.

    BREAKS IF: `available` on either partner entity is turned into
    anything computed once at construction, or if the entities are built
    against a snapshot of coordinator state rather than reading it live.
    A partner that stayed unavailable until somebody reloaded the entry
    would be a quieter version of the same bug.
    """
    entry = partner_entry(hass)
    partner = ColdFailPartnerClient()

    api, ws = clients(FakeClient(), partner, ws_manager)
    with api, ws:
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        rows = partner_rows(hass, entry)
        assert rows, "nothing was built, so there is nothing to make available"
        assert all(
            hass.states.get(row.entity_id).state == "unavailable" for row in rows
        ), "the partner started out available, so this test proves nothing"

        # The network comes back. No reload, no reconfiguration, just the
        # next ordinary poll.
        partner.fail_user = None
        await entry.runtime_data.async_refresh()
        await hass.async_block_till_done()

    coordinator = entry.runtime_data
    assert coordinator.partner_identity_confirmed is True, (
        "the partner was reachable and matched the recorded account id, so "
        "identity should be confirmed"
    )
    assert coordinator.has_partner_for_device(BED_A) is True, (
        "a successful fetch did not restore trust for the bed"
    )
    assert entry.state is ConfigEntryState.LOADED, (
        "the entry reloaded, which would make this test pass for the wrong "
        "reason. The point is that recovery needs no reload"
    )

    recovered = [
        hass.states.get(row.entity_id)
        for row in partner_rows(hass, entry)
    ]
    assert recovered and all(state.state != "unavailable" for state in recovered), (
        "the partner verified on a later poll and their entities stayed "
        "unavailable. They can only recover on a reload, which is the "
        f"failure this whole change exists to remove: {recovered}"
    )

    # The friendly name has to catch up too. `partner_name()` can only
    # answer "Partner" while `partner_user` is empty, and the entities are
    # now constructed in exactly that state, so freezing the name into
    # `_attr_name` at construction would leave every one of them labelled
    # "Partner" until somebody reloaded. Both partner classes therefore
    # compute `name` as a property.
    #
    # BREAKS IF: either `name` property is turned back into an `_attr_name`
    # assignment in `__init__`.
    assert all(
        state.attributes["friendly_name"].startswith("Sleepy Bo")
        for state in recovered
    ), (
        "the partner verified and their entities are still labelled from "
        "the failed fetch. The name was frozen at construction and now "
        f"needs a reload to correct: {[s.attributes['friendly_name'] for s in recovered]}"
    )


async def test_an_unverified_partner_is_never_journalled(hass, ws_manager):
    """The hazard test. The most important assertion in this file.

    Constructing entities for a partner nobody verified is only safe if
    that partner's id cannot reach the downgrade journal. 2.x has exactly
    ONE role-keyed row per partner key, fed by whichever partner is
    linked at the time, so a record naming an unverified account is what
    hands the previous partner's entities to the current one and merges
    two people's heart rate, HRV and apnea history under one identity.

    Asserted directly on `entry.data[CONF_UID_MIGRATION]` rather than by
    reasoning about the call chain, because the call chain is exactly the
    thing a future refactor will change.

    BREAKS IF: `migrations._partner_recovery_renames` stops vetoing on
    `coordinator.partner_identity_confirmed`, or that flag is made to
    survive a failed fetch, or `has_partner_for_device` is loosened into
    the existence predicate. The journalling loop in
    `async_migrate_unique_ids` consumes the same pairs with no second
    veto, so weakening the single one is enough.
    """
    entry = partner_entry(hass)
    partner = ColdFailPartnerClient()

    api, ws = clients(FakeClient(), partner, ws_manager)
    with api, ws:
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED
    coordinator = entry.runtime_data

    assert partner_rows(hass, entry), (
        "no partner entities were built, so this run took the old code path "
        "and proves nothing about journalling an entity that does exist"
    )
    assert coordinator.partner_identity_confirmed is False, (
        "a partner nobody could fetch was reported as identity-confirmed, "
        "which is the single veto protecting the journal"
    )

    journalled = [
        record
        for record in (entry.data.get(CONF_UID_MIGRATION) or [])
        if record.get("role") == "partner"
    ]
    assert not journalled, (
        "a setup that never verified the partner wrote partner rename "
        "records anyway. Reverting these on a downgrade renames one "
        f"person's entities onto the ids 2.x feeds from another: {journalled}"
    )


async def test_building_entities_did_not_start_journalling_a_replaced_partner(
    hass, ws_manager
):
    """The same hazard from the direction that has a real reference value.

    A cold failure gives the journal nothing to work with because
    `partner_user` is empty. A partner REPLACEMENT is the harder case:
    the fetch SUCCEEDS, so `partner_user` holds a real id, and the only
    thing standing between that id and a journal record is the
    recorded-versus-returned comparison. Entities now get built for this
    entry where previously they did not, so it is worth pinning that
    building them changed nothing about what gets recorded.

    BREAKS IF: `partner_entity_key_id` starts preferring the FETCHED id
    over the recorded one, or `_partner_identity_verified` is fed the
    recorded id and becomes a tautology, or the identity comparison is
    dropped.
    """
    entry = partner_entry(hass)
    partner = ColdFailPartnerClient()
    # Reachable, and somebody else. Exactly what a relinked-at-the-vendor
    # partner looks like from here.
    partner.fail_user = None
    partner.user = {
        "id": "44444444-4444-4444-8444-444444444444",
        "name": "Someone Else",
        "email": "eve@example.com",
    }

    api, ws = clients(FakeClient(), partner, ws_manager)
    with api, ws:
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED
    coordinator = entry.runtime_data
    assert coordinator.partner_identity_confirmed is False, (
        "the server returned a different account than this entry recorded "
        "and the identity check agreed with it anyway"
    )

    journalled = [
        record
        for record in (entry.data.get(CONF_UID_MIGRATION) or [])
        if record.get("role") == "partner"
    ]
    assert not journalled, (
        "the returned partner account did not match the recorded one and a "
        f"rename record naming it was written regardless: {journalled}"
    )
    assert not [
        row
        for row in er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)
        if "44444444-4444-4444-8444-444444444444" in row.unique_id
    ], (
        "entities were minted on the id of an account this integration "
        "explicitly refused to verify. Those ids are permanent, and the "
        "previous partner's history is now stranded on the old ones"
    )


async def test_an_entry_with_no_partner_builds_no_partner_entities(hass, patched):
    """Negative control, so "always construct" cannot pass this file.

    BREAKS IF: `has_partner_configured_for_device` stops checking
    `partner_api_client is None` or `partner_device_serial`, which is the
    obvious way to make the first test in this file pass by simply
    building partner entities for everyone.
    """
    entry = make_entry(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED

    coordinator = entry.runtime_data
    assert coordinator.partner_api_client is None, "this entry has no partner linked"
    assert coordinator.has_partner_configured_for_device(BED_A) is False, (
        "an entry with no partner tokens reported a configured partner"
    )
    assert not partner_rows(hass, entry) and not legacy_partner_rows(hass, entry), (
        "partner entities were built for a household that has no partner. "
        "Every one of them is permanently unavailable and permanently "
        "unexplainable"
    )


async def test_a_leftover_recorded_partner_id_alone_conjures_no_entities(hass, patched):
    """The negative control that actually bites.

    The weaker one above passes even if the tokens check is deleted,
    because an entry with no partner also has no recorded id and no
    partner serial, so three separate guards all say no and removing any
    one of them changes nothing. This entry carries the recorded id AND
    the device serial and withholds only the TOKENS, which leaves exactly
    one guard load bearing: `partner_api_client is None`.
    `async_setup_entry` builds the partner client from
    `CONF_PARTNER_ACCESS_TOKEN` alone, so no tokens means no client.

    `OrionSleepOptionsFlow` removes all of these keys in one write, so
    this shape is not reachable from a supported unlink. That is the same
    thing `_partner_identity_verified` says about its case 3, and it fails
    closed there anyway. A recorded id and a serial describe WHO the
    partner was and WHICH bed they shared. Only the tokens establish that
    there is a partner at all.

    BREAKS IF: `has_partner_configured_for_device` stops checking
    `partner_api_client is None` and leans on `partner_entity_key_id` or
    the serial comparison alone.
    """
    entry = make_entry(
        hass,
        data={
            CONF_PARTNER_ACCOUNT_ID: PARTNER,
            CONF_PARTNER_DEVICE_SERIAL: SERIAL_A,
        },
    )
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED

    coordinator = entry.runtime_data
    assert coordinator.partner_api_client is None, "no partner tokens were written"
    assert coordinator.partner_entity_key_id() == PARTNER, (
        "the leftover recorded id is supposed to still be readable here. If "
        "it is not, this test is passing for a reason it does not describe"
    )
    assert coordinator.has_partner_configured_for_device(BED_A) is False, (
        "a leftover account id with no partner tokens was treated as a "
        "configured partner. A recorded id says who the partner WAS. Only "
        "the tokens say there is one"
    )
    assert not partner_rows(hass, entry) and not legacy_partner_rows(hass, entry), (
        "a full set of partner entities was built from a leftover string in "
        "`data`. Every one of them is permanently unavailable, permanently "
        "unexplainable, and permanently in the registry"
    )


async def test_a_legacy_entry_with_no_recorded_partner_id_still_waits(hass, ws_manager):
    """The one case existence-based construction deliberately does not cover.

    An entry linked before `CONF_PARTNER_ACCOUNT_ID` existed has partner
    tokens but no durable answer to WHICH account. Building its entities
    on a failed fetch would key them on the 2.x role-keyed fallback, the
    registry would hold that id forever, and the next healthy boot would
    mint the account-keyed id as a second entity with the history split
    between them. Waiting is strictly better than that, and it is exactly
    what those entries already do today, so nothing regresses.

    Pinned rather than left implicit because the tempting "fix" is to let
    `person_unique_id` take its legacy fallback here, and that trade is
    the wrong way round.

    BREAKS IF: `partner_entity_key_id` gains a fallback that returns a
    role string, a placeholder, or the primary's id when nothing is
    recorded and nothing has been fetched.
    """
    entry = partner_entry(hass, account_id=None)
    partner = ColdFailPartnerClient()

    api, ws = clients(FakeClient(), partner, ws_manager)
    with api, ws:
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED
    coordinator = entry.runtime_data
    assert coordinator.partner_entity_key_id() is None, (
        "an entry with nothing recorded and nothing fetched produced an "
        "account id from somewhere. Whatever it is, it is invented, and it "
        "is about to become a permanent unique_id"
    )
    assert not legacy_partner_rows(hass, entry), (
        "partner entities were minted on the 2.x role-keyed ids. Those are "
        "permanent, and the account-keyed ids will be minted alongside them "
        "on the next boot that reaches the server"
    )
