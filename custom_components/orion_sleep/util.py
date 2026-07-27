"""Dependency-free helpers for defensive API response handling."""

from __future__ import annotations

import re

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
