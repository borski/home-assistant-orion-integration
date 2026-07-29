"""What may and may not end up in an entity_id.

Home Assistant slugifies an entity's name into its entity_id at FIRST
registration and never revisits it on rename. `orion_user_label` in the
API library falls back through first_name, firstName, name, email, and
finally phone, so an account with no display name set puts a login
credential into that permanent identifier.

Not an edge case. Orion stores whatever was supplied at signup, and an
account created by email or SMS verification alone never gets a name. It
was the default path in this very suite: the fixture profile carried only
an email and two availability tests asserted on
`climate.sleepy_alice_example_com_climate`, which encoded the leak as the
expected result.

An entity_id is worse than a recorded attribute for this, and the
codebase already refuses `orion_user_label` for the attribute case in
`OrionAccessSensor._people`. An attribute can be purged. An entity_id
lands in every dashboard, automation, recorder row, long term statistics
row, backup, and screenshot pasted into a bug report, and it stays there.
"""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntryState

from custom_components.orion_sleep.const import (
    CONF_AUTH_METHOD,
    CONF_AUTH_VALUE,
    CONF_DISPLAY_ALIASES,
)
from tests_ha.conftest import ACCOUNT, make_entry

EMAIL = "alice@example.com"
PHONE = "+1 (555) 123-4567"


async def _loaded(hass, patched, *, options=None, data=None):
    entry = make_entry(hass, data=data)
    if options:
        hass.config_entries.async_update_entry(entry, options=options)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    return entry


def _all_entity_ids(hass) -> list[str]:
    return [state.entity_id for state in hass.states.async_all()]


async def test_an_email_only_profile_never_reaches_an_entity_id(
    hass, patched, client, ws_manager
):
    """The regression guard. This is the whole point of the fix.

    Asserts against every entity the integration registers rather than a
    named few, because the leak was never specific to one platform. It
    came from `primary_name`, which feeds climate, sensor, binary_sensor,
    switch, number and time alike.
    """
    client.user = {"id": ACCOUNT, "email": EMAIL}

    await _loaded(hass, patched)
    entity_ids = _all_entity_ids(hass)
    assert entity_ids, "no entities registered, so this test proves nothing"

    slugified = EMAIL.replace("@", "_").replace(".", "_")
    offenders = [eid for eid in entity_ids if slugified in eid or "alice" in eid]
    assert not offenders, (
        "an account email was slugified into a permanent entity_id: "
        f"{offenders}. Home Assistant never rewrites an entity_id on "
        "rename, so this cannot be undone by fixing the name later."
    )


async def test_a_phone_only_profile_never_reaches_an_entity_id(
    hass, patched, client, ws_manager
):
    """The last link in the fallback chain.

    `orion_user_label` tries email before phone, so a profile with a
    phone and no email is the only way to reach the final fallback. This
    is the account shape of anyone who signed up by SMS.

    The entry has to be set up with the phone too. `profile_carries_address`
    refuses a profile that does not name the address the entry was created
    with, so an email entry paired with a phone-only profile fails setup
    on identity grounds and never reaches the naming code at all.
    """
    client.user = {"id": ACCOUNT, "phone": PHONE}

    await _loaded(
        hass,
        patched,
        data={CONF_AUTH_METHOD: "phone", CONF_AUTH_VALUE: PHONE},
    )
    entity_ids = _all_entity_ids(hass)
    assert entity_ids, "no entities registered, so this test proves nothing"

    offenders = [eid for eid in entity_ids if "555" in eid or "123" in eid]
    assert not offenders, (
        f"a phone number was slugified into a permanent entity_id: {offenders}"
    )


async def test_an_email_only_profile_still_gets_a_usable_name(
    hass, patched, client, ws_manager
):
    """Safe must not mean blank.

    An entity with an empty name is worse than one with an ugly name, and
    the id-derived fallback is what makes refusing the vendor's label
    affordable. It is also recoverable, which the alias test below shows.
    """
    client.user = {"id": ACCOUNT, "email": EMAIL}

    entry = await _loaded(hass, patched)
    coordinator = entry.runtime_data

    assert coordinator.primary_name() == f"User {ACCOUNT[:8]}"
    assert coordinator.display_name_for_user(ACCOUNT) == f"User {ACCOUNT[:8]}"


async def test_a_real_vendor_name_is_still_used(hass, patched, client, ws_manager):
    """The filter rejects credentials, not names.

    A predicate aggressive enough to drop "Alex" would make every
    household's entities unreadable to buy nothing, so this is the other
    half of the contract.
    """
    entry = await _loaded(hass, patched)
    coordinator = entry.runtime_data

    # The default fixture profile carries both a name and an email.
    assert coordinator.primary_name() == "Alex"
    assert "climate.sleepy_alex_climate" in _all_entity_ids(hass)


async def test_an_alias_still_wins_over_everything(hass, patched, client, ws_manager):
    """The documented escape hatch, unbroken.

    `const.py` states that aliases affect friendly names only and that a
    person's name never reaches a unique id. That design is correct and
    the credential filter must not interfere with it. The alias is the
    household's own choice, so it is taken as given.
    """
    client.user = {"id": ACCOUNT, "email": EMAIL}

    entry = await _loaded(
        hass, patched, options={CONF_DISPLAY_ALIASES: {ACCOUNT: "Bob"}}
    )
    coordinator = entry.runtime_data

    assert coordinator.primary_name() == "Bob"


async def test_the_access_roster_matches_the_entity_names(
    hass, patched, client, ws_manager
):
    """`OrionAccessSensor._people` and entity names agree on a person.

    That sensor already refused `orion_user_label` and routed through
    `display_name_for_user` instead, but `display_name_for_user` was
    itself label-derived, so the refusal did nothing. With the filter in
    the shared method both call sites get the same answer, which is what
    the sensor's comment always claimed.
    """
    client.user = {"id": ACCOUNT, "email": EMAIL}

    entry = await _loaded(hass, patched)
    coordinator = entry.runtime_data

    name = coordinator.display_name_for_user(ACCOUNT)
    assert EMAIL not in name
    assert name == f"User {ACCOUNT[:8]}"
