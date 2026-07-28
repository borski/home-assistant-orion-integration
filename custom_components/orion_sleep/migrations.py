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

        if partner:
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
    registry = er.async_get(hass)
    known = er.async_entries_for_config_entry(registry, entry.entry_id)
    by_unique_id = {e.unique_id: e for e in known}

    # Registry-wide, not entry-local. Home Assistant enforces uniqueness
    # per (domain, platform) across every config entry, so asking only
    # what THIS entry owns approves renames it then refuses.
    in_use = {
        e.unique_id
        for e in registry.entities.values()
        if e.platform == DOMAIN and e.unique_id not in by_unique_id
    }

    migrated = 0
    for old, new in helpers.renames_to_apply(
        _planned_renames(entry, coordinator), set(by_unique_id), in_use
    ):
        entity_id = by_unique_id[old].entity_id
        try:
            registry.async_update_entity(entity_id, new_unique_id=new)
        except ValueError:
            # Belt and braces. The plan above already excludes ids the
            # registry holds, so reaching this means the registry changed
            # underneath us. A rename losing a race is not a reason to
            # fail setup: the entity keeps the id it has, still works,
            # and the next start tries again.
            _LOGGER.warning("Could not re-key %s, leaving it as it is", entity_id)
            continue
        migrated += 1

    if migrated:
        _LOGGER.info(
            "Re-keyed %d Orion entities onto their owner's account id. "
            "Entity ids and history are unchanged",
            migrated,
        )
    return migrated
