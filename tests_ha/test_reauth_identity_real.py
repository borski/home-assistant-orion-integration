"""One account-identity rule, applied by setup and by reauth alike.

Three findings, and they compound.

The identity is recorded TWICE. `entry.unique_id` is what
`ConfigFlow._async_reauth_account_matches` compared, `CONF_ACCOUNT_ID` is
what the coordinator compares, and the reauth write then spread
`CONF_ACCOUNT_ID` in unconditionally. So reauth validated one field and
wrote the other. That is the "never overwrite a recorded account id"
invariant `__init__.py` states, violated two modules away. It survived
only because the bed-overlap check happened to reject most mismatches
first, and two accounts sharing one bed is the household this integration
is built for.

The address rule was written twice. `profile_carries_address` in the
coordinator and a copy in `config_flow`. Two copies of a rule are merely
untidy until something makes them diverge, and the unverified-account
option did exactly that: setup could be relaxed by it, reauth could not.

`CONF_ALLOW_UNVERIFIED_ACCOUNT` was the escape hatch for one specific
lockout and was exposed in no form at all, so it could not be set. The
lockout is real: Orion has been measured returning `{"response": null}`
from the profile endpoint, a profile carrying no `email`, `phone` and
`phone_number` fails the address assertion, and the reauth flow the
failure launches applied the same test.

The three together produce the worst version. Expired tokens mean setup
cannot run, so the option never gets consulted there and reauth is the
only door left. A reauth that refuses is a household with nowhere to go.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

from homeassistant import config_entries
from homeassistant.config_entries import ConfigEntryState
from orion_sleep_api import OrionConnectionError

from custom_components.orion_sleep.const import (
    CONF_ACCOUNT_ID,
    CONF_ALLOW_UNVERIFIED_ACCOUNT,
    CONF_DEVICE_IDS,
    CONF_INSIGHTS_DAYS,
    CONF_SCAN_INTERVAL,
    DOMAIN,
)
from tests_ha.conftest import ACCOUNT, BED_A, make_entry

# A third account, so no assertion can pass by matching the entry's own.
INTRUDER = "33333333-3333-4333-8333-333333333333"
EMAIL = "alice@example.com"


def base_form(**overrides: Any) -> dict[str, Any]:
    """The options init step payload, with every field the schema wants."""
    payload = {
        CONF_SCAN_INTERVAL: 300,
        CONF_INSIGHTS_DAYS: 7,
        "partner_action": "keep",
        "edit_aliases": False,
        CONF_ALLOW_UNVERIFIED_ACCOUNT: False,
    }
    payload.update(overrides)
    return payload


class FakeAuthClient:
    """The client the config flow builds, on both of its calls.

    `async_step_verify` builds one with no tokens to spend the code, then
    `_async_account_identity` builds a second WITH the new tokens to ask
    who they belong to. The same class answers both, because the flow
    imports one name.

    `profile` is a class attribute so a test can reshape the account the
    server describes without reaching into an instance the flow owns.
    """

    profile: dict[str, Any] = {"id": ACCOUNT, "name": "Alex", "email": EMAIL}
    devices: list[dict[str, Any]] = [{"id": BED_A, "serial_number": "AA11BB22CC33"}]

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        return

    async def request_auth_code(self, email=None, phone=None) -> bool:
        return True

    async def verify_auth_code(self, code, email=None, phone=None) -> dict[str, Any]:
        return {
            "access_token": "fresh-at",
            "refresh_token": "fresh-rt",
            "expires_at": 9e12,
        }

    def set_token_refresh_callback(self, _cb) -> None:
        return

    async def ensure_valid_token(self) -> None:
        return

    async def get_current_user(self) -> dict[str, Any]:
        return dict(self.profile)

    async def list_devices(self) -> list[dict[str, Any]]:
        return [dict(d) for d in self.devices]


async def drive_reauth(hass, entry) -> dict[str, Any]:
    """Run the real reauth flow to completion and return its result."""
    with patch(
        "custom_components.orion_sleep.config_flow.OrionApiClient",
        side_effect=FakeAuthClient,
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={
                "source": config_entries.SOURCE_REAUTH,
                "entry_id": entry.entry_id,
            },
            data=dict(entry.data),
        )
        assert result["step_id"] == "reauth_confirm", result
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
        assert result["step_id"] == "verify", result
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"code": "123456"}
        )
    await hass.async_block_till_done()
    return result


def legacy_entry(hass, **kwargs):
    """A pre-3.0 entry: keyed on the typed address, no recorded account id.

    This is the shape the whole reauth fallback exists for. The config
    flow only began writing `CONF_ACCOUNT_ID` in 3.0, so every entry older
    than that arrives with `unique_id` holding the address it was set up
    with and nothing recording the account.
    """
    return make_entry(hass, unique_id=EMAIL, **kwargs)


# ── The bypass has to be reachable at all ─────────────────────────────


async def test_the_bypass_option_is_settable_through_the_options_flow(hass):
    """It was read by the coordinator and written by nothing.

    An option no form offers is an option nobody can set, so the
    documented way out of the lockout did not exist. This drives the real
    options flow and asserts the value lands where the coordinator reads
    it, which is `options` and not `data`.

    BREAKS IF: the field is dropped from `async_step_init`'s schema. The
    flow then rejects the key as unexpected rather than storing it.
    """
    entry = make_entry(hass, data={CONF_ACCOUNT_ID: ACCOUNT})
    assert CONF_ALLOW_UNVERIFIED_ACCOUNT not in entry.options

    flow = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        flow["flow_id"], base_form(**{CONF_ALLOW_UNVERIFIED_ACCOUNT: True})
    )
    assert result["type"] == "create_entry", result
    await hass.async_block_till_done()

    assert entry.options.get(CONF_ALLOW_UNVERIFIED_ACCOUNT) is True


async def test_the_bypass_survives_an_unrelated_options_save(hass):
    """Its default comes from the entry, not from the constant.

    A household using this to keep a locked-out entry loading will
    eventually open options for some other reason. If the field defaulted
    to the constant, that save would switch the hatch back off and the
    entry would stop loading for reasons nobody connected to what they
    just did.

    BREAKS IF: the schema default is `DEFAULT_ALLOW_UNVERIFIED_ACCOUNT`
    rather than the entry's current value.
    """
    entry = make_entry(hass, data={CONF_ACCOUNT_ID: ACCOUNT})
    hass.config_entries.async_update_entry(
        entry, options={CONF_ALLOW_UNVERIFIED_ACCOUNT: True}
    )

    flow = await hass.config_entries.options.async_init(entry.entry_id)
    # Everything EXCEPT the bypass field, which is what an unrelated save
    # looks like when the user does not touch that control.
    payload = base_form(**{CONF_SCAN_INTERVAL: 900})
    payload.pop(CONF_ALLOW_UNVERIFIED_ACCOUNT)
    result = await hass.config_entries.options.async_configure(
        flow["flow_id"], payload
    )
    assert result["type"] == "create_entry", result
    await hass.async_block_till_done()

    assert entry.options[CONF_SCAN_INTERVAL] == 900, "the save did not happen"
    assert entry.options.get(CONF_ALLOW_UNVERIFIED_ACCOUNT) is True, (
        "changing the polling interval switched the recovery hatch off, so "
        "the entry stops loading for a reason nobody would connect to it"
    )


async def test_the_bypass_set_through_the_flow_actually_loads_the_entry(hass, patched, client):
    """End to end: the form writes it and the coordinator obeys it.

    The two halves were previously tested apart and nothing joined them,
    which is how an option could be read by one layer, written by none,
    and still look covered.

    BREAKS IF: the field is dropped from the schema, or written into
    `data` instead of `options`.
    """
    entry = legacy_entry(hass)
    # The profile Orion has been measured returning: an id and nothing
    # that corroborates it.
    client.user = {"id": ACCOUNT, "name": "Alex"}

    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    # The positive control. Without it this test would pass against an
    # integration that had simply stopped checking anything.
    assert entry.state is not ConfigEntryState.LOADED
    assert not entry.data.get(CONF_ACCOUNT_ID)

    flow = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        flow["flow_id"], base_form(**{CONF_ALLOW_UNVERIFIED_ACCOUNT: True})
    )
    assert result["type"] == "create_entry", result
    await hass.async_block_till_done()

    # Reload rather than set up. The entry is in SETUP_ERROR, which is the
    # state a locked-out household is actually looking at, and Home
    # Assistant refuses `async_setup` on anything that is not NOT_LOADED.
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED, (
        f"the escape hatch was set through the only form that offers it "
        f"and the entry still refused to load. reason={entry.reason!r}"
    )
    assert entry.data.get(CONF_ACCOUNT_ID) == ACCOUNT


# ── Setup and reauth applying one rule ────────────────────────────────


async def test_reauth_refuses_an_addressless_profile_by_default(hass, patched):
    """The default stays fail closed, and this is the control for below.

    Accepted credentials prove the code was valid. They do not prove which
    account issued it, and a reauth that treats them as proof replaces one
    household's tokens with another's.

    BREAKS IF: the address assertion is dropped from the reauth path, or
    the bypass is consulted without being set.
    """
    entry = legacy_entry(hass)
    FakeAuthClient.profile = {"id": ACCOUNT, "name": "Alex"}

    result = await drive_reauth(hass, entry)

    assert result["type"] == "abort", result
    assert result["reason"] == "reauth_account_mismatch", result


async def test_reauth_and_setup_agree_once_the_bypass_is_set(hass, patched):
    """The divergence, closed. This is the whole point of Change 4.

    Same entry and same address-less profile as the test above. The only
    difference is the option, and setup already honours it. Reauth
    honouring it too is what makes the hatch an escape rather than a
    detour, because expired tokens mean setup never runs and reauth is the
    only door the household has.

    BREAKS IF: the reauth path keeps its own copy of the address rule, or
    reads the option from `data`, or checks it around the wrong branch.
    """
    entry = legacy_entry(hass)
    hass.config_entries.async_update_entry(
        entry, options={CONF_ALLOW_UNVERIFIED_ACCOUNT: True}
    )
    FakeAuthClient.profile = {"id": ACCOUNT, "name": "Alex"}

    result = await drive_reauth(hass, entry)

    assert result["type"] == "abort", result
    assert result["reason"] == "reauth_successful", (
        "setup accepts this account and reauth refuses it, so a household "
        "that set the documented escape hatch can load the entry and still "
        f"never reauthenticate: {result}"
    )
    assert entry.data["access_token"] == "fresh-at"


async def test_the_bypass_does_not_let_reauth_ratify_a_recorded_mismatch(hass, patched):
    """The scope of the hatch on the reauth path.

    A recorded account id is a real reference value, so a mismatch against
    it is a real finding rather than an absence of evidence. Ratifying it
    swaps two people's sleep history, and no option may buy that.

    BREAKS IF: the option is hoisted to guard the whole decision instead of
    the address assertion inside the no-recorded-id branch. That is the
    likeliest regression, because it looks like a simplification.
    """
    entry = make_entry(hass, data={CONF_ACCOUNT_ID: ACCOUNT})
    hass.config_entries.async_update_entry(
        entry, options={CONF_ALLOW_UNVERIFIED_ACCOUNT: True}
    )
    # A different account, and a profile that DOES carry the entry's
    # address, so the address rule cannot be what rejects this. Only the
    # recorded-versus-returned comparison can.
    FakeAuthClient.profile = {"id": INTRUDER, "name": "Alex", "email": EMAIL}

    result = await drive_reauth(hass, entry)

    assert result["type"] == "abort", result
    assert result["reason"] == "reauth_account_mismatch", result
    assert entry.data[CONF_ACCOUNT_ID] == ACCOUNT


# ── The recorded id is never overwritten ──────────────────────────────


async def test_reauth_populates_an_absent_account_id(hass, patched, client):
    """Absent is populated. Recorded is left alone. Both halves are needed.

    The obvious over-correction for the overwrite defect is to stop
    writing `CONF_ACCOUNT_ID` on the reauth path entirely. That would
    leave every pre-3.0 entry unable to record its account through the one
    flow it can still reach once its tokens have expired, which is the
    same lockout from a different direction.

    THE RELOAD IS DELIBERATELY BROKEN, and without that this test proves
    nothing. Reauth ends by reloading the entry, and `async_setup_entry`
    populates an absent `CONF_ACCOUNT_ID` itself. So an assertion on the
    end state is satisfied by the reload no matter what the reauth write
    did, and an unconditional pop passes it. Failing `list_devices` makes
    setup raise before `async_setup_entry` reaches its identity write, so
    a recorded account id at the end can only have come from the flow.

    BREAKS IF: `CONF_ACCOUNT_ID` is popped unconditionally rather than
    only when one is already recorded.
    """
    entry = legacy_entry(hass)
    assert not entry.data.get(CONF_ACCOUNT_ID)
    FakeAuthClient.profile = {"id": ACCOUNT, "name": "Alex", "email": EMAIL}
    client.fail_devices = OrionConnectionError("no route to host")

    result = await drive_reauth(hass, entry)

    assert result["reason"] == "reauth_successful", result
    # The reload really did stop short of the identity write, so the
    # assertion below is about the flow rather than about setup.
    assert entry.state is not ConfigEntryState.LOADED

    assert entry.data.get(CONF_ACCOUNT_ID) == ACCOUNT, (
        "a pre-3.0 entry completed reauth and still records no account. "
        "Setup normally papers over that, but an entry whose reload does "
        "not get that far now has nothing to verify a returned id against"
    )


async def test_a_recorded_account_id_is_never_overwritten_by_reauth(hass, patched):
    """Defence in depth, tested by removing the check that hides it.

    Honest about what it does: `_async_reauth_account_matches` is forced
    to True so the write is reached with a mismatched profile. No
    supported flow can produce that today, and that is the point. The
    write used to be safe only because two UNRELATED checks rejected the
    mismatch first, one of which compares a different field and one of
    which is about beds. Both are one refactor away from moving, and the
    write itself has to hold the invariant rather than inherit it.

    The recorded device ids match the ones the fake returns, so the bed
    check cannot be what saves this either. That check is exactly the
    "unrelated third check" that made the defect survivable, and two
    accounts sharing one bed is the household this integration is for.

    BREAKS IF: `CONF_ACCOUNT_ID` goes back to being spread into the reauth
    update unconditionally.
    """
    entry = make_entry(
        hass,
        data={CONF_ACCOUNT_ID: ACCOUNT, CONF_DEVICE_IDS: [BED_A]},
    )
    FakeAuthClient.profile = {"id": INTRUDER, "name": "Mallory", "email": EMAIL}

    with patch(
        "custom_components.orion_sleep.config_flow.OrionSleepConfigFlow."
        "_async_reauth_account_matches",
        return_value=True,
    ):
        result = await drive_reauth(hass, entry)

    # The write really was reached, so the assertion below is about the
    # pop rather than about a flow that aborted upstream.
    assert result["reason"] == "reauth_successful", result
    assert entry.data["access_token"] == "fresh-at"

    assert entry.data[CONF_ACCOUNT_ID] == ACCOUNT, (
        "reauth overwrote the recorded account id with the one the server "
        "returned. That does not surface a mismatch, it ratifies one, and "
        "every later boot compares the id to itself and reports agreement "
        "while the entity migration re-keys one person's history onto the "
        "other person's identity"
    )
