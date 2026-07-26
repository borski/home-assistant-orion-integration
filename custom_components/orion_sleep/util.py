"""Dependency-free helpers for defensive API response handling."""

from __future__ import annotations


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
