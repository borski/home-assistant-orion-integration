"""Executable registry tests for the v3 identity migration.

The migration module itself is now imported for real. What is faked is
narrower and lower down: the two Home Assistant registries it reads,
because a real `EntityRegistry` needs a real `hass` and that is 130
seconds of `tests_ha`, not 4 seconds of this suite.

WHAT STOPPED BEING FAKED, AND WHY IT MATTERED. This file used to build an
entire fake `homeassistant` package tree in `sys.modules`, plus a stub of
`custom_components.orion_sleep.descriptions`, and then load `migrations`
against them. Two things were wrong with that.

The stubbed `descriptions` module declared exactly one sensor key,
`sleep_score`. `_planned_renames` derives every unique_id rename from
`INSIGHT_SENSOR_DESCRIPTIONS`, so every rename assertion in this file was
really an assertion about a one-element list this file wrote itself. The
real module has 27 keys. A key that renames badly, collides with
`session_active`, or disappears was unobservable here.

The fake `ConfigEntryState` was a class of five string constants standing
in for a real enum of eight members. `_entries_still_starting` compares
against `NOT_LOADED` and `SETUP_IN_PROGRESS`. If Home Assistant renamed
either one, production would break and this file would keep passing,
because it was comparing its own strings to its own strings.

Both are gone. The real enum and the real description list are used, and
only `async_get` and `async_entries_for_config_entry` are swapped out.
"""

from __future__ import annotations

import types
from dataclasses import dataclass, replace

import _orion
import pytest
from homeassistant.config_entries import ConfigEntryState

DOMAIN = "orion_sleep"
ACCOUNT = "11111111-1111-4111-8111-111111111111"
PARTNER = "22222222-2222-4222-8222-222222222222"
DEVICE = "bed-1"
# The config entry now owns the identity of every account-scoped entity,
# so the expected ids below are keyed on it rather than on a bed.
ENTRY = "entry-1"
BED_A = "bed-aaa"
BED_B = "bed-bbb"


@dataclass(frozen=True)
class Row:
    """Immutable, because Home Assistant's registry rows are.

    `EntityRegistry._async_update_entity` does `attr.evolve(old, ...)` and
    stores the NEW object, so a caller holding the old one keeps a stale
    copy. An earlier version of this fake mutated in place, which made a
    test pass by reading a value real Home Assistant would never have
    given it.
    """

    entity_id: str
    domain: str
    unique_id: str
    config_entry_id: str = "entry-1"
    platform: str = DOMAIN

    @property
    def id(self):
        # Real registry rows have an opaque id distinct from entity_id.
        return f"reg::{self.entity_id}"


class EntityRegistry:
    def __init__(self, rows=()):
        self.entities = {row.entity_id: row for row in rows}

    def async_update_entity(self, entity_id, *, new_unique_id):
        row = self.entities[entity_id]
        collision = next(
            (
                other
                for other in self.entities.values()
                if other.id != row.id
                and other.domain == row.domain
                and other.platform == row.platform
                and other.unique_id == new_unique_id
            ),
            None,
        )
        if collision:
            raise ValueError("unique id occupied")
        updated = replace(row, unique_id=new_unique_id)
        self.entities[entity_id] = updated
        return updated


class DeviceRegistry:
    def __init__(self, entries=None):
        self.entries = entries or {}

    def async_get_device(self, *, identifiers):
        device_id = next(iter(identifiers))[1]
        owners = self.entries.get(device_id)
        if owners is None:
            return None
        return types.SimpleNamespace(config_entries=set(owners))


class Entry:
    def __init__(
        self,
        *,
        entry_id="entry-1",
        data=None,
        options=None,
        unique_id=ACCOUNT,
        state=ConfigEntryState.NOT_LOADED,
    ):
        self.entry_id = entry_id
        self.data = data or {"auth_value": "alice@example.com"}
        self.options = options or {}
        self.unique_id = unique_id
        self.domain = DOMAIN
        self.disabled_by = None
        self.state = state


class ConfigEntries:
    def __init__(self, entries):
        self._entries = entries

    def async_entries(self, _domain):
        return self._entries

    def async_get_entry(self, entry_id):
        return next((entry for entry in self._entries if entry.entry_id == entry_id), None)

    def async_update_entry(self, entry, **changes):
        for name, value in changes.items():
            setattr(entry, name, value)


class Hass:
    def __init__(self, entries, entity_registry, device_registry=None):
        self.config_entries = ConfigEntries(entries)
        self.entity_registry = entity_registry
        self.device_registry = device_registry or DeviceRegistry()


class Coordinator:
    def __init__(self):
        self.devices = [{"id": DEVICE, "serial_number": "SERIAL"}]
        self.user_id = ACCOUNT
        self.partner_user = {}
        # Mirrors the real coordinator's initial value, which is False.
        # This stub omitted the attribute entirely, so every test using it
        # exercised `_partner_recovery_renames` through a `getattr`
        # fallback rather than through the flag production code reads. The
        # fallback used to default to True, which meant the stub silently
        # asserted that an unverifiable partner may still claim ownership
        # of a unique_id. A test double that cannot answer a question the
        # production code asks is not a smaller coordinator, it is a
        # different one.
        self.partner_identity_confirmed = False

    def has_partner_for_device(self, _device_id):
        return False


@pytest.fixture
def migrations(monkeypatch):
    """The real migrations module, with only the two registries swapped.

    The swap replaces the `dr` and `er` names inside the migrations
    module rather than patching attributes on Home Assistant's own
    modules. Patching `homeassistant.helpers.entity_registry.async_get`
    in place would be visible to anything else that imported it, and this
    suite shares a process with every other file in `tests/`. Rebinding
    the module's own reference is scoped to exactly the code under test.

    `monkeypatch` undoes both on teardown, so an ordering change between
    test files cannot leave a fake registry installed for the next one.
    That is the specific failure the old module-scoped `sys.modules`
    surgery in this file could produce, and it produced it silently: a
    later test would get a fake `homeassistant` and fail in a way that
    read like a Home Assistant bug.
    """
    module = _orion.real("migrations")

    fake_dr = types.SimpleNamespace(async_get=lambda hass: hass.device_registry)
    fake_er = types.SimpleNamespace(
        async_get=lambda hass: hass.entity_registry,
        async_entries_for_config_entry=lambda registry, entry_id: [
            row
            for row in registry.entities.values()
            if row.config_entry_id == entry_id
        ],
    )
    monkeypatch.setattr(module, "dr", fake_dr)
    monkeypatch.setattr(module, "er", fake_er)
    return module


def record(domain, old, new, platform=DOMAIN, role="primary"):
    return {
        "domain": domain,
        "platform": platform,
        "old": old,
        "new": new,
        "role": role,
    }


def test_duplicate_bed_detection_does_not_need_other_runtime_data(migrations):
    first = Entry()
    second = Entry(entry_id="entry-2")
    hass = Hass(
        [first, second],
        EntityRegistry(),
        DeviceRegistry({DEVICE: {"entry-1", "entry-2"}}),
    )
    assert migrations.overlapping_entry_ids(hass, first.entry_id, {DEVICE}) == {
        "entry-2"
    }


def test_first_legacy_entry_waits_until_every_entry_has_captured_beds(migrations):
    first = Entry(data={"auth_value": "alice@example.com", "_device_ids_v3": [DEVICE]})
    second = Entry(entry_id="entry-2", data={"auth_value": "bob@example.com"})
    hass = Hass([first, second], EntityRegistry())
    assert migrations.unresolved_device_entries(hass, first.entry_id) == {"entry-2"}


def test_device_registry_owners_from_other_domains_are_ignored(migrations):
    first = Entry()
    other = Entry(entry_id="foreign")
    other.domain = "matter"
    hass = Hass(
        [first, other],
        EntityRegistry(),
        DeviceRegistry({DEVICE: {"entry-1", "foreign"}}),
    )
    assert migrations.overlapping_entry_ids(hass, first.entry_id, {DEVICE}) == set()


def test_fresh_v3_rows_are_journalled_without_waiting_for_a_restart(migrations):
    new = f"{ENTRY}_user_{ACCOUNT}_sleep_score"
    row = Row("sensor.sleep_score", "sensor", new)
    entry = Entry(data={"auth_value": "alice@example.com", "_device_ids_v3": [DEVICE]})
    hass = Hass([entry], EntityRegistry([row]))

    assert migrations.async_migrate_unique_ids(hass, entry, Coordinator()) == 0
    assert entry.data["_uid_migration_v3"] == [
        record("sensor", f"{DEVICE}_sleep_score", new)
    ]


def test_a_partner_record_is_distrusted_once_that_partner_is_unverifiable(migrations):
    """The reverse rename would hand 2.x the wrong person's entities.

    2.x has one role-keyed partner row and feeds it from whichever partner
    account is linked at the time. So a record naming a partner this pass
    cannot verify must not be REVERTED: doing so would put the previous
    partner's sleep, heart rate and apnea entities on the id that 2.x then
    writes the CURRENT partner's readings to.

    This used to be spelled as an unconditional delete, and that
    overshot. A single transient fetch failure reaches this code path too,
    and deleting on that destroyed the household's only rollback record
    over an 800ms network blip, with no self-healing. The record is now
    kept and marked stale instead, which withholds the revert without
    withholding the data. `async_revert_unique_ids` refuses on the flag.
    """
    old = f"{DEVICE}_sleep_score"
    new = f"{ENTRY}_user_{ACCOUNT}_sleep_score"
    partner_record = record(
        "sensor",
        f"{DEVICE}_partner_sleep_score",
        f"{ENTRY}_user_{PARTNER}_sleep_score",
        role="partner",
    )
    row = Row("sensor.sleep_score", "sensor", old)
    entry = Entry(
        data={
            "auth_value": "alice@example.com",
            "_device_ids_v3": [DEVICE],
            "_uid_migration_v3": [partner_record],
        }
    )
    hass = Hass([entry], EntityRegistry([row]))

    assert migrations.async_migrate_unique_ids(hass, entry, Coordinator()) == 1
    journal = entry.data["_uid_migration_v3"]
    assert record("sensor", old, new) in journal
    kept = [r for r in journal if r["role"] == "partner"]
    assert kept == [{**partner_record, "stale": True}], (
        "an unverifiable partner record was either deleted, which loses the "
        "rollback path over a transient failure, or left trusted, which lets "
        "a revert rename the wrong person's entities"
    )


def test_a_legacy_partner_record_without_a_role_is_still_recognised(migrations):
    """Journals written before `role` existed must not slip past the guard.

    Recognition is observable as the stale marking rather than as a
    delete. A record this reader failed to recognise would come back
    labelled "primary" and carrying no flag, so asserting the flag proves
    the same thing the old delete assertion did, and proves it about the
    exact record rather than about the journal being empty.
    """
    legacy = {
        "domain": "sensor",
        "platform": DOMAIN,
        "old": f"{DEVICE}_partner_sleep_score",
        "new": f"{ENTRY}_user_{PARTNER}_sleep_score",
    }
    entry = Entry(
        data={
            "auth_value": "alice@example.com",
            "_device_ids_v3": [DEVICE],
            "_uid_migration_v3": [legacy],
        }
    )
    hass = Hass([entry], EntityRegistry())

    migrations.async_migrate_unique_ids(hass, entry, Coordinator())
    assert entry.data["_uid_migration_v3"] == [
        {**legacy, "role": "partner", "stale": True}
    ]


def test_partial_revert_keeps_only_the_mapping_that_failed(migrations):
    new_one = f"{ENTRY}_user_{ACCOUNT}_sleep_score"
    new_two = f"{ENTRY}_user_{ACCOUNT}_session_active"
    old_one = f"{DEVICE}_sleep_score"
    old_two = f"{DEVICE}_session_active"
    records = [
        record("sensor", old_one, new_one),
        record("binary_sensor", old_two, new_two),
    ]
    entry = Entry(
        data={"auth_value": "alice@example.com", "_uid_migration_v3": records}
    )
    rows = [
        Row("sensor.score", "sensor", new_one),
        Row("binary_sensor.active", "binary_sensor", new_two),
        Row(
            "binary_sensor.blocker",
            "binary_sensor",
            old_two,
            config_entry_id="other",
        ),
    ]
    hass = Hass([entry], EntityRegistry(rows))

    result = migrations.async_revert_unique_ids(hass, entry)
    assert result.reverted == 1
    assert result.remaining == 1
    assert entry.data["_uid_migration_v3"] == [records[1]]
    assert entry.data["_uid_recovery_active_v3"] is True


def test_complete_revert_restores_the_2x_config_entry_identity(migrations):
    old = f"{DEVICE}_sleep_score"
    new = f"{ENTRY}_user_{ACCOUNT}_sleep_score"
    entry = Entry(
        data={
            "auth_value": "Alice@Example.com",
            "_uid_migration_v3": [record("sensor", old, new)],
        },
        unique_id=ACCOUNT,
    )
    row = Row("sensor.score", "sensor", new)
    hass = Hass([entry], EntityRegistry([row]))

    result = migrations.async_revert_unique_ids(hass, entry)
    assert result.complete
    assert entry.unique_id == "alice@example.com"
    assert "_uid_migration_v3" not in entry.data
    assert hass.entity_registry.entities["sensor.score"].unique_id == old


def test_deleted_entity_does_not_block_downgrade_forever(migrations):
    old = f"{DEVICE}_sleep_score"
    new = f"{ENTRY}_user_{ACCOUNT}_sleep_score"
    entry = Entry(
        data={
            "auth_value": "alice@example.com",
            "_uid_migration_v3": [record("sensor", old, new)],
        },
        unique_id=ACCOUNT,
    )
    hass = Hass([entry], EntityRegistry())
    result = migrations.async_revert_unique_ids(hass, entry)
    assert result.complete
    assert "_uid_migration_v3" not in entry.data


def test_a_foreign_owner_of_a_legacy_id_does_not_block_recovery(migrations):
    """There is nothing to revert, so retaining the record helped nobody.

    Keeping it made `remaining` permanently non-zero, which made the
    recovery service raise on every call while the latch was already set.
    An unactionable, endless failure of the only rollback path. 2.x will
    not reclaim that id, which is worth a warning and is not something
    this entry can fix by trying again.
    """
    old = f"{DEVICE}_sleep_score"
    new_id = f"{ENTRY}_user_{ACCOUNT}_sleep_score"
    entry = Entry(
        data={
            "auth_value": "alice@example.com",
            "_uid_migration_v3": [record("sensor", old, new_id)],
        }
    )
    foreign = Row("sensor.foreign", "sensor", old, config_entry_id="entry-2")
    hass = Hass([entry, Entry(entry_id="entry-2")], EntityRegistry([foreign]))

    result = migrations.async_revert_unique_ids(hass, entry)
    assert result.remaining == 0
    assert result.complete


def test_account_identity_collision_is_fatal(migrations):
    entry = Entry(unique_id="alice@example.com")
    duplicate = Entry(entry_id="entry-2", unique_id=ACCOUNT)
    hass = Hass([entry, duplicate], EntityRegistry())
    with pytest.raises(Exception, match="already owns"):
        migrations.async_migrate_entry_identity(hass, entry, Coordinator())


def test_declined_entity_rename_aborts_before_platform_setup(migrations):
    old = f"{DEVICE}_sleep_score"
    new = f"{ENTRY}_user_{ACCOUNT}_sleep_score"
    entry = Entry(data={"auth_value": "alice@example.com", "_device_ids_v3": [DEVICE]})
    source = Row("sensor.source", "sensor", old)
    blocker = Row("sensor.blocker", "sensor", new, config_entry_id="other")
    hass = Hass([entry], EntityRegistry([source, blocker]))
    with pytest.raises(Exception, match="already holds the id they need") as err:
        migrations.async_migrate_unique_ids(hass, entry, Coordinator())
    # Names the blocker, not just the row being moved. Reporting only the
    # source sent users to delete the entity that still worked.
    assert "sensor.blocker" in str(err.value)


def test_ambiguous_legacy_partner_history_is_never_auto_assigned(migrations):
    old = f"{DEVICE}_partner_sleep_score"
    row = Row("sensor.partner_score", "sensor", old)
    entry = Entry(data={"auth_value": "alice@example.com", "_device_ids_v3": [DEVICE]})
    hass = Hass([entry], EntityRegistry([row]))
    coordinator = Coordinator()
    coordinator.partner_user = {"id": PARTNER}
    coordinator.has_partner_for_device = lambda _device_id: True

    assert migrations.async_migrate_unique_ids(hass, entry, coordinator) == 0
    assert hass.entity_registry.entities["sensor.partner_score"].unique_id == old


def test_fresh_v3_partner_row_is_still_recoverable_for_downgrade(migrations):
    old = f"{DEVICE}_partner_sleep_score"
    new = f"{ENTRY}_user_{PARTNER}_sleep_score"
    row = Row("sensor.partner_score", "sensor", new)
    entry = Entry(data={"auth_value": "alice@example.com", "_device_ids_v3": [DEVICE]})
    hass = Hass([entry], EntityRegistry([row]))
    coordinator = Coordinator()
    coordinator.partner_user = {"id": PARTNER}
    coordinator.has_partner_for_device = lambda _device_id: True
    # Stated, not inherited. This test's premise is a partner whose
    # identity this setup positively established, because that is the only
    # state in which a partner rename record may be written at all. A
    # partner nobody could verify must NOT be journalled, and that case is
    # covered separately in `tests_ha/test_partner_journal_split_real.py`.
    coordinator.partner_identity_confirmed = True

    migrations.async_migrate_unique_ids(hass, entry, coordinator)
    assert record("sensor", old, new, role="partner") in entry.data["_uid_migration_v3"]


def test_original_pair_journal_is_upgraded_out_of_options(migrations):
    old = f"{DEVICE}_sleep_score"
    new = f"{ENTRY}_user_{ACCOUNT}_sleep_score"
    entry = Entry(
        data={"auth_value": "alice@example.com"},
        options={"_uid_migration_v3": [[old, new]], "scan_interval": 600},
    )
    row = Row("sensor.score", "sensor", new)
    hass = Hass([entry], EntityRegistry([row]))

    migrations.async_migrate_unique_ids(hass, entry, Coordinator())
    assert entry.data["_uid_migration_v3"] == [record("sensor", old, new)]
    assert entry.options == {"scan_interval": 600}


# ── Two beds on one account ───────────────────────────────────────────
#
# There was no two-device fixture in this suite, and that absence is
# exactly why a permanent multi-bed lockout survived three review rounds.
# In 2.x the account-level temperature select was built inside the
# per-device loop, so a two-bed account has two registry rows for one
# value. Only one can move onto the account-keyed id.


def two_beds(*ids):
    return [{"id": i, "serial_number": f"SER-{i}"} for i in ids]


def scale_rows(*ids):
    return [Row(f"select.scale_{i}", "select", f"{i}_temperature_display_unit") for i in ids]


def test_a_two_bed_account_migrates_instead_of_locking_itself_out(migrations):
    entry = Entry(data={"auth_value": "a@b.c", "_device_ids_v3": [BED_A, BED_B]})
    hass = Hass([entry], EntityRegistry(scale_rows(BED_A, BED_B)))
    coordinator = Coordinator()
    coordinator.devices = two_beds(BED_A, BED_B)

    assert migrations.async_migrate_unique_ids(hass, entry, coordinator) == 1
    ids = {r.unique_id for r in hass.entity_registry.entities.values()}
    assert f"{entry.entry_id}_temperature_display_unit" in ids
    # The surplus copy of one account value is left alone, never declined.
    assert f"{BED_B}_temperature_display_unit" in ids


def test_a_reordered_device_list_does_not_brick_the_entry(migrations):
    """`dedupe_devices_by_id` preserves vendor response order.

    Keying the rename on that list's first element meant the vendor
    returning the beds the other way round planned a rename onto an id the
    surviving row already held, and a declined rename is fatal.
    """
    entry = Entry(data={"auth_value": "a@b.c", "_device_ids_v3": [BED_A, BED_B]})
    hass = Hass([entry], EntityRegistry(scale_rows(BED_A, BED_B)))
    coordinator = Coordinator()

    coordinator.devices = two_beds(BED_A, BED_B)
    migrations.async_migrate_unique_ids(hass, entry, coordinator)
    coordinator.devices = two_beds(BED_B, BED_A)
    assert migrations.async_migrate_unique_ids(hass, entry, coordinator) == 0


def test_selling_the_first_bed_does_not_brick_the_entry(migrations):
    """Removing a bed is a supported user action, not an error."""
    entry = Entry(data={"auth_value": "a@b.c", "_device_ids_v3": [BED_A, BED_B]})
    hass = Hass([entry], EntityRegistry(scale_rows(BED_A, BED_B)))
    coordinator = Coordinator()

    coordinator.devices = two_beds(BED_A, BED_B)
    migrations.async_migrate_unique_ids(hass, entry, coordinator)

    coordinator.devices = two_beds(BED_B)
    entry.data = {**entry.data, "_device_ids_v3": [BED_B]}
    assert migrations.async_migrate_unique_ids(hass, entry, coordinator) == 0


def test_a_legacy_options_partner_pair_is_still_recognised(migrations):
    """The pair format is the one this project shipped WITH partner renames.

    Labelling it "primary" let every legacy partner record past the guard,
    so a downgrade handed the previous partner's entities to 2.x, which
    then wrote the current partner's readings onto them.

    Asserted as the stale marking rather than as a delete, for the same
    reason as the role-less record above. A pair this reader mislabelled
    would arrive as "primary" with no flag and would be reverted.
    """
    old = f"{DEVICE}_partner_sleep_score"
    new = f"{ENTRY}_user_{PARTNER}_sleep_score"
    row = Row("sensor.partner_score", "sensor", new)
    entry = Entry(
        data={"auth_value": "a@b.c", "_device_ids_v3": [DEVICE]},
        options={"_uid_migration_v3": [[old, new]]},
    )
    hass = Hass([entry], EntityRegistry([row]))

    migrations.async_migrate_unique_ids(hass, entry, Coordinator())
    assert entry.data["_uid_migration_v3"] == [
        record("sensor", old, new, role="partner") | {"stale": True}
    ]


def test_the_plan_does_not_depend_on_vendor_response_order(migrations):
    """`dedupe_devices_by_id` preserves the vendor's array order verbatim.

    Two runs that see the same beds in a different order must plan the
    same rename, or which bed owns the account-level id is decided by
    whatever the API happened to return first. `select.py` documents
    escaping that exact dependency, and the migration reintroduced it.
    """
    entry = Entry()
    forward, backward = Coordinator(), Coordinator()
    forward.devices = two_beds(BED_A, BED_B)
    backward.devices = two_beds(BED_B, BED_A)

    a = migrations._planned_renames(entry, forward)
    b = migrations._planned_renames(entry, backward)

    # Per-person renames legitimately come out in device order, and the
    # rename loop is order-insensitive, so compare the content.
    assert set(a) == set(b)

    # The account-level pair is the one that must not drift. There is a
    # single target per entry, so choosing its source by response order is
    # what let a reordered array plan a rename onto an occupied id.
    def account_pair(pairs):
        return [p for p in pairs if p[1].endswith("_temperature_display_unit")]

    assert account_pair(a) == account_pair(b)


# ── Checks that only became possible with the real description list ───
#
# Everything below reads `descriptions.INSIGHT_SENSOR_DESCRIPTIONS`
# through the migration. While that module was stubbed here with a
# single made-up key, none of these could say anything: they would have
# been assertions about a one-element list this file wrote itself.


def test_every_real_insight_key_gets_a_rename_planned(migrations):
    """The plan must cover the whole description list, not a sample.

    `_planned_renames` is the only thing that moves a 2.x unique_id onto
    its 3.x name. A key present in `INSIGHT_SENSOR_DESCRIPTIONS` and
    absent from the plan is an entity that keeps its old id, gets a new
    entity built alongside it under the new id, and leaves the user with
    a duplicate and a dead sensor holding all the history.

    Reading the list off the real module rather than restating it here on
    purpose. A copy would pass forever after somebody added a sensor.
    """
    descriptions = _orion.real("descriptions")
    keys = [d.key for d in descriptions.INSIGHT_SENSOR_DESCRIPTIONS]
    assert len(keys) > 20, (
        "the real description list is suspiciously short, which usually "
        f"means a stub crept back in: {keys}"
    )

    planned = migrations._planned_renames(Entry(), Coordinator())
    new_ids = {new for _old, new in planned}
    missing = [key for key in keys if not any(n.endswith(f"_{key}") for n in new_ids)]
    assert missing == [], f"no rename planned for these real sensor keys: {missing}"


def test_no_two_description_keys_plan_the_same_new_unique_id(migrations):
    """A collision here silently drops one entity's history.

    `async_update_entity` raises when the target id is occupied, and the
    migration counts that as a failure it cannot retry past. Two keys
    that build the same new id make that unavoidable rather than
    transient. The stub could not see this: one key cannot collide with
    itself.

    The realistic way in is a key that is a suffix of another key, since
    the id is built by concatenation. That is exactly the shape of
    `total_sleep_time` next to `sleep_time`.
    """
    planned = migrations._planned_renames(Entry(), Coordinator())
    new_ids = [new for _old, new in planned]
    duplicates = sorted({n for n in new_ids if new_ids.count(n) > 1})
    assert duplicates == [], (
        "two planned renames target the same unique_id, so one of them "
        f"will always fail and that entity keeps its 2.x id: {duplicates}"
    )

    old_ids = [old for old, _new in planned]
    dupe_sources = sorted({o for o in old_ids if old_ids.count(o) > 1})
    assert dupe_sources == [], (
        f"one old unique_id is planned to move to two places: {dupe_sources}"
    )


def test_the_partner_plan_covers_the_same_keys_plus_the_session_flag(migrations):
    """Partner renames are derived from the same list, and must stay so.

    `_partner_recovery_renames` builds its keys as
    `INSIGHT_SENSOR_DESCRIPTIONS + ["session_active"]`. If the two lists
    ever drift, a partner keeps 2.x ids for the sensors the primary
    already moved, and a later revert reverses only half a household.
    """
    descriptions = _orion.real("descriptions")
    expected = {d.key for d in descriptions.INSIGHT_SENSOR_DESCRIPTIONS}
    expected.add("session_active")

    coordinator = Coordinator()
    coordinator.partner_identity_confirmed = True
    coordinator.partner_user = {"id": PARTNER}
    coordinator.has_partner_for_device = lambda _device_id: True

    planned = migrations._partner_recovery_renames(Entry(), coordinator)
    assert planned, "no partner renames planned for a verified partner"
    covered = {
        key for key in expected if any(new.endswith(f"_{key}") for _old, new in planned)
    }
    assert covered == expected, f"partner plan misses: {sorted(expected - covered)}"
