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

Exactly two functions consult a `_partner_` substring anyway, and they
are `_role_for` and `_has_legacy_partner_rows`. They sit next to each
other so a reader finds both at once, because an earlier version of this
docstring claimed there was only one and a second had already landed.
Neither decides a rename. Each exists because the thing it is asked
about cannot be derived from a description list: a journal record
written before the `role` key existed, and a pre-3.0 row this version
never builds and therefore has no description for.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
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
    CONF_PARTNER_ACCESS_TOKEN,
    CONF_PARTNER_ACCOUNT_ID,
    CONF_PARTNER_REPLACED,
    CONF_UID_MIGRATION,
    CONF_UID_RECOVERY_ACTIVE,
    DOMAIN,
)
from .descriptions import INSIGHT_SENSOR_DESCRIPTIONS

_LOGGER = logging.getLogger(__name__)

# What a decline names when the registry row holding the wanted id cannot
# be resolved to an entity_id.
#
# This used to be `f"unique_id {new}"`, where `new` is
# `{device_uuid}_user_{orion_user_uuid}_{key}`. That string is not a log
# line. It travels out through `UnmigratedEntities` and becomes a repair
# issue placeholder, rendered in the UI and readable by every non-admin
# member of the household, and `device_id` is in the diagnostics redaction
# set precisely so it does not appear in places like that.
#
# Nothing is lost by dropping it. A raw unique_id is not something a user
# can act on: the repairs dialog offers no way to search by one, and the
# actionable half of the message, the entity that could not move, is
# already the other element of the pair. The full ids are logged at DEBUG
# for whoever is actually debugging this.
_UNRESOLVED_BLOCKER = "an entity Orion could not identify"


class UnmigratedEntities(Exception):
    """Some planned renames could not be applied, and setup must not die.

    Deliberately NOT a `ConfigEntryError`. Raising one of those from
    `async_setup_entry` stops `async_forward_entry_setups` from ever
    running, so a single squatted unique_id took down all nine platforms:
    every climate control, every biometric sensor, and every automation
    referencing them, permanently, with recovery requiring the user to
    hand-delete entity registry rows named in a log line.

    The registry half of that decision was right and has not changed. A
    declined rename is still refused rather than forced, because forcing
    it would merge two entities' recorder history. What changed is who
    pays for it. The entities that could not move stay on their previous
    ids and keep working, and the caller turns this into a repair issue
    so the user gets an actionable prompt instead of a dead integration.

    `conflicts` is already formatted for display, one line per blocked
    rename, naming the BLOCKER as well as the row being moved. Reporting
    only the source sent users to delete the entity that still worked,
    taking its history with it, while the actual squatter stayed.
    """

    def __init__(self, conflicts: list[str], migrated: int = 0) -> None:
        self.conflicts = list(conflicts)
        self.migrated = migrated
        super().__init__(
            "Orion could not re-key these entities because something else "
            f"already holds the id they need: {self.conflicts}. They keep "
            "working on their previous ids, but their history is not attached "
            "to an account. Delete the blocking entity, then reload. If you "
            "would rather go back to 2.x ids, run the "
            "orion_sleep.revert_unique_ids action, and "
            "orion_sleep.resume_unique_ids if you change your mind"
        )


@dataclass(frozen=True)
class RevertResult:
    """Outcome of one downgrade preparation pass."""

    reverted: int
    remaining: int
    identity_restored: bool
    # A partner is linked, no partner mappings survived to be reverted, AND
    # no pre-3.0 role-keyed row is still sitting where 2.x will look for it.
    #
    # This now means one specific thing: the partner was REPLACED, and the
    # records naming the previous one were evicted because this setup
    # proved they named somebody else. A partner that merely could not be
    # reached keeps its records and reports `partner_stale` instead, so
    # the caller can tell the two apart in the message it shows.
    #
    # The third condition is what keeps that warning off households in no
    # danger at all. A normal 2.x upgrade leaves the legacy
    # `{device}_partner_{key}` rows exactly where they are on purpose, so no
    # partner record is ever journalled for them and 2.x finds them
    # untouched. Reporting that as unmapped made the revert service raise
    # for every partnered household that upgraded, on a revert that had in
    # fact completed. See `_has_legacy_partner_rows`.
    partner_unmapped: bool = False
    # At least one partner record was kept but not applied, because the
    # setup that wrote the journal could not confirm which account the
    # partner tokens belonged to.
    #
    # Distinct from `partner_unmapped` on purpose, and the distinction is
    # the entire point of keeping these records instead of deleting them.
    # "Your partner changed" and "I could not confirm your partner" need
    # different words and lead to different actions, and the caller cannot
    # invent that difference from a single flag.
    #
    # Does NOT feed `complete`. These records were not blocked by a
    # registry conflict, so telling the user to go resolve one would send
    # them looking for something that is not there.
    partner_stale: bool = False
    # `partner_unmapped` is true AND pre-3.0 `{device}_partner_{key}` rows
    # are still in the registry, which together mean the partner was
    # replaced while those rows were sitting on the old ids.
    #
    # A strictly narrower case than `partner_unmapped`, carried separately
    # because the two need OPPOSITE instructions and the caller cannot
    # derive which one it is holding.
    #
    # Plain `partner_unmapped`: the records naming the previous partner
    # were evicted and the current partner simply has not been journalled
    # yet. Reloading fixes it, because the next setup journals the current
    # partner and the revert then has records to apply.
    #
    # This one: reloading can NEVER fix it. The journalling loop records a
    # pair only when the old id is absent, and the legacy rows are
    # occupying exactly those old ids, so every reload produces this same
    # state. Worse, the rows hold the PREVIOUS partner's history, so a
    # household that follows the generic advice reloads forever and, if it
    # ever gives up and installs 2.x anyway, gets the current partner's
    # heart rate and apnea written into the previous partner's history.
    #
    # The correct wording for this existed only as a `_LOGGER.warning` in
    # this module, which no household reads. `__init__.py` raised the
    # generic message for both, so the refusal was right and the remedy it
    # named was wrong. This flag is what lets the service say the true
    # thing.
    partner_rows_outrank_journal: bool = False
    # The entity_ids of those legacy rows, so the refusal can name what to
    # delete instead of describing a unique_id substring and leaving the
    # household to go find them. Empty whenever the flag above is False.
    legacy_partner_entity_ids: tuple[str, ...] = ()

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


@dataclass(frozen=True)
class PartnerRevertBlockers:
    """Every partner refusal a revert can earn, decided before it runs.

    The three booleans here are the same three `RevertResult` carries,
    and that is not an accident of naming. `partner_revert_blockers` is
    the ONLY thing that computes them, and `async_revert_unique_ids`
    copies them onto its own result rather than working them out a second
    time. See that function for why two agreeing implementations is the
    thing being avoided.

    They exist as a separate type because they are answerable from pure
    reads, and `RevertResult` is not. `reverted` and `remaining` are
    outcomes of a registry transaction. These are facts about the journal
    and the registry that hold before, during and after it.

    The bug that put this type here: `_handle_revert` used to evaluate
    every partner refusal on the RETURNED `RevertResult`, which meant it
    raised after `async_revert_unique_ids` had already renamed rows and
    rewritten the journal. In the `partner_stale` case that is not merely
    untidy. The revert applies non-stale records and skips stale ones, so
    a journal holding both, which is exactly what the split-brain state in
    `_partner_recovery_renames` produces, got a PARTIAL partner rename and
    only then a refusal. Half of one person's entities land on the
    role-keyed ids 2.x feeds from the other person, and the household is
    told to go fix something while the registry has already been half
    moved. The message describes a state that no longer exists.
    """

    # At least one journal record was kept but is not trusted, because the
    # setup that wrote it could not confirm which account the partner
    # tokens belonged to. Refuses unconditionally, which matches the old
    # behaviour: `partner_stale` was in the "nothing recorded to undo"
    # early return precisely so a stale-only journal could not slip past
    # it in silence.
    partner_stale: bool = False
    # A partner is linked, no partner record survived to be reverted, and
    # no pre-3.0 role-keyed row is still vouching for the current partner.
    partner_unmapped: bool = False
    # The strictly narrower replacement case. Needs the OPPOSITE remedy
    # from plain `partner_unmapped`, which is why it is carried separately.
    partner_rows_outrank_journal: bool = False
    # entity_ids of the pre-3.0 rows a replacement refusal has to name.
    # Empty whenever `partner_rows_outrank_journal` is False, so a
    # household that is not being refused is never handed a list of its
    # own entities to delete.
    legacy_partner_entity_ids: tuple[str, ...] = ()
    # How many records this revert would actually act on. Equal to
    # `reverted + remaining` after the fact, and computed here without
    # running anything. See `_source_row` for why that equality holds.
    #
    # This number exists for one caller and one condition. Plain
    # `partner_unmapped` was deliberately NOT added to the "nothing
    # recorded to undo" early return in `__init__.py`, because doing so
    # refuses every partnered entry whose journal is simply empty because
    # nothing ever migrated. Running the revert speculatively on such an
    # entry is a supported thing to do and `test_speculative_revert_real`
    # covers the damage the old code did on that path.
    #
    # Moving the refusal into a preflight would have reintroduced exactly
    # that, because a preflight has no `reverted` to gate on. Gating on
    # `pending_renames` restores the old gate exactly rather than
    # approximately, so the blast radius of this change stays at zero
    # entries that were previously told "nothing changed".
    pending_renames: int = 0


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


def bed_owner(hass: HomeAssistant, device_ids: set[str]) -> str | None:
    """Which config entry the registry says already owns these beds.

    Two entries covering one bed is a shape 2.x permitted, because it had
    no cross-entry check. Before 3.0 the primary account's entities were
    `{device}_{key}` with no person in them, so both entries computed the
    same id and whichever registered first got the row. The history in
    that row belongs to whoever HA happened to register, and no amount of
    reasoning at migration time recovers which account that was.

    So the tie is broken by where the history actually is, rather than by
    who wrote a claim first. Ownership by boot order was the original bug,
    and a claim published by a setup that then failed is indistinguishable
    from one held by a healthy entry, so claims cannot be the arbiter
    either. Registry rows can: they are what carries the recorder history,
    they survive restarts, and they do not move on their own.

    Returns None when nobody owns any rows yet, which is a genuinely fresh
    install and where the caller falls back to a stable tie-break.
    """
    if not device_ids:
        return None

    registry = er.async_get(hass)
    owners: dict[str, int] = {}
    prefixes = tuple(f"{device_id}_" for device_id in device_ids)
    for row in registry.entities.values():
        if row.platform != DOMAIN or not row.config_entry_id:
            continue
        if row.unique_id.startswith(prefixes):
            owners[row.config_entry_id] = owners.get(row.config_entry_id, 0) + 1

    if not owners:
        return None
    most = max(owners.values())
    # Sorted, so a tie resolves the same way on every host and every start.
    return min(entry_id for entry_id, count in owners.items() if count == most)


def resolve_bed_owner(
    hass: HomeAssistant, entry_id: str, device_ids: set[str]
) -> str:
    """Which entry may act on these beds. Never returns None.

    Ownership follows the registry rows, because that is where the
    history is. Only when nobody owns any rows does it fall back to a
    stable tie-break, and at that point there is no history to misassign.

    This expression decides which config entry owns a household's
    biometric history, and it existed in two places with two copies of
    that reasoning written out longhand beside it. Both had to agree
    forever. If they ever drifted, two entries would each believe they
    owned the same bed and the migration would run twice over the same
    rows, which is the failure the check exists to prevent.

    Callers collapse to `if resolve_bed_owner(...) != entry_id: raise`,
    so the only thing left to differ between them is the message, which
    is the part that SHOULD differ. `coordinator` fails a poll and
    `async_migrate_unique_ids` refuses a migration, and those want
    different words.

    `entry_id` is included in the tie-break rather than assumed absent
    from it, so the answer does not depend on whether the caller happens
    to be among the rivals it computed.
    """
    return bed_owner(hass, device_ids) or min(
        overlapping_entry_ids(hass, entry_id, device_ids) | {entry_id}
    )


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


def _record_key(record: dict[str, Any]) -> tuple[str, str, str]:
    return record["domain"], record["platform"], record["new"]


def _role_for(old: str) -> str:
    """Label an already recorded pair whose journal predates `role`.

    One of the two places a `_partner_` substring is consulted, and the
    one with the smaller consequence: it decides a label rather than a
    rename. `_has_legacy_partner_rows` below is the other, and it sits
    here so the pair is impossible to read separately. An earlier version
    of this docstring said "the only place" and was already wrong when it
    was written.

    Both journal readers share this function, because having one reader
    apply the rule and the other default to "primary" is precisely how
    legacy partner records slipped past the eviction.
    """
    return "partner" if "_partner_" in old else "primary"


def _has_legacy_partner_rows(registry: er.EntityRegistry, entry_id: str) -> bool:
    """Whether a pre-3.0 role-keyed partner row is still in the registry.

    The SECOND consultation of a `_partner_` substring in this module,
    and the heavier of the two. `_role_for` picks a label. This decides
    whether `async_revert_unique_ids` reports `partner_unmapped`, which
    makes the revert service raise and blocks a downgrade.

    It cannot be derived from a description list, which is what the rest
    of this module insists on, because there is no description to derive
    it from. `{device}_partner_{key}` is a shape 2.x built and 3.x never
    does. The forward migration deliberately leaves those rows alone,
    since a role-keyed row cannot prove WHICH historical partner owns its
    history, so nothing in this version's entity descriptions names them.
    A substring is the only evidence they exist.

    That is safe here in a way it would not be for a rename. The worst a
    false positive can do is suppress a warning about a downgrade that
    was going to be fine anyway. A false positive on a rename hands one
    person's heart rate and apnea history to another person's identity.
    """
    return bool(_legacy_partner_row_ids(registry, entry_id))


def _legacy_partner_row_ids(
    registry: er.EntityRegistry, entry_id: str
) -> tuple[str, ...]:
    """entity_ids of the pre-3.0 role-keyed partner rows, for the message.

    Same substring test `_has_legacy_partner_rows` documents, returning
    what it found rather than only whether it found anything.

    The reason this exists at all is the remedy. When a partner has been
    replaced, the only instruction that can work is "delete these rows",
    and an instruction to delete something is worth very little if the
    household has to translate a `unique_id` substring into entity_ids by
    hand first. `unique_id` is not shown anywhere in the Home Assistant
    UI. `entity_id` is what the registry is searchable by.

    Sorted so the message is stable between runs, which matters when
    somebody pastes two attempts into a bug report and expects to be able
    to diff them.
    """
    return tuple(
        sorted(
            row.entity_id
            for row in er.async_entries_for_config_entry(registry, entry_id)
            if "_partner_" in row.unique_id
        )
    )


def _read_journal(entry: ConfigEntry, rows: list[Any]) -> list[dict[str, Any]]:
    """Read the structured journal and upgrade the original pair format."""
    records: list[dict[str, Any]] = []
    raw = entry.data.get(CONF_UID_MIGRATION) or []
    for value in raw if isinstance(raw, list) else []:
        if not isinstance(value, dict):
            continue
        record: dict[str, Any] = {
            key: str(value.get(key) or "")
            for key in ("domain", "platform", "old", "new")
        }
        if not all(record.values()) or record["old"] == record["new"]:
            continue
        record["role"] = str(value.get("role") or "") or _role_for(record["old"])
        if value.get("stale"):
            # Carried through rather than recomputed, because this reader
            # cannot recompute it. Staleness records that some PAST setup
            # could not confirm which account the partner tokens belonged
            # to, and nothing in the entry or the registry says so
            # afterwards. The original field-by-field copy listed four keys
            # and dropped everything else, which would have silently
            # promoted every distrusted record back to trusted on the very
            # next read and handed `async_revert_unique_ids` a journal it
            # had already been told not to believe.
            record["stale"] = True
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


def _is_partner_record(value: dict[str, Any]) -> bool:
    """Whether a structured journal record names the partner.

    Deliberately the same expression `_read_journal` uses to fill in
    `role`, and it has to stay that way. The journal shipped before the
    `role` key did, so a record written by an earlier 3.0.x build has no
    `role` at all. Testing `role == "partner"` on its own let every one
    of those past the eviction, and the reader that actually performs the
    revert then labelled them "partner" via `_role_for` and renamed them
    anyway.
    """
    return (
        str(value.get("role") or "") or _role_for(str(value.get("old") or ""))
    ) == "partner"


def evict_partner_journal(
    data: Mapping[str, Any], options: Mapping[str, Any]
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Drop every partner rename record from BOTH journals.

    Returns the new `data` and `options` for one `async_update_entry`.
    Called by the config flow when the partner account is removed or
    replaced, so the eviction does not depend on the reload afterwards
    succeeding.

    `async_migrate_unique_ids` evicts partner records too, but only on
    its next SUCCESSFUL run. If the reload after a partner change fails,
    for any reason at all, that eviction never happens and the journal
    still names the PREVIOUS partner while the tokens name the new one.
    Reverting that journal renames the previous partner's rows onto the
    role-keyed `{device}_partner_{key}` ids 2.x feeds from the currently
    linked account, merging two people's heart rate, HRV and apnea
    history under one identity, with every rename reporting success.

    This lives beside `_read_journal` because that reader is what decides
    which records are partner records. It reads the structured journal in
    `data` AND the original `[old, new]` pair journal in `options`, and
    it labels a record carrying no `role` via `_role_for`. An eviction
    that trusted an explicit `role` therefore skipped every record older
    than that key, and an eviction that walked only `data` never touched
    the pair journal, which is the one format this project shipped while
    partner renames were still being planned. Both misses survived a
    partner change once already. Keeping the eviction in the same file as
    the readers is what stops them drifting apart a third time.

    Primary records are kept on purpose. They map the primary account,
    which did not change, and they are the only thing standing between a
    downgrade and a stranded history. Dropping them would leave
    `async_revert_unique_ids` with nothing to undo in exactly the failed
    reload window this function exists to cover, and the service would
    then report "no recorded Orion renames to undo" while every primary
    entity sat on a 3.x id 2.x never asks for.
    """
    new_data = dict(data)
    new_options = dict(options)

    records = new_data.get(CONF_UID_MIGRATION)
    if isinstance(records, list):
        kept = [
            record
            for record in records
            if not (isinstance(record, dict) and _is_partner_record(record))
        ]
        if len(kept) != len(records):
            # Removed rather than left empty, so a journal that held
            # nothing but partner records does not linger as an empty list
            # that later reads as "a journal exists here".
            if kept:
                new_data[CONF_UID_MIGRATION] = kept
            else:
                new_data.pop(CONF_UID_MIGRATION, None)

    legacy = new_options.get(CONF_UID_MIGRATION)
    if isinstance(legacy, list):
        # `[old, new]` pairs carry no role, so `_role_for` is the only
        # label they will ever get, here and in `_read_journal`. Anything
        # that is not a well formed pair is left alone: the reader skips
        # it too, so it can never drive a rename.
        kept_pairs = [
            value
            for value in legacy
            if not (
                isinstance(value, (list, tuple))
                and len(value) == 2
                and _role_for(str(value[0])) == "partner"
            )
        ]
        if len(kept_pairs) != len(legacy):
            if kept_pairs:
                new_options[CONF_UID_MIGRATION] = kept_pairs
            else:
                new_options.pop(CONF_UID_MIGRATION, None)

    return new_data, new_options


def _write_journal(
    hass: HomeAssistant,
    entry: ConfigEntry,
    records: list[dict[str, Any]],
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


# Single values on the account, not per bed and not per sleeper, and all
# three were built inside the per-device loop at some point. Only one row
# can move onto the account-keyed id; the surplus copies are redundant
# views of the same value and are left where they are.
_ACCOUNT_LEVEL_KEYS: tuple[str, ...] = (
    "temperature_display_unit",
    "zone_split_mode",
    "away_mode",
)


def _person_renames(
    entry_id: str, device_id: str, user_id: str, keys: list[str], legacy_prefix: str
) -> list[tuple[str, str]]:
    """2.x ids to account-scoped ids for one person.

    `device_id` still appears on the LEFT because that is where 2.x put
    it. It is deliberately absent from the right: every key routed
    through here reads an account-wide response, so the entity belongs to
    the person and the entry rather than to a bed.
    """
    return [
        (
            f"{device_id}_{legacy_prefix}{key}",
            helpers.account_person_unique_id(entry_id, key, user_id, legacy=""),
        )
        for key in keys
    ]


def _planned_renames(entry: ConfigEntry, coordinator) -> list[tuple[str, str]]:
    """Every old to new unique_id pair this version wants to apply."""
    insight_keys = [d.key for d in INSIGHT_SENSOR_DESCRIPTIONS]
    primary = coordinator.user_id

    pairs: list[tuple[str, str]] = []
    for device in coordinator.devices:
        device_id = device.get("id")
        if not device_id:
            continue

        if primary:
            # These read the AUTHENTICATED user's account-wide session, so
            # they were never the bed's. 3.0 keyed them on the person and
            # left the device prefix in place, which was described here as
            # "the half of that fix which does not need a device to test
            # against". This is the other half.
            #
            # The device prefix was not cosmetic. These entities were built
            # inside the per-device loop, so a two-bed account got two
            # sleep scores, two HRVs and two apnea counts, each reading the
            # same account-wide response. A night slept in one bed was
            # recorded against both, which is a biometric attribution
            # error rather than clutter.
            own = insight_keys + ["session_active", "server_in_bed", "current_phase"]
            pairs += _person_renames(entry.entry_id, device_id, primary, own, "")

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
        pairs += [
            (
                f"{device_ids[0]}_{key}",
                helpers.account_unique_id(entry.entry_id, key),
            )
            for key in _ACCOUNT_LEVEL_KEYS
        ]

    return pairs


def _account_rekey_pairs(
    entry: ConfigEntry, coordinator, known: list[Any]
) -> list[tuple[Any, str, str]]:
    """Rows still on 3.0's `{device}_user_{user}_{key}`, and where they go.

    The 3.0 migration moved account-wide entities onto the person and
    left the bed in the prefix, which it documented as half a fix. This
    completes it, so the second half has to find rows that already made
    the first hop.

    Derived from the ROWS rather than from an enumerated key list, and
    that is the important property. The account-scoped families are
    spread across six platforms and keep growing, so any list written
    here would be a list to forget to update, and a schedule entity left
    behind on a bed-prefixed id is exactly the duplicate this is removing.

    Safe as a prefix match because `_user_` is only ever produced by
    `person_unique_id` and `schedule_unique_id`, and every entity built
    through either reads an account-wide response. The device id on the
    left is matched exactly against this entry's own beds rather than
    pattern-guessed, so this is not the loose `_partner_` style matching
    the module docstring warns about.
    """
    device_ids = sorted(str(d["id"]) for d in coordinator.devices if d.get("id"))
    pairs: list[tuple[Any, str, str]] = []
    for row in known:
        for device_id in device_ids:
            prefix = f"{device_id}_user_"
            if not row.unique_id.startswith(prefix):
                continue
            # Everything from `user_` onwards is preserved verbatim, so
            # the person and the key survive untouched and only the bed
            # is dropped.
            suffix = row.unique_id[len(device_id) + 1 :]
            pairs.append((row, row.unique_id, f"{entry.entry_id}_{suffix}"))
            break
    return pairs


def _partner_recovery_renames(entry: ConfigEntry, coordinator) -> list[tuple[str, str]]:
    """How 2.x would name a verified partner created fresh under 3.x.

    These pairs are for downgrade journalling only. They must never drive
    the forward migration because a pre-3.0 role-keyed row cannot prove
    which historical partner owns it.

    UNCONFIRMED IDENTITY EMITS NOTHING. Every pair here is a claim about
    WHICH partner owns a unique_id, and a setup that could not confirm the
    partner is not in a position to make that claim.

    That used to be enforced one layer downstream instead, as a second
    veto inside `_reconcile_partner_journal`, and one layer was exactly
    one too few. `async_migrate_unique_ids` consumes these pairs TWICE:
    the reconcile at the top, and the journalling loop below it. The
    reconcile carried the identity veto and the loop did not, so a run
    with a populated `partner_user` and an unconfirmed identity stamped
    every pre-existing partner record `stale` and then wrote the newly
    discovered ones with no `stale` flag at all. Same partner, same run,
    half the records trusted.

    That split-brain journal is worse than either state on its own.
    `async_revert_unique_ids` applies non-stale records and skips stale
    ones, so it performed a PARTIAL partner rename and only then raised
    `partner_stale`, after the registry had already been mutated. Half of
    one person's entities land on the role-keyed ids 2.x feeds from the
    other person. Two people's biometrics merge, through the mechanism
    built to stop exactly that.

    Both states that reach here as "unconfirmed" are ordinary. The second
    of the two `_async_refresh_partner_identity` calls per setup can hit a
    dropped connection while the first succeeded, which leaves
    `partner_user` populated and `partner_identity_confirmed` false. So
    can one try block where `get_current_user` returns and `list_devices`
    throws. Neither is exotic and neither is proof of anything.

    The default is False on purpose. A coordinator that cannot answer the
    question is not a coordinator that answered yes.
    """
    if not getattr(coordinator, "partner_identity_confirmed", False):
        return []
    partner = (coordinator.partner_user or {}).get("id")
    if not isinstance(partner, str) or not partner:
        return []
    keys = [d.key for d in INSIGHT_SENSOR_DESCRIPTIONS] + ["session_active"]
    pairs: list[tuple[str, str]] = []
    seen: set[str] = set()
    for device in coordinator.devices:
        device_id = device.get("id")
        if device_id and coordinator.has_partner_for_device(device_id):
            for old, new in _person_renames(
                entry.entry_id, device_id, partner, keys, "partner_"
            ):
                # Deduped on the TARGET, which only became possible to
                # collide once the target stopped carrying the device id.
                # `has_partner_for_device` already requires a single bed,
                # so this cannot fire today. It is here because a pair
                # list with two routes onto one id is the shape that makes
                # a revert rename half a household and then refuse.
                if new in seen:
                    continue
                seen.add(new)
                pairs.append((old, new))
    return pairs


def _reconcile_partner_journal(
    journal: dict[tuple[str, str, str], dict[str, Any]],
    partner_planned: list[tuple[str, str]],
    coordinator,
) -> None:
    """Decide what to do with partner records this run cannot vouch for.

    2.x has exactly ONE role-keyed row per partner key, and it is fed by
    whichever partner account is linked at the time. Reverting a record
    that names a REPLACED partner therefore hands the previous partner's
    entities to 2.x, which then writes the CURRENT partner's heart rate
    and apnea onto them. That is real, and it is why this used to delete
    every partner record on every pass, unconditionally.

    The problem was that the deletion could not tell two states apart.
    "This partner was replaced" and "the server did not answer for 800ms"
    both arrive here as an unverified partner, and the second one is a
    Tuesday. `async_migrate_unique_ids` runs twice per setup, so one
    restart inside one network blip permanently destroyed that
    household's only rollback path off the 3.x ids, announced by a single
    warning, self-healing never.

    So the two states are now separated, and only the provable one
    deletes anything:

      * PROVABLY DIFFERENT. `partner_planned` is non-empty, so this setup
        reached the server, verified which account the partner tokens
        belong to, and enumerated exactly the ids that partner's entities
        hold. A partner record whose target is not in that set names
        somebody else. Evict it, and let the loop below re-record the
        current partner from scratch.
      * MERELY UNCONFIRMED. `partner_planned` is empty. The partner was
        unreachable, or the tokens were rejected, or the bed topology no
        longer resolves. Keep every record and mark it stale, so nothing
        is lost and `async_revert_unique_ids` still knows not to trust it.

    `partner_planned` being non-empty is what proves the first case, and
    the chain that makes that true is worth stating because breaking any
    link in it silently re-arms the original bug.
    `_partner_recovery_renames` returns nothing at all unless
    `coordinator.partner_identity_confirmed` is true, and beyond that it
    only emits pairs for a device where `coordinator.has_partner_for_device`
    is true, which requires `partner_mapping_valid`, which is `topology_ok
    and _partner_identity_verified(...)`. A fresh coordinator is built on
    every setup with an empty `partner_user`, so a setup whose partner
    fetch failed cannot produce targets at all.

    `partner_identity_confirmed` is NOT re-tested here, and that is a
    deliberate change rather than an omission. It used to be a second veto
    at this one call site, which meant the identity rule was enforced for
    this function and not for the journalling loop further down
    `async_migrate_unique_ids`, which consumes the same `partner_planned`
    list with no veto on it at all. One run could therefore mark every
    existing partner record stale here and then write brand new partner
    records without the flag, leaving `async_revert_unique_ids` to apply
    half of them and refuse the other half. See `_partner_recovery_renames`
    for the full account of that failure.

    The veto now lives in `_partner_recovery_renames`, which is the single
    place the pairs come from, so it covers every consumer instead of the
    one that remembered to ask. An empty `partner_planned` here now means
    "unproven" for either reason, unreachable partner or unconfirmed
    identity, and both correctly mean the same thing: keep and distrust.
    """
    targets = {new for _old, new in partner_planned}
    # No targets means no proof. `_partner_recovery_renames` already
    # refuses to emit pairs for a partner whose identity this setup could
    # not confirm, so a non-empty set is now sufficient on its own.
    may_evict = bool(targets)

    for key, record in list(journal.items()):
        if not _is_partner_record(record):
            continue
        if not may_evict:
            # Retained, not deleted. A record we cannot vouch for is still
            # the only description of where this partner's entities came
            # from, and the next setup that reaches the server clears the
            # flag again.
            record["stale"] = True
            continue
        if record.get("new") in targets:
            record.pop("stale", None)
        else:
            journal.pop(key)


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

    Raises `UnmigratedEntities` when something else holds an id a rename
    needs. That is a report, not a verdict on whether the integration may
    load. The caller decides, and `async_setup_entry` deliberately keeps
    loading. See `UnmigratedEntities` for why.
    """
    device_ids = {str(d["id"]) for d in coordinator.devices if d.get("id")}
    if overlapping_entry_ids(hass, entry.entry_id, device_ids):
        # Exactly one entry may migrate a shared bed. See `resolve_bed_owner`
        # for why the registry rather than boot order decides which.
        owner = resolve_bed_owner(hass, entry.entry_id, device_ids)
        if owner != entry.entry_id:
            raise ConfigEntryError(
                "Another Orion config entry already holds this bed's entity "
                f"history ({owner}). Two entries cannot both own it, so this "
                "one will not migrate. Remove whichever entry you do not want, "
                "then reload"
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
    # Named, not pattern-matched. These are the entities built once per
    # device for a single ACCOUNT value, so they are the only targets
    # where a surplus row of ours is expected rather than a conflict
    # worth refusing.
    surplus_ok = {
        helpers.account_unique_id(entry.entry_id, key) for key in _ACCOUNT_LEVEL_KEYS
    }

    # The 3.0 to 3.1 hop, applied BEFORE the 2.x plan below so the targets
    # it needs are free.
    #
    # The journal is rewritten in place rather than appended to. Its
    # records exist to name a 2.x id for every row, and a downgrade has to
    # be one hop: if this wrote a second record saying "3.1 came from 3.0"
    # the revert would have to walk backwards through both, and a
    # two-generation journal is documented below as the one registry shape
    # revert cannot resolve. Rewriting `new` and leaving `old` alone keeps
    # every record pointing at the id 2.x actually asks for.
    rekeyed = 0
    for row, old, new in _account_rekey_pairs(entry, coordinator, known):
        target = (row.domain, row.platform, new)
        holder = occupied.get(target)
        if holder is not None and holder != row.id:
            # A surplus copy from a second bed. The winner already moved,
            # so this one keeps its bed-prefixed id and stops being read.
            continue
        try:
            registry.async_update_entity(row.entity_id, new_unique_id=new)
        except ValueError:
            _LOGGER.debug(
                "Could not move %s off its per-bed id; it keeps %s",
                row.entity_id,
                old,
            )
            continue
        source_key = (row.domain, row.platform, old)
        occupied.pop(source_key, None)
        occupied[target] = row.id
        record = journal.pop(source_key, None)
        if record is not None:
            record["new"] = new
            journal[target] = record
        rekeyed += 1

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

    declined: list[tuple[str, str]] = []
    migrated = rekeyed
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
                if holder in owned_row_ids and new in surplus_ok:
                    # Only the account-level value legitimately has more
                    # than one 2.x row, because it was built inside the
                    # per-device loop. Treating that as a conflict made
                    # removing a bed a permanent setup failure.
                    #
                    # Scoped to that target by name. Accepting it for ANY
                    # row of ours silently swallowed a two-generation
                    # registry, which is what an upgrade followed by a
                    # downgrade without reverting produces. That splits one
                    # person's history across two entities and leaves the
                    # registry in the one shape revert cannot resolve.
                    continue
                declined.append((row.entity_id, holder_entity_ids.get(holder, new)))
                continue
            try:
                registry.async_update_entity(row.entity_id, new_unique_id=new)
            except ValueError:
                blocker = occupied.get((row.domain, row.platform, new))
                resolved = holder_entity_ids.get(blocker)
                if resolved is None:
                    _LOGGER.debug(
                        "Rename of %s from %s to %s was refused by the "
                        "registry and the holder could not be resolved",
                        row.entity_id,
                        old,
                        new,
                    )
                declined.append((row.entity_id, resolved or _UNRESOLVED_BLOCKER))
                continue
            occupied.pop((row.domain, row.platform, old), None)
            occupied[target] = row.id
            record = _journal_record(row, old, new)
            journal.setdefault(_record_key(record), record)
            migrated += 1
            progressed = True
        if not progressed:
            for row, _old, new in deferred:
                resolved = holder_entity_ids.get(
                    occupied.get((row.domain, row.platform, new))
                )
                if resolved is None:
                    _LOGGER.debug(
                        "Deferred rename of %s onto %s never became "
                        "applicable and the holder could not be resolved",
                        row.entity_id,
                        new,
                    )
                declined.append((row.entity_id, resolved or _UNRESOLVED_BLOCKER))
            break
        pending = deferred

    # A clean 3.0 install creates the new ids after the first migration
    # pass. The second pass after platform setup reaches this branch and
    # records how 2.x would have named each row. Existing installs whose
    # first migration predated journalling self-heal here too.
    current = er.async_entries_for_config_entry(registry, entry.entry_id)
    partner_planned = _partner_recovery_renames(entry, coordinator)

    _reconcile_partner_journal(journal, partner_planned, coordinator)

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

    if migrated:
        # Logged BEFORE the decline check, not after. It used to sit below
        # the raise, so the one run that most needed to say how far it got
        # was the one run that said nothing.
        _LOGGER.info(
            "Re-keyed %d Orion entities onto their owner's account id. "
            "Entity ids and history are unchanged",
            migrated,
        )
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
        # The journal write above is deliberately ahead of this. Whatever
        # the caller decides to do about the decline, the renames that DID
        # succeed have to stay reversible, and a raise before the write
        # would have made a partial migration permanent.
        raise UnmigratedEntities(detail, migrated)
    return migrated


def _source_row(
    registry: er.EntityRegistry, entry_id: str, record: Mapping[str, Any]
) -> Any:
    """The row of ours currently sitting on a record's 3.x unique_id.

    Shared by the revert loop and the preflight, and it has to stay
    shared. `pending_renames` is only allowed to gate a refusal because it
    is EXACTLY `reverted + remaining`, and that equality is a property of
    this lookup rather than a coincidence. Every branch the loop takes
    after finding a source counts the record: a mismatched old row and a
    registry refusal both land in `remaining`, and everything else lands
    in `reverted`. A source of None is the only way a record is counted in
    neither. Two lookups that drifted apart by one clause would turn a
    refusal on or off for a household with no other symptom.

    Counting them all up front is only equal to counting them as the loop
    goes because no record's target is another record's source. That is
    the same no-chain property `async_revert_unique_ids` documents and
    relies on for its own ordering: every old id is `{device}_{key}` and
    every new id is `{device}_user_{uuid}_{key}` or
    `{entry}_temperature_display_unit`. If a rename ever became capable of
    freeing or occupying another record's new id, the count taken ahead of
    the loop would stop matching the work the loop actually finds, and the
    gate on the plain unmapped refusal would drift with it.

    Re-reads the registry on every call rather than closing over a list
    captured once. The loop renames rows as it goes, so a snapshot taken
    before it starts is stale by the second iteration.
    """
    return next(
        (
            row
            for row in er.async_entries_for_config_entry(registry, entry_id)
            if row.domain == record["domain"]
            and row.platform == record["platform"]
            and row.unique_id == record["new"]
        ),
        None,
    )


def partner_revert_blockers(
    hass: HomeAssistant, entry: ConfigEntry
) -> PartnerRevertBlockers:
    """Work out whether a revert may proceed, without moving anything.

    PURE READS. No `async_update_entity`, no `_write_journal`, no
    `async_update_entry`. That is the entire contract, because the caller
    that matters runs this BEFORE `async_revert_unique_ids` specifically
    so a refusal costs nothing. A mutation added here would put the
    partial rename back in a place nobody would think to look for it.

    The rules themselves are not restated here. `_read_journal` decides
    what a record is, `_is_partner_record` decides which ones name the
    partner, and `_has_legacy_partner_rows` and `_legacy_partner_row_ids`
    decide what a pre-3.0 role-keyed row is. All four already existed and
    all four are called rather than reimplemented.

    That is a rule this module has broken before, in both directions.
    `_is_partner_record`'s docstring records a version that tested
    `role == "partner"` directly while `_read_journal` labelled unlabelled
    records via `_role_for`, so every journal written by an earlier 3.0.x
    build walked straight past the eviction. `_has_legacy_partner_rows`
    was extracted for the same reason, so the `_partner_` substring test
    lives in one place. Note that the membership test below is
    `_is_partner_record` and NOT the raw `role == "partner"` the old
    inline computation used. The two agree today only because
    `_read_journal` fills `role` in on every record it returns. Depending
    on that is how the original bug happened.

    Called from two places on purpose, and they are not redundant.
    `__init__.py` calls it to decide whether to raise. This module calls
    it from `async_revert_unique_ids` so a direct caller, and every test
    that has one, sees the same three booleans on the result without a
    second implementation existing anywhere.
    """
    registry = er.async_get(hass)
    known = er.async_entries_for_config_entry(registry, entry.entry_id)
    recorded = _read_journal(entry, known)

    stale = [record for record in recorded if record.get("stale")]
    pending = sum(
        1
        for record in recorded
        if not record.get("stale")
        and _source_row(registry, entry.entry_id, record) is not None
    )

    # Only when a downgrade would genuinely strand something. A normal
    # 2.x upgrade leaves the legacy `{device}_partner_{key}` rows in place
    # on purpose, so no partner record is ever journalled for them and 2.x
    # finds them exactly where it left them. Reporting that as unmapped
    # made the service raise for every partnered household that upgraded,
    # on a revert that had in fact completed.
    legacy_partner_row_ids = _legacy_partner_row_ids(registry, entry.entry_id)
    legacy_partner_rows = bool(legacy_partner_row_ids)
    # Whether the partner linked NOW differs from the one those legacy rows
    # hold. Written by the config flow at the moment of a change, because
    # that is the only moment anything knows it happened. Nothing in the
    # registry or the journal records it afterwards.
    #
    # Was `bool(entry.data.get(CONF_PARTNER_REPLACED))`, and a bare truth
    # test could not tell a real replacement from a household relinking the
    # same person after a token expiry, which is the fix this integration
    # itself prescribes for that failure. The marker now names the partner
    # whose history is in those rows, so the comparison can be made.
    partner_replaced = helpers.partner_changed_since_legacy_rows(
        entry.data.get(CONF_PARTNER_REPLACED),
        entry.data.get(CONF_PARTNER_ACCOUNT_ID),
    )
    # The suppression above is only sound while the legacy rows still hold
    # the CURRENT partner's history, and that is exactly what a replacement
    # ends.
    #
    # The sequence this closes has every check green at every step. A
    # household upgrades from 2.x with partner A, so `{device}_partner_{key}`
    # rows exist holding A's history and the forward migration correctly
    # leaves them. The partner is then replaced with B, and
    # `evict_partner_journal` correctly drops A's records. On the next
    # reload B verifies, but the journalling loop skips B entirely: it only
    # records a pair when the OLD id is absent, and the old id is
    # `{device}_partner_{key}`, which is still sitting there. So no partner
    # record exists for B, `_has_legacy_partner_rows` is true, the
    # suppression fires, and the revert reports ready for downgrade. 2.x
    # then reads the role-keyed row, finds A's history, and writes B's heart
    # rate into it.
    #
    # `_has_legacy_partner_rows` says a `_partner_` row exists. It has never
    # been able to say who filled it, and its own docstring's claim that a
    # false positive only ever suppresses a warning about a downgrade that
    # was going to be fine is true for an untouched partner and false here.
    legacy_rows_vouch_for_current_partner = legacy_partner_rows and not partner_replaced
    partner_unmapped = (
        bool(entry.data.get(CONF_PARTNER_ACCESS_TOKEN))
        and not any(_is_partner_record(record) for record in recorded)
        and not legacy_rows_vouch_for_current_partner
    )
    # Carried separately rather than only logged. The refusal the
    # household actually reads is raised by `__init__.py`, and with only
    # `partner_unmapped` to read it raised the generic "reload and run this
    # again" for both causes. That is the one instruction that can never
    # succeed here, so the service refused correctly and then told the user
    # to do the wrong thing.
    partner_rows_outrank_journal = partner_unmapped and legacy_partner_rows

    if partner_rows_outrank_journal:
        # A different problem from the one below, with a different remedy.
        # Reloading cannot help: the journalling loop will keep skipping the
        # current partner for as long as the legacy rows occupy the old ids,
        # so "reload and run this again" is an instruction that can never
        # succeed. The rows themselves have to go.
        _LOGGER.warning(
            "This entry still holds pre-3.0 partner entity rows, and the "
            "partner account has been changed since they were written, so "
            "they hold the PREVIOUS partner's sleep and biometric history. "
            "Installing 2.x would feed the current partner's readings into "
            "them, merging two people's history under one identity. Delete "
            "the partner entities whose unique ids contain '_partner_' from "
            "the entity registry, accepting the loss of the previous "
            "partner's history, then reload Orion and run this again"
        )
    elif partner_unmapped:
        _LOGGER.warning(
            "A partner account is linked but no partner entity mappings were "
            "recorded, so the partner's entities will stay on their 3.x ids "
            "after a downgrade. This happens when the linked partner account "
            "was replaced, which discards the records naming the previous "
            "one. Reload Orion and run this again"
        )
    if stale:
        _LOGGER.warning(
            "%d partner entity mappings were left alone because the setup "
            "that recorded them could not confirm which Orion account the "
            "partner tokens belong to. They have NOT been discarded. Reload "
            "Orion so the partner verifies, then run this again",
            len(stale),
        )

    return PartnerRevertBlockers(
        partner_stale=bool(stale),
        partner_unmapped=partner_unmapped,
        partner_rows_outrank_journal=partner_rows_outrank_journal,
        # Only when the flag is set. A household whose partner rows are
        # doing their proper job must never see a list of entities to
        # delete attached to a result that is not refusing anything.
        legacy_partner_entity_ids=(
            legacy_partner_row_ids if partner_rows_outrank_journal else ()
        ),
        pending_renames=pending,
    )


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
    # Read BEFORE the loop, which is a change from computing these flags
    # after it, and the change is safe for a reason worth writing down
    # rather than trusting.
    #
    # Only one input to `partner_revert_blockers` could conceivably be
    # disturbed by the loop, and that is the set of `_partner_` rows in the
    # registry. It cannot be. Every flag that reads those rows is gated on
    # `partner_unmapped`, and `partner_unmapped` requires that the journal
    # holds NO partner record at all. With no partner record, no record in
    # this loop has an `old` id containing `_partner_`, because
    # `_role_for` labels any such record "partner". A rename only ever
    # moves a row onto an `old` id, so the loop cannot create one. And no
    # `new` id contains `_partner_` either, since new ids are
    # `{device}_user_{uuid}_{key}` or `{entry}_temperature_display_unit`,
    # so the loop cannot consume one either. The set is therefore identical
    # before and after in every case where anything reads it.
    #
    # `_write_journal` further down does not disturb them either. It copies
    # `entry.data` and touches only the two internal recovery keys, so
    # `CONF_PARTNER_ACCESS_TOKEN` and `CONF_PARTNER_REPLACED` are untouched,
    # and `recorded` was already read into a local list above.
    blockers = partner_revert_blockers(hass, entry)

    reverted = 0
    remaining: list[dict[str, Any]] = []
    stale: list[dict[str, Any]] = []
    for record in recorded:
        if record.get("stale"):
            # Kept, and deliberately NOT applied. A stale partner record
            # was written by a setup that could not confirm which account
            # the partner tokens belonged to, so applying it might rename
            # the previous partner's entities onto the ids 2.x feeds from
            # the current partner, merging two people's biometrics.
            #
            # Not counted as `remaining` either. Nothing is blocking it,
            # so reporting a registry conflict would send the user hunting
            # for a squatted row that does not exist. It travels back to
            # the caller as `partner_stale` and gets its own message.
            stale.append(record)
            continue
        domain = record["domain"]
        platform = record["platform"]
        old = record["old"]
        new = record["new"]
        source = _source_row(registry, entry.entry_id, record)
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
            # Nothing of ours sits on the new id, so there is no rename to
            # perform. Retaining the record made `remaining` permanently
            # non-zero, which made the service raise on every call while
            # the recovery latch was already set: an unactionable, endless
            # failure of the only rollback path.
            #
            # A foreign entry holding the old id is worth saying out loud,
            # because 2.x will not get that id back, but it is not
            # something this entry can fix by trying again.
            if old_row is not None and old_row.config_entry_id != entry.entry_id:
                # `short_id` at WARNING, whole value at DEBUG. `old` is
                # `{device_uuid}_partner_{key}` or `{device_uuid}_{key}`, so
                # logging it whole publishes a device UUID at a level that
                # gets pasted into bug reports. `device_id` is in the
                # diagnostics redaction set, and a log line contradicting
                # that is the same leak by another route. The entity_id is
                # the part a reader can act on and carries no UUID.
                _LOGGER.warning(
                    "%s (Orion id ending %s) is held by another config entry, "
                    "so 2.x will not reclaim it",
                    old_row.entity_id,
                    helpers.short_id(old),
                )
                _LOGGER.debug("The unreclaimable pre-3.0 unique_id is %s", old)
            continue
        if old_row is not None and old_row.id != source.id:
            remaining.append(record)
            continue
        try:
            registry.async_update_entity(source.entity_id, new_unique_id=old)
        except ValueError:
            # entity_id only. It is the actionable half and carries no
            # UUID, unlike `old` and `new`, which name the bed and the
            # person. Those go to DEBUG, where turning them on is a
            # decision somebody made deliberately.
            _LOGGER.warning("Could not revert %s", source.entity_id)
            _LOGGER.debug(
                "Reverting %s from %s back to %s was refused by the registry",
                source.entity_id,
                new,
                old,
            )
            remaining.append(record)
            continue
        reverted += 1

    auth_value = str(entry.data.get(CONF_AUTH_VALUE) or "").strip().lower()
    identity_restored = entry.unique_id == auth_value
    # `reverted` is load-bearing and was missing. Without it a run that
    # found an empty journal still rewrote `entry.unique_id` from the
    # account id back to the typed email or phone, while every entity
    # stayed on its 3.x id. That is not a no-op, and the caller then
    # reported it as one: it logged "nothing changed", fell through, and
    # logged "ready for downgrade" as well. A user who believed either
    # message and installed 2.x stranded every entity they own, which is
    # the exact failure this whole path exists to prevent.
    #
    # Gating on work done also keeps the restore honest in the other
    # direction. The entry's unique_id is cosmetic to 2.x, so moving it
    # is only ever worth doing as part of a revert that actually moved
    # the entities carrying the history.
    if reverted and not remaining and auth_value and not identity_restored:
        collision = any(
            other.entry_id != entry.entry_id and other.unique_id == auth_value
            for other in hass.config_entries.async_entries(DOMAIN)
        )
        if not collision:
            hass.config_entries.async_update_entry(entry, unique_id=auth_value)
            identity_restored = True

    # Latch only when this run actually did something, or still has work
    # left. Latching on a no-op meant running the action speculatively
    # bricked the entry: 3.x refuses to load while the flag is set, and
    # the way back is a differently named service the message never
    # mentioned.
    # Stale records are written back alongside the unapplied ones. They are
    # the whole reason this run refuses, so losing them here would make the
    # next call succeed against a journal it had already been told not to
    # believe.
    _write_journal(
        hass,
        entry,
        remaining + stale,
        recovery_active=True if (reverted or remaining or stale) else None,
    )
    if reverted:
        _LOGGER.info("Reverted %d Orion entities to their pre-3.0 ids", reverted)
    # Keyword arguments from here on. This call grew past the point where
    # five positional booleans and counts could be read at a glance, and a
    # transposed pair here reports the wrong cause to the household.
    #
    # Every partner flag is COPIED from `blockers` rather than worked out
    # here. This function used to compute them inline, immediately below
    # this line, from the same journal and the same registry, which meant
    # the rules existed twice the moment `__init__.py` needed to consult
    # them before anything moved. Two implementations that agree today is
    # how `_is_partner_record` came to exist, and how a whole generation of
    # journals walked past the partner eviction. There is one now.
    return RevertResult(
        reverted=reverted,
        remaining=len(remaining),
        identity_restored=identity_restored,
        partner_unmapped=blockers.partner_unmapped,
        partner_stale=blockers.partner_stale,
        partner_rows_outrank_journal=blockers.partner_rows_outrank_journal,
        legacy_partner_entity_ids=blockers.legacy_partner_entity_ids,
    )
