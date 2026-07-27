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


# ── Sleep session deletion ────────────────────────────────────────────
#
# Wire values read from Orion Android v2.4.1. The delete sheet offers
# exactly two reasons, at decompiled lines 1423857 and 1423877, and the
# caller at 1423492 sends them as `{"reason": ...}` in the DELETE body.
#
# `not_real_session` is the one that matters here: it is what the vendor
# sends when the bed logged a night that never happened, which is the
# case this integration exists to clean up.
SESSION_DELETE_REASONS: frozenset[str] = frozenset(
    {"not_real_session", "no_longer_needed"}
)


def validate_session_delete_reason(reason: object) -> str:
    """Return an accepted deletion reason or raise.

    Deliberately an allowlist. This is the only irreversible call in the
    integration, and a reason the server does not recognise is a reason
    to stop rather than to send anything and find out.
    """
    if not isinstance(reason, str) or reason not in SESSION_DELETE_REASONS:
        allowed = ", ".join(sorted(SESSION_DELETE_REASONS))
        raise ValueError(f"reason must be one of: {allowed}")
    return reason


def summarize_sessions(insights_data: object, limit: int = 30) -> list[dict]:
    """Flatten insights into a list a human can pick a session out of.

    Exists because deleting a session needs its id, and an id is the one
    thing this integration has always deliberately kept out of entity
    state. Rather than leak identifiers into attributes that get recorded
    forever, they are produced on demand by a service call.

    Newest first, because the session someone wants to delete is almost
    always the one that just appeared. Sessions still in progress are
    included and flagged rather than hidden: a running session is exactly
    the kind a sleeper might want gone, and silently omitting it would
    look like the service was broken.
    """
    if not isinstance(insights_data, dict):
        return []

    rows: list[dict] = []
    for date_key in sorted(insights_data, reverse=True):
        day = insights_data.get(date_key)
        if not isinstance(day, dict):
            continue
        sessions = day.get("sessions")
        if not isinstance(sessions, list):
            continue
        for session in sessions:
            if not isinstance(session, dict):
                continue
            session_id = session.get("session_id")
            if not isinstance(session_id, str) or not session_id:
                continue
            row: dict = {
                "session_id": session_id,
                "date": date_key,
                "in_progress": session_in_progress(session),
            }
            for src, dest in (
                ("start_time", "start_time"),
                ("end_time", "end_time"),
                ("zone_id", "zone_id"),
            ):
                value = session.get(src)
                if value is not None:
                    row[dest] = value
            summary = session_subsection(session, "sleep_summary")
            asleep = summary.get("time_asleep")
            if isinstance(asleep, (int, float)) and not isinstance(asleep, bool):
                row["minutes_asleep"] = int(round(float(asleep)))
            score = day.get("score")
            if isinstance(score, (int, float)) and not isinstance(score, bool):
                row["day_score"] = score
            # The bed's own "was this you?" prompt. Deliberately reduced
            # to two flags: the raw block carries every sleeper's full
            # name and profile URL, which has no business in a service
            # response that gets pasted into logs and issue reports.
            confirmation = session_subsection(session, "manual_confirmation")
            if confirmation:
                row["needs_confirmation"] = bool(confirmation.get("needs_confirmation"))
                status = confirmation.get("status")
                if isinstance(status, str) and status:
                    row["confirmation_status"] = status
            rows.append(row)
            if len(rows) >= limit:
                return rows
    return rows


def apnea_number(value: object) -> float | None:
    """Coerce one apnea figure to a float, or None if unusable.

    Zero is a real and common answer: a night with no obstructive
    events reports 0, not a missing field. So this cannot use the
    usual falsy check, and bool has to be rejected explicitly or
    ``False`` would sail through as a legitimate zero.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def duration_minutes(start: object, end: object) -> float | None:
    """Minutes between two ISO-8601 instants, or None if unusable.

    Returns None rather than a negative number when the pair is out of
    order, because a negative duration is a data fault and should read
    as unknown instead of quietly plotting below zero.
    """
    first = parse_iso_datetime(start)
    second = parse_iso_datetime(end)
    if first is None or second is None:
        return None
    minutes = (second - first).total_seconds() / 60
    return None if minutes < 0 else minutes


def sleep_efficiency(asleep_minutes: object, in_bed_minutes: object) -> float | None:
    """Percentage of time in bed actually spent asleep.

    Guards three ways. Bool is rejected before the numeric check, a zero
    or negative time in bed cannot divide, and a ratio above 100 is
    treated as a data fault rather than clamped: sleeping longer than
    you were in bed means one of the two figures is wrong, and clamping
    would hide that behind a plausible-looking 100%.
    """
    if isinstance(asleep_minutes, bool) or not isinstance(asleep_minutes, (int, float)):
        return None
    if isinstance(in_bed_minutes, bool) or not isinstance(in_bed_minutes, (int, float)):
        return None
    if in_bed_minutes <= 0 or asleep_minutes < 0:
        return None
    percent = (asleep_minutes / in_bed_minutes) * 100
    return None if percent > 100 else percent


def confidence_percent(value: object) -> float | None:
    """Session confidence as a percentage, or None if out of contract.

    The vendor reports a 0-to-1 float. Anything outside that range means
    the scale changed under us, and silently rescaling it would produce
    a confident-looking number built on a broken assumption.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if value < 0 or value > 1:
        return None
    return value * 100


# ── Session edits ─────────────────────────────────────────────────────
#
# PATCH /v1/sleep-sessions/{id} takes exactly two fields and requires
# both. The server told us so itself: a PATCH with an empty body returns
# 400 with a Zod error naming `fallasleep_timestamp` and
# `wakeup_timestamp` as required. A wrong key is a harmless no-op, but an
# incomplete pair is a rejection, so both always travel together.

SESSION_EDIT_WIRE_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


def session_edit_window(fell_asleep: object, woke_up: object) -> tuple[str, str]:
    """Validate and format the two timestamps a session edit requires.

    Both must be timezone-aware. A naive datetime is refused rather than
    assumed to be local, because guessing wrong silently moves a night by
    hours and the server recomputes every derived metric from it. The
    caller knows the user's timezone. This function does not.

    A window that ends before it starts is refused too. The server might
    well accept it, but nothing good comes of asking.
    """
    for label, value in (("fell_asleep", fell_asleep), ("woke_up", woke_up)):
        if not isinstance(value, _dt_datetime):
            raise ValueError(f"session_edit_window: {label} must be a datetime")
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError(f"session_edit_window: {label} must carry a timezone")
    start = fell_asleep.astimezone(_dt_timezone.utc)
    end = woke_up.astimezone(_dt_timezone.utc)
    if end <= start:
        raise ValueError("session_edit_window: woke_up must be after fell_asleep")
    return (
        start.strftime(SESSION_EDIT_WIRE_FORMAT),
        end.strftime(SESSION_EDIT_WIRE_FORMAT),
    )


# ── Per-session value series ──────────────────────────────────────────

DEVICE_ORIENTATIONS: tuple[str, ...] = ("left", "right")


def series_stats(values: object) -> dict | None:
    """Average, minimum and maximum of a session value series.

    The insights payload reports temperature as a list of a few hundred
    samples with gaps punched out as nulls. A series is not something
    Home Assistant can hold in a state, so this reduces it to the three
    numbers that are worth graphing.

    Nulls and non-numbers are skipped rather than treated as zero: a
    dropout is missing data, and folding it in as zero would drag the
    average toward freezing. Bool is rejected for the usual reason, that
    it would otherwise arrive as a plausible 0 or 1 degrees.

    Returns None when nothing usable survives, so the sensor reads
    unknown instead of inventing a number from an empty night.
    """
    if not isinstance(values, list):
        return None
    clean = [
        float(v)
        for v in values
        if not isinstance(v, bool) and isinstance(v, (int, float))
    ]
    if not clean:
        return None
    return {
        "average": round(sum(clean) / len(clean), 2),
        "min": min(clean),
        "max": max(clean),
        "samples": len(clean),
    }


def validate_device_orientation(value: object) -> str:
    """Return a valid orientation or raise ValueError.

    The device reports a single orientation for the whole bed, not one
    per sleeper, and the vendor app treats changing it as the fix for
    insights landing on the wrong side.
    """
    if not isinstance(value, str) or value not in DEVICE_ORIENTATIONS:
        raise ValueError(
            f"orientation must be one of {sorted(DEVICE_ORIENTATIONS)}, got {value!r}"
        )
    return value


# ── User access management ────────────────────────────────────────────
#
# The app shows five role labels (owner, admin, member, guest, other) but
# the invite route accepts only two. "Invite as Member" sends `admin` on
# the wire. Keeping the user-facing word and the wire value in one table
# stops that gap turning into a silent 400, the same way
# `device_led_brightness` versus `led_brightness` did.

INVITE_ROLES = ("member", "guest")

_INVITE_ROLE_WIRE = {"member": "admin", "guest": "guest"}


def invite_role_wire(role: object) -> str:
    """Map a user-facing role to the value the invite route accepts."""
    if not isinstance(role, str) or role.strip().lower() not in _INVITE_ROLE_WIRE:
        raise ValueError(
            f"role must be one of {', '.join(INVITE_ROLES)}, got {role!r}"
        )
    return _INVITE_ROLE_WIRE[role.strip().lower()]


def normalize_phone(value: object) -> str:
    """Strip formatting from a phone number and sanity-check the length.

    Deliberately permissive about country. Orion is US-only today but the
    invite screen carries a country picker, so rejecting anything that is
    not eleven digits would break the moment that changes. Punctuation and
    a leading plus are dropped because the auth route was measured to want
    bare digits.
    """
    if not isinstance(value, str):
        raise ValueError("phone number must be a string")
    digits = "".join(ch for ch in value if ch.isdigit())
    if not 10 <= len(digits) <= 15:
        raise ValueError(
            "phone number must contain between 10 and 15 digits, "
            f"got {len(digits)}"
        )
    return digits


def access_role(entry: object) -> tuple[str, object]:
    """Pull the role and expiry out of one `shared_with` entry.

    MEASURED 2026-07-27: `access` is an object, not a string. It carries
    `role`, `expiry` (null for permanent access), and `allowed_actions`.
    The key name reads like a role and it is not one, which is worth a
    named function rather than an inline `.get`.

    A plain string is still accepted, because guessing the shape wrong
    once already cost a crash and the cheap defence is to handle both.
    """
    if not isinstance(entry, dict):
        return "unknown", None
    access = entry.get("access")
    if isinstance(access, str) and access.strip():
        return access, None
    if not isinstance(access, dict):
        return "unknown", None
    role = access.get("role")
    expiry = access.get("expiry")
    return (
        role if isinstance(role, str) and role.strip() else "unknown",
        expiry if isinstance(expiry, str) else None,
    )


def summarize_access(devices: object, device_id: object = None) -> list[dict]:
    """Who has access to a device, as name/role/id records.

    Reads the role from `shared_with[].access.role` rather than from
    `zones[].user.user_type`, which was measured to be null on every
    zone.

    Profile image URLs are deliberately dropped. The user id is kept
    because revoking access needs it and there is no other way to get one
    without going back to the API.
    """
    if not isinstance(devices, list):
        return []
    people: list[dict] = []
    seen: set[str] = set()
    for device in devices:
        if not isinstance(device, dict):
            continue
        if device_id is not None and device.get("id") != device_id:
            continue
        for entry in device.get("shared_with") or []:
            if not isinstance(entry, dict):
                continue
            user = entry.get("user")
            if not isinstance(user, dict):
                continue
            uid = user.get("id")
            if not isinstance(uid, str) or not uid or uid in seen:
                continue
            seen.add(uid)
            role, expiry = access_role(entry)
            people.append(
                {
                    "name": orion_user_label(user) or f"User {uid[:8]}",
                    "role": role,
                    "user_id": uid,
                    "away": bool(entry.get("is_away")),
                    "expires": expiry,
                }
            )
    return sorted(people, key=lambda p: (p["role"], p["name"]))


# ── Account configuration ─────────────────────────────────────────────

# The scale the Orion app displays. `relative` is the -10 to +10 offset
# ladder this integration exposes as number entities; `fahrenheit` is the
# absolute scale. Both lookup tables ship on the device, so this changes
# what the app shows rather than what the bed does.
TEMPERATURE_DISPLAY_UNITS = ("relative", "fahrenheit")

# Whether the two halves of the bed are driven as one. MEASURED as
# `combined` on a two-person bed; `split` is what the app's Split Zones
# action produces.
ZONE_SPLIT_MODES = ("combined", "split")
