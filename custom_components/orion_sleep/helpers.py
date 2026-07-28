"""Helpers that belong to this integration, not to the API client.

These came back from `orion_sleep_api` because none of them describe the
vendor's API. They describe how Home Assistant presents it: entity
registry identifiers, display strings, options-flow schema keys, and the
redaction policy for a diagnostics download.

A client library has no business choosing an entity's unique_id or
formatting a duration as "7h 30m".
"""

from __future__ import annotations

import re

# Matches the vendor's HH:mm schedule times. The library keeps its own
# copy for write validation; this one is only used to render a duration.
_SCHEDULE_TIME_RE = re.compile(r"^(?:[01][0-9]|2[0-3]):[0-5][0-9]$")


_SENSITIVE_DIAGNOSTIC_BRANCHES = frozenset(
    {
        "insights",
        "partner_insights",
        "recommendations",
        "schedules",
        "sensors",
        "timeline",
        "today_sleep_schedule",
        # Whether somebody is in bed right now, since when, and which
        # side. Occupancy over a date range also reads as "the house was
        # empty", which is why this belongs with the biometrics rather
        # than with the config.
        "live_session",
        # Chronotype and sleep targets.
        "sleep_config",
        # The same schedule as `timeline`, which the coordinator stores at
        # top level under a different name. Omitting `schedules` and
        # `today_sleep_schedule` and then letting it out under a third
        # spelling is how a deliberate omission becomes a leak.
        "ws_timelines",
    }
)


_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)


def short_id(value: object) -> str:
    """Last four characters of an identifier, for logs.

    The library already does this for device serials, and says why: logs
    get pasted into bug reports, and `serial_number` and `user_id` are
    both in the diagnostics redaction set. Publishing them at INFO or
    WARNING contradicts a policy the rest of the project keeps. Four
    characters still tell a two-person household which record is meant.
    DEBUG keeps the whole value, because enabling it is deliberate.
    """
    text = value if isinstance(value, str) else str(value or "")
    return "…" + text[-4:] if len(text) > 4 else (text or "?")


def nested_mapping(container: object, *keys: str) -> dict:
    """Walk nested mappings, returning ``{}`` at the first non-mapping level.

    Guards the coordinator accessors. ``(self.data or {}).get("insights", {})``
    happily returns a list if the vendor ever sends one, and the very next
    ``.get`` then raises AttributeError from inside a property, where
    nothing catches it and the entity goes permanently broken.
    """
    current = container
    for key in keys:
        if not isinstance(current, dict):
            return {}
        current = current.get(key)
    return current if isinstance(current, dict) else {}


def renames_to_apply(
    pairs: list[tuple[str, str]],
    on_this_entry: set[str],
    already_in_use: set[str],
) -> list[tuple[str, str]]:
    """Which of `pairs` are safe to rename, and in what order.

    Split out of the migration so it can be tested without Home
    Assistant. The decisions here are the whole risk of the migration,
    and the module that performs them cannot be imported in this suite.

    `on_this_entry` is what this config entry owns. `already_in_use` must
    be every unique_id the ENTITY REGISTRY holds for this platform, not
    just this entry's. Home Assistant's uniqueness check is registry-wide
    per platform, so an entry-local view will happily approve a rename
    that `async_update_entity` then refuses with a ValueError. Two config
    entries for one household reach that: both see the same device, and
    one entry's partner is the other's primary, so both compute the same
    target id.
    """
    out: list[tuple[str, str]] = []
    # Seeded with BOTH, because an id this entry currently holds is just
    # as occupied as one another entry holds. Seeding only from
    # `already_in_use` (which the caller builds by excluding this entry's
    # own ids) meant a rename onto a live id was approved and then refused
    # by the registry. `taken.discard(old)` below is what makes a chain
    # work: the rename that vacates an id has to come first, and one that
    # does not is deferred to the next startup rather than attempted.
    taken = set(already_in_use) | set(on_this_entry)
    for old, new in pairs:
        if not new or old == new or old not in on_this_entry:
            continue
        if new in taken:
            continue
        out.append((old, new))
        taken.add(new)
        taken.discard(old)
    return out


def person_unique_id(
    device_id: str, key: str, user_id: str | None, *, legacy: str
) -> str:
    """Stable unique_id for any entity that belongs to one person.

    Same scheme `schedule_unique_id` has always used, generalised because
    the reasoning in its docstring was never specific to schedules. Sleep
    scores, session flags and occupancy are just as much one person's as
    a bedtime is, and those shipped keyed on the literal role "partner",
    which is the exact thing that docstring warns against.

    `legacy` is returned when the Orion user id is not known yet. That
    only happens if identity is missing at platform setup, and inventing
    a placeholder there would mint a second entity for a person who
    already has one. Returning what previously shipped keeps them on the
    entity they already have until identity resolves.
    """
    if not user_id:
        return legacy
    return f"{device_id}_user_{user_id}_{key}"


def schedule_unique_id(device_id: str, key: str, user_id: str) -> str:
    """Stable unique_id for one person's schedule entity.

    Keyed on the immutable Orion user id. Never on a role like "partner",
    which would silently swap owners if the integration were ever
    re-authenticated as the other account, grafting one person's history
    onto the other. Never on a display name, which is user-editable.

    Deliberately uniform. An earlier draft kept nine un-namespaced ids for
    the authenticated user so pre-existing history would survive, but
    nothing had been built on those entities yet, so the only thing that
    exception bought was a permanent special case in the code and an
    asymmetry between the two people on the bed.
    """
    return f"{device_id}_user_{user_id}_{key}"


def schedule_duration_text(schedule: object) -> str | None:
    """Human "Xh Ym" between a schedule's bedtime and wakeup.

    Handles the overnight rollover. Returns None unless both times are
    present and well formed.
    """
    if not isinstance(schedule, dict):
        return None
    bedtime = schedule.get("bedtime")
    wakeup = schedule.get("wakeup")
    if not isinstance(bedtime, str) or not isinstance(wakeup, str):
        return None
    if not _SCHEDULE_TIME_RE.match(bedtime) or not _SCHEDULE_TIME_RE.match(wakeup):
        return None

    start_h, start_m = (int(part) for part in bedtime.split(":"))
    end_h, end_m = (int(part) for part in wakeup.split(":"))
    minutes = (end_h * 60 + end_m) - (start_h * 60 + start_m)
    if minutes <= 0:
        minutes += 24 * 60
    return f"{minutes // 60}h {minutes % 60}m"


def unique_alias_labels(users: object) -> dict[str, str]:
    """Map each user id to a unique, human-readable form-field label.

    Options-flow schema keys must be unique, and two people can share a
    first name. Duplicates get a numeric suffix. Users with no readable
    name fall back to a shortened id so the field is still addressable.
    """
    labels: dict[str, str] = {}
    if not isinstance(users, list):
        return labels

    used: set[str] = set()
    for user in users:
        if not isinstance(user, dict):
            continue
        user_id = user.get("id")
        if not isinstance(user_id, str) or not user_id:
            continue
        base = (user.get("name") or "").strip() or f"User {user_id[:8]}"
        label = base
        suffix = 2
        while label in used:
            label = f"{base} ({suffix})"
            suffix += 1
        used.add(label)
        labels[user_id] = label
    return labels


def clean_alias_map(value: object, known_ids: object = None) -> dict[str, str]:
    """Normalize a stored alias mapping into ``{user_id: alias}``.

    Blank aliases are dropped so clearing a field removes the override.
    When ``known_ids`` is supplied, ids outside it are discarded, which
    keeps stale entries from a previous account out of the options.
    """
    if not isinstance(value, dict):
        return {}
    allowed = set(known_ids) if isinstance(known_ids, (set, list, tuple)) else None

    cleaned: dict[str, str] = {}
    for user_id, alias in value.items():
        if not isinstance(user_id, str) or not user_id:
            continue
        if allowed is not None and user_id not in allowed:
            continue
        if not isinstance(alias, str):
            continue
        stripped = alias.strip()
        if stripped:
            cleaned[user_id] = stripped
    return cleaned


def clamp_cooling_minutes(value: object, default: int, low: int, high: int) -> int:
    """Coerce a rapid-cool duration to a usable whole number of minutes.

    The duration is chosen locally and then sent to a route that changes
    the physical bed, so this refuses to guess. A missing, malformed, or
    non-numeric value falls back to ``default`` rather than to zero,
    because a zero-minute window is a request the server has never been
    asked to honour.

    ``bool`` is rejected explicitly: it subclasses ``int``, so ``True``
    would otherwise be accepted as a one-minute window.

    Out-of-range values are clamped rather than rejected. A slider that
    silently refuses is worse than one that saturates, and both bounds
    are ours rather than the vendor's.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    try:
        minutes = int(round(float(value)))
    except (TypeError, ValueError, OverflowError):
        return default
    return max(low, min(high, minutes))


def redact_identifier_keys(value: object) -> object:
    """Redact UUIDs wherever they appear, preserving container shape.

    Keys and list elements both. Home Assistant's `async_redact_data`
    matches on field NAMES, so a uuid sitting in a plain list is invisible
    to it: `/v1/auth/me` returns `devices` as an array of device ids, and
    the field is called `devices`, not anything identifier-shaped. Neither
    defence looked at it.
    """
    if isinstance(value, list):
        return [
            "**REDACTED**"
            if isinstance(item, str) and _UUID_RE.fullmatch(item)
            else redact_identifier_keys(item)
            for item in value
        ]
    if not isinstance(value, dict):
        return value

    result: dict[object, object] = {}
    redacted_index = 0
    for key, item in value.items():
        safe_key: object = key
        if isinstance(key, str) and _UUID_RE.fullmatch(key):
            while True:
                redacted_index += 1
                candidate = f"**REDACTED_KEY_{redacted_index}**"
                if candidate not in value and candidate not in result:
                    safe_key = candidate
                    break
        result[safe_key] = redact_identifier_keys(item)
    return result


def omit_sensitive_diagnostic_branches(value: object) -> object:
    """Remove raw biometric, schedule, occupancy, and timeline branches."""
    if isinstance(value, list):
        return [omit_sensitive_diagnostic_branches(item) for item in value]
    if not isinstance(value, dict):
        return value
    return {
        key: omit_sensitive_diagnostic_branches(item)
        for key, item in value.items()
        if key not in _SENSITIVE_DIAGNOSTIC_BRANCHES
    }
