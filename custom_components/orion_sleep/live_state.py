"""Pure helpers for reading Orion live-device state."""

from __future__ import annotations


def _find_zone(zones: object, zone_id: str) -> dict | None:
    if not isinstance(zones, list):
        return None
    for zone in zones:
        if isinstance(zone, dict) and zone.get("id") == zone_id:
            return zone
    return None


def _numeric(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def zone_setpoint(live: dict | None, zone_id: str) -> float | None:
    """Return a zone target temperature in Celsius."""
    if not isinstance(live, dict):
        return None
    zone = _find_zone(live.get("zones"), zone_id)
    return _numeric(zone.get("temp")) if zone else None


def zone_is_on(live: dict | None, zone_id: str) -> bool | None:
    """Return a zone power state when present and correctly typed."""
    if not isinstance(live, dict):
        return None
    zone = _find_zone(live.get("zones"), zone_id)
    if not zone:
        return None
    value = zone.get("on")
    return value if isinstance(value, bool) else None


def zone_measured_temp(live: dict | None, zone_id: str) -> float | None:
    """Return a zone measured temperature in Celsius."""
    if not isinstance(live, dict):
        return None
    status = live.get("status")
    if not isinstance(status, dict):
        return None
    zone = _find_zone(status.get("zones"), zone_id)
    return _numeric(zone.get("temp")) if zone else None


def zone_thermal_relief(live: dict | None, zone_id: str) -> dict | None:
    """Return a zone's active thermal-relief (hot flash) block, or None.

    Measured shape, read from the app's own consumer at decompiled line
    664358 onward: ``zones[].thermal_relief`` is absent or null when no
    relief is running, and otherwise carries ``end_time`` (Unix ms),
    ``previous_temp`` and ``previous_on`` (the state to restore when it
    expires), and ``type``.

    The app treats relief as active only when ``end_time`` is a finite
    number in the future, so a stale block left behind by the server is
    not mistaken for a running session. Callers should apply the same
    test rather than trusting the block's mere presence.
    """
    if not isinstance(live, dict):
        return None
    zone = _find_zone(live.get("zones"), zone_id)
    if not isinstance(zone, dict):
        return None
    relief = zone.get("thermal_relief")
    return relief if isinstance(relief, dict) else None


def thermal_relief_end_ms(relief: object) -> float | None:
    """Return a thermal-relief end time as a Unix millisecond value."""
    if not isinstance(relief, dict):
        return None
    return _numeric(relief.get("end_time"))


def zone_thermal_state(live: dict | None, zone_id: str) -> str | None:
    """Return the raw thermal state for a zone."""
    if not isinstance(live, dict):
        return None
    status = live.get("status")
    if not isinstance(status, dict):
        return None
    zone = _find_zone(status.get("zones"), zone_id)
    if not zone:
        return None
    state = zone.get("thermal_state")
    return state if isinstance(state, str) else None


def firmware(live: dict | None) -> dict | None:
    """Return the firmware block."""
    status = live.get("status") if isinstance(live, dict) else None
    value = status.get("firmware") if isinstance(status, dict) else None
    return value if isinstance(value, dict) else None


def network_info(live: dict | None) -> dict | None:
    """Return the network diagnostics block."""
    status = live.get("status") if isinstance(live, dict) else None
    value = status.get("network") if isinstance(status, dict) else None
    return value if isinstance(value, dict) else None


def wifi_rssi(live: dict | None) -> int | None:
    """Return Wi-Fi RSSI in dBm."""
    network = network_info(live)
    value = network.get("rssi") if network else None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value)


def safety_error(live: dict | None) -> bool | None:
    """Return whether the device reports a safety error."""
    status = live.get("status") if isinstance(live, dict) else None
    safety = status.get("safety") if isinstance(status, dict) else None
    if not isinstance(safety, dict):
        return None
    if safety.get("error"):
        return True
    codes = safety.get("error_codes")
    return bool(codes) if isinstance(codes, list) else False


def safety_info(live: dict | None) -> dict | None:
    """Return the full safety diagnostics block."""
    status = live.get("status") if isinstance(live, dict) else None
    value = status.get("safety") if isinstance(status, dict) else None
    return value if isinstance(value, dict) else None


def device_online(live: dict | None) -> bool | None:
    """Whether the device reports itself reachable.

    Distinct from the WebSocket connection state, which describes OUR link
    to the server. This is the server's own view of the topper. Returns
    None when unreported so an entity stays unavailable instead of
    claiming the bed is offline.
    """
    if not isinstance(live, dict):
        return None
    status = live.get("status")
    if not isinstance(status, dict):
        return None
    value = status.get("online")
    return value if isinstance(value, bool) else None


def pending_update_available(live: dict | None) -> bool | None:
    """Return whether the device is advertising a firmware update.

    Reads `status.pending_update.is_available`. Returns None when the
    block is missing or wrongly typed, so a device that has never
    reported the field stays unavailable instead of claiming "no update".

    This is what backs the `update` entity's latest_version. Note the
    block has only ever carried `is_available` on this account, never a
    version string, so the entity has to fall back to a placeholder when
    an update is genuinely waiting.
    """
    status = live.get("status") if isinstance(live, dict) else None
    pending = status.get("pending_update") if isinstance(status, dict) else None
    if not isinstance(pending, dict):
        return None
    value = pending.get("is_available")
    return value if isinstance(value, bool) else None


def pending_update_info(live: dict | None) -> dict | None:
    """Return the full pending-update block for entity attributes."""
    status = live.get("status") if isinstance(live, dict) else None
    value = status.get("pending_update") if isinstance(status, dict) else None
    return value if isinstance(value, dict) else None


def firmware_update_info(live: dict | None) -> dict | None:
    """Return the in-flight firmware update block.

    Reads `status.firmware_update`, which carries `in_progress`,
    `current_step`, `result`, `workflow_id` and a few timestamps. Only
    `current_step: "complete"` and `result: "success"` have ever been
    captured, so anything describing a running update is unverified.
    """
    status = live.get("status") if isinstance(live, dict) else None
    value = status.get("firmware_update") if isinstance(status, dict) else None
    return value if isinstance(value, dict) else None


def led_brightness(live: dict | None) -> int | None:
    """Return front-panel LED brightness from zero to one hundred."""
    value = live.get("led_brightness") if isinstance(live, dict) else None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value)
