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
from dataclasses import dataclass
from typing import Any

from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from . import helpers
from .const import (
    CONF_AUTH_VALUE,
    CONF_DEVICE_IDS,
    CONF_UID_MIGRATION,
    CONF_UID_RECOVERY_ACTIVE,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class RevertResult:
    """Outcome of one downgrade preparation pass."""

    reverted: int
    remaining: int
    identity_restored: bool

    @property
    def complete(self) -> bool:
        """Whether every entity is back on the id 2.x will ask for.

        Deliberately not gated on `identity_restored`. The config entry's
        own unique_id is cosmetic to 2.x, and a sibling entry holding this
        entry's typed address would otherwise make the recovery service
        report failure on every call forever, with recovery mode latched
        and 3.x refusing to load. The entities are what carry history.
        """
        return self.remaining == 0


def overlapping_entry_ids(
    hass: HomeAssistant, entry_id: str | None, device_ids: set[str]
) -> set[str]:
    """Other Orion entries already attached to one of these beds.

    This deliberately does not inspect ``runtime_data``. During startup,
    the entry that loads first otherwise sees every later entry as empty
    and wins ownership of the shared registry rows by boot order.
    """
    if not device_ids:
        return set()

    overlapping: set[str] = set()
    for other in hass.config_entries.async_entries(DOMAIN):
        if other.entry_id == entry_id:
            continue
        recorded = other.data.get(CONF_DEVICE_IDS) or []
        if device_ids & {str(value) for value in recorded}:
            overlapping.add(other.entry_id)

    registry = dr.async_get(hass)
    for device_id in device_ids:
        device = registry.async_get_device(identifiers={(DOMAIN, device_id)})
        if device is None:
            continue
        for configured in device.config_entries:
            if configured == entry_id:
                continue
            other = hass.config_entries.async_get_entry(configured)
            if other is not None and other.domain == DOMAIN:
                overlapping.add(configured)
    return overlapping


def unresolved_device_entries(hass: HomeAssistant, entry_id: str) -> set[str]:
    """Sibling entries still starting that have not named their beds yet.

    Restricted to entries that can still resolve on their own. An entry
    writes its bed set only after a successful first refresh, so one whose
    token has expired never will. Waiting on that entry held every OTHER
    account in a retry loop indefinitely: two unrelated beds down because
    one of them needed a reauth.

    An entry that already loaded once is covered without this. It owns
    device registry rows, and `overlapping_entry_ids` reads those.
    """
    waiting = (ConfigEntryState.NOT_LOADED, ConfigEntryState.SETUP_IN_PROGRESS)
    return {
        other.entry_id
        for other in hass.config_entries.async_entries(DOMAIN)
        if other.entry_id != entry_id
        and getattr(other, "disabled_by", None) is None
        and getattr(other, "state", None) in waiting
        and CONF_DEVICE_IDS not in other.data
    }


def entry_identity_conflict(
    hass: HomeAssistant, entry: ConfigEntry, account_id: str
) -> bool:
    """Whether another entry already owns this immutable Orion account."""
    return any(
        other.entry_id != entry.entry_id and other.unique_id == account_id
        for other in hass.config_entries.async_entries(DOMAIN)
    )


def _journal_record(
    row: Any, old: str, new: str, role: str = "primary"
) -> dict[str, str]:
    """One reversible rename, with the provenance the caller already knew.

    `role` is recorded rather than recovered later by looking for
    `_partner_` in the old id. This module opens by promising never to
    decide anything from a string pattern, and a partner eviction driven
    by substring match is exactly that. The only place the pattern is
    still consulted is reading a journal written before this field
    existed, where labelling an already recorded pair is all it does.
    """
    return {
        "domain": row.domain,
        "platform": row.platform,
        "old": old,
        "new": new,
        "role": role,
    }


def _record_key(record: dict[str, str]) -> tuple[str, str, str]:
    return record["domain"], record["platform"], record["new"]


def _role_for(old: str) -> str:
    """Label an already recorded pair whose journal predates `role`.

    The only place a `_partner_` substring is consulted, and it decides a
    label rather than a rename. Both journal readers share it, because
    having one reader apply the rule and the other default to "primary"
    is precisely how legacy partner records slipped past the eviction.
    """
    return "partner" if "_partner_" in old else "primary"


def _read_journal(entry: ConfigEntry, rows: list[Any]) -> list[dict[str, str]]:
    """Read the structured journal and upgrade the original pair format."""
    records: list[dict[str, str]] = []
    raw = entry.data.get(CONF_UID_MIGRATION) or []
    for value in raw if isinstance(raw, list) else []:
        if not isinstance(value, dict):
            continue
        record = {
            key: str(value.get(key) or "")
            for key in ("domain", "platform", "old", "new")
        }
        if not all(record.values()) or record["old"] == record["new"]:
            continue
        record["role"] = str(value.get("role") or "") or _role_for(record["old"])
        records.append(record)

    # The first implementation stored [old, new] pairs in user options.
    # Expand them using the actual registry row before removing that copy.
    legacy = entry.options.get(CONF_UID_MIGRATION) or []
    for value in legacy if isinstance(legacy, list) else []:
        if not isinstance(value, (list, tuple)) or len(value) != 2:
            continue
        old, new = str(value[0]), str(value[1])
        for row in rows:
            if row.unique_id == new:
                # Same provenance rule the structured reader above uses.
                # Defaulting to "primary" here let every legacy partner
                # pair past the eviction that exists to stop a downgrade
                # handing the previous partner's entities to the current
                # one. The pair format is the ONE this project shipped
                # while partner renames were still being planned, so it is
                # the reader most likely to be holding partner data.
                records.append(_journal_record(row, old, new, _role_for(old)))

    deduped: dict[tuple[str, str, str], dict[str, str]] = {}
    for record in records:
        deduped.setdefault(_record_key(record), record)
    return list(deduped.values())


def _write_journal(
    hass: HomeAssistant,
    entry: ConfigEntry,
    records: list[dict[str, str]],
    *,
    recovery_active: bool | None = None,
) -> None:
    """Persist internal recovery state without firing options reloads."""
    data = dict(entry.data)
    if records:
        data[CONF_UID_MIGRATION] = records
    else:
        data.pop(CONF_UID_MIGRATION, None)
    if recovery_active is not None:
        if recovery_active:
            data[CONF_UID_RECOVERY_ACTIVE] = True
        else:
            data.pop(CONF_UID_RECOVERY_ACTIVE, None)

    options = dict(entry.options)
    options.pop(CONF_UID_MIGRATION, None)
    changes: dict[str, Any] = {}
    if data != entry.data:
        changes["data"] = data
    if options != entry.options:
        changes["options"] = options
    if changes:
        hass.config_entries.async_update_entry(entry, **changes)


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

        # Legacy partner rows do not say which partner owns their history.
        # If the account was ever replaced, assigning the row to today's
        # partner would expose the previous person's health data under the
        # wrong identity. Leave it intact and let the account-keyed entity
        # start fresh beside it.

    # Account-level, and previously built INSIDE the per-device loop, so a
    # two-bed account has two rows for one setting. Only one can move onto
    # the account-keyed id, and the surplus copies are redundant views of
    # the same value, so they are left where they are.
    #
    # Sorted, not `coordinator.devices` order. That list is whatever the
    # vendor's array happened to contain, which `dedupe_devices_by_id`
    # preserves verbatim. Keying on its first element meant a reordered
    # response, or selling the bed that happened to be listed first,
    # planned a rename onto an id the surviving row already held. A
    # declined rename is fatal, so a supported user action permanently
    # bricked the entry on its NEXT start rather than its first. That is
    # the same ordering dependency `select.py` documents escaping from.
    device_ids = sorted(str(d["id"]) for d in coordinator.devices if d.get("id"))
    if device_ids:
        pairs.append(
            (
                f"{device_ids[0]}_temperature_display_unit",
                f"{entry.entry_id}_temperature_display_unit",
            )
        )

    return pairs


def _partner_recovery_renames(coordinator) -> list[tuple[str, str]]:
    """How 2.x would name a verified partner created fresh under 3.x.

    These pairs are for downgrade journalling only. They must never drive
    the forward migration because a pre-3.0 role-keyed row cannot prove
    which historical partner owns it.
    """
    from .sensor import INSIGHT_SENSOR_DESCRIPTIONS

    partner = (coordinator.partner_user or {}).get("id")
    if not isinstance(partner, str) or not partner:
        return []
    keys = [d.key for d in INSIGHT_SENSOR_DESCRIPTIONS] + ["session_active"]
    pairs: list[tuple[str, str]] = []
    for device in coordinator.devices:
        device_id = device.get("id")
        if device_id and coordinator.has_partner_for_device(device_id):
            pairs += _person_renames(device_id, partner, keys, "partner_")
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
    if entry_identity_conflict(hass, entry, account_id):
        raise ConfigEntryError(
            "Another Orion entry already owns this account id. Remove the "
            "duplicate entry before migration"
        )
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
    device_ids = {d.get("id") for d in coordinator.devices if d.get("id")}
    if overlapping_entry_ids(hass, entry.entry_id, device_ids):
        raise ConfigEntryError(
            "another Orion config entry covers the same bed; refusing to assign "
            "shared history by startup order"
        )

    registry = er.async_get(hass)
    known = er.async_entries_for_config_entry(registry, entry.entry_id)
    planned = _planned_renames(entry, coordinator)
    existing_journal = _read_journal(entry, known)
    journal = {_record_key(record): record for record in existing_journal}

    # Registry uniqueness is (domain, platform, unique_id). Work with the
    # exact row rather than flattening on unique_id, because a sensor and a
    # binary sensor may legitimately use the same id.
    occupied = {
        (row.domain, row.platform, row.unique_id): row.id
        for row in registry.entities.values()
    }
    # Who holds an id, so a decline can name the row that is actually in
    # the way. Reporting only the row being moved sent users to delete the
    # wrong entity, taking its history with it.
    holder_entity_ids = {row.id: row.entity_id for row in registry.entities.values()}
    owned_row_ids = {row.id for row in known}
    pending: list[tuple[Any, str, str]] = []
    seen_sources: set[tuple[str, str, str]] = set()
    for old, new in planned:
        if not old or not new or old == new:
            continue
        for row in known:
            source = (row.domain, row.platform, old)
            if row.unique_id == old and source not in seen_sources:
                pending.append((row, old, new))
                seen_sources.add(source)

    declined: list[str] = []
    migrated = 0
    while pending:
        progressed = False
        deferred: list[tuple[Any, str, str]] = []
        pending_source_ids = {row.id for row, _old, _new in pending}
        for row, old, new in pending:
            target = (row.domain, row.platform, new)
            holder = occupied.get(target)
            if holder is not None and holder != row.id:
                if holder in pending_source_ids:
                    deferred.append((row, old, new))
                    continue
                if holder in owned_row_ids:
                    # Our own row already sits on the target, so the work
                    # this rename exists to do is done. The only plan that
                    # reaches here is the account-level one, where a
                    # multi-bed account has surplus 2.x rows for a single
                    # value. Treating that as a conflict made removing a
                    # bed a permanent, unretryable setup failure.
                    continue
                declined.append((row.entity_id, holder_entity_ids.get(holder, new)))
                continue
            try:
                registry.async_update_entity(row.entity_id, new_unique_id=new)
            except ValueError:
                declined.append((row.entity_id, new))
                continue
            occupied.pop((row.domain, row.platform, old), None)
            occupied[target] = row.id
            record = _journal_record(row, old, new)
            journal.setdefault(_record_key(record), record)
            migrated += 1
            progressed = True
        if not progressed:
            declined.extend((row.entity_id, new) for row, _old, new in deferred)
            break
        pending = deferred

    # A clean 3.0 install creates the new ids after the first migration
    # pass. The second pass after platform setup reaches this branch and
    # records how 2.x would have named each row. Existing installs whose
    # first migration predated journalling self-heal here too.
    current = er.async_entries_for_config_entry(registry, entry.entry_id)
    partner_planned = _partner_recovery_renames(coordinator)

    # 2.x has exactly ONE role-keyed row per partner key, and it is fed by
    # whichever partner account is linked at the time. A record naming a
    # partner we cannot currently verify must not survive: reverting it
    # would hand the previous partner's entities to 2.x, which then writes
    # the CURRENT partner's heart rate and apnea onto them. Every path that
    # leaves the partner unverified reaches here, including a single
    # transient fetch failure, so the eviction is unconditional and the
    # records are rebuilt below whenever a partner is verified again.
    for key, existing in list(journal.items()):
        if existing.get("role") == "partner":
            journal.pop(key)

    for pairs, role in ((planned, "primary"), (partner_planned, "partner")):
        for old, new in pairs:
            for row in current:
                if row.unique_id != new:
                    continue
                key = (row.domain, row.platform, new)
                old_exists = (row.domain, row.platform, old) in occupied
                if not old_exists and key not in journal:
                    journal[key] = _journal_record(row, old, new, role)

    _write_journal(hass, entry, list(journal.values()))

    if declined:
        # Name the BLOCKER, not just the row being moved. Reporting only
        # the source told users to delete the entity that still worked,
        # taking its history with it, while the actual squatter stayed.
        detail = sorted({f"{source} (blocked by {blocker})" for source, blocker in declined})
        _LOGGER.warning(
            "Left %d Orion entities on their previous ids: %s. They keep "
            "working, but their history is not attached to an account",
            len(detail),
            detail,
        )
        raise ConfigEntryError(
            "Orion could not re-key these entities because something else "
            f"already holds the id they need: {detail}. Delete the blocking "
            "entity, then reload. If you would rather go back to 2.x ids, run "
            "the orion_sleep.revert_unique_ids action, and orion_sleep."
            "resume_unique_ids if you change your mind. No platforms were "
            "loaded, though the renames that did succeed are recorded and "
            "reversible"
        )
    if migrated:
        _LOGGER.info(
            "Re-keyed %d Orion entities onto their owner's account id. "
            "Entity ids and history are unchanged",
            migrated,
        )
    return migrated


def async_revert_unique_ids(hass: HomeAssistant, entry: ConfigEntry) -> RevertResult:
    """Put every re-keyed entity back on the id it had before 3.0.

    For someone rolling back to 2.x, and for anyone who discovers after
    the fact that their household was one of the shapes this migration
    cannot read correctly.

    Processes each record once, in journal order. There is deliberately no
    chain ordering here, because the recorded map contains none: every old
    id is `{device}_{key}` and every new id is `{device}_user_{uuid}_{key}`
    or `{entry}_temperature_display_unit`, so no record's target is another
    record's source. A record it cannot apply is kept in `remaining` and
    the caller is told the run is incomplete rather than being left to
    assume it succeeded.
    """
    registry = er.async_get(hass)
    known = er.async_entries_for_config_entry(registry, entry.entry_id)
    recorded = _read_journal(entry, known)

    reverted = 0
    remaining: list[dict[str, str]] = []
    for record in recorded:
        domain = record["domain"]
        platform = record["platform"]
        old = record["old"]
        new = record["new"]
        source = next(
            (
                row
                for row in er.async_entries_for_config_entry(registry, entry.entry_id)
                if row.domain == domain
                and row.platform == platform
                and row.unique_id == new
            ),
            None,
        )
        old_row = next(
            (
                row
                for row in registry.entities.values()
                if row.domain == domain
                and row.platform == platform
                and row.unique_id == old
            ),
            None,
        )
        if source is None:
            if old_row is not None and old_row.config_entry_id != entry.entry_id:
                remaining.append(record)
            continue
        if old_row is not None and old_row.id != source.id:
            remaining.append(record)
            continue
        try:
            registry.async_update_entity(source.entity_id, new_unique_id=old)
        except ValueError:
            _LOGGER.warning("Could not revert %s", source.entity_id)
            remaining.append(record)
            continue
        reverted += 1

    auth_value = str(entry.data.get(CONF_AUTH_VALUE) or "").strip().lower()
    identity_restored = entry.unique_id == auth_value
    if not remaining and auth_value and not identity_restored:
        collision = any(
            other.entry_id != entry.entry_id and other.unique_id == auth_value
            for other in hass.config_entries.async_entries(DOMAIN)
        )
        if not collision:
            hass.config_entries.async_update_entry(entry, unique_id=auth_value)
            identity_restored = True

    _write_journal(
        hass,
        entry,
        remaining,
        recovery_active=True,
    )
    if reverted:
        _LOGGER.info("Reverted %d Orion entities to their pre-3.0 ids", reverted)
    return RevertResult(reverted, len(remaining), identity_restored)
