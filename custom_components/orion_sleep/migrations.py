"""Entity registry migrations.

Changing a `unique_id` without migrating does not move an entity. It
abandons it. Home Assistant sees an id it has no record of, creates a
second entity, and appends `_2` to the entity_id. The original keeps its
recorder history and its place in every dashboard and automation, and
goes unavailable forever.

Renaming the `unique_id` in the registry instead keeps the SAME entity:
same entity_id, same history, same references. Nothing the user built
notices. That is the only reason these renames are safe to ship.

Every rename here is derived from the same description lists the
platforms build entities from, never from a string pattern. `"{device}_
{key}"` for an insight is indistinguishable by shape from `"{device}_
access"` for the bed's guest list, and a pattern-matched migration would
happily rename the wrong one.
"""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from . import helpers
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

# Where the applied old -> new pairs are kept so the rename can be undone.
UID_MIGRATION_KEY = "_uid_migration_v3"


def _person_renames(
    device_id: str, user_id: str, keys: list[str], legacy_prefix: str
) -> list[tuple[str, str]]:
    """Old to new ids for one person on one bed."""
    return [
        (
            f"{device_id}_{legacy_prefix}{key}",
            helpers.person_unique_id(device_id, key, user_id, legacy=""),
        )
        for key in keys
    ]


def _planned_renames(entry: ConfigEntry, coordinator) -> list[tuple[str, str]]:
    """Every old to new unique_id pair this version wants to apply."""
    from .sensor import INSIGHT_SENSOR_DESCRIPTIONS

    insight_keys = [d.key for d in INSIGHT_SENSOR_DESCRIPTIONS]
    primary = coordinator.user_id
    partner = (coordinator.partner_user or {}).get("id")

    pairs: list[tuple[str, str]] = []
    for device in coordinator.devices:
        device_id = device.get("id")
        if not device_id:
            continue

        if primary:
            # These three read the AUTHENTICATED user's session, so they
            # were always one person's readings, not the bed's. They are
            # still built inside the per-device loop, so a second bed
            # would still duplicate them. Keying them on the person is
            # the half of that fix which does not need a device to test
            # against, and the naming no longer lies about whose they are.
            own = insight_keys + ["session_active", "server_in_bed", "current_phase"]
            pairs += _person_renames(device_id, primary, own, "")

        # Same predicate the platforms use. `partner_user` being populated
        # is not enough: `has_partner_for_device` also needs the serial to
        # match and (coordinator.py) a single device on the account, so on
        # a two-bed account no partner entity is ever built. Renaming on
        # the strength of the profile alone would re-key her entities onto
        # ids nothing claims, and the legacy ids they could be recovered
        # from would be gone. This migration is one-way.
        if partner and coordinator.has_partner_for_device(device_id):
            # The reason this migration exists. Keyed on the literal role
            # "partner", replacing the linked account handed the new
            # person the previous person's entity, and with it their
            # recorder history and long-term statistics. Sleep scores,
            # heart rates and apnea counts, attributed to the wrong human.
            partner_keys = insight_keys + ["session_active"]
            pairs += _person_renames(device_id, partner, partner_keys, "partner_")

        # Account-level, and previously keyed on whichever device sorted
        # first. Removing that bed orphaned the entity.
        pairs.append(
            (
                f"{device_id}_temperature_display_unit",
                f"{entry.entry_id}_temperature_display_unit",
            )
        )

    return pairs


def async_migrate_entry_identity(
    hass: HomeAssistant, entry: ConfigEntry, coordinator
) -> bool:
    """Move the config entry's own unique_id onto the Orion account id.

    Entries created before this shipped are keyed on the email or phone
    number that was typed into the form. The config flow now keys new
    entries on the account, and the duplicate check only works if both
    sides agree, so existing entries have to come along.
    """
    account_id = coordinator.user_id
    if not account_id or entry.unique_id == account_id:
        return False
    for other in hass.config_entries.async_entries(DOMAIN):
        if other.entry_id != entry.entry_id and other.unique_id == account_id:
            # Two entries sharing a unique_id makes every future
            # _abort_if_unique_id_configured unreliable, and Home Assistant
            # currently only logs it.
            _LOGGER.warning(
                "Not re-identifying this entry: another entry is already "
                "configured for the same Orion account"
            )
            return False
    hass.config_entries.async_update_entry(entry, unique_id=account_id)
    _LOGGER.info(
        "Config entry is now identified by its Orion account rather than "
        "the address it was set up with"
    )
    return True


def async_migrate_unique_ids(
    hass: HomeAssistant, entry: ConfigEntry, coordinator
) -> int:
    """Re-key person entities onto immutable Orion user ids.

    Idempotent. Runs on every setup and does nothing once applied,
    because the old ids are gone by then. Safe to run before the identity
    is known: nothing is renamed without a user id to rename it onto.
    """
    # Before 3.0 the duplicate check was on the typed address, so one
    # household could add two accounts for the same bed and get two
    # entries. Their device-scoped ids are identical, the registry row is
    # shared, and which entry owns it is decided by boot order. Migrating
    # in that state keys one person's entire history to the other and
    # leaves the loser building a fresh, empty `_2` entity. There is no
    # safe plan here, so decline and say why.
    device_ids = {d.get("id") for d in coordinator.devices if d.get("id")}
    for other in hass.config_entries.async_entries(DOMAIN):
        if other.entry_id == entry.entry_id:
            continue
        other_coordinator = getattr(other, "runtime_data", None)
        other_devices = {
            d.get("id") for d in getattr(other_coordinator, "devices", []) or []
        }
        if device_ids & other_devices:
            _LOGGER.warning(
                "Not re-keying Orion entities: another config entry covers the "
                "same bed, so there is no way to tell whose history is whose. "
                "Remove the duplicate entry and restart Home Assistant"
            )
            return 0

    registry = er.async_get(hass)
    known = er.async_entries_for_config_entry(registry, entry.entry_id)
    # Keyed on (domain, unique_id) because that is what Home Assistant
    # enforces. A sensor and a binary_sensor may legitimately share a
    # unique_id, and a flat dict would keep one and rename the wrong one.
    by_unique_id = {(e.domain, e.unique_id): e for e in known}

    # Registry-wide, not entry-local. Home Assistant enforces uniqueness
    # per (domain, platform) across every config entry, so asking only
    # what THIS entry owns approves renames it then refuses.
    owned = {unique_id for _domain, unique_id in by_unique_id}
    in_use = {
        e.unique_id
        for e in registry.entities.values()
        if e.platform == DOMAIN and e.unique_id not in owned
    }
    # unique_id -> the registry row, for the rename itself. The plan works
    # in unique_ids because that is what the entities are keyed on.
    rows = {unique_id: row for (_d, unique_id), row in by_unique_id.items()}

    planned = _planned_renames(entry, coordinator)
    applying = helpers.renames_to_apply(planned, owned, in_use)

    # Anything planned, present, and NOT applied was declined. Silence
    # here is how a run that stranded twenty-eight entities on dead ids
    # still logged "Entity ids and history are unchanged".
    declined = [
        rows[old].entity_id
        for old, _new in planned
        if old in rows and (old, _new) not in applying
    ]

    migrated = 0
    for old, new in applying:
        entity_id = rows[old].entity_id
        try:
            registry.async_update_entity(entity_id, new_unique_id=new)
        except ValueError:
            # Belt and braces. The plan above already excludes ids the
            # registry holds, so reaching this means the registry changed
            # underneath us. A rename losing a race is not a reason to
            # fail setup: the entity keeps the id it has, still works,
            # and the next start tries again.
            _LOGGER.warning("Could not re-key %s, leaving it as it is", entity_id)
            declined.append(entity_id)
            continue
        migrated += 1

    # The map that makes this a two-way door. Rolling back to 2.x asks the
    # registry for ids that no longer exist, builds a second entity for
    # each person, and leaves the original permanently unavailable with
    # every reading on it. That is the failure this module exists to
    # prevent, produced by this module in reverse.
    #
    # Includes pairs already applied on an earlier start, recognised by
    # the new id being present while the old one is gone. Without that, an
    # install that migrated before this recording existed would have no
    # way back, and neither would one that migrated across two restarts.
    already = [
        (old, new)
        for old, new in planned
        if new in owned and old not in owned and old != new
    ]
    recoverable = [list(pair) for pair in applying] + [list(pair) for pair in already]
    if recoverable and entry.options.get(UID_MIGRATION_KEY) != recoverable:
        hass.config_entries.async_update_entry(
            entry,
            options={**entry.options, UID_MIGRATION_KEY: recoverable},
        )

    if declined:
        _LOGGER.warning(
            "Left %d Orion entities on their previous ids: %s. They will keep "
            "working but their history is not attached to an account",
            len(declined),
            sorted(set(declined)),
        )
    if migrated:
        _LOGGER.info(
            "Re-keyed %d Orion entities onto their owner's account id. "
            "Entity ids and history are unchanged",
            migrated,
        )
        if any(old.startswith(tuple(f"{d}_partner_" for d in device_ids)) for old, _ in applying):
            # The pre-3.0 ids recorded nothing about who owned the row, so
            # if the partner account was ever replaced, these carry the
            # PREVIOUS partner's readings. This migration cannot know, and
            # cementing that silently would be worse than saying so.
            _LOGGER.warning(
                "If you have ever used 'Replace partner account', the partner "
                "entities carry the previous person's sleep history. Delete "
                "and re-add them to start clean"
            )
    return migrated


def async_revert_unique_ids(hass: HomeAssistant, entry: ConfigEntry) -> int:
    """Put every re-keyed entity back on the id it had before 3.0.

    For someone rolling back to 2.x, and for anyone who discovers after
    the fact that their household was one of the shapes this migration
    cannot read correctly. Replays the recorded map backwards through the
    same ordering rules, so it is as safe as the forward pass and just as
    happy to defer a rename it cannot make yet.
    """
    recorded = entry.options.get(UID_MIGRATION_KEY) or []
    pairs = [
        (str(new), str(old))
        for old, new in (p for p in recorded if isinstance(p, list | tuple) and len(p) == 2)
    ]
    if not pairs:
        return 0

    registry = er.async_get(hass)
    known = er.async_entries_for_config_entry(registry, entry.entry_id)
    rows = {e.unique_id: e for e in known}
    owned = set(rows)
    in_use = {
        e.unique_id
        for e in registry.entities.values()
        if e.platform == DOMAIN and e.unique_id not in owned
    }

    reverted = 0
    for old, new in helpers.renames_to_apply(pairs, owned, in_use):
        try:
            registry.async_update_entity(rows[old].entity_id, new_unique_id=new)
        except ValueError:
            _LOGGER.warning("Could not revert %s", rows[old].entity_id)
            continue
        reverted += 1

    if reverted:
        hass.config_entries.async_update_entry(
            entry,
            options={k: v for k, v in entry.options.items() if k != UID_MIGRATION_KEY},
        )
        _LOGGER.info("Reverted %d Orion entities to their pre-3.0 ids", reverted)
    return reverted
