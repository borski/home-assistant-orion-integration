"""Relinking a partner, and what an options save is allowed to throw away.

Two defects, both in the options flow, both of the same shape: a value
that one layer depends on is never written by the layer that owns it.

`CONF_PARTNER_ACCOUNT_ID` was read three times and written zero times.
`coordinator.recorded_partner_account_id` reads it and
`_partner_identity_verified` compares the returned partner id against it,
so the moment an entry recorded partner A by any route, linking partner B
stored B's tokens beside A's recorded id. The next poll called that a
mismatch and disabled partner insights, with a warning instructing the
household to relink the partner in the Orion options. Relinking is the
action that had just failed. The removal path did not clear the key
either, so remove-then-re-add did not help. There was no supported flow
that could clear it, and once A was linked B could never be accepted.

The second defect is quieter. `async_create_entry` REPLACES `entry.options`
wholesale with whatever the form collected, and the form never collected
`CONF_UID_MIGRATION`. That key is the original `[old, new]` pair journal,
the map back from 3.x unique ids to the ids 2.x asks for. Changing the
polling interval on an entry that had not yet completed a migration pass
deleted it. Nothing fails. `async_revert_unique_ids` later reports "no
recorded Orion renames to undo" and exits clean while every entity sits on
an id 2.x never asks for.

Every test here drives the real options flow, because both defects are
about what the flow WRITES and a unit-level assertion on a helper would
pass against a flow that never called it.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

from homeassistant.config_entries import ConfigEntryState

from custom_components.orion_sleep.const import (
    CONF_ACCOUNT_ID,
    CONF_ALLOW_UNVERIFIED_ACCOUNT,
    CONF_INSIGHTS_DAYS,
    CONF_PARTNER_ACCESS_TOKEN,
    CONF_PARTNER_ACCOUNT_ID,
    CONF_PARTNER_AUTH_VALUE,
    CONF_PARTNER_DEVICE_SERIAL,
    CONF_PARTNER_EXPIRES_AT,
    CONF_PARTNER_REFRESH_TOKEN,
    CONF_SCAN_INTERVAL,
    CONF_UID_MIGRATION,
)
from tests_ha.conftest import (
    ACCOUNT,
    BED_A,
    ENTRY,
    PARTNER,
    SERIAL_A,
    FakeClient,
    device,
    make_entry,
)

# Partner B. Deliberately neither ACCOUNT nor PARTNER, so no assertion
# below can pass by matching the wrong side of the swap.
PARTNER_B = "44444444-4444-4444-8444-444444444444"
PARTNER_A_EMAIL = "bob@example.com"
PARTNER_B_EMAIL = "carol@example.com"


def bed_shared_with(user_id: str) -> dict[str, Any]:
    """The one bed, with its second zone owned by `user_id`.

    `conftest.device` hardcodes PARTNER on zone_b, and PARTNER is the
    account being replaced here. Naming the zone explicitly keeps the
    primary's and each partner's view of the same physical bed consistent
    with whoever is actually linked at the time.
    """
    bed = device(BED_A, SERIAL_A)
    bed["zones"] = [
        {"id": "zone_a", "user": {"id": ACCOUNT}},
        {"id": "zone_b", "user": {"id": user_id}},
    ]
    return bed


# Keyed on the access token rather than on call order. An alternating fake
# keeps answering as partner A after a swap, so the reload would re-verify
# A against A and report success while B's tokens sat in the entry, which
# is a test that passes without the fix. Answering per token is also what
# a real server does.
ACCOUNTS: dict[str, tuple[str, str, str]] = {
    "at": (ACCOUNT, "Alex", "alice@example.com"),
    "partner-at": (PARTNER, "Bo", PARTNER_A_EMAIL),
    "partner-b-at": (PARTNER_B, "Carol", PARTNER_B_EMAIL),
}


def client_for_token(*_args: Any, **kwargs: Any) -> FakeClient:
    """A client that answers as whoever the supplied token belongs to."""
    user_id, name, email = ACCOUNTS[kwargs["access_token"]]
    built = FakeClient()
    built.user = {"id": user_id, "name": name, "email": email}
    # The bed's second zone belongs to whichever partner is asking, or to
    # partner A when the primary is asking. The primary's zone map is not
    # what any assertion here turns on.
    built.devices = [bed_shared_with(user_id if user_id != ACCOUNT else PARTNER)]
    return built


class FakeAuthClient:
    """The client the options flow builds while linking a new partner.

    Separate from `conftest.FakeClient` because the config flow makes the
    two auth-code calls the coordinator never makes, and because it has to
    answer as partner B rather than as whoever is already linked.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.user: dict[str, Any] = {
            "id": PARTNER_B,
            "name": "Carol",
            "email": PARTNER_B_EMAIL,
        }
        self.devices: list[dict[str, Any]] = [bed_shared_with(PARTNER_B)]

    async def request_auth_code(self, email=None, phone=None) -> bool:
        return True

    async def verify_auth_code(self, code, email=None, phone=None) -> dict[str, Any]:
        return {
            "access_token": "partner-b-at",
            "refresh_token": "partner-b-rt",
            "expires_at": 9e12,
        }

    def set_token_refresh_callback(self, _cb) -> None:
        return

    async def ensure_valid_token(self) -> None:
        return

    async def get_current_user(self) -> dict[str, Any]:
        return dict(self.user)

    async def list_devices(self) -> list[dict[str, Any]]:
        return [dict(d) for d in self.devices]


def partner_entry(hass, *, data=None, options=None):
    """An entry with partner A linked, recorded the way the flow records it."""
    entry = make_entry(
        hass,
        data={
            CONF_ACCOUNT_ID: ACCOUNT,
            CONF_PARTNER_ACCESS_TOKEN: "partner-at",
            CONF_PARTNER_REFRESH_TOKEN: "partner-rt",
            CONF_PARTNER_EXPIRES_AT: 9e12,
            CONF_PARTNER_DEVICE_SERIAL: SERIAL_A,
            CONF_PARTNER_AUTH_VALUE: PARTNER_A_EMAIL,
            CONF_PARTNER_ACCOUNT_ID: PARTNER,
            **(data or {}),
        },
    )
    if options:
        hass.config_entries.async_update_entry(entry, options=options)
    return entry


def base_form(**overrides: Any) -> dict[str, Any]:
    """The init step's payload, with every field the schema requires."""
    payload = {
        CONF_SCAN_INTERVAL: 300,
        CONF_INSIGHTS_DAYS: 7,
        "partner_action": "keep",
        "edit_aliases": False,
        CONF_ALLOW_UNVERIFIED_ACCOUNT: False,
    }
    payload.update(overrides)
    return payload


def options_journal(entry) -> list[Any]:
    return list(entry.options.get(CONF_UID_MIGRATION) or [])


def partner_pair(user_id: str = PARTNER, key: str = "efficiency") -> list[str]:
    """A partner rename in the original `[old, new]` options format."""
    return [f"{BED_A}_partner_{key}", f"{ENTRY}_user_{user_id}_{key}"]


def primary_pair(key: str = "efficiency") -> list[str]:
    """A primary rename in the original `[old, new]` options format."""
    return [f"{BED_A}_{key}", f"{ENTRY}_user_{ACCOUNT}_{key}"]


# ── Replacing a partner, end to end ───────────────────────────────────


async def test_replacing_a_partner_works_end_to_end(hass, ws_manager):
    """The live bug, stated as the thing a household actually does.

    Partner A is linked and verified. The user replaces them with partner
    B through the real options flow, exactly as the warning message tells
    them to. The entry reloads and has to accept B.

    Before the fix this failed at the last two assertions. B's tokens were
    written, `CONF_PARTNER_ACCOUNT_ID` still said A, and the reload
    compared B against A, logged "Orion returned a different partner
    account than this entry recorded", and left partner insights disabled
    for good. The remedy the warning prescribed was the action that had
    just failed.

    The entry is set up for real because the replacement path reads
    `runtime_data` for the primary's devices before it will accept a
    partner. A flow that aborted before `_write_partner_change` would make
    a weaker version of this test pass without ever writing anything.

    BREAKS IF: `CONF_PARTNER_ACCOUNT_ID: partner_id` is dropped from the
    dict `async_step_partner_verify` hands to `_write_partner_change`.
    """
    entry = partner_entry(hass)

    with (
        patch(
            "custom_components.orion_sleep.OrionApiClient",
            side_effect=client_for_token,
        ),
        patch(
            "custom_components.orion_sleep.coordinator.OrionWebSocketManager",
            return_value=ws_manager,
        ),
    ):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        assert entry.state is ConfigEntryState.LOADED
        # The positive control. Partner A is genuinely accepted first, so
        # a later failure is the replacement rather than a partner path
        # that never worked in this fixture at all.
        assert entry.runtime_data.partner_mapping_valid is True
        assert entry.runtime_data.partner_user["id"] == PARTNER

        with patch(
            "custom_components.orion_sleep.config_flow.OrionApiClient",
            side_effect=FakeAuthClient,
        ):
            flow = await hass.config_entries.options.async_init(entry.entry_id)
            result = await hass.config_entries.options.async_configure(
                flow["flow_id"], base_form(partner_action="add")
            )
            result = await hass.config_entries.options.async_configure(
                result["flow_id"], {"auth_method": "email"}
            )
            result = await hass.config_entries.options.async_configure(
                result["flow_id"], {"email": PARTNER_B_EMAIL}
            )
            result = await hass.config_entries.options.async_configure(
                result["flow_id"], {"code": "123456"}
            )
        assert result["type"] == "create_entry", result
        await hass.async_block_till_done()

    # The replacement landed, so nothing below is measuring a flow that
    # aborted somewhere harmless.
    assert entry.data[CONF_PARTNER_ACCESS_TOKEN] == "partner-b-at"

    assert entry.data.get(CONF_PARTNER_ACCOUNT_ID) == PARTNER_B, (
        "the entry still records the PREVIOUS partner's account id beside "
        "the new partner's tokens, which is the pairing the coordinator "
        "reads as a mismatch and disables partner insights over"
    )

    # The consequence, which is the part that was actually broken. The
    # recorded id only matters because the coordinator compares against it.
    coordinator = entry.runtime_data
    assert coordinator.partner_user["id"] == PARTNER_B
    assert coordinator.partner_mapping_valid is True, (
        "partner insights are still disabled after replacing the partner "
        "through the supported flow, and the flow the warning tells the "
        "household to use is the one that just ran"
    )
    assert coordinator.partner_identity_confirmed is True


async def test_removing_a_partner_clears_the_recorded_partner_account_id(
    hass, ws_manager
):
    """Removal has to clear the identity claim along with the tokens.

    A recorded id left behind describes tokens that are no longer there,
    and `_partner_identity_verified` prefers a recorded id over the linked
    address, so the NEXT partner would have been judged against the
    previous one. It is also why remove-then-re-add was not a way out of
    the replacement bug.

    BREAKS IF: `CONF_PARTNER_ACCOUNT_ID` is dropped from the `partner_keys`
    set in `_async_finish_init`.
    """
    entry = partner_entry(hass)
    # The positive control, and it has to be here. Without it this test
    # would pass on an entry that never recorded a partner id at all.
    assert entry.data[CONF_PARTNER_ACCOUNT_ID] == PARTNER

    flow = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        flow["flow_id"], base_form(partner_action="remove")
    )
    assert result["type"] == "create_entry", result
    await hass.async_block_till_done()

    # The removal actually ran, rather than the flow taking "keep".
    assert CONF_PARTNER_ACCESS_TOKEN not in entry.data

    assert CONF_PARTNER_ACCOUNT_ID not in entry.data, (
        "the removed partner's account id outlived their tokens, so it is "
        "still the reference value the next partner gets compared against: "
        f"{entry.data.get(CONF_PARTNER_ACCOUNT_ID)!r}"
    )
    # Not vacuous, and not a test of "removal deletes everything". The
    # primary's own account id has to survive a partner removal.
    assert entry.data[CONF_ACCOUNT_ID] == ACCOUNT


# ── What an options save is allowed to throw away ─────────────────────


async def test_an_options_save_preserves_an_unmigrated_pair_journal(hass):
    """Changing the polling interval must not delete the downgrade map.

    The entry is deliberately NOT set up. A migration pass pops this key
    out of `options` and into `data`, so a loaded entry no longer has one,
    and setting the entry up would quietly remove the very thing this test
    is about. Pre-migration is the only state where the defect is
    reachable, and it is exactly the state a fresh upgrade sits in.

    BREAKS IF: `_async_save_options` stops carrying `CONF_UID_MIGRATION`
    forward, or if any `async_create_entry` in the options flow goes back
    to being called directly.
    """
    kept = primary_pair()
    entry = make_entry(hass, data={CONF_ACCOUNT_ID: ACCOUNT})
    hass.config_entries.async_update_entry(
        entry, options={CONF_SCAN_INTERVAL: 600, CONF_UID_MIGRATION: [kept]}
    )

    flow = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        flow["flow_id"], base_form(**{CONF_SCAN_INTERVAL: 300})
    )
    assert result["type"] == "create_entry", result
    await hass.async_block_till_done()

    # The save really happened, so a surviving journal is preservation
    # rather than a flow that wrote nothing.
    assert entry.options[CONF_SCAN_INTERVAL] == 300

    assert kept in options_journal(entry), (
        "an options save deleted the pair journal, so a later downgrade "
        "reports nothing to undo while every entity sits on a 3.x id that "
        f"2.x never asks for: {entry.options.get(CONF_UID_MIGRATION)!r}"
    )


async def test_preserving_the_journal_does_not_resurrect_evicted_pairs(hass):
    """The hazard the preservation itself introduces, pinned.

    A partner removal evicts the partner's records from BOTH journals and
    writes the pruned options to the entry, and only then does the flow
    save. Carrying a copy of `options` captured before that eviction would
    put the previous partner's pairs back immediately after removing them,
    which is the exact history merge `evict_partner_journal` exists to
    prevent, performed by the code meant to prevent data loss.

    Both halves matter and they pull in opposite directions, which is why
    they are asserted in one test. Dropping everything passes the first
    assertion and fails the second. Preserving everything passes the
    second and fails the first.

    BREAKS IF: `_async_save_options` reads a captured options dict instead
    of `self._config_entry.options` at call time, or is called before
    `_write_partner_change`.
    """
    evicted = partner_pair()
    kept = primary_pair()
    entry = partner_entry(
        hass, options={CONF_SCAN_INTERVAL: 600, CONF_UID_MIGRATION: [evicted, kept]}
    )
    # Both fixtures are real to the reader that decides what a revert
    # renames: one is labelled partner by `_role_for`, one is not.
    assert "_partner_" in evicted[0]
    assert "_partner_" not in kept[0]

    flow = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        flow["flow_id"], base_form(partner_action="remove")
    )
    assert result["type"] == "create_entry", result
    await hass.async_block_till_done()
    assert CONF_PARTNER_ACCESS_TOKEN not in entry.data

    assert evicted not in options_journal(entry), (
        "the removed partner's rename came back after the eviction "
        "dropped it, so a downgrade renames their rows onto the id 2.x "
        "now feeds from the next partner's account"
    )
    assert kept in options_journal(entry), (
        "the primary's rename was dropped by the same save, which strands "
        "every primary entity on an id 2.x never asks for"
    )


async def test_an_options_save_without_a_journal_creates_no_empty_one(hass):
    """Absent is not the same as empty, and the difference is load bearing.

    Downstream readers treat a present-but-empty journal as "a journal
    exists here and it is complete", which is the most confident possible
    way to be wrong about whether a downgrade has anything to undo.
    `migrations._write_journal` pops the key rather than leaving `[]`, and
    the preservation has to hold the same line.

    BREAKS IF: the preservation is written as an unconditional
    `options[CONF_UID_MIGRATION] = self._config_entry.options.get(...)`
    or otherwise carries a falsy value forward.
    """
    entry = make_entry(hass, data={CONF_ACCOUNT_ID: ACCOUNT})
    hass.config_entries.async_update_entry(
        entry, options={CONF_SCAN_INTERVAL: 600, CONF_UID_MIGRATION: []}
    )

    flow = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        flow["flow_id"], base_form()
    )
    assert result["type"] == "create_entry", result
    await hass.async_block_till_done()

    assert CONF_UID_MIGRATION not in entry.options, (
        "an empty journal was carried forward as a real one, and a reader "
        "cannot tell that from a complete journal with nothing to undo: "
        f"{entry.options.get(CONF_UID_MIGRATION)!r}"
    )
