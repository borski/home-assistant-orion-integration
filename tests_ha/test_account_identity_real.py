"""Who these tokens actually belong to, on both accounts.

Two defects live here and they are the same defect pointed in opposite
directions.

The PARTNER account was never identified. `partner_mapping_valid` compared
device serials and device counts, which establishes that two accounts still
share one bed and establishes nothing about which partner account is behind
the partner tokens. That unverified id is what
`migrations._partner_recovery_renames` builds the partner half of the
downgrade journal from, and 2.x has exactly one role-keyed row per partner
key. Reverting a journal that names the wrong partner hands the previous
partner's entities to the current one and merges two people's heart rate,
HRV and apnea history under a single identity, with every rename reporting
success. The primary account had a recorded-versus-returned check guarding
that same journal from its own side. The partner had nothing.

The PRIMARY account was identified and could not stop being identified. The
address assertion fails closed, correctly, and the reauth flow it launches
runs a copy of the same test, so a profile Orion returns without `email`,
`phone` and `phone_number` fails setup and fails every attempt to escape
setup. This endpoint has been measured returning `{"response": null}`, so
that is an observed shape rather than a hypothesis.

Every test here is behavioural against a real Home Assistant, because both
defects are about what the code DOES with a response rather than how it is
written, and the `ast` suite is structurally blind to that.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from homeassistant.config_entries import ConfigEntryState

from custom_components.orion_sleep.const import (
    CONF_ACCOUNT_ID,
    CONF_ALLOW_UNVERIFIED_ACCOUNT,
    CONF_PARTNER_ACCESS_TOKEN,
    CONF_PARTNER_ACCOUNT_ID,
    CONF_PARTNER_AUTH_VALUE,
    CONF_PARTNER_DEVICE_SERIAL,
    CONF_PARTNER_EXPIRES_AT,
    CONF_PARTNER_REFRESH_TOKEN,
)
from tests_ha.conftest import ACCOUNT, BED_A, PARTNER, SERIAL_A, FakeClient, make_entry

PARTNER_EMAIL = "bob@example.com"
# A third account. Not ACCOUNT and not PARTNER, so a test using it cannot
# pass by accidentally matching either side of the pair.
INTRUDER = "33333333-3333-4333-8333-333333333333"


@pytest.fixture
def partner_client() -> FakeClient:
    """The account behind the partner tokens, as the server describes it."""
    client = FakeClient()
    client.user = {"id": PARTNER, "name": "Bo", "email": PARTNER_EMAIL}
    return client


@pytest.fixture
def paired(client: FakeClient, partner_client: FakeClient, ws_manager):
    """Give the primary and the partner DIFFERENT clients.

    The shared `patched` fixture hands the same object to both
    constructions in `async_setup_entry`, which makes the partner a perfect
    copy of the primary and quietly removes the possibility of the two
    accounts disagreeing. That is the exact disagreement these tests are
    about, so this fixture alternates instead. Alternating rather than
    consuming a fixed list means a reload gets a fresh pair rather than a
    StopIteration.
    """
    order = (client, partner_client)
    calls = {"n": 0}

    def _client(*_args: object, **_kwargs: object) -> FakeClient:
        chosen = order[calls["n"] % 2]
        calls["n"] += 1
        return chosen

    with (
        patch("custom_components.orion_sleep.OrionApiClient", side_effect=_client),
        patch(
            "custom_components.orion_sleep.coordinator.OrionWebSocketManager",
            return_value=ws_manager,
        ),
    ):
        yield


def partner_entry(hass, **overrides):
    """An entry with a linked partner sharing one bed with the primary.

    `CONF_ACCOUNT_ID` is recorded deliberately. It keeps the PRIMARY on its
    recorded-versus-returned branch so nothing in a partner test can be
    explained by the primary's own identity handling.
    """
    data = {
        CONF_ACCOUNT_ID: ACCOUNT,
        CONF_PARTNER_ACCESS_TOKEN: "partner-at",
        CONF_PARTNER_REFRESH_TOKEN: "partner-rt",
        CONF_PARTNER_EXPIRES_AT: 9e12,
        CONF_PARTNER_DEVICE_SERIAL: SERIAL_A,
        CONF_PARTNER_AUTH_VALUE: PARTNER_EMAIL,
        CONF_PARTNER_ACCOUNT_ID: PARTNER,
    }
    data.update(overrides)
    return make_entry(hass, data=data)


async def setup(hass, entry) -> None:
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


# ── The partner account ───────────────────────────────────────────────


async def test_a_verified_partner_is_still_mapped(hass, paired):
    """The positive control, and it has to come first.

    Every other partner test asserts that something is refused. Without
    this one they would all pass against a coordinator that refused every
    partner unconditionally, which is a different bug wearing the fix as a
    disguise.
    """
    entry = partner_entry(hass)

    await setup(hass, entry)

    assert entry.state is ConfigEntryState.LOADED
    coordinator = entry.runtime_data
    assert coordinator.partner_mapping_valid is True
    assert coordinator.has_partner_for_device(BED_A) is True
    assert sorted(coordinator.schedule_user_ids()) == sorted([ACCOUNT, PARTNER])


async def test_a_partner_naming_a_different_account_is_refused(
    hass, paired, partner_client
):
    """The finding, stated as behaviour.

    The recorded partner is PARTNER. The server answers with a different
    account. Nothing about device serials or device counts changes, so the
    topology test that used to be the only test still passes, and the
    entry would previously have gone on to treat this stranger as the
    linked partner.

    BREAKS IF: the recorded-versus-returned comparison in
    `_partner_identity_verified` is removed, or its result stops being
    folded into `partner_mapping_valid`. Reinstating the old
    `partner_mapping_valid = <topology only>` expression fails this.
    """
    entry = partner_entry(hass)
    partner_client.user = {"id": INTRUDER, "name": "Bo", "email": PARTNER_EMAIL}

    await setup(hass, entry)

    coordinator = entry.runtime_data
    assert coordinator.partner_mapping_valid is False, (
        "the coordinator accepted a partner account it had never seen before "
        "because the serial numbers still lined up"
    )
    # What the finding is actually about. `_partner_recovery_renames` builds
    # a partner journal record for every device this returns True for, and
    # those records are what a downgrade replays.
    assert coordinator.has_partner_for_device(BED_A) is False, (
        "an unverified partner is still eligible for downgrade journal "
        "records, which is the path that merges two people's history"
    )


async def test_a_partner_mismatch_does_not_take_down_the_primary(
    hass, paired, partner_client
):
    """A partner fault must cost the partner's entities and nothing else.

    Raising here would be worse than the bug. `ConfigEntryAuthFailed`
    launches a reauth flow that re-verifies the PRIMARY account's address,
    so it prompts the wrong person for credentials that cannot fix a
    partner token, and every primary entity is unavailable until they
    answer.

    BREAKS IF: `_partner_identity_verified` raises instead of returning
    False, or if the partner verdict is moved anywhere that can abort
    `_async_setup`.
    """
    entry = partner_entry(hass)
    partner_client.user = {"id": INTRUDER, "email": PARTNER_EMAIL}

    await setup(hass, entry)

    assert entry.state is ConfigEntryState.LOADED, (
        f"a partner identity failure took the whole entry down. "
        f"reason={entry.reason!r}"
    )
    coordinator = entry.runtime_data
    # The primary is untouched: still identified, still the only person the
    # integration will build person-scoped entities for.
    assert coordinator.user_id == ACCOUNT
    assert coordinator.schedule_user_ids() == [ACCOUNT]
    assert coordinator.primary_name() == "Alex"
    assert hass.states.async_all(), "no entities at all, so this proves nothing"


async def test_the_partner_address_verifies_when_no_account_id_is_recorded(
    hass, paired
):
    """Every partner linked before the recorded id existed lands here.

    The config flow writes `CONF_PARTNER_AUTH_VALUE` in the same call as
    the partner tokens and removes it in the same call, so an entry with
    partner tokens and no recorded partner account id still has the
    address the partner was linked with. That is a real reference value
    and it is the same one the primary falls back to.

    BREAKS IF: the `elif typed` fallback is dropped, which would fail every
    existing partner closed on upgrade, or if it is widened to accept a
    profile that does not carry the address.
    """
    entry = partner_entry(hass, **{CONF_PARTNER_ACCOUNT_ID: None})
    # None is not absent. Write the entry without the key at all.
    hass.config_entries.async_update_entry(
        entry,
        data={k: v for k, v in entry.data.items() if k != CONF_PARTNER_ACCOUNT_ID},
    )

    await setup(hass, entry)

    coordinator = entry.runtime_data
    assert coordinator.partner_mapping_valid is True, (
        "a partner linked before CONF_PARTNER_ACCOUNT_ID existed was refused, "
        "even though the address it was linked with is recorded and matches"
    )


async def test_an_unverifiable_partner_address_is_refused(hass, paired, partner_client):
    """The fallback has to be a test, not a formality.

    BREAKS IF: `profile_carries_address` is bypassed on the partner path,
    or if an absent recorded id is treated as permission to skip the check
    rather than as a reason to fall back to the address.
    """
    entry = partner_entry(hass)
    hass.config_entries.async_update_entry(
        entry,
        data={k: v for k, v in entry.data.items() if k != CONF_PARTNER_ACCOUNT_ID},
    )
    # A different account AND a profile that does not carry the linked
    # address, which is what a swapped partner token actually looks like.
    partner_client.user = {"id": INTRUDER, "email": "stranger@example.com"}

    await setup(hass, entry)

    coordinator = entry.runtime_data
    assert coordinator.partner_mapping_valid is False
    assert entry.state is ConfigEntryState.LOADED


# ── The primary account, and the way out ──────────────────────────────


async def test_an_address_less_profile_fails_closed(hass, patched, client):
    """The default has to stay fail-closed. This is the F1 guarantee.

    No recorded account id, so this boot is the one that would write one
    and re-key every registry row onto it. The profile carries no address,
    so nothing in the response corroborates the id.

    BREAKS IF: the address assertion is removed, or if
    `_unverified_account_allowed` returns True without the option being
    set. The second assertion is the one that matters most: failing to
    load is not the point, refusing to RECORD an unverified account id is.
    """
    entry = make_entry(hass)
    client.user = {"id": ACCOUNT, "name": "Alex"}

    await setup(hass, entry)

    assert entry.state is not ConfigEntryState.LOADED, (
        "an entry recorded an account id it could not verify, and that write "
        "is what the entity re-key keys every person's history on"
    )
    assert not entry.data.get(CONF_ACCOUNT_ID), (
        "setup failed but still persisted the unverified account id, so the "
        "next boot compares it to itself and reports agreement forever"
    )


async def test_the_bypass_option_opens_the_locked_door(hass, patched, client):
    """The escape hatch, used the way a locked-out household would use it.

    Same entry and same address-less profile as the test above. The only
    difference is an option the household set deliberately.

    BREAKS IF: the option is not read, is read from `data` instead of
    `options`, or is checked around the wrong branch.
    """
    entry = make_entry(hass)
    client.user = {"id": ACCOUNT, "name": "Alex"}
    hass.config_entries.async_update_entry(
        entry, options={CONF_ALLOW_UNVERIFIED_ACCOUNT: True}
    )

    await setup(hass, entry)

    assert entry.state is ConfigEntryState.LOADED, (
        f"the documented escape hatch did not open the door. "
        f"reason={entry.reason!r}"
    )
    assert entry.data.get(CONF_ACCOUNT_ID) == ACCOUNT


async def test_the_bypass_does_not_relax_a_recorded_mismatch(hass, patched, client):
    """The scope of the hatch, which is the whole safety argument for it.

    An entry that ALREADY records an account id has a real reference
    value, so a mismatch is a real finding rather than an absence of
    evidence. Ratifying it is the identity swap that moves one person's
    sleep history onto another person, and no option may buy that.

    BREAKS IF: `_unverified_account_allowed` is hoisted to guard the whole
    identity block instead of the single address assertion inside the
    `elif typed` branch. That is the likeliest way for this to regress,
    because it looks like a simplification.
    """
    entry = make_entry(hass, data={CONF_ACCOUNT_ID: ACCOUNT})
    client.user = {"id": INTRUDER, "name": "Alex", "email": "alice@example.com"}
    hass.config_entries.async_update_entry(
        entry, options={CONF_ALLOW_UNVERIFIED_ACCOUNT: True}
    )

    await setup(hass, entry)

    assert entry.state is not ConfigEntryState.LOADED, (
        "the unverified-account option was allowed to ratify a mismatch "
        "against a recorded account id, which swaps two people's history"
    )
    assert entry.data.get(CONF_ACCOUNT_ID) == ACCOUNT, (
        "the recorded account id was overwritten with the one the server "
        "returned, which is the invariant __init__.py exists to hold"
    )
