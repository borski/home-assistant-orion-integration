"""What a diagnostics download is allowed to contain, checked two ways.

The regression this file exists for: `CONF_PARTNER_ACCOUNT_ID` landed in
the partner-identity wave and `diagnostics.TO_REDACT` was not updated, so
a downloaded diagnostics file published the partner's Orion user id in
full while the primary account's identical field came out `**REDACTED**`.

```
_account_id_v3          -> **REDACTED**
_partner_account_id_v3  -> deadbeef-1111-4111-8111-999999999999
partner_auth_value      -> **REDACTED**
```

Neither existing defence saw it. `async_redact_data` matches keys
exactly and the key was not listed. `redact_identifier_keys` only reaches
a UUID used as a mapping KEY or as a list element, never one sitting in a
dict value. Both facts are pinned below so that "the guard is redundant"
is a claim somebody has to disprove rather than assume.

That id is `partner_profile["id"]`, the exact required input to
`remove_user_access`, `update_user_phone` and `assign_zones`. Those three
are admin gated because that id is the key to revoking somebody's bed
access and rewriting the phone number the vendor sends login codes to.

This was the third time a new field missed an existing enumeration in
this project, so the structural guard matters more than the one-line
fix. A behavioural test proves today's leak is closed. The enumeration
guard is the thing that survives the next parallel edit wave.
"""

from __future__ import annotations

import re

import pytest

from custom_components.orion_sleep import const
from custom_components.orion_sleep.diagnostics import (
    TO_REDACT,
    async_get_config_entry_diagnostics,
)
from custom_components.orion_sleep.helpers import redact_identifier_keys
from tests_ha.conftest import ACCOUNT, SERIAL_A, make_entry

# A distinctive value so an assertion on the rendered payload cannot pass
# by accident. Deliberately the one the reviewer measured.
PARTNER_ACCOUNT = "deadbeef-1111-4111-8111-999999999999"
# The primary has to be the account the fake client answers as. Setup
# refuses an entry whose recorded account id disagrees with the profile
# the tokens fetch, which is a different and already tested guard, and an
# entry that never finishes setup has no diagnostics to leak.
PRIMARY_ACCOUNT = ACCOUNT


# Constant NAMES whose stored value is a credential, an account
# identifier, a device serial, or internal migration state that embeds
# either of the first two inside a longer string. Anything matching has
# to be in `TO_REDACT`.
#
# Matched on the constant name rather than on the stored string because
# the names are the part a human writes deliberately. `CONF_ACCOUNT_ID`
# is spelled `_account_id_v3` on disk and `CONF_PARTNER_ACCOUNT_ID` is
# `_partner_account_id_v3`, and no rule reading only those two strings
# would group them together as obviously as their names do.
_SENSITIVE_NAME_RE = re.compile(
    r"ACCOUNT_ID|TOKEN|AUTH_VALUE|SERIAL|DEVICE_IDS|UID_|API_KEY"
)

# Config keys deliberately left in a diagnostics download, each with the
# reason, so the guard below can tell "considered and allowed" apart from
# "nobody thought about it". Same shape as `_INTENTIONALLY_UNGATED` in
# `test_entity_service_auth_real.py` and for the same reason.
_DELIBERATELY_NOT_REDACTED = frozenset(
    {
        # "email" or "phone". Which KIND of credential the household logs
        # in with, never the credential. The value itself is
        # CONF_AUTH_VALUE and that is redacted.
        "CONF_AUTH_METHOD",
        "CONF_PARTNER_AUTH_METHOD",
        # Unix timestamps. Knowing when a token expires identifies
        # nobody, and it is the single most useful field in a bug report
        # about tokens.
        "CONF_EXPIRES_AT",
        "CONF_PARTNER_EXPIRES_AT",
        # A bool and an opaque reload nonce.
        "CONF_PARTNER_CONFIGURED",
        "CONF_PARTNER_REVISION",
        # Household settings. The whole reason somebody downloads this.
        "CONF_SCAN_INTERVAL",
        "CONF_ALLOW_UNVERIFIED_ACCOUNT",
        "CONF_INSIGHTS_DAYS",
        # Which physical zone is the left side of the bed, "zone_a" or
        # "zone_b". A furniture-orientation preference for a bedside dial.
        # It names no person and carries no credential, and it is exactly
        # the kind of setting a bug report about a dial pointing at the
        # wrong side needs to show.
        "CONF_ZONE_LEFT",
        # A bare `True`, written when a linked partner is swapped for a
        # different one. It names nobody. It does not carry the previous
        # partner's id, which is `CONF_PARTNER_ACCOUNT_ID` and is
        # redacted, and it is the single most useful field in a bug
        # report about a downgrade going wrong, because it is what
        # `async_revert_unique_ids` reads to decide whether the legacy
        # `_partner_` rows still belong to the current partner.
        #
        # Listed here despite the `_..._v3` private-namespace spelling.
        # The other keys with that shape are redacted because they embed
        # complete account and device UUIDs inside longer unique-id
        # strings. This one embeds nothing.
        "CONF_PARTNER_REPLACED",
    }
)


def _conf_constants() -> dict[str, str]:
    """Every `CONF_*` string constant declared in `const.py`."""
    return {
        name: value
        for name, value in vars(const).items()
        if name.startswith("CONF_") and isinstance(value, str)
    }


# ── The enumeration guard ─────────────────────────────────────────────


def test_the_scan_found_the_constants():
    """A scan that silently matched nothing would green-light everything.

    Both guards below draw their cases from `_conf_constants`. An empty
    result turns them into no-ops that report as passing, which is the
    one failure mode a derived list has and a hardcoded one does not.
    """
    constants = _conf_constants()
    assert constants, "no CONF_* constants found in const.py at all"
    sensitive = [n for n in constants if _SENSITIVE_NAME_RE.search(n)]
    assert sensitive, (
        "the sensitivity pattern matched no constant, so the coverage "
        "guard below is checking an empty set"
    )
    # The regression itself, named, so a future edit to the pattern that
    # stops covering it fails here with an unmistakable message rather
    # than quietly shrinking what gets checked.
    assert "CONF_PARTNER_ACCOUNT_ID" in sensitive, (
        "the sensitivity pattern no longer matches CONF_PARTNER_ACCOUNT_ID, "
        "which is the exact constant that shipped unredacted"
    )


@pytest.mark.parametrize(
    "name", sorted(n for n in _conf_constants() if _SENSITIVE_NAME_RE.search(n))
)
def test_every_credential_shaped_config_key_is_redacted(name):
    """The guard that would have caught this the moment the constant landed.

    Parametrized so the failure names the offending constant instead of
    reporting a set difference.
    """
    value = _conf_constants()[name]
    assert value in TO_REDACT, (
        f"{name} = {value!r} holds a credential, an account id, a device "
        "serial, or migration state that embeds one, and it is not in "
        "diagnostics.TO_REDACT. A diagnostics download therefore publishes "
        "it verbatim. Add the CONSTANT to TO_REDACT, not a retyped copy of "
        "its string."
    )


def test_every_config_key_has_been_decided_about():
    """A new constant either passes or fails loudly. It never passes silently.

    The coverage guard above only sees names matching the sensitivity
    pattern, so a sensitive field named outside that pattern would slip
    through exactly the way `CONF_PARTNER_ACCOUNT_ID` did. This closes
    that by requiring every `CONF_*` constant to land in one of three
    places: matched by the pattern, already redacted anyway, or listed in
    `_DELIBERATELY_NOT_REDACTED` with a reason.

    A newly added constant is in none of them and fails here with its own
    name. That is deliberate and it is the point. This project has now
    missed an existing enumeration three times, and the only mechanism
    that survives several agents editing in parallel is one that refuses
    to stay quiet.
    """
    undecided = sorted(
        name
        for name, value in _conf_constants().items()
        if not _SENSITIVE_NAME_RE.search(name)
        and value not in TO_REDACT
        and name not in _DELIBERATELY_NOT_REDACTED
    )
    assert not undecided, (
        f"config keys nobody has decided about: {undecided}. Either add the "
        "constant to diagnostics.TO_REDACT, or add its NAME to "
        "_DELIBERATELY_NOT_REDACTED here with the reason it is safe to "
        "publish. Do not skip this by renaming the constant."
    )


def test_the_allowlist_has_not_gone_stale():
    """An allowlist naming constants that no longer exist protects nothing.

    Without this, deleting a config key leaves its exemption behind, and
    the next constant that happens to reuse the name inherits a decision
    nobody made about it.
    """
    constants = _conf_constants()
    stale = sorted(_DELIBERATELY_NOT_REDACTED - set(constants))
    assert not stale, f"allowlist names constants that no longer exist: {stale}"
    contradictory = sorted(
        name
        for name in _DELIBERATELY_NOT_REDACTED
        if _SENSITIVE_NAME_RE.search(name) or constants[name] in TO_REDACT
    )
    assert not contradictory, (
        f"these are exempted and redacted at the same time: {contradictory}. "
        "One of the two statements is wrong and the file no longer says "
        "which."
    )


# ── Why neither existing defence covers it ────────────────────────────


def test_a_uuid_sitting_in_a_dict_value_is_not_redacted_by_itself():
    """`redact_identifier_keys` is a key and list-element rule, not a value rule.

    Pinned so nobody deletes the enumeration guard on the theory that the
    UUID scrubber already covers account ids. It does not, and that gap
    is precisely why the partner id came out in plain text next to a
    redacted primary id.

    If this ever starts failing because `redact_identifier_keys` grew a
    value rule, that is a real improvement and this test should be
    rewritten rather than deleted. The enumeration guard still earns its
    place, because several redacted keys hold things that are not UUIDs.
    """
    payload = {"_partner_account_id_v3": PARTNER_ACCOUNT}
    assert redact_identifier_keys(payload) == payload

    # And the shapes it DOES cover, so this is not passing because the
    # function stopped working.
    assert redact_identifier_keys({PARTNER_ACCOUNT: 1}) != {PARTNER_ACCOUNT: 1}
    assert redact_identifier_keys([PARTNER_ACCOUNT]) == ["**REDACTED**"]


# ── The leak itself, end to end ───────────────────────────────────────


async def test_the_partner_account_id_never_reaches_the_download(
    hass, patched, client
):
    """The reviewer's reproduction, as a test.

    Goes through the real `async_get_config_entry_diagnostics` rather
    than inspecting `TO_REDACT`, because the structural guards above
    would both pass against a redaction set that was never applied to
    `entry.data`.
    """
    entry = make_entry(
        hass,
        data={
            const.CONF_ACCOUNT_ID: PRIMARY_ACCOUNT,
            const.CONF_PARTNER_ACCOUNT_ID: PARTNER_ACCOUNT,
            const.CONF_PARTNER_AUTH_VALUE: "bo@example.com",
            const.CONF_PARTNER_ACCESS_TOKEN: "partner-at",
            const.CONF_PARTNER_REFRESH_TOKEN: "partner-rt",
            const.CONF_PARTNER_DEVICE_SERIAL: SERIAL_A,
        },
    )
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    rendered = repr(await async_get_config_entry_diagnostics(hass, entry))

    assert PARTNER_ACCOUNT not in rendered, (
        "the partner's Orion user id shipped in the diagnostics download. "
        "That value is the required input to remove_user_access, "
        "update_user_phone and assign_zones, all three of which are admin "
        "gated for exactly that reason."
    )
    # The symmetry, which is the part that made this worse than a plain
    # omission. Publishing one half of a couple's identifiers while
    # redacting the other is not a partial fix, it is a targeted leak.
    assert PRIMARY_ACCOUNT not in rendered, (
        "the primary account id leaked too, so the asymmetry assertion "
        "below proves nothing"
    )
    assert "bo@example.com" not in rendered
    assert "partner-at" not in rendered
    assert "partner-rt" not in rendered
    assert SERIAL_A not in rendered


# ── API keys never reach a diagnostics download ───────────────────────


async def test_an_api_key_never_reaches_the_download(hass, patched, client):
    """Definition-of-done item 4: no `os_live` string anywhere.

    A key-authed entry stores the raw key both as CONF_API_KEY and, so the
    client can send it, as CONF_ACCESS_TOKEN. Both must be redacted, and
    the same for a key-authed partner. `os_live_` is the distinctive
    prefix, so a grep for it is the exact check the spec calls for.
    """
    primary_key = "os_live_" + "P" * 43
    partner_key = "os_live_" + "Q" * 43
    entry = make_entry(
        hass,
        data={
            const.CONF_ACCOUNT_ID: PRIMARY_ACCOUNT,
            const.CONF_AUTH_METHOD: "api_key",
            const.CONF_API_KEY: primary_key,
            const.CONF_ACCESS_TOKEN: primary_key,
            const.CONF_PARTNER_ACCOUNT_ID: PARTNER_ACCOUNT,
            const.CONF_PARTNER_AUTH_METHOD: "api_key",
            const.CONF_PARTNER_API_KEY: partner_key,
            const.CONF_PARTNER_ACCESS_TOKEN: partner_key,
            const.CONF_PARTNER_DEVICE_SERIAL: SERIAL_A,
        },
    )
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    rendered = repr(await async_get_config_entry_diagnostics(hass, entry))

    assert "os_live" not in rendered, (
        "an Orion API key reached the diagnostics download. A key is a live "
        "credential and must never appear in a bundle."
    )
    assert primary_key not in rendered
    assert partner_key not in rendered
