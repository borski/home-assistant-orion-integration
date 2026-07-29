"""A repair issue must not carry the login credential the entry is named after.

`config_flow.py` builds every entry title as
`f"Orion Sleep ({self._auth_value})"`, where `_auth_value` is the literal
email address or phone number typed at setup. The rest of this
integration treats that string as a credential. `diagnostics.py` redacts
it three separate ways, under `auth_value`, `email` and `phone`, and
`helpers.short_id` exists for no other reason than keeping it out of log
lines.

The repair issue raised for a declined rename put it back, as
`translation_placeholders["entry_title"]`.

Why that is worse than the title merely existing. `repairs/list_issues`
in `homeassistant/components/repairs/websocket_api.py` has no
`require_admin` decorator, unlike `RepairsFlowIndexView.post` a few lines
below it in the same file, and it returns `translation_placeholders`
verbatim to any authenticated user. Every non-admin household member
sees the repairs dashboard. It is also the single most screenshotted page
in Home Assistant, because it is where people go when something is wrong
and it is what they paste into a forum thread asking for help.

These tests assert on the placeholder DICT rather than on rendered text.
The rendered string is produced from the frontend translation catalogue,
so a test that asserted on it would keep passing if the placeholder came
back and the description stopped consuming it. What travels over the
websocket is the dict.
"""

from __future__ import annotations

import json
from pathlib import Path

from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.orion_sleep import ISSUE_UNIQUE_ID_CONFLICT
from custom_components.orion_sleep.const import (
    CONF_ACCESS_TOKEN,
    CONF_ACCOUNT_ID,
    CONF_AUTH_METHOD,
    CONF_AUTH_VALUE,
    CONF_DEVICE_IDS,
    CONF_EXPIRES_AT,
    CONF_REFRESH_TOKEN,
    DOMAIN,
)
from tests_ha.conftest import ACCOUNT, BED_A

# The address a user types into the config flow. `conftest.make_entry`
# uses the same one, and `FakeClient` reports it as the account email.
EMAIL = "alice@example.com"
# A phone number account, for the other half of `CONF_AUTH_METHOD`. Same
# credential, different shape, and `diagnostics.py` redacts both.
PHONE = "+15551234567"
# Exactly what `config_flow.py` builds. Written out here rather than
# imported because the point of the test is that this string reaches the
# repair issue, so it has to be asserted against literally.
TITLE_TEMPLATE = "Orion Sleep ({0})"

LEGACY_ID = f"{BED_A}_sleep_score"
ACCOUNT_ID = f"{BED_A}_user_{ACCOUNT}_sleep_score"

COMPONENT = Path(__file__).resolve().parents[1] / "custom_components" / "orion_sleep"


def conflicted_entry(hass, *, auth_value: str, auth_method: str = "email"):
    """An entry titled the way the config flow titles it, carrying a conflict.

    Two things this fixture must do that `conftest.make_entry` does not.

    It sets `title` explicitly. `MockConfigEntry` defaults to "Mock
    Title", which contains no credential at all, so a test built on the
    shared helper would assert that an email is absent from a placeholder
    that never had a chance to contain one. It would pass against the
    unfixed code and prove nothing.

    It seeds both generations of one registry row, which is the shape
    that makes the migration decline a rename and therefore the only
    shape that raises this issue. The 3.x row survived an upgrade and is
    what blocks. The 2.x row was minted by a downgrade that could not
    find the 3.x id.
    """
    entry = MockConfigEntry(
        domain=DOMAIN,
        entry_id="entry-titled",
        unique_id=ACCOUNT,
        title=TITLE_TEMPLATE.format(auth_value),
        data={
            CONF_AUTH_METHOD: auth_method,
            CONF_AUTH_VALUE: auth_value,
            CONF_ACCESS_TOKEN: "at",
            CONF_REFRESH_TOKEN: "rt",
            CONF_EXPIRES_AT: 9e12,
            CONF_DEVICE_IDS: [BED_A],
            CONF_ACCOUNT_ID: ACCOUNT,
        },
    )
    entry.add_to_hass(hass)
    registry = er.async_get(hass)
    registry.async_get_or_create("sensor", DOMAIN, ACCOUNT_ID, config_entry=entry)
    registry.async_get_or_create("sensor", DOMAIN, LEGACY_ID, config_entry=entry)
    return entry


async def raise_the_issue(hass, entry) -> ir.IssueEntry:
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    issue = ir.async_get(hass).async_get_issue(
        DOMAIN, f"{ISSUE_UNIQUE_ID_CONFLICT}_{entry.entry_id}"
    )
    assert issue is not None, (
        "the fixture never produced a declined rename, so this test would "
        "pass vacuously"
    )
    return issue


async def test_the_conflict_issue_does_not_carry_the_account_email(hass, patched):
    """The finding, stated as narrowly as it can be stated.

    Breaks if `entry_title`, or anything else derived from `entry.title`
    or `CONF_AUTH_VALUE`, is added back to `translation_placeholders` in
    `_async_report_unique_id_conflicts`.
    """
    entry = conflicted_entry(hass, auth_value=EMAIL)
    issue = await raise_the_issue(hass, entry)

    placeholders = issue.translation_placeholders or {}
    # Serialised, because the leak does not have to arrive under the key
    # it arrived under last time. Asserting only on `entry_title` would
    # miss a rename to `title` or `entry_name` carrying the same string.
    payload = json.dumps(placeholders)

    assert EMAIL not in payload, (
        "the repair issue carries the account email as a translation "
        "placeholder. `repairs/list_issues` has no require_admin and returns "
        f"placeholders verbatim to every household member: {placeholders}"
    )
    assert entry.title not in payload, (
        "the entry title reached the repair issue, and the config flow "
        f"builds that title out of the typed login address: {placeholders}"
    )


async def test_the_conflict_issue_does_not_carry_the_account_phone(hass, patched):
    """The same leak for the other `CONF_AUTH_METHOD`.

    Not redundant with the email case. `diagnostics.py` redacts `email`
    and `phone` under separate keys precisely because they arrive by
    separate paths, and a fix that filtered on an "@" would pass the
    email test and leak every phone-number account.

    Breaks under exactly the same change as the test above.
    """
    entry = conflicted_entry(hass, auth_value=PHONE, auth_method="phone")
    issue = await raise_the_issue(hass, entry)

    payload = json.dumps(issue.translation_placeholders or {})
    assert PHONE not in payload, (
        "the repair issue carries the account phone number as a translation "
        f"placeholder: {issue.translation_placeholders}"
    )


async def test_the_conflict_issue_still_names_the_conflicting_entities(hass, patched):
    """Positive control, so the fix cannot be "send no placeholders".

    Dropping the whole dict would pass both tests above and leave the
    user with a repair that says something is wrong and nothing about
    what. `conflicts` names the blocked row AND the blocker, and naming
    only the blocked one is what previously sent users to delete the
    entity that still worked.

    Breaks if `conflicts` is dropped, renamed, or reduced to one side of
    the conflict.
    """
    entry = conflicted_entry(hass, auth_value=EMAIL)
    issue = await raise_the_issue(hass, entry)

    conflicts = (issue.translation_placeholders or {}).get("conflicts", "")
    rows = {
        row.unique_id: row.entity_id
        for row in er.async_entries_for_config_entry(
            er.async_get(hass), entry.entry_id
        )
    }
    assert rows[LEGACY_ID] in conflicts, "the blocked entity is no longer named"
    assert rows[ACCOUNT_ID] in conflicts, "the blocking entity is no longer named"


def test_the_issue_text_asks_for_no_placeholder_the_code_stopped_sending():
    """A description referencing a placeholder that is gone renders raw.

    Home Assistant does not substitute a missing key. It leaves the
    literal `{entry_title}` in the dialog, so the user reads a brace
    expression where an instruction should be. That is the failure mode
    of fixing the code and forgetting the strings, and it is silent in
    every other test in this suite because none of them render.

    Checks both catalogues. `strings.json` is what the HA translation
    pipeline ingests and `translations/en.json` is what actually renders,
    and this project keeps them byte-identical.

    Breaks if `{entry_title}` is reintroduced into either file without
    the placeholder being restored in `__init__.py`.
    """
    for name in ("strings.json", "translations/en.json"):
        catalogue = json.loads((COMPONENT / name).read_text(encoding="utf-8"))
        issue = catalogue["issues"][ISSUE_UNIQUE_ID_CONFLICT]
        text = f"{issue['title']}\n{issue['description']}"
        assert "{entry_title}" not in text, (
            f"{name} still interpolates the entry title, which the code no "
            "longer sends. The repairs dialog will render the literal braces"
        )
        assert "{conflicts}" in text, (
            f"{name} stopped interpolating the entity list, so the repair no "
            "longer tells the user which entities are affected"
        )
