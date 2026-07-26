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


def led_brightness(live: dict | None) -> int | None:
    """Return front-panel LED brightness from zero to one hundred."""
    value = live.get("led_brightness") if isinstance(live, dict) else None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value)
