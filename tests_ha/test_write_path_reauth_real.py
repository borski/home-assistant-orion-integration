"""An expired token on a write path asks for a login, not for a retry.

`errors.orion_call` wraps every write in this integration. It used to
catch `OrionApiError` and nothing else, and `OrionAuthError` SUBCLASSES
`OrionApiError`, so an expired refresh token on any write came out as
`HomeAssistantError("Orion could not save that schedule change: ...")`.

That sentence sends the user to the wrong place. It reads as though the
vendor rejected the value they typed. The account is simply logged out,
and the only thing that fixes it is signing in again.
`ConfigEntryAuthFailed` is the type Home Assistant recognises for that,
and it is what all dozen `OrionAuthError` handlers in `coordinator.py`
already raise. This module was the one place that flattened them.

Scope, stated plainly so nobody reads more into these tests than is
there. Home Assistant starts a reauth flow from `ConfigEntryAuthFailed`
in exactly two places, `ConfigEntry.async_setup` and
`DataUpdateCoordinator._async_refresh`. Neither is on the path of an
entity write or an entity service call, so raising the right type here
does not by itself pop the reauth card. The prompt arrives from the
coordinator's next poll hitting the same dead token. What is asserted
below is the type and the ordering, which is what makes the message
correct today and the path correct if any of these ever move under a
coordinator method.
"""

from __future__ import annotations

from datetime import time as dt_time
from typing import Any

import pytest
from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError
from homeassistant.helpers.entity_platform import async_get_platforms
from orion_sleep_api import OrionApiError, OrionAuthError

from custom_components.orion_sleep.const import DOMAIN
from custom_components.orion_sleep.errors import orion_call
from tests_ha.conftest import ACCOUNT, make_entry


def test_the_subclass_relationship_this_all_depends_on():
    """`OrionAuthError` is an `OrionApiError`, which is why order matters.

    Everything else in this file is downstream of this one fact. If the
    vendor library ever makes them siblings, the ordering requirement in
    `orion_call` evaporates and the comment there becomes misleading.
    Cheap to assert, and it names the assumption instead of burying it.

    What breaks this: `OrionAuthError` no longer subclassing
    `OrionApiError` in the `orion_sleep_api` package.
    """
    assert issubclass(OrionAuthError, OrionApiError)
    assert OrionAuthError is not OrionApiError


async def test_an_expired_token_raises_config_entry_auth_failed():
    """The bug itself, at the smallest layer that can hold it.

    Called directly on the context manager rather than through an entity,
    because the entity is not what was broken. Routing this through a
    service call would also pass if `orion_call` were deleted entirely
    and the raw `OrionAuthError` escaped, since `pytest.raises` on a
    subclass would still match. This asserts the conversion.

    What breaks this: removing the `except OrionAuthError` arm from
    `errors.orion_call`, or placing it AFTER the `except OrionApiError`
    arm, which makes it unreachable.
    """
    with pytest.raises(ConfigEntryAuthFailed) as caught:
        async with orion_call("save that schedule change"):
            raise OrionAuthError("refresh token expired", status=401)

    # Not the generic one. `ConfigEntryAuthFailed` subclasses
    # `HomeAssistantError`, so a test that only asserted the base class
    # would have passed against the broken version too.
    assert type(caught.value) is ConfigEntryAuthFailed, type(caught.value)
    assert isinstance(caught.value.__cause__, OrionAuthError)


async def test_a_plain_api_error_is_still_a_plain_home_assistant_error():
    """The other half. Adding the auth arm must not swallow everything.

    An `except OrionAuthError` arm placed correctly changes nothing for a
    vendor 500. Written down because the obvious wrong fix, catching
    `OrionApiError` and branching on `isinstance` inside, is easy to get
    backwards in a way this test notices and the one above does not.

    What breaks this: catching `OrionApiError` and raising
    `ConfigEntryAuthFailed` for all of them, which would prompt for a
    login every time the vendor has a bad day.
    """
    with pytest.raises(HomeAssistantError) as caught:
        async with orion_call("save that schedule change"):
            raise OrionApiError("internal server error", status=500)

    assert not isinstance(caught.value, ConfigEntryAuthFailed), (
        "a vendor 500 now asks the user to sign in again. The auth arm is "
        "catching the base class instead of OrionAuthError."
    )
    assert "Orion could not save that schedule change" in str(caught.value)


async def test_a_value_error_is_still_reported_as_itself():
    """The `ValueError` arm has to keep running first.

    The client raises `ValueError` bare for input validation, and
    `orion_call` surfaces its message unchanged so the user reads what
    they typed wrong. That arm sits above both API arms and stays there.

    What breaks this: reordering the handlers in `errors.orion_call`, or
    wrapping the `ValueError` message in the "Orion could not ..."
    sentence, which would blame the vendor for a local validation failure.
    """
    with pytest.raises(HomeAssistantError) as caught:
        async with orion_call("change the bed orientation"):
            raise ValueError("not a valid orientation")

    assert not isinstance(caught.value, ConfigEntryAuthFailed)
    assert str(caught.value) == "not a valid orientation"


def _seed_schedule(client) -> None:
    """Give the account a schedule row so the time entity is available."""

    async def get_sleep_schedules() -> dict[str, Any]:
        return {
            "today_sleep_schedule": {
                ACCOUNT: {"day": 1, "bedtime": "22:30", "wakeup": "06:30"}
            }
        }

    client.get_sleep_schedules = get_sleep_schedules


async def test_a_real_schedule_write_surfaces_the_auth_failure(hass, patched, client):
    """End to end, through an entity, because the wiring is the other half.

    The unit tests above prove `orion_call` converts. They would keep
    passing if a platform module stopped using it, which is exactly what
    `select.py` and `switch.py` had done. This drives a genuine entity
    write and asserts the type that comes out the far side.

    What breaks this: `OrionScheduleTime.async_set_value` no longer
    wrapping its API call in `orion_call`, or growing its own
    `except OrionApiError` arm that catches the auth error first.
    """
    _seed_schedule(client)

    async def update_schedule_field(**kwargs: Any) -> None:
        raise OrionAuthError("refresh token expired", status=401)

    client.update_schedule_field = update_schedule_field

    entry = make_entry(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    entity = next(
        e
        for platform in async_get_platforms(hass, DOMAIN)
        if platform.domain == "time"
        for e in platform.entities.values()
        if e.available
    )

    with pytest.raises(ConfigEntryAuthFailed):
        await entity.async_set_value(dt_time(23, 15))
