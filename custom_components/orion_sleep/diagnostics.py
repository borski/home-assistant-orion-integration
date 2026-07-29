"""Diagnostics support for Orion Sleep."""

from __future__ import annotations

import time
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import (
    CONF_ACCESS_TOKEN,
    CONF_ACCOUNT_ID,
    CONF_AUTH_VALUE,
    CONF_DEVICE_IDS,
    CONF_DISPLAY_ALIASES,
    CONF_PARTNER_ACCESS_TOKEN,
    CONF_PARTNER_ACCOUNT_ID,
    CONF_PARTNER_AUTH_VALUE,
    CONF_PARTNER_DEVICE_SERIAL,
    CONF_PARTNER_REFRESH_TOKEN,
    CONF_REFRESH_TOKEN,
    CONF_UID_MIGRATION,
    CONF_UID_RECOVERY_ACTIVE,
)
from .coordinator import OrionDataUpdateCoordinator
from .helpers import omit_sensitive_diagnostic_branches, redact_identifier_keys

# Every config-entry key in here is referenced through its constant in
# `const.py` rather than retyped as a literal, and that is the whole
# point rather than a style preference.
#
# `CONF_PARTNER_ACCOUNT_ID` shipped unredacted because this set was a
# list of hand-typed strings sitting in a different file from the
# constants it shadowed. The constant landed in the partner-identity wave
# with a comment saying it is "the same KIND of thing as
# CONF_ACCOUNT_ID", and the line that acts on that reasoning was never
# added. Neither existing defence caught it: `async_redact_data` is exact
# key match and the key was not listed, and `redact_identifier_keys` only
# reaches a UUID used as a mapping key or a list element, never one
# sitting in a dict VALUE. So a diagnostics download published the
# partner's Orion user id in full while the primary's identical field
# came out `**REDACTED**`.
#
# That id is `partner_profile["id"]`, which is the exact required input
# to `remove_user_access`, `update_user_phone` and `assign_zones`. Those
# three are admin-gated precisely because that id is the key to revoking
# somebody's bed access and rewriting the phone number the vendor sends
# login codes to. Publishing one half of a couple's identifiers while
# redacting the other is not a partial fix. It is a targeted leak, and
# the person it targets is the one who did not set the entry up.
#
# `tests_ha/test_diagnostics_redaction_real.py` now asserts that every
# credential-shaped or identifier-shaped `CONF_*` constant appears here,
# so the next one fails CI the moment it lands rather than three waves
# later.
TO_REDACT = {
    CONF_ACCESS_TOKEN,
    CONF_REFRESH_TOKEN,
    "email",
    "phone",
    "phone_number",
    CONF_AUTH_VALUE,
    "user_id",
    "session_id",
    "id",
    # Stable identifiers that survived because only `id` and `user_id`
    # were listed. `redact_identifier_keys` catches UUIDs used as mapping
    # KEYS, which is a different thing from a UUID sitting in a field.
    #
    # `zone_id` is deliberately NOT here: zones are "zone_a" and "zone_b",
    # not UUIDs, so redacting them would make a zone-level bug report
    # unreadable and protect nothing.
    "device_id",
    "workflow_id",
    "invite_id",
    # The aliases are user-typed household names. `redact_identifier_keys`
    # scrubs the uuid KEYS of this map and leaves the names sitting in the
    # values, which is the same personal data `name` and `first_name`
    # above are already redacted for.
    CONF_DISPLAY_ALIASES,
    "name",
    "first_name",
    "last_name",
    "firstName",
    "lastName",
    "serial_number",
    "intercom_jwt",
    "dob",
    "birth_date",
    "date_of_birth",
    "profile_image_url",
    "activation_code",
    "age",
    "gender",
    "height",
    "height_unit",
    "weight",
    "weight_unit",
    "sex",
    # Partner credentials and account identifiers.
    CONF_PARTNER_ACCESS_TOKEN,
    CONF_PARTNER_REFRESH_TOKEN,
    CONF_PARTNER_AUTH_VALUE,
    CONF_PARTNER_DEVICE_SERIAL,
    # The partner's Orion user id, recorded when the partner is linked so
    # a later profile response can be checked against it. This is the
    # regression the whole comment above is about. It is the partner's
    # half of the pair whose primary equivalent, `CONF_ACCOUNT_ID`, has
    # been redacted since the field existed.
    CONF_PARTNER_ACCOUNT_ID,
    # Internal migration state embeds complete account and device UUIDs
    # inside longer unique-id strings. UUID-value redaction cannot see
    # those substrings, so omit the fields as a whole.
    CONF_DEVICE_IDS,
    CONF_ACCOUNT_ID,
    CONF_UID_MIGRATION,
    CONF_UID_RECOVERY_ACTIVE,
    # Network PII from the live-device WS payload.
    "ip",
    "mac",
    # SSID (appears as `name` inside status.network but redacted above too).
}


def _redact(value: Any) -> Any:
    """Redact sensitive values and identifiers used as mapping keys."""
    without_health_data = omit_sensitive_diagnostic_branches(value)
    return redact_identifier_keys(async_redact_data(without_health_data, TO_REDACT))


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    coordinator: OrionDataUpdateCoordinator = entry.runtime_data

    now_monotonic = time.monotonic()
    # Use a list of objects rather than a dict keyed by serial, so the
    # async_redact_data call below scrubs the serial_number field.
    websocket_summary: list[dict[str, Any]] = []
    for device in coordinator.devices:
        serial = device.get("serial_number")
        if not serial:
            continue
        last_at = coordinator.ws_last_message_at(serial)
        age = (now_monotonic - last_at) if last_at else None
        websocket_summary.append(
            {
                "serial_number": serial,
                "state": coordinator.ws_state(serial),
                "seconds_since_last_message": age,
            }
        )

    return {
        "config_entry_data": _redact(dict(entry.data)),
        "config_entry_options": _redact(dict(entry.options)),
        "coordinator_data": _redact(coordinator.data or {}),
        "devices": _redact(coordinator.devices),
        "live_devices": _redact(dict(coordinator.live_devices)),
        "user": _redact(coordinator.user),
        "websocket": _redact(websocket_summary),
    }
