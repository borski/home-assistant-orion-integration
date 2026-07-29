"""Fixtures for tests that run against a real Home Assistant.

Deliberately separate from `tests/`. That suite parses the component with
`ast` and never imports Home Assistant, which keeps it fast and lets it
assert things a behavioural test cannot. It also cannot catch a
behavioural regression, and four rounds of review found defects it was
structurally blind to. This directory exists for those.

`tests/test_migrations.py` installs fake `homeassistant` modules into
`sys.modules` and never removes them, so the two suites must not share a
pytest process. They are separate invocations on purpose.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.orion_sleep.const import (
    CONF_ACCESS_TOKEN,
    CONF_AUTH_METHOD,
    CONF_AUTH_VALUE,
    CONF_EXPIRES_AT,
    CONF_REFRESH_TOKEN,
    DOMAIN,
)

ACCOUNT = "11111111-1111-4111-8111-111111111111"
PARTNER = "22222222-2222-4222-8222-222222222222"
BED_A = "aaaaaaaa-1111-4111-8111-111111111111"
BED_B = "bbbbbbbb-2222-4222-8222-222222222222"
SERIAL_A = "AA11BB22CC33"
SERIAL_B = "DD44EE55FF66"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Let Home Assistant discover custom_components/orion_sleep."""
    return


def device(device_id: str, serial: str, user_id: str = ACCOUNT) -> dict[str, Any]:
    return {
        "id": device_id,
        "serial_number": serial,
        "name": "Sleepy",
        "model": "OSCT001-1",
        "type": "control_tower",
        "temperature_range": {"min": 10, "max": 45},
        "zones": [
            {"id": "zone_a", "user": {"id": user_id}},
            {"id": "zone_b", "user": {"id": PARTNER}},
        ],
        "default_zone_id": "zone_a",
    }


class FakeClient:
    """Stands in for OrionApiClient with no network.

    Only the methods the coordinator actually calls. Anything it does not
    implement should fail loudly rather than silently returning a Mock,
    which is why this is a real class and not MagicMock.
    """

    def __init__(self, **kwargs: Any) -> None:
        # A display name AND the email, deliberately both. This profile
        # used to carry only the email, and because `orion_user_label`
        # falls through to it, every entity in this suite registered an
        # entity_id with a login credential baked into it. Two tests then
        # asserted on those ids, which turned the leak into the expected
        # result. Keeping the email here means the default path now
        # proves the opposite: that a real name outranks it.
        #
        # The email-only and phone-only accounts are still covered, on
        # purpose, by `test_display_names_real.py`, which reassigns this
        # attribute before the entry is set up.
        self.user: dict[str, Any] = {
            "id": ACCOUNT,
            "name": "Alex",
            "email": "alice@example.com",
        }
        self.devices: list[dict[str, Any]] = [device(BED_A, SERIAL_A)]
        self.fail_devices: Exception | None = None
        self.fail_insights: Exception | None = None
        self.calls: list[str] = []

    def set_token_refresh_callback(self, _cb) -> None:
        return

    async def ensure_valid_token(self) -> None:
        return

    async def get_current_user(self) -> dict[str, Any]:
        self.calls.append("get_current_user")
        return dict(self.user)

    async def list_devices(self) -> list[dict[str, Any]]:
        self.calls.append("list_devices")
        if self.fail_devices is not None:
            raise self.fail_devices
        return [dict(d) for d in self.devices]

    async def get_sleep_schedules(self) -> dict[str, Any]:
        return {"schedules": {}, "today_sleep_schedule": {}}

    async def get_insights(self, days: int = 7, *, expected_user_id=None) -> dict:
        self.calls.append("get_insights")
        if self.fail_insights is not None:
            raise self.fail_insights
        return {"user_id": expected_user_id or ACCOUNT, "data": {}, "overview": {}}

    async def get_live_session(self) -> dict[str, Any]:
        return {}

    async def get_sleep_configurations(self) -> dict[str, Any]:
        return {}

    async def get_live_device(self, serial: str) -> dict[str, Any]:
        # Real zone rows, because several entities are unavailable without
        # them for reasons that have nothing to do with poll health.
        return {
            "serial_number": serial,
            "zones": [
                {"id": "zone_a", "on": True, "temp": 24.0},
                {"id": "zone_b", "on": False, "temp": 22.0},
            ],
            "status": {
                "online": True,
                "sensors": {
                    "sensor1": {"heart_rate": 61, "breath_rate": 14, "status_text": "normal"},
                    "sensor2": {"heart_rate": 0, "breath_rate": 0, "status_text": "empty"},
                },
            },
        }


class FakeWebSocketManager:
    """No sockets. Freshness is driven by the test."""

    def __init__(self, **kwargs: Any) -> None:
        self.serials: list[str] = []
        self._fresh: set[str] = set()

    def sync_to_serials(self, serials: list[str]) -> None:
        self.serials = list(serials)

    def state(self, serial: str) -> str:
        return "connected" if serial in self._fresh else "stopped"

    def is_fresh(self, serial: str) -> bool:
        return serial in self._fresh

    def last_message_at(self, serial: str) -> float:
        return 0.0

    async def async_stop(self) -> None:
        return


@pytest.fixture
def client() -> FakeClient:
    return FakeClient()


@pytest.fixture
def ws_manager() -> FakeWebSocketManager:
    return FakeWebSocketManager()


@pytest.fixture
def patched(client: FakeClient, ws_manager: FakeWebSocketManager):
    """Patch the two things that would otherwise reach the network."""
    with (
        patch(
            "custom_components.orion_sleep.OrionApiClient",
            return_value=client,
        ),
        patch(
            "custom_components.orion_sleep.coordinator.OrionWebSocketManager",
            return_value=ws_manager,
        ),
    ):
        yield


# The default config entry id. Account-scoped entities are keyed on the
# ENTRY rather than on a bed, so tests asserting their unique ids need
# this rather than BED_A.
ENTRY = "entry-1"


def make_entry(hass, *, entry_id=ENTRY, unique_id=ACCOUNT, data=None) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        entry_id=entry_id,
        unique_id=unique_id,
        data={
            CONF_AUTH_METHOD: "email",
            CONF_AUTH_VALUE: "alice@example.com",
            CONF_ACCESS_TOKEN: "at",
            CONF_REFRESH_TOKEN: "rt",
            CONF_EXPIRES_AT: 9e12,
            **(data or {}),
        },
    )
    entry.add_to_hass(hass)
    return entry
