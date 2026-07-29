"""Partner rename records must not survive a partner change.

The failure this guards is the quietest one in the repo. The downgrade
journal maps 3.x unique_ids back to the ids 2.x asks for. 2.x has exactly
one role-keyed `{device}_partner_{key}` row per partner key, and it feeds
that row from whichever partner account is linked at the time. If a
journal record naming the PREVIOUS partner survives a partner removal or
replacement, a later revert renames that person's registry rows onto the
id 2.x then writes the CURRENT partner's heart rate, HRV and apnea into.
Two people's health history merges under one identity. Every rename
reports success, every log line is clean, and nothing surfaces it.

`migrations.evict_partner_journal` is the fix. It has to recognise a
partner record in three shapes, because the journal format changed twice
and both old shapes are still on disk in the field:

1. A structured record in `entry.data` carrying `role: "partner"`.
2. A structured record in `entry.data` with no usable `role` at all,
   written before that key existed. `_read_journal` labels those with
   `_role_for`, so an eviction testing `role == "partner"` skips them
   and the reader that performs the revert then calls them partner
   records anyway and renames them.
3. The original `[old, new]` pair journal in `entry.options`, which
   carries no role by construction. It is the one format this project
   shipped while partner renames were still being planned, so it is the
   reader most likely to be holding partner data.

Both misses survived a partner change once already. The tests below pin
each shape, and `test_no_surviving_record_labels_as_partner` pins the
invariant underneath all three so a fourth format cannot reopen the hole
quietly.

Levels are called out per test. Where the options flow genuinely
exercises the eviction the test drives the real flow, because the
original defect was eviction and labelling disagreeing across layers.
Where the flow would pass for the wrong reason the test calls
`evict_partner_journal` directly and says so.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

from homeassistant.config_entries import ConfigEntryState
from homeassistant.helpers import entity_registry as er

from custom_components.orion_sleep import migrations
from custom_components.orion_sleep.const import (
    CONF_ACCOUNT_ID,
    CONF_INSIGHTS_DAYS,
    CONF_PARTNER_ACCESS_TOKEN,
    CONF_PARTNER_ACCOUNT_ID,
    CONF_PARTNER_AUTH_VALUE,
    CONF_PARTNER_DEVICE_SERIAL,
    CONF_PARTNER_EXPIRES_AT,
    CONF_PARTNER_REFRESH_TOKEN,
    CONF_SCAN_INTERVAL,
    CONF_UID_MIGRATION,
    DOMAIN,
)
from custom_components.orion_sleep.migrations import evict_partner_journal
from tests_ha.conftest import (
    ACCOUNT,
    BED_A,
    PARTNER,
    SERIAL_A,
    FakeClient,
    device,
    make_entry,
)

# The partner who replaces PARTNER in the replacement test. Deliberately
# neither ACCOUNT nor PARTNER, so no assertion can pass by matching the
# wrong side of the swap.
PARTNER_B = "44444444-4444-4444-8444-444444444444"
PARTNER_A_EMAIL = "bob@example.com"
PARTNER_B_EMAIL = "carol@example.com"


def bed_shared_with(user_id: str) -> dict[str, Any]:
    """The one bed, with its second zone owned by `user_id`.

    `conftest.device` hardcodes PARTNER on zone_b, which is the account
    being replaced in the test below. Naming the zone explicitly keeps
    the primary's and each partner's view of the same physical bed
    consistent with whoever is actually linked.
    """
    bed = device(BED_A, SERIAL_A)
    bed["zones"] = [
        {"id": "zone_a", "user": {"id": ACCOUNT}},
        {"id": "zone_b", "user": {"id": user_id}},
    ]
    return bed


# ── Journal fixtures ──────────────────────────────────────────────────
#
# Written as literals rather than built by the production helpers on
# purpose. These stand in for records already on disk, and half of them
# are shapes the current code no longer writes, so deriving them from the
# current writer would only ever produce the shape that already works.


def partner_record(user_id: str = PARTNER, key: str = "sleep_score") -> dict[str, str]:
    """A structured partner record carrying an explicit role."""
    return {
        "domain": "sensor",
        "platform": DOMAIN,
        "old": f"{BED_A}_partner_{key}",
        "new": f"{BED_A}_user_{user_id}_{key}",
        "role": "partner",
    }


def role_less_partner_record(key: str = "sleep_latency") -> dict[str, str]:
    """A partner record from a build that predates the `role` key."""
    return {
        "domain": "sensor",
        "platform": DOMAIN,
        "old": f"{BED_A}_partner_{key}",
        "new": f"{BED_A}_user_{PARTNER}_{key}",
    }


def empty_role_partner_record(key: str = "breath_rate") -> dict[str, str]:
    """A partner record whose `role` is present but blank.

    `_read_journal` coerces with `str(value.get("role") or "")`, so a
    blank string is exactly as unset as a missing key. An eviction that
    only tested for a missing key would still let this one past.
    """
    return {
        "domain": "sensor",
        "platform": DOMAIN,
        "old": f"{BED_A}_partner_{key}",
        "new": f"{BED_A}_user_{PARTNER}_{key}",
        "role": "",
    }


def primary_record(key: str = "sleep_score") -> dict[str, str]:
    """A structured record for the account that did not change."""
    return {
        "domain": "sensor",
        "platform": DOMAIN,
        "old": f"{BED_A}_{key}",
        "new": f"{BED_A}_user_{ACCOUNT}_{key}",
        "role": "primary",
    }


def partner_pair(user_id: str = PARTNER, key: str = "efficiency") -> list[str]:
    """A partner rename in the original `[old, new]` options format."""
    return [f"{BED_A}_partner_{key}", f"{BED_A}_user_{user_id}_{key}"]


def primary_pair(key: str = "efficiency") -> list[str]:
    """A primary rename in the original `[old, new]` options format."""
    return [f"{BED_A}_{key}", f"{BED_A}_user_{ACCOUNT}_{key}"]


def naive_explicit_role(record: dict[str, Any]) -> bool:
    """The eviction rule that shipped before role-less records existed.

    Kept here so each test can assert its own fixture against it. If this
    returns False for a record the test then demands be evicted, the test
    is proven to exercise a miss rather than the path that already
    worked. That is how these fixtures are verified without touching
    source that a parallel agent is editing.
    """
    return record.get("role") == "partner"


# ── Driving the real options flow ─────────────────────────────────────


def partner_entry(hass, *, data=None, options=None):
    """An entry with a linked partner, so the removal action is offered.

    `async_step_init` only shows "Remove partner account" when
    `CONF_PARTNER_ACCESS_TOKEN` is in `entry.data`, and
    `_async_finish_init` only takes the removal branch for the same
    reason. Without these keys every test here would drive a flow that
    silently did nothing and still passed.
    """
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


async def remove_the_partner(hass, entry) -> None:
    """Drive the real options flow down its partner-removed path."""
    flow = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        flow["flow_id"],
        {
            CONF_SCAN_INTERVAL: 300,
            CONF_INSIGHTS_DAYS: 7,
            "partner_action": "remove",
            "edit_aliases": False,
        },
    )
    assert result["type"] == "create_entry", result
    await hass.async_block_till_done()
    # The removal has to have actually happened, or an eviction assertion
    # below would be measuring a flow that took the "keep" branch.
    assert CONF_PARTNER_ACCESS_TOKEN not in entry.data


def data_journal(entry) -> list[Any]:
    return list(entry.data.get(CONF_UID_MIGRATION) or [])


def options_journal(entry) -> list[Any]:
    return list(entry.options.get(CONF_UID_MIGRATION) or [])


def options_journal_of(options: dict[str, Any]) -> list[Any]:
    """The same view, for an options dict the flow has not written yet."""
    return list(options.get(CONF_UID_MIGRATION) or [])


# ── 1. The shape that always worked ───────────────────────────────────


async def test_an_explicit_partner_record_is_evicted_on_partner_removal(hass):
    """Integration level. The baseline, and it has to come first.

    Every other test here asserts that a harder shape is also caught.
    Without this one they could all pass against an eviction that had
    stopped running at all.
    """
    record = partner_record()
    entry = partner_entry(hass, data={CONF_UID_MIGRATION: [record, primary_record()]})

    await remove_the_partner(hass, entry)

    assert record not in data_journal(entry), (
        "the previous partner's rename survived their removal, so a later "
        "revert renames their rows onto the id 2.x feeds from the next "
        "partner's account"
    )


# ── 2. The record format that predates `role` ─────────────────────────


async def test_a_role_less_partner_record_is_evicted(hass):
    """Integration level. The first miss.

    The journal shipped before the `role` key did. A record written by an
    earlier 3.0.x build carries no `role` at all, and `_read_journal`
    fills one in with `_role_for`, which calls anything whose old id
    contains `_partner_` a partner record. An eviction that trusted an
    explicit `role` therefore skipped exactly the records the reader
    would go on to rename.
    """
    record = role_less_partner_record()
    assert "role" not in record
    # Proves the fixture exercises the miss rather than the happy path.
    # An eviction testing `role == "partner"` keeps this record.
    assert naive_explicit_role(record) is False

    entry = partner_entry(hass, data={CONF_UID_MIGRATION: [record]})

    await remove_the_partner(hass, entry)

    assert record not in data_journal(entry), (
        "a partner record with no `role` key survived, and `_read_journal` "
        "will label it partner via `_role_for` and revert it anyway"
    )


# ── 3. The same miss, one character wider ─────────────────────────────


async def test_an_empty_role_partner_record_is_evicted(hass):
    """Integration level. The miss again, in the shape a narrow fix leaves.

    Separate from the test above because the obvious repair for that one
    is `"role" not in record`, which passes there and fails here.
    `_read_journal` treats a blank `role` as unset, so the eviction has
    to as well.
    """
    record = empty_role_partner_record()
    assert record["role"] == ""
    assert naive_explicit_role(record) is False
    # And the reader really does call this a partner record, which is what
    # makes leaving it behind dangerous rather than untidy.
    assert migrations._is_partner_record(record) is True

    entry = partner_entry(hass, data={CONF_UID_MIGRATION: [record]})

    await remove_the_partner(hass, entry)

    assert record not in data_journal(entry), (
        "a partner record with a blank `role` survived, and the reader "
        "treats blank exactly like absent"
    )


# ── 4. The other journal entirely ─────────────────────────────────────


async def test_the_options_pair_journal_is_evicted_too(hass):
    """Unit level, deliberately, and this is the one place it matters.

    `evict_partner_journal` is called directly rather than through the
    options flow, and the reason is narrower than it used to be.

    `async_create_entry` still REPLACES `entry.options` wholesale with
    the values the form collected, but every options save in this flow
    now goes through `config_flow._async_save_options`, which reads the
    pair journal off the entry and carries it forward. So a flow-driven
    entry KEEPS the journal, and an integration-level assertion here
    would be measuring that carry-forward rather than the eviction.

    Driving the contract directly is still the only way to see the
    eviction on its own. It is also the only level at which the ordering
    between the two is visible: `_write_partner_change` evicts and writes
    the pruned options back BEFORE `_async_save_options` reads them, and
    reversing that would restore the pairs this test proves were removed.
    """
    pair = partner_pair()
    data = {CONF_ACCOUNT_ID: ACCOUNT}
    options = {CONF_SCAN_INTERVAL: 300, CONF_UID_MIGRATION: [pair]}
    # The miss this pins: nothing in `data` names this rename, so an
    # eviction that walked only the structured journal has nothing to do
    # and reports success.
    assert CONF_UID_MIGRATION not in data
    assert "_partner_" in pair[0]

    new_data, new_options = evict_partner_journal(data, options)

    assert pair not in options_journal_of(new_options), (
        "the original `[old, new]` pair journal still names the previous "
        "partner, and `_read_journal` expands those pairs and labels them "
        "with the same `_role_for` rule the structured records use"
    )
    # The last pair went, so the key goes with it rather than lingering as
    # an empty list that later reads as "a journal exists here".
    assert CONF_UID_MIGRATION not in new_options
    # The caller writes both halves in one `async_update_entry`, so the
    # untouched half has to come back intact rather than empty.
    assert new_data == data


# ── 5. The complement, and the reason this is not "drop everything" ───


async def test_primary_records_survive_in_both_journals(hass):
    """Unit level, because it is a claim about both journals at once.

    The cheap fix for everything above is to empty the journal. That
    trades a silent failure for a different silent failure. The primary
    account did not change, its records are the only thing standing
    between a downgrade and a stranded history, and with an empty journal
    `async_revert_unique_ids` reports "no recorded Orion renames to undo"
    while every primary entity sits on a 3.x id 2.x never asks for.

    Asserted through the direct call for the same reason as the test
    above: to measure the eviction rather than the save path around it.
    `config_flow._async_save_options` now carries an unmigrated pair
    journal forward across an options save, so a flow-driven check of the
    options half would pass on that carry-forward alone and would say
    nothing about whether the eviction kept the primary's pair.
    """
    kept_record = primary_record()
    kept_pair = primary_pair()
    data = {CONF_UID_MIGRATION: [partner_record(), kept_record]}
    options = {CONF_UID_MIGRATION: [partner_pair(), kept_pair]}

    new_data, new_options = evict_partner_journal(data, options)

    assert kept_record in (new_data.get(CONF_UID_MIGRATION) or []), (
        "the primary account's rename was dropped, so a downgrade now "
        "reports nothing to undo while every primary entity sits on an id "
        "2.x never asks for"
    )
    assert kept_pair in options_journal_of(new_options), (
        "the primary pair was dropped from the options journal, same "
        "stranded history, same clean success message"
    )
    # And the partner half really did go, so this is not passing because
    # the eviction stopped running.
    assert len(new_data[CONF_UID_MIGRATION]) == 1
    assert len(new_options[CONF_UID_MIGRATION]) == 1


# ── 6. Absent, not empty ──────────────────────────────────────────────


async def test_the_key_is_removed_rather_than_left_empty(hass):
    """Integration level on `entry.data`.

    An empty list is not the same as no journal. `_write_journal` pops
    the key when there is nothing to record, and a lingering `[]` reads
    downstream as "a journal exists here and it is complete", which is
    the most confident possible way to be wrong about whether a downgrade
    has anything to undo.
    """
    entry = partner_entry(
        hass,
        data={CONF_UID_MIGRATION: [partner_record(), role_less_partner_record()]},
    )

    await remove_the_partner(hass, entry)

    assert CONF_UID_MIGRATION not in entry.data, (
        f"the journal key survived as {entry.data.get(CONF_UID_MIGRATION)!r} "
        "after its last record was evicted"
    )


# ── 7. The scenario the whole fix exists for ──────────────────────────


class FakeAuthClient:
    """The client the options flow builds while linking a new partner.

    Separate from `conftest.FakeClient` because the config flow needs the
    two auth-code calls the coordinator never makes, and because this one
    has to answer as a DIFFERENT account than the one already linked.
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


async def test_a_partner_replacement_evicts_the_previous_partner(hass, ws_manager):
    """Integration level, end to end, and the closest thing here to the bug.

    Partner A is linked and journalled. The user replaces them with
    partner B through the real options flow. Nothing that survives may
    still name A, because 2.x has one role-keyed row per partner key and
    it is now fed from B's account. A surviving record naming A renames
    A's rows onto that id, and B's readings land on top of A's history.

    The entry is set up for real because the replacement path reads
    `runtime_data` for the primary's devices before it will accept a
    partner, and a flow that aborts before `_write_partner_change` would
    make this test pass without ever running the eviction.

    This deliberately asserts only that A is gone, not that B took A's
    place, and that is now a scoping choice rather than a workaround.

    It used to be a workaround. The replacement flow wrote new tokens
    without updating `CONF_PARTNER_ACCOUNT_ID`, so the reload compared
    B's profile against A's recorded id, refused B, and wrote no partner
    records for anybody. That is fixed. `config_flow.py` writes
    `CONF_PARTNER_ACCOUNT_ID: partner_id` in the same
    `_write_partner_change` call as the tokens, deliberately in one write
    so no reload can ever observe new tokens beside the previous
    partner's id.

    The scoping stands anyway. This test is about eviction, and B's
    arrival is `test_partner_relink_real.py`. Asserting both here would
    mean a regression in either one fails the same test and the message
    stops naming which.
    """
    # Keyed on the access token, not on call order. An alternating fake
    # keeps answering as partner A after the swap, so the reload
    # re-journals A from a coordinator that never noticed the
    # replacement, and the test then fails on records the eviction was
    # never given the chance to remove. Answering per token is also what
    # a real server does.
    accounts = {
        "at": (ACCOUNT, "Alex", "alice@example.com", ACCOUNT),
        "partner-at": (PARTNER, "Bo", PARTNER_A_EMAIL, PARTNER),
        "partner-b-at": (PARTNER_B, "Carol", PARTNER_B_EMAIL, PARTNER_B),
    }

    def _client(*_args: Any, **kwargs: Any) -> FakeClient:
        user_id, name, email, sharer = accounts[kwargs["access_token"]]
        built = FakeClient()
        built.user = {"id": user_id, "name": name, "email": email}
        built.devices = [bed_shared_with(sharer)]
        return built

    entry = partner_entry(
        hass,
        data={CONF_UID_MIGRATION: [partner_record(PARTNER), primary_record()]},
        options={CONF_UID_MIGRATION: [partner_pair(PARTNER), primary_pair()]},
    )

    with (
        patch("custom_components.orion_sleep.OrionApiClient", side_effect=_client),
        patch(
            "custom_components.orion_sleep.coordinator.OrionWebSocketManager",
            return_value=ws_manager,
        ),
    ):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        assert entry.state is ConfigEntryState.LOADED
        # The positive control, and without it this test could pass on an
        # entry that never journalled partner A at all. Setup verifies A
        # and records their full downgrade map, which is exactly the map
        # the replacement below has to destroy.
        assert PARTNER in repr(data_journal(entry))

        with patch(
            "custom_components.orion_sleep.config_flow.OrionApiClient",
            side_effect=FakeAuthClient,
        ):
            flow = await hass.config_entries.options.async_init(entry.entry_id)
            result = await hass.config_entries.options.async_configure(
                flow["flow_id"],
                {
                    CONF_SCAN_INTERVAL: 300,
                    CONF_INSIGHTS_DAYS: 7,
                    "partner_action": "add",
                    "edit_aliases": False,
                },
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

    # The replacement actually landed, so an eviction assertion below is
    # not measuring a flow that aborted somewhere harmless.
    assert entry.data[CONF_PARTNER_ACCESS_TOKEN] == "partner-b-at"

    # Asserted on the NEW ids, not the old ones. Every partner record old
    # id is `{device}_partner_{key}` whoever it belongs to, so the old id
    # cannot tell A from B. The new id carries the account.
    surviving = repr(data_journal(entry)) + repr(options_journal(entry))
    assert PARTNER not in surviving, (
        "a rename naming the previous partner survived their replacement. "
        "Reverting it hands their entities to the id 2.x now feeds from "
        f"the new partner's account: {surviving}"
    )


# ── 8. The invariant underneath all of the above ──────────────────────


async def test_no_surviving_record_labels_as_partner(hass):
    """Eviction called directly, then read back through the real reader.

    The strongest assertion available and the one that will not rot. Every
    test above names a record shape, and the record shape has already
    changed twice. This one makes no claim about shape at all. It evicts,
    puts the result back on the entry, and runs the survivors through
    `_read_journal`, which is the function that actually decides what a
    revert will rename. If nothing comes back labelled "partner", no
    revert can rename a partner row, whatever the journal format becomes.

    The registry rows are real and are what make this test bite. The pair
    journal reader only expands a pair when a registry row already holds
    its new id, so without those rows the options half of this assertion
    would be vacuous.
    """
    records = [
        partner_record(),
        role_less_partner_record(),
        empty_role_partner_record(),
        primary_record(),
    ]
    pairs = [partner_pair(), primary_pair()]
    entry = partner_entry(
        hass,
        data={CONF_UID_MIGRATION: records},
        options={CONF_UID_MIGRATION: pairs},
    )

    registry = er.async_get(hass)
    for new_id in [r["new"] for r in records] + [p[1] for p in pairs]:
        registry.async_get_or_create("sensor", DOMAIN, new_id, config_entry=entry)
    rows = er.async_entries_for_config_entry(registry, entry.entry_id)

    # The reader sees every one of them before the eviction, so a clean
    # result afterwards is the eviction working rather than the reader
    # never having looked.
    before = migrations._read_journal(entry, rows)
    assert sum(1 for r in before if r["role"] == "partner") == 4

    new_data, new_options = evict_partner_journal(entry.data, entry.options)
    hass.config_entries.async_update_entry(entry, data=new_data, options=new_options)

    after = migrations._read_journal(entry, rows)
    assert [r for r in after if r["role"] == "partner"] == [], (
        "a surviving record still reads as a partner rename, so the "
        "revert path will rename a partner row onto the id 2.x feeds from "
        "the currently linked account"
    )
    # Not vacuous. The primary renames are still there to be undone.
    assert after, "the eviction emptied the journal instead of filtering it"
