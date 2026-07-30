"""API key as a third auth method in the config flow.

An Orion API key (os_live_...) authenticates directly, so the flow skips
the send-code / verify-code round trip entirely: pick "API key", paste the
key, the flow validates it against /v1/auth/me and creates the entry. A
bad key is rejected rather than accepted.

The entry that results is key-shaped: the key is stored under CONF_API_KEY
and as CONF_ACCESS_TOKEN so the client can send it, with NO refresh token
or expiry, because a key never rotates.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

from homeassistant.data_entry_flow import FlowResultType

from custom_components.orion_sleep.config_flow import (
    AUTH_METHOD_API_KEY,
)
from custom_components.orion_sleep.const import (
    CONF_ACCESS_TOKEN,
    CONF_API_KEY,
    CONF_AUTH_METHOD,
    CONF_EXPIRES_AT,
    CONF_REFRESH_TOKEN,
    DOMAIN,
)
from tests_ha.conftest import ACCOUNT, BED_A, SERIAL_A, device

GOOD_KEY = "os_live_" + "A" * 43


class KeyFlowClient:
    """A flow client that authenticates only when given the good key.

    Mirrors how the real client behaves: a bad key surfaces as an
    OrionAuthError from the first authenticated request.
    """

    def __init__(self, **kwargs: Any) -> None:
        # Capture how the flow constructed us. The API-key path must pass
        # is_api_key=True and the key as the access token.
        self.kwargs = kwargs
        self._key = kwargs.get("access_token")

    def set_token_refresh_callback(self, _cb) -> None:
        return

    async def ensure_valid_token(self) -> None:
        return

    async def get_current_user(self) -> dict[str, Any]:
        from orion_sleep_api import OrionAuthError

        if self._key != GOOD_KEY or not self.kwargs.get("is_api_key"):
            raise OrionAuthError("401")
        return {"id": ACCOUNT, "name": "Alex", "email": "alice@example.com"}

    async def list_devices(self) -> list[dict[str, Any]]:
        return [device(BED_A, SERIAL_A)]


async def _run_key_flow(hass, key: str):
    with patch(
        "custom_components.orion_sleep.config_flow.OrionApiClient",
        side_effect=KeyFlowClient,
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": "user"}
        )
        assert result["step_id"] == "user"
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_AUTH_METHOD: AUTH_METHOD_API_KEY}
        )
        assert result["step_id"] == "api_key", result
        return await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_API_KEY: key}
        )


async def test_a_good_key_creates_a_key_shaped_entry(hass):
    result = await _run_key_flow(hass, GOOD_KEY)
    assert result["type"] == FlowResultType.CREATE_ENTRY, result
    data = result["data"]
    assert data[CONF_AUTH_METHOD] == AUTH_METHOD_API_KEY
    assert data[CONF_API_KEY] == GOOD_KEY
    # Stored as the access token so the client can send it.
    assert data[CONF_ACCESS_TOKEN] == GOOD_KEY
    # And NO OTP-era fields: a key never refreshes.
    assert CONF_REFRESH_TOKEN not in data
    assert CONF_EXPIRES_AT not in data


async def test_the_entry_title_never_contains_the_key(hass):
    result = await _run_key_flow(hass, GOOD_KEY)
    assert result["type"] == FlowResultType.CREATE_ENTRY
    # The title is the account label, never the credential.
    assert "os_live" not in result["title"]
    assert result["title"] == "Orion Sleep (Alex)"


async def test_a_bad_key_is_rejected_not_accepted(hass):
    result = await _run_key_flow(hass, "os_live_" + "B" * 43)
    assert result["type"] == FlowResultType.FORM, result
    assert result["step_id"] == "api_key"
    assert result["errors"] == {"base": "invalid_api_key"}


async def test_the_flow_builds_a_key_mode_client(hass):
    """The client the flow validates with must be in key mode.

    If is_api_key were not passed, the client would try to refresh the
    "token" (the key) on its first expired-looking request, which for a
    key raises. The flow must construct it correctly.
    """
    captured: list[dict[str, Any]] = []

    class Recorder(KeyFlowClient):
        def __init__(self, **kwargs: Any) -> None:
            captured.append(kwargs)
            super().__init__(**kwargs)

    with patch(
        "custom_components.orion_sleep.config_flow.OrionApiClient",
        side_effect=Recorder,
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": "user"}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_AUTH_METHOD: AUTH_METHOD_API_KEY}
        )
        await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_API_KEY: GOOD_KEY}
        )

    assert captured, "the flow never constructed a client"
    key_clients = [c for c in captured if c.get("access_token") == GOOD_KEY]
    assert key_clients, "no client was built with the key as its access token"
    assert all(c.get("is_api_key") for c in key_clients), (
        "the key path built a client without is_api_key=True, so it would "
        "try to refresh the key and fail"
    )
