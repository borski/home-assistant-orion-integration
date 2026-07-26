"""Tests for dependency-free utility helpers."""

import importlib.util
from pathlib import Path

MODULE_PATH = (
    Path(__file__).parent.parent / "custom_components" / "orion_sleep" / "util.py"
)
SPEC = importlib.util.spec_from_file_location("orion_util", MODULE_PATH)
assert SPEC and SPEC.loader
util = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(util)


def test_dedupe_devices_keeps_first_and_preserves_order():
    devices = [
        {"id": "a", "name": "first"},
        {"id": "b", "name": "other"},
        {"id": "a", "name": "second"},
    ]
    assert [item["name"] for item in util.dedupe_devices_by_id(devices)] == [
        "first",
        "other",
    ]


def test_dedupe_devices_handles_malformed_values():
    devices = [{"id": "a"}, "bad", None, {"id": "a"}, {"name": "no-id"}]
    assert util.dedupe_devices_by_id(devices) == [
        {"id": "a"},
        {"name": "no-id"},
    ]
    assert util.dedupe_devices_by_id(None) == []


INSIGHTS = {
    "2026-05-28": {
        "sessions": [
            {"session_id": "s1", "zone_id": "zone_a"},
            {"session_id": "s2", "zone_id": "zone_b"},
        ]
    },
    "2026-05-29": {"sessions": [{"session_id": "s3", "zone_id": "zone_a"}]},
}


def test_latest_session_for_zone_uses_newest_match():
    assert util.latest_session_for_zone(INSIGHTS, "zone_a")["session_id"] == "s3"
    assert util.latest_session_for_zone(INSIGHTS, "zone_b")["session_id"] == "s2"
    assert util.latest_session_for_zone(INSIGHTS, "zone_c") is None


def test_latest_session_for_zone_handles_malformed_values():
    assert util.latest_session_for_zone(None, "zone_a") is None
    assert util.latest_session_for_zone({"date": {"sessions": "bad"}}, "zone_a") is None


def test_latest_session_uses_newest_valid_session():
    assert util.latest_session(INSIGHTS)["session_id"] == "s3"
    assert util.latest_session(None) is None
    assert util.latest_session({"date": {"sessions": [None, "bad"]}}) is None


def test_shared_device_serials_uses_physical_identity():
    primary = [
        {"id": "primary-a", "serial_number": "SERIAL-A"},
        {"id": "primary-b", "serial_number": "SERIAL-B"},
    ]
    partner = [
        {"id": "different-uuid", "serial_number": "SERIAL-A"},
        {"id": "partner-c", "serial_number": "SERIAL-C"},
    ]
    assert util.shared_device_serials(primary, partner) == {"SERIAL-A"}
    assert util.shared_device_serials(None, partner) == set()
