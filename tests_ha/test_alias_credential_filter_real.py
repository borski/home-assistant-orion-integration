"""A household-typed alias is a permanent identifier too.

`helpers.is_safe_display_name` was added because
`orion_sleep_api.util.orion_user_label` falls back through first_name,
firstName, name, email and finally phone, so an account that never had a
display name set puts a login credential into a name. Home Assistant
slugifies the FIRST name an entity registers with into its entity_id and
never revisits it on rename, so that credential lands in the entity
registry, every recorder row, every long term statistics row and every
backup, permanently.

`coordinator.display_name_for_user` filtered the vendor's label through
that predicate and did not filter the configured alias, which sits three
lines above it and outranks it. So the exact string the filter existed to
keep out of a permanent identifier walked in through the field beside the
guarded one.

Self-inflicted, because the household typed it. That lowers the severity
and changes nothing about the permanence, and "you typed it" is not a
remedy once the id is minted. Clearing the alias afterwards changes the
friendly name and leaves the entity_id exactly where it is.

The fix is in two places on purpose:

  * `config_flow.async_step_aliases`, the write boundary. The only place
    that can explain the refusal to the person who typed it, and the only
    place where the value is not yet permanent.
  * `coordinator.display_name_for_user`, on read. Belt and braces for an
    alias stored before that validation existed, which the form will
    never see again because nothing re-validates an option on read.

Deliberately NOT in `helpers.clean_alias_map`. That runs from
`coordinator.__init__` over already-stored options, so a filter there
would silently DELETE an alias a household is relying on, with no
message. The last test in this file holds that line.
"""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigEntryState

from custom_components.orion_sleep import helpers
from custom_components.orion_sleep.const import (
    CONF_DISPLAY_ALIASES,
    CONF_INSIGHTS_DAYS,
    CONF_SCAN_INTERVAL,
)
from tests_ha.conftest import ACCOUNT, make_entry

# The two credential shapes `orion_user_label` ends its fallback chain on,
# typed into the alias field instead of arriving from the vendor. The
# permanence argument does not care which door they came through.
EMAIL_ALIAS = "alice@example.com"
PHONE_ALIAS = "+1 (555) 123-4567"


async def loaded(hass, *, options: dict[str, Any] | None = None):
    entry = make_entry(hass)
    if options:
        hass.config_entries.async_update_entry(entry, options=options)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    return entry


def entity_ids(hass) -> list[str]:
    return [state.entity_id for state in hass.states.async_all()]


def slugged(value: str) -> str:
    """Roughly what Home Assistant would put in an entity_id.

    Not `homeassistant.util.slugify`, on purpose. The assertions below
    want a substring that is recognisably the credential, and computing it
    with the very function under discussion would let a change in that
    function quietly make these tests agree with whatever happened.
    """
    out = "".join(char if char.isalnum() else "_" for char in value.lower())
    return "_".join(part for part in out.split("_") if part)


async def open_alias_form(hass, entry):
    """Drive the real options flow to the aliases step."""
    flow = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        flow["flow_id"],
        {
            CONF_SCAN_INTERVAL: 300,
            CONF_INSIGHTS_DAYS: 7,
            "partner_action": "keep",
            "edit_aliases": True,
        },
    )
    assert result["type"] == "form", result
    assert result["step_id"] == "aliases", result
    return flow, result


def primary_label(entry) -> str:
    """The alias form's field name for the authenticated account.

    Read from the same helper the flow builds its schema with rather than
    hardcoded, because the label is derived from the vendor profile and a
    hardcoded guess would turn a renamed fixture into a silent skip.
    """
    labels = helpers.unique_alias_labels(entry.runtime_data.known_users())
    assert ACCOUNT in labels, labels
    return labels[ACCOUNT]


# ---------------------------------------------------------------------
# The write boundary. `config_flow.async_step_aliases`.
# ---------------------------------------------------------------------


async def test_an_email_alias_is_refused_by_the_options_form(
    hass, patched, client, ws_manager
):
    """The half that can explain itself.

    Breaks if the per-field validation is removed from
    `config_flow.async_step_aliases`, which would let the value be stored
    and leave only the read-side filter standing between it and an
    entity_id it can never be removed from.
    """
    entry = await loaded(hass)
    flow, _ = await open_alias_form(hass, entry)
    label = primary_label(entry)

    result = await hass.config_entries.options.async_configure(
        flow["flow_id"], {label: EMAIL_ALIAS}
    )

    assert result["type"] == "form", (
        "the alias form accepted an email address and saved it. Home "
        "Assistant never rewrites an entity_id on rename, so this cannot "
        f"be undone by clearing the alias later: {result}"
    )
    assert result["step_id"] == "aliases", result
    assert result.get("errors"), (
        "the form came back with no error, so the household is looking at "
        "a field that silently refused to save and cannot tell why"
    )
    assert result["errors"].get(label) == "unsafe_alias", (
        "the refusal is not attached to the field that caused it: "
        f"{result['errors']}"
    )
    assert not entry.options.get(CONF_DISPLAY_ALIASES), (
        "the options were written anyway, so the refusal was cosmetic: "
        f"{entry.options.get(CONF_DISPLAY_ALIASES)}"
    )


async def test_a_phone_alias_is_refused_by_the_options_form(
    hass, patched, client, ws_manager
):
    """The other end of `orion_user_label`'s fallback chain.

    Covered separately because `is_safe_display_name` reaches it through a
    different branch: an email is rejected on an "@" and a phone number on
    a digit count with no letters. A validator wired to only one of them
    passes the test above and ships the other.
    """
    entry = await loaded(hass)
    flow, _ = await open_alias_form(hass, entry)
    label = primary_label(entry)

    result = await hass.config_entries.options.async_configure(
        flow["flow_id"], {label: PHONE_ALIAS}
    )

    assert result["type"] == "form", (
        f"the alias form accepted a phone number and saved it: {result}"
    )
    assert result["errors"].get(label) == "unsafe_alias", result.get("errors")


async def test_the_refused_form_shows_back_what_was_typed(
    hass, patched, client, ws_manager
):
    """A rejection the household can act on.

    Re-showing the previous stored value instead of the offending one
    reads as if the refusal came from somewhere else, and leaves the
    person retyping a name they cannot see was rejected.
    """
    entry = await loaded(hass)
    flow, _ = await open_alias_form(hass, entry)
    label = primary_label(entry)

    result = await hass.config_entries.options.async_configure(
        flow["flow_id"], {label: EMAIL_ALIAS}
    )

    defaults = {
        str(key): key.default() for key in result["data_schema"].schema
    }
    assert defaults.get(label) == EMAIL_ALIAS, (
        "the form discarded what was typed, so the household cannot see "
        f"or edit the value that was refused: {defaults}"
    )


async def test_a_real_name_alias_is_still_accepted(hass, patched, client, ws_manager):
    """Positive control, and it is not optional.

    Every assertion above is satisfied by a form that refuses every alias,
    which would break the documented escape hatch from an ugly fallback
    name while turning this file green.
    """
    entry = await loaded(hass)
    flow, _ = await open_alias_form(hass, entry)
    label = primary_label(entry)

    result = await hass.config_entries.options.async_configure(
        flow["flow_id"], {label: "Bob"}
    )
    await hass.async_block_till_done()

    assert result["type"] == "create_entry", (
        f"an ordinary display name was refused by the alias form: {result}"
    )
    assert result["data"][CONF_DISPLAY_ALIASES] == {ACCOUNT: "Bob"}


async def test_a_blank_alias_is_still_how_you_clear_one(
    hass, patched, client, ws_manager
):
    """Clearing a field is not an error.

    It is the documented way to drop an override and fall back to the
    account name. A validator that treats an empty string as unsafe
    removes the only way back out of an alias.
    """
    entry = await loaded(hass, options={CONF_DISPLAY_ALIASES: {ACCOUNT: "Bob"}})
    flow, _ = await open_alias_form(hass, entry)
    label = primary_label(entry)

    result = await hass.config_entries.options.async_configure(
        flow["flow_id"], {label: ""}
    )
    await hass.async_block_till_done()

    assert result["type"] == "create_entry", (
        f"clearing an alias was refused as unsafe: {result}"
    )
    assert not result["data"][CONF_DISPLAY_ALIASES], result["data"]


# ---------------------------------------------------------------------
# The read side. `coordinator.display_name_for_user`.
#
# THESE TWO ARE THE ONES THAT FAIL AGAINST THE PRE-FIX CODE. The form
# validation above is new, so it cannot be regressed by code that never
# had it. An alias already sitting in options predates the validation and
# is never shown to the form again, so nothing but this filter stands
# between it and a permanent entity_id.
# ---------------------------------------------------------------------


async def test_a_stored_email_alias_never_reaches_an_entity_id(
    hass, patched, client, ws_manager
):
    """The finding.

    Against the pre-fix coordinator this registers
    `climate.sleepy_alice_example_com_climate` and every sibling entity
    with the same stem. Asserted across every entity the integration
    registers rather than a named few, because `primary_name` feeds
    climate, sensor, binary_sensor, switch, number and time alike.
    """
    entry = await loaded(
        hass, options={CONF_DISPLAY_ALIASES: {ACCOUNT: EMAIL_ALIAS}}
    )

    ids = entity_ids(hass)
    assert ids, "no entities registered, so this test proves nothing"

    stem = slugged(EMAIL_ALIAS)
    offenders = [eid for eid in ids if stem in eid or "alice" in eid]
    assert not offenders, (
        "an alias containing an email address was slugified into a "
        f"permanent entity_id: {offenders}. The vendor's own label is "
        "filtered on this exact path and the household's is not, which is "
        "an asymmetry with no principle behind it. Home Assistant never "
        "rewrites an entity_id on rename, so clearing the alias does not "
        "undo this."
    )
    assert entry.runtime_data.display_name_for_user(ACCOUNT) != EMAIL_ALIAS


async def test_a_stored_phone_alias_never_reaches_an_entity_id(
    hass, patched, client, ws_manager
):
    """The last link in the chain, stored rather than vendor-supplied."""
    await loaded(hass, options={CONF_DISPLAY_ALIASES: {ACCOUNT: PHONE_ALIAS}})

    ids = entity_ids(hass)
    assert ids, "no entities registered, so this test proves nothing"

    offenders = [eid for eid in ids if "555" in eid or "123" in eid]
    assert not offenders, (
        f"an alias containing a phone number reached an entity_id: {offenders}"
    )


async def test_a_refused_alias_falls_back_rather_than_blanking(
    hass, patched, client, ws_manager
):
    """Safe must not mean nameless.

    An entity with an empty name is worse than one with an ugly name, and
    the id-derived fallback is what makes refusing an alias affordable at
    all. Refusing to USE the value is the whole intervention. It must not
    turn into refusing to name the entity.
    """
    entry = await loaded(
        hass, options={CONF_DISPLAY_ALIASES: {ACCOUNT: EMAIL_ALIAS}}
    )
    coordinator = entry.runtime_data

    # Falls through to the vendor's own name, which the default fixture
    # profile carries and which is perfectly safe.
    assert coordinator.display_name_for_user(ACCOUNT) == "Alex"
    assert coordinator.primary_name() == "Alex"


async def test_a_refused_alias_still_reaches_the_id_fallback(
    hass, patched, client, ws_manager
):
    """Both credential paths refused at once, which is the real case.

    A household that typed an email alias very often did so because the
    vendor name was already a credential and the entity was unreadable.
    Refusing both has to land on the id-derived fallback rather than on
    nothing.

    The profile carries the address the entry was set up with, and it has
    to. `profile_carries_address` refuses a profile that names a different
    address, so a profile invented for this test fails setup on identity
    grounds and never reaches the naming code at all. That makes the
    vendor label and the alias the same string here, which is exactly the
    shape a household in this situation is looking at.
    """
    client.user = {"id": ACCOUNT, "email": EMAIL_ALIAS}
    entry = await loaded(
        hass, options={CONF_DISPLAY_ALIASES: {ACCOUNT: EMAIL_ALIAS}}
    )
    coordinator = entry.runtime_data

    name = coordinator.display_name_for_user(ACCOUNT)
    assert name == f"User {ACCOUNT[:8]}", name
    assert "@" not in name


async def test_a_safe_stored_alias_still_wins_over_the_vendor_name(
    hass, patched, client, ws_manager
):
    """Positive control for the read side.

    `const.py` documents the alias as the way to override the account
    name. The credential filter must not cost that, or the fix has traded
    a permanence bug for a broken feature.
    """
    entry = await loaded(hass, options={CONF_DISPLAY_ALIASES: {ACCOUNT: "Bob"}})

    assert entry.runtime_data.primary_name() == "Bob"
    assert "climate.sleepy_bob_climate" in entity_ids(hass)


# ---------------------------------------------------------------------
# Where the filter must NOT be.
# ---------------------------------------------------------------------


def test_clean_alias_map_still_does_not_delete_an_unsafe_alias():
    """The filter belongs at the write boundary, not on stored options.

    `helpers.clean_alias_map` is called by `coordinator.__init__` on
    options that are already saved. Filtering there would silently blank
    an alias a household already relies on, on the next reload, with
    nothing said and nothing to restore it from.

    Refusing to USE a value is recoverable: the name falls back, the
    household edits the field, and the stored string was never touched.
    Deleting it is not recoverable. That asymmetry is why this helper is
    deliberately left permissive, and why the entity_id defence lives in
    the two places that read and write rather than in the one that
    normalises.
    """
    stored = helpers.clean_alias_map({ACCOUNT: EMAIL_ALIAS}, {ACCOUNT})

    assert stored == {ACCOUNT: EMAIL_ALIAS}, (
        "the credential filter was pushed down into clean_alias_map, which "
        "silently destroys a stored alias on the next reload instead of "
        "declining to use it"
    )
