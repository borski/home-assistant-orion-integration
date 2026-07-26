"""Tests for dependency-free live-state helpers."""

import importlib.util
from pathlib import Path

MODULE_PATH = (
    Path(__file__).parent.parent
    / "custom_components"
    / "orion_sleep"
    / "live_state.py"
)
SPEC = importlib.util.spec_from_file_location("orion_live_state", MODULE_PATH)
assert SPEC and SPEC.loader
live_state = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(live_state)

SAMPLE = {
    "zones": [
        {"id": "zone_a", "temp": 21.0, "on": True},
        {"id": "zone_b", "temp": 18.5, "on": False},
    ],
    "led_brightness": 60,
    "status": {
        "firmware": {"cb": "1.2.3", "ib": "4.5.6"},
        "network": {"name": "WiFi", "rssi": -55},
        "safety": {"error": False, "error_codes": []},
        "zones": [
            {"id": "zone_a", "temp": 20.4, "thermal_state": "standby"},
            {"id": "zone_b", "temp": 19.1, "thermal_state": "standby"},
        ],
    },
}


def test_zone_values_use_the_correct_zone_list():
    assert live_state.zone_setpoint(SAMPLE, "zone_a") == 21.0
    assert live_state.zone_measured_temp(SAMPLE, "zone_a") == 20.4
    assert live_state.zone_is_on(SAMPLE, "zone_a") is True
    assert live_state.zone_is_on(SAMPLE, "zone_b") is False
    assert live_state.zone_thermal_state(SAMPLE, "zone_a") == "standby"


def test_zone_values_reject_malformed_data_and_bools():
    malformed = {
        "zones": [{"id": "zone_a", "temp": True, "on": 1}],
        "status": {"zones": [{"id": "zone_a", "temp": False}]},
    }
    assert live_state.zone_setpoint(malformed, "zone_a") is None
    assert live_state.zone_measured_temp(malformed, "zone_a") is None
    assert live_state.zone_is_on(malformed, "zone_a") is None
    assert live_state.zone_setpoint(None, "zone_a") is None


def test_diagnostic_helpers():
    assert live_state.firmware(SAMPLE) == {"cb": "1.2.3", "ib": "4.5.6"}
    assert live_state.network_info(SAMPLE)["name"] == "WiFi"
    assert live_state.wifi_rssi(SAMPLE) == -55
    assert live_state.safety_error(SAMPLE) is False
    assert live_state.led_brightness(SAMPLE) == 60


def test_safety_error_detects_flag_or_codes():
    assert live_state.safety_error({"status": {"safety": {"error": True}}}) is True
    assert live_state.safety_error(
        {"status": {"safety": {"error": False, "error_codes": ["E1"]}}}
    ) is True
