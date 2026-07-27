"""Diagnostics support for Orion Sleep."""

from __future__ import annotations

import time
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .coordinator import OrionDataUpdateCoordinator
from .util import omit_sensitive_diagnostic_branches, redact_identifier_keys

TO_REDACT = {
    "access_token",
    "refresh_token",
    "email",
    "phone",
    "auth_value",
    "user_id",
    "session_id",
    "id",
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
    "partner_access_token",
    "partner_refresh_token",
    "partner_auth_value",
    "partner_device_serial",
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
