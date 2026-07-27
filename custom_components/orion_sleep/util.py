"""Dependency-free helpers for defensive API response handling."""

from __future__ import annotations

import re
from datetime import datetime as _dt_datetime
from datetime import time as _dt_time
from datetime import timezone as _dt_timezone

_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
_USER_ALREADY_PRESENT_ERROR = "User has no previous device to return to"
_SAFE_API_ERROR_CODES = frozenset({"device_not_found", "invalid_action_type", "invalid_request"})
_SENSITIVE_DIAGNOSTIC_BRANCHES = frozenset(
    {
        "insights",
        "partner_insights",
        "recommendations",
        "schedules",
        "sensors",
        "timeline",
        "today_sleep_schedule",
    }
)

# Fields writable through PUT /v1/sleep-schedules, grouped by value type
# because each group validates differently.
#
# Temperatures are MEASURED. Of the rest, only `wakeup` has been executed
# against the live server (2026-07-26). The others are APP-DERIVED: the
# vendor app sends them through the identical request builder, but they
# have not been individually confirmed. See the Verification Log in
# AGENTS.md.
SCHEDULE_TEMPERATURE_FIELDS = frozenset(
    {"bedtime_temp", "phase_1_temp", "phase_2_temp", "wakeup_temp"}
)
SCHEDULE_TIME_FIELDS = frozenset({"bedtime", "wakeup"})
SCHEDULE_FLAG_FIELDS = frozenset(
    {
        "bedtime_is_active",
        "wakeup_is_active",
        "auto_turn_off",
        "is_smart_temperature_active",
    }
)
SCHEDULE_WRITABLE_FIELDS = (
    SCHEDULE_TEMPERATURE_FIELDS | SCHEDULE_TIME_FIELDS | SCHEDULE_FLAG_FIELDS
)

# 24-hour wall clock. Schedule times carry no date and no timezone; the
# device reports its own `timezone` separately.
_SCHEDULE_TIME_RE = re.compile(r"^(?:[01][0-9]|2[0-3]):[0-5][0-9]$")


def validate_schedule_write(day: object, field: object, value: object) -> None:
    """Raise ValueError unless this is a well-formed schedule write.

    Lives here rather than in api.py so it is reachable without aiohttp,
    which is the whole reason this module exists. A bad schedule write is
    worth catching before it reaches the wire: the API returns 200 for
    shapes it then silently ignores, so a type error would look like a
    successful no-op rather than a failure.
    """
    if field not in SCHEDULE_WRITABLE_FIELDS:
        raise ValueError(f"Unsupported Orion schedule field: {field!r}")
    if not isinstance(day, int) or isinstance(day, bool) or day not in range(7):
        raise ValueError(f"Orion schedule day must be 0 through 6, got {day!r}")

    if field in SCHEDULE_FLAG_FIELDS:
        if not isinstance(value, bool):
            raise ValueError(f"Orion schedule field {field} requires a bool, got {value!r}")
    elif field in SCHEDULE_TIME_FIELDS:
        if not isinstance(value, str) or not _SCHEDULE_TIME_RE.match(value):
            raise ValueError(
                f"Orion schedule field {field} requires an HH:mm string, got {value!r}"
            )
    # bool is a subclass of int, so reject it before the numeric check.
    elif isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"Orion schedule field {field} requires a number, got {value!r}")


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


def parse_schedule_time(value: object) -> _dt_time | None:
    """Parse the API's ``HH:mm`` wall clock into a ``datetime.time``.

    Returns None rather than raising on anything malformed, so a vendor
    response that drifts leaves an entity unavailable instead of taking
    down a platform setup.
    """
    if not isinstance(value, str) or not _SCHEDULE_TIME_RE.match(value):
        return None
    hour, minute = (int(part) for part in value.split(":"))
    return _dt_time(hour=hour, minute=minute)


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


def dedupe_devices_by_id(devices: object) -> list[dict]:
    """Remove duplicate device ids while preserving response order."""
    if not isinstance(devices, list):
        return []

    seen: set[object] = set()
    result: list[dict] = []
    for device in devices:
        if not isinstance(device, dict):
            continue
        device_id = device.get("id")
        if device_id is not None:
            if device_id in seen:
                continue
            seen.add(device_id)
        result.append(device)
    return result


def latest_session_for_zone(insights_data: object, zone_id: str) -> dict | None:
    """Return the newest insights session matching a zone."""
    if not isinstance(insights_data, dict):
        return None

    for date_key in sorted(insights_data, reverse=True):
        day = insights_data.get(date_key)
        if not isinstance(day, dict):
            continue
        sessions = day.get("sessions")
        if not isinstance(sessions, list):
            continue
        for session in reversed(sessions):
            if isinstance(session, dict) and session.get("zone_id") == zone_id:
                return session
    return None


def latest_session(insights_data: object) -> dict | None:
    """Return the newest session from an insights data mapping."""
    if not isinstance(insights_data, dict):
        return None
    for date_key in sorted(insights_data, reverse=True):
        day = insights_data.get(date_key)
        if not isinstance(day, dict):
            continue
        sessions = day.get("sessions")
        if not isinstance(sessions, list):
            continue
        for session in reversed(sessions):
            if isinstance(session, dict):
                return session
    return None


def session_in_progress(session: object) -> bool:
    """Whether a session is still running.

    Only an explicit ``True`` counts. A missing or malformed flag is read
    as finished, because the alternative is hiding a completed night
    behind a field the vendor forgot to send.
    """
    return isinstance(session, dict) and session.get("is_in_progress") is True


def latest_completed_session(insights_data: object) -> dict | None:
    """Return the newest session that has actually finished.

    Deliberately NOT ``latest_session`` plus a filter on ``end_time``.
    Measured 2026-07-26: the vendor populates ``end_time`` while
    ``is_in_progress`` is still ``True``, so an end-time check would
    happily report a night that is currently being slept as complete.
    ``is_in_progress`` is the only trustworthy discriminator.
    """
    if not isinstance(insights_data, dict):
        return None
    for date_key in sorted(insights_data, reverse=True):
        day = insights_data.get(date_key)
        if not isinstance(day, dict):
            continue
        sessions = day.get("sessions")
        if not isinstance(sessions, list):
            continue
        for session in reversed(sessions):
            if isinstance(session, dict) and not session_in_progress(session):
                return session
    return None


def shared_device_serials(primary_devices: object, partner_devices: object) -> set[str]:
    """Return physical device serials visible to both accounts."""
    primary = (
        {
            device.get("serial_number")
            for device in primary_devices
            if isinstance(device, dict) and device.get("serial_number")
        }
        if isinstance(primary_devices, list)
        else set()
    )
    partner = (
        {
            device.get("serial_number")
            for device in partner_devices
            if isinstance(device, dict) and device.get("serial_number")
        }
        if isinstance(partner_devices, list)
        else set()
    )
    return primary & partner


def user_is_away(device: object, user_id: str) -> bool | None:
    """Return whether one user is absent from every assigned device zone."""
    if not isinstance(device, dict) or not user_id:
        return None
    zones = device.get("zones")
    if not isinstance(zones, list) or not zones:
        return None
    saw_valid_zone = False
    for zone in zones:
        if not isinstance(zone, dict):
            return None
        saw_valid_zone = True
        user = zone.get("user")
        if user is None:
            continue
        if not isinstance(user, dict) or not user.get("id"):
            return None
        if user["id"] == user_id:
            return False
    return True if saw_valid_zone else None


def redact_identifier_keys(value: object) -> object:
    """Redact UUIDs used as mapping keys while preserving container shape."""
    if isinstance(value, list):
        return [redact_identifier_keys(item) for item in value]
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


def safe_api_error_code(value: object) -> str | None:
    """Return a non-sensitive API error identifier, never a free-form message."""
    if not isinstance(value, dict):
        return None
    candidates = (value.get("error"), value.get("code"), value.get("message"))
    if _USER_ALREADY_PRESENT_ERROR in candidates:
        return "user_already_present"
    for candidate in candidates:
        if isinstance(candidate, str) and candidate in _SAFE_API_ERROR_CODES:
            return candidate
    return None


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


def session_subsection(session: object, key: str) -> dict:
    """Return one nested block of an insights session, or ``{}``.

    Every caller immediately does ``.get(...)`` on the result, so a
    vendor-supplied list here would raise AttributeError deep inside a
    value_fn lambda.
    """
    if not isinstance(session, dict):
        return {}
    block = session.get(key)
    return block if isinstance(block, dict) else {}


def auth_tokens_from_session(session: object) -> dict | None:
    """Return ``{access_token, refresh_token, expires_at}`` or ``None``.

    BOTH tokens are required. The previous code validated only
    ``access_token`` and then subscripted ``session["refresh_token"]``,
    so a successful-but-malformed auth response raised KeyError. KeyError
    is not an OrionApiError, so it bypassed every handler in the
    coordinator and surfaced as an unhandled integration crash.
    """
    if not isinstance(session, dict):
        return None
    access = session.get("access_token")
    refresh = session.get("refresh_token")
    if not isinstance(access, str) or not access:
        return None
    if not isinstance(refresh, str) or not refresh:
        return None
    expires_at = session.get("expires_at", 0)
    if isinstance(expires_at, bool) or not isinstance(expires_at, (int, float)):
        expires_at = 0
    return {
        "access_token": access,
        "refresh_token": refresh,
        "expires_at": expires_at,
    }


def should_refresh_token(
    expires_at: object, now: float, margin_seconds: float = 60
) -> bool:
    """Return True when an access token is expired or about to expire.

    A missing or non-numeric expiry is treated as expired. Refreshing an
    unknown token is cheap. Trusting one is not.
    """
    if isinstance(expires_at, bool) or not isinstance(expires_at, (int, float)):
        return True
    return now + margin_seconds >= expires_at


def orion_user_label(user: object) -> str:
    """Best available human label for one Orion user object.

    Checks both snake_case and camelCase name fields because the vendor
    schema uses both. Returns an empty string when nothing usable is found.
    """
    if not isinstance(user, dict):
        return ""
    for key in ("first_name", "firstName", "name", "email", "phone"):
        value = user.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def collect_known_users(
    devices: object = (), extra_users: object = ()
) -> list[dict[str, str]]:
    """Return deduped ``{"id", "name"}`` records for every visible Orion user.

    ``extra_users`` is walked first so the authenticated and partner account
    objects win over the sparser copies embedded in device zones. Users
    without an id are skipped: the id is the only stable alias key.
    """
    found: dict[str, str] = {}

    def _absorb(user: object) -> None:
        if not isinstance(user, dict):
            return
        user_id = user.get("id")
        if not isinstance(user_id, str) or not user_id:
            return
        label = orion_user_label(user)
        if user_id not in found or (label and not found[user_id]):
            found[user_id] = label

    if isinstance(extra_users, (list, tuple)):
        for user in extra_users:
            _absorb(user)
    if isinstance(devices, list):
        for device in devices:
            if not isinstance(device, dict):
                continue
            zones = device.get("zones")
            if not isinstance(zones, list):
                continue
            for zone in zones:
                if isinstance(zone, dict):
                    _absorb(zone.get("user"))

    return [{"id": user_id, "name": name} for user_id, name in found.items()]


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


def describe_api_error(value: object) -> str:
    """Return a log-safe description of an API error payload.

    Never returns vendor-supplied free text. Recognized errors become a
    stable code. Anything else is reduced to its top-level key names so an
    unexpected failure is still traceable without leaking the payload.
    """
    code = safe_api_error_code(value)
    if code is not None:
        return f"code: {code}"
    if isinstance(value, dict) and value:
        keys = ", ".join(sorted(str(key) for key in value))
        return f"unrecognized error, keys: {keys}"
    return "unrecognized error, no detail"


def auth_session_from_response(value: object, *, allow_top_level: bool = False) -> dict | None:
    """Extract an auth session from verified nested or refresh response shapes."""
    if not isinstance(value, dict):
        return None
    response = value.get("response")
    if isinstance(response, dict):
        session = response.get("session")
        if isinstance(session, dict):
            return session
    if allow_top_level and "access_token" in value:
        return value
    return None


# Timeline labels the device emits on `live_device.update`. Measured values
# only. An unrecognized label is passed through rather than dropped, because
# a new vendor action is worth surfacing even if we cannot name it nicely.
TIMELINE_LABELS: dict[str, str] = {
    "bedtime": "Bedtime",
    "phase_1": "Asleep Phase 1",
    "phase_2": "Asleep Phase 2",
    "wake_up": "Wake Up",
    "turn_off": "Turn Off",
}


def timeline_label(label: object) -> str | None:
    """Human name for a timeline entry label."""
    if not isinstance(label, str) or not label:
        return None
    return TIMELINE_LABELS.get(label, label.replace("_", " ").title())


def parse_iso_datetime(value: object) -> _dt_datetime | None:
    """Parse an ISO 8601 timestamp into an aware datetime, or None.

    The vendor sends UTC with a trailing `Z`. A naive result is treated as
    UTC rather than local time, because assuming local would silently shift
    every scheduled action by the machine's offset.
    """
    if not isinstance(value, str) or not value:
        return None
    text = value.strip()
    if text.endswith(("z", "Z")):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = _dt_datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=_dt_timezone.utc)
    return parsed


def next_timeline_entry(timeline: object, user_id: object, now: object) -> dict | None:
    """The soonest timeline entry for one user that has not happened yet.

    Returns None when the timeline is absent, malformed, belongs to someone
    else, or holds nothing in the future. The timeline arrives only on
    `live_device.update`, never on the snapshot, so an empty result shortly
    after a reconnect is normal rather than an error.
    """
    if not isinstance(timeline, list) or not isinstance(user_id, str) or not user_id:
        return None
    if not isinstance(now, _dt_datetime):
        return None

    upcoming: list[tuple[_dt_datetime, dict]] = []
    for entry in timeline:
        if not isinstance(entry, dict) or entry.get("user_id") != user_id:
            continue
        scheduled = parse_iso_datetime(entry.get("scheduled_time"))
        if scheduled is None or scheduled <= now:
            continue
        upcoming.append((scheduled, entry))

    if not upcoming:
        return None
    return min(upcoming, key=lambda pair: pair[0])[1]


def timeline_target_temps(entry: object) -> dict[str, float]:
    """Per-zone target temperatures carried by a timeline entry's action."""
    if not isinstance(entry, dict):
        return {}
    action = entry.get("action")
    if not isinstance(action, dict):
        return {}
    zones = action.get("zones")
    if not isinstance(zones, list):
        return {}

    targets: dict[str, float] = {}
    for zone in zones:
        if not isinstance(zone, dict):
            continue
        zone_id = zone.get("id")
        temp = zone.get("temp")
        if isinstance(zone_id, str) and zone_id and isinstance(temp, (int, float)):
            if not isinstance(temp, bool):
                targets[zone_id] = float(temp)
    return targets


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
