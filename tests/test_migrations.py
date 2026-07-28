"""Executable registry tests for the v3 identity migration.

The ordinary suite intentionally has no Home Assistant dependency. These
fakes implement the same registry operations the migration uses, so the
tests exercise the migration itself rather than a copied decision helper.
"""

from __future__ import annotations

import importlib.util
import sys
import types
from dataclasses import dataclass, replace
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent / "custom_components" / "orion_sleep"
DOMAIN = "orion_sleep"
ACCOUNT = "11111111-1111-4111-8111-111111111111"
PARTNER = "22222222-2222-4222-8222-222222222222"
DEVICE = "bed-1"
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


class ConfigEntryState:
    """Only the members the migration actually branches on."""

    NOT_LOADED = "not_loaded"
    SETUP_IN_PROGRESS = "setup_in_progress"
    LOADED = "loaded"
    SETUP_RETRY = "setup_retry"
    SETUP_ERROR = "setup_error"


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

    def has_partner_for_device(self, _device_id):
        return False


@pytest.fixture(scope="module")
def migrations():
    """Restores sys.modules on the way out.

    This installs fake `homeassistant` and `custom_components` modules and
    used to leave them there for the rest of the process. Harmless only
    while nothing else imports Home Assistant. The moment a real HA test
    lands in the same run, collection order decides whether it gets the
    real package or these stubs, and the failure looks like an HA bug.
    """
    saved = dict(sys.modules)
    try:
        yield from _load_migrations()
    finally:
        sys.modules.clear()
        sys.modules.update(saved)


def _load_migrations():
    package = types.ModuleType("custom_components.orion_sleep")
    package.__path__ = [str(ROOT)]
    sys.modules["custom_components"] = types.ModuleType("custom_components")
    sys.modules["custom_components.orion_sleep"] = package

    for name in ("const", "helpers"):
        spec = importlib.util.spec_from_file_location(
            f"custom_components.orion_sleep.{name}", ROOT / f"{name}.py"
        )
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)

    sensor = types.ModuleType("custom_components.orion_sleep.sensor")
    sensor.INSIGHT_SENSOR_DESCRIPTIONS = [types.SimpleNamespace(key="sleep_score")]
    sys.modules[sensor.__name__] = sensor

    homeassistant = types.ModuleType("homeassistant")
    config_entries = types.ModuleType("homeassistant.config_entries")
    config_entries.ConfigEntry = Entry
    config_entries.ConfigEntryState = ConfigEntryState
    core = types.ModuleType("homeassistant.core")
    core.HomeAssistant = Hass
    exceptions = types.ModuleType("homeassistant.exceptions")
    exceptions.ConfigEntryError = type("ConfigEntryError", (Exception,), {})
    helpers = types.ModuleType("homeassistant.helpers")
    device_registry = types.ModuleType("homeassistant.helpers.device_registry")
    entity_registry = types.ModuleType("homeassistant.helpers.entity_registry")
    device_registry.async_get = lambda hass: hass.device_registry
    entity_registry.async_get = lambda hass: hass.entity_registry
    entity_registry.async_entries_for_config_entry = lambda registry, entry_id: [
        row for row in registry.entities.values() if row.config_entry_id == entry_id
    ]
    helpers.device_registry = device_registry
    helpers.entity_registry = entity_registry
    sys.modules.update(
        {
            "homeassistant": homeassistant,
            "homeassistant.config_entries": config_entries,
            "homeassistant.core": core,
            "homeassistant.exceptions": exceptions,
            "homeassistant.helpers": helpers,
            "homeassistant.helpers.device_registry": device_registry,
            "homeassistant.helpers.entity_registry": entity_registry,
        }
    )

    spec = importlib.util.spec_from_file_location(
        "custom_components.orion_sleep.migrations", ROOT / "migrations.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    yield module


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
    new = f"{DEVICE}_user_{ACCOUNT}_sleep_score"
    row = Row("sensor.sleep_score", "sensor", new)
    entry = Entry(data={"auth_value": "alice@example.com", "_device_ids_v3": [DEVICE]})
    hass = Hass([entry], EntityRegistry([row]))

    assert migrations.async_migrate_unique_ids(hass, entry, Coordinator()) == 0
    assert entry.data["_uid_migration_v3"] == [
        record("sensor", f"{DEVICE}_sleep_score", new)
    ]


def test_a_partner_record_is_dropped_once_that_partner_is_unverifiable(migrations):
    """The reverse rename would hand 2.x the wrong person's entities.

    2.x has one role-keyed partner row and feeds it from whichever partner
    account is linked at the time. So a record naming a partner this pass
    cannot verify must not survive: reverting it would put the previous
    partner's sleep, heart rate and apnea entities on the id that 2.x then
    writes the CURRENT partner's readings to. One transient fetch failure
    is enough to reach this, so the eviction is unconditional.
    """
    old = f"{DEVICE}_sleep_score"
    new = f"{DEVICE}_user_{ACCOUNT}_sleep_score"
    stale_partner = record(
        "sensor",
        f"{DEVICE}_partner_sleep_score",
        f"{DEVICE}_user_{PARTNER}_sleep_score",
        role="partner",
    )
    row = Row("sensor.sleep_score", "sensor", old)
    entry = Entry(
        data={
            "auth_value": "alice@example.com",
            "_device_ids_v3": [DEVICE],
            "_uid_migration_v3": [stale_partner],
        }
    )
    hass = Hass([entry], EntityRegistry([row]))

    assert migrations.async_migrate_unique_ids(hass, entry, Coordinator()) == 1
    journal = entry.data["_uid_migration_v3"]
    assert stale_partner not in journal
    assert record("sensor", old, new) in journal


def test_a_legacy_partner_record_without_a_role_is_still_recognised(migrations):
    """Journals written before `role` existed must not slip past the guard."""
    legacy = {
        "domain": "sensor",
        "platform": DOMAIN,
        "old": f"{DEVICE}_partner_sleep_score",
        "new": f"{DEVICE}_user_{PARTNER}_sleep_score",
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
    assert "_uid_migration_v3" not in entry.data


def test_partial_revert_keeps_only_the_mapping_that_failed(migrations):
    new_one = f"{DEVICE}_user_{ACCOUNT}_sleep_score"
    new_two = f"{DEVICE}_user_{ACCOUNT}_session_active"
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
    new = f"{DEVICE}_user_{ACCOUNT}_sleep_score"
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
    new = f"{DEVICE}_user_{ACCOUNT}_sleep_score"
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
    new_id = f"{DEVICE}_user_{ACCOUNT}_sleep_score"
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
    new = f"{DEVICE}_user_{ACCOUNT}_sleep_score"
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
    new = f"{DEVICE}_user_{PARTNER}_sleep_score"
    row = Row("sensor.partner_score", "sensor", new)
    entry = Entry(data={"auth_value": "alice@example.com", "_device_ids_v3": [DEVICE]})
    hass = Hass([entry], EntityRegistry([row]))
    coordinator = Coordinator()
    coordinator.partner_user = {"id": PARTNER}
    coordinator.has_partner_for_device = lambda _device_id: True

    migrations.async_migrate_unique_ids(hass, entry, coordinator)
    assert record("sensor", old, new, role="partner") in entry.data["_uid_migration_v3"]


def test_original_pair_journal_is_upgraded_out_of_options(migrations):
    old = f"{DEVICE}_sleep_score"
    new = f"{DEVICE}_user_{ACCOUNT}_sleep_score"
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


def test_a_legacy_options_partner_pair_is_still_evicted(migrations):
    """The pair format is the one this project shipped WITH partner renames.

    Labelling it "primary" let every legacy partner record past the
    eviction, so a downgrade handed the previous partner's entities to
    2.x, which then wrote the current partner's readings onto them.
    """
    old = f"{DEVICE}_partner_sleep_score"
    new = f"{DEVICE}_user_{PARTNER}_sleep_score"
    row = Row("sensor.partner_score", "sensor", new)
    entry = Entry(
        data={"auth_value": "a@b.c", "_device_ids_v3": [DEVICE]},
        options={"_uid_migration_v3": [[old, new]]},
    )
    hass = Hass([entry], EntityRegistry([row]))

    migrations.async_migrate_unique_ids(hass, entry, Coordinator())
    assert "_uid_migration_v3" not in entry.data


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
