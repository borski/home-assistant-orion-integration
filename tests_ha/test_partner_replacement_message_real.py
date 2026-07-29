"""The revert refuses correctly. It has to say the right thing about it.

A previous pass closed the cross-person merge itself. `CONF_PARTNER_REPLACED`
is written by `config_flow._write_partner_change`, and
`async_revert_unique_ids` no longer lets legacy `{device}_partner_{key}`
rows suppress `partner_unmapped` once a partner has been swapped. That
part works and `test_partner_legacy_rows_real.py` holds it.

What did not land was the wording. `RevertResult` carried a single
`partner_unmapped` boolean, so `__init__.py` raised one generic message
for two situations that need opposite instructions:

  * No legacy rows in the way. The old ids are free, the next setup
    journals the current partner, and "reload Orion and run this again"
    is a remedy that genuinely works.
  * Legacy rows in the way. The journalling loop records a pair only
    when the old id is FREE, and these rows are occupying exactly those
    ids, so every reload lands back in the same state. "Reload and run
    this again" is an instruction that can be followed forever without
    ever succeeding. And the rows are not empty. They hold the PREVIOUS
    partner's history, so a household that eventually stops believing
    the message and installs 2.x anyway gets the current partner's heart
    rate written into the previous partner's history, which is the exact
    merge the refusal exists to prevent.

The correct text existed, as a `_LOGGER.warning` in `migrations`, which
no household reads. The service raised the generic one. So the refusal
was right and the remedy it named was wrong, which is arguably worse than
a refusal with no explanation at all: it sends somebody in a loop and
tells them the loop is the fix.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import Context
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er

from custom_components.orion_sleep.const import (
    CONF_ACCOUNT_ID,
    CONF_DEVICE_IDS,
    CONF_PARTNER_ACCESS_TOKEN,
    CONF_PARTNER_ACCOUNT_ID,
    CONF_PARTNER_AUTH_VALUE,
    CONF_PARTNER_DEVICE_SERIAL,
    CONF_PARTNER_EXPIRES_AT,
    CONF_PARTNER_REFRESH_TOKEN,
    CONF_PARTNER_REPLACED,
    DOMAIN,
)
from custom_components.orion_sleep.migrations import RevertResult
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


class PartnerClient(FakeClient):
    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.user = {"id": PARTNER, "name": "Bo", "email": PARTNER_EMAIL}


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


def partner_entry(hass, *, replaced: bool):
    """A partnered entry whose partner has, or has not, been changed.

    `CONF_DEVICE_IDS` is pre-recorded so the first poll does not rewrite
    the bed list and reload the entry mid-test.
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
            **({CONF_PARTNER_REPLACED: True} if replaced else {}),
        },
    )


def seed_legacy_partner_rows(hass, entry) -> list[str]:
    """The rows 2.x built for the PREVIOUS partner, still in the registry.

    EVERY partner key, not a sample. The journalling loop skips a pair
    only when the old id is already occupied, so seeding two keys leaves
    the rest free to be journalled, a partner record then exists, and
    `partner_unmapped` is false for a reason that has nothing to do with
    what is under test.

    Derived from the same description list the migration plans from, so a
    new insight cannot quietly fall out of this fixture.
    """
    from custom_components.orion_sleep.descriptions import (
        INSIGHT_SENSOR_DESCRIPTIONS,
    )

    registry = er.async_get(hass)
    created = []
    keys = [(d.key, "sensor") for d in INSIGHT_SENSOR_DESCRIPTIONS]
    keys.append(("session_active", "binary_sensor"))
    for key, domain in keys:
        row = registry.async_get_or_create(
            domain,
            DOMAIN,
            f"{BED_A}_partner_{key}",
            config_entry=entry,
            suggested_object_id=f"partner_{key}",
        )
        created.append(row.entity_id)
    return created


async def admin_context(hass) -> Context:
    user = await hass.auth.async_create_user("Admin", group_ids=["system-admin"])
    return Context(user_id=user.id)


async def revert(hass, entry):
    return await hass.services.async_call(
        DOMAIN,
        "revert_unique_ids",
        {"config_entry_id": entry.entry_id, "confirm": True},
        blocking=True,
        context=await admin_context(hass),
    )


# ---------------------------------------------------------------------
# End to end. A genuinely replaced partner, through the real service.
# ---------------------------------------------------------------------


async def test_the_replacement_refusal_names_the_rows_not_a_reload(
    hass, ws_manager
):
    """The finding, asserted where the household actually reads it.

    `test_partner_legacy_rows_real.py` asserts this wording against the
    `migrations` log, and says in its own docstring that it does so only
    because `__init__.py` could not yet tell the two causes apart. This
    is that assertion moved to the exception, which is the text a person
    running the service in the UI is shown.
    """
    entry = partner_entry(hass, replaced=True)
    seeded = seed_legacy_partner_rows(hass, entry)

    api, ws = clients(FakeClient(), PartnerClient(), ws_manager)
    with api, ws:
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        assert entry.state is ConfigEntryState.LOADED

        with pytest.raises(HomeAssistantError) as err:
            await revert(hass, entry)

    message = str(err.value)
    lowered = message.lower()

    assert "previous partner" in lowered, (
        "the refusal does not say the legacy rows hold the PREVIOUS "
        f"partner's history, which is the whole finding: {message}"
    )
    assert "delete" in lowered, (
        "the refusal does not name the one remedy that can work. Every "
        f"reload produces this same state forever: {message}"
    )
    assert any(entity_id in message for entity_id in seeded), (
        "the refusal does not name a single entity to delete, so the "
        f"household cannot act on it: {message}"
    )
    assert "resume_unique_ids" not in message, (
        "the refusal still tells the household to reload. The journalling "
        "loop skips the current partner for as long as those rows occupy "
        f"the old ids, so that instruction can never succeed: {message}"
    )


async def test_the_replacement_refusal_says_2x_would_merge_the_history(
    hass, ws_manager
):
    """Deleting entities is a real cost and the message has to justify it.

    Told to delete entities with no reason given, a reasonable person
    declines and installs 2.x anyway, which is the outcome the refusal
    exists to prevent.
    """
    entry = partner_entry(hass, replaced=True)
    seed_legacy_partner_rows(hass, entry)

    api, ws = clients(FakeClient(), PartnerClient(), ws_manager)
    with api, ws:
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        with pytest.raises(HomeAssistantError) as err:
            await revert(hass, entry)

    lowered = str(err.value).lower()
    assert "merge" in lowered, (
        f"the refusal never says what installing 2.x would do: {err.value}"
    )
    assert "2.x" in lowered, (
        f"the refusal never names the version that would do it: {err.value}"
    )


async def test_running_it_twice_does_not_report_all_clear(hass, ws_manager, caplog):
    """The refusal has to survive the run after the one that raised.

    Found while writing the message test above, and it made the message
    fix half decorative on its own.

    `async_revert_unique_ids` rewrites the journal as `remaining + stale`
    on every pass, so the run that raises leaves it empty. On the next run
    `reverted`, `remaining` and `partner_stale` are all zero, which
    satisfied the "nothing recorded to undo" early return, and the early
    return sits ABOVE every refusal. So the second attempt announced
    "Nothing changed, and nothing is prepared for a downgrade" while the
    legacy rows were still in the registry holding the previous partner's
    history and 2.x was still going to write the current partner's
    readings into them.

    A household that runs an action twice before acting on it is not doing
    anything unusual. They would have been told, in order, that this is
    dangerous and then that there is nothing to worry about, with the
    reassurance last.

    This is the same failure the `partner_stale` clause in that condition
    already documents. That flag counts in neither `reverted` nor
    `remaining` either, which is exactly what let it slip past. The danger
    here does not live in the journal at all, so an empty journal says
    nothing about whether it is gone.
    """
    entry = partner_entry(hass, replaced=True)
    seed_legacy_partner_rows(hass, entry)

    api, ws = clients(FakeClient(), PartnerClient(), ws_manager)
    with api, ws:
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        with pytest.raises(HomeAssistantError):
            await revert(hass, entry)

        caplog.clear()
        with pytest.raises(HomeAssistantError) as err:
            await revert(hass, entry)

    assert "previous partner" in str(err.value).lower(), (
        "the second run stopped refusing while the legacy rows were still "
        f"in the registry: {err.value}"
    )
    assert "No recorded Orion renames to undo" not in caplog.text, (
        "the second run reported the entry clear. The rows are still there "
        "and 2.x will still merge two people's history"
    )


async def test_the_result_carries_the_cause_and_the_rows(hass, ws_manager):
    """The data the message is built from, asserted directly.

    A message assertion alone passes if `__init__.py` re-derives the
    legacy rows itself, which would put the registry lookup in two places
    and let them disagree. The cause and the row list belong on the
    result, because `migrations` is the layer that already knows both.
    """
    entry = partner_entry(hass, replaced=True)
    seeded = set(seed_legacy_partner_rows(hass, entry))

    api, ws = clients(FakeClient(), PartnerClient(), ws_manager)
    with api, ws:
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        from custom_components.orion_sleep.migrations import (
            async_revert_unique_ids,
        )

        result = async_revert_unique_ids(hass, entry)

    assert result.partner_unmapped, (
        "the revert is not refusing at all, so this test is not exercising "
        "the message path"
    )
    assert result.partner_rows_outrank_journal, (
        "the result cannot distinguish a replaced partner from an "
        "unjournalled one, so the caller has to guess and will raise the "
        "generic message again"
    )
    assert set(result.legacy_partner_entity_ids) == seeded, (
        "the rows the household is told to delete are not the rows that "
        f"are actually in the way: {result.legacy_partner_entity_ids}"
    )


async def test_an_untouched_partner_upgrade_carries_neither(hass, ws_manager):
    """The suppression this whole area exists to preserve.

    A household that upgraded from 2.x and never touched its partner is in
    no danger. 2.x finds the legacy rows where it left them, holding the
    same person's history. It must not be refused, and it must certainly
    not be handed a list of its own entities to delete.
    """
    entry = partner_entry(hass, replaced=False)
    seed_legacy_partner_rows(hass, entry)

    api, ws = clients(FakeClient(), PartnerClient(), ws_manager)
    with api, ws:
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        assert entry.state is ConfigEntryState.LOADED

        from custom_components.orion_sleep.migrations import (
            async_revert_unique_ids,
        )

        result = async_revert_unique_ids(hass, entry)

    assert not result.partner_rows_outrank_journal, (
        "an untouched partner upgrade was flagged as a replacement, which "
        "refuses the downgrade for a household in no danger"
    )
    assert result.legacy_partner_entity_ids == (), (
        "a household that is not being refused was still handed a list of "
        f"entities to delete: {result.legacy_partner_entity_ids}"
    )


# ---------------------------------------------------------------------
# The branch in `__init__.py`, both ways.
#
# The generic cause cannot be produced end to end in the same test as the
# replacement one. With no legacy rows in the registry the old ids are
# free, so setup journals the current partner and `partner_unmapped` is
# false by construction. Driving the branch from a built `RevertResult` is
# the honest way to prove the two causes produce different text, and it is
# what stops the fix from being "swap one message for the other".
# ---------------------------------------------------------------------


def revert_stub(result: RevertResult):
    return patch(
        "custom_components.orion_sleep.async_revert_unique_ids",
        return_value=result,
    )


async def test_the_generic_cause_still_gets_the_generic_message(
    hass, ws_manager
):
    """The other branch, unchanged.

    Here the old ids ARE free, so reloading genuinely does journal the
    current partner and the revert then has records to apply. Telling this
    household to delete entities would cost them the partner history the
    revert was about to preserve.
    """
    entry = partner_entry(hass, replaced=True)

    api, ws = clients(FakeClient(), PartnerClient(), ws_manager)
    with api, ws:
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        stub = RevertResult(
            reverted=3,
            remaining=0,
            identity_restored=True,
            partner_unmapped=True,
            partner_rows_outrank_journal=False,
        )
        with revert_stub(stub), pytest.raises(HomeAssistantError) as err:
            await revert(hass, entry)

    message = str(err.value)
    assert "resume_unique_ids" in message, (
        "the generic unmapped case lost its remedy. Reloading is exactly "
        f"what works when no legacy rows are occupying the old ids: {message}"
    )
    assert "delete" not in message.lower(), (
        "a household whose old ids are free was told to delete entities, "
        f"which discards history the revert could have preserved: {message}"
    )


async def test_the_two_causes_do_not_produce_the_same_message(hass, ws_manager):
    """The regression that the single boolean actually was.

    Both flags true is the replacement case, since
    `partner_rows_outrank_journal` is strictly narrower than
    `partner_unmapped`. If both inputs yield identical text then
    `RevertResult` gained a field and `__init__.py` ignored it, which is
    the exact state this item was opened to fix.
    """
    entry = partner_entry(hass, replaced=True)

    api, ws = clients(FakeClient(), PartnerClient(), ws_manager)
    with api, ws:
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        messages = []
        for outranks in (False, True):
            stub = RevertResult(
                reverted=3,
                remaining=0,
                identity_restored=True,
                partner_unmapped=True,
                partner_rows_outrank_journal=outranks,
                legacy_partner_entity_ids=(
                    ("sensor.partner_sleep_score",) if outranks else ()
                ),
            )
            with revert_stub(stub), pytest.raises(HomeAssistantError) as err:
                await revert(hass, entry)
            messages.append(str(err.value))

    generic, replacement = messages
    assert generic != replacement, (
        "a replaced partner and an unjournalled one are given the same "
        "instruction. One of the two is being told to do something that "
        f"cannot work: {generic}"
    )
    assert "sensor.partner_sleep_score" in replacement, (
        f"the replacement message does not name the row: {replacement}"
    )


async def test_a_long_row_list_is_capped_but_counted(hass, ws_manager):
    """A real 2.x install built one of these per insight sensor.

    Pasting two dozen entity_ids into a service error produces something
    nobody reads, and truncating without saying so produces a household
    that deletes six rows, reruns, and is refused again by the eighteen it
    was never told about.
    """
    entry = partner_entry(hass, replaced=True)
    rows = tuple(f"sensor.partner_metric_{index}" for index in range(20))

    api, ws = clients(FakeClient(), PartnerClient(), ws_manager)
    with api, ws:
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        stub = RevertResult(
            reverted=1,
            remaining=0,
            identity_restored=True,
            partner_unmapped=True,
            partner_rows_outrank_journal=True,
            legacy_partner_entity_ids=rows,
        )
        with revert_stub(stub), pytest.raises(HomeAssistantError) as err:
            await revert(hass, entry)

    message = str(err.value)
    assert rows[0] in message, message
    assert "14 more" in message, (
        "the row list was truncated without saying how many were left "
        f"out, so the household stops after the ones it was shown: {message}"
    )
