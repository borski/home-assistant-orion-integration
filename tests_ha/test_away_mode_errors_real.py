"""Away Mode keeps its one tolerated error and stops leaking the rest.

`OrionAwayModeSwitch._set_away` had a legitimate special case and one
defect sitting inside the same handler. The special case: Orion returns
``400 "User has no previous device to return to"`` with error code
``user_already_present`` when told to mark an already-present user as
present, and a redundant toggle from an automation re-asserting state
should not be a hard failure in the UI.

The defect: the `else` arm re-raised the raw `OrionApiError`. That is the
precise failure `errors.orion_call` was written to remove. A vendor 500
on this route reached the user as a traceback instead of a sentence.

The fix nests the special case INSIDE `orion_call` rather than beside it,
because the inner handler needs `err.error_code` and `orion_call` has
already converted the exception by the time it would be readable outside.
That shape is easy to get wrong in a way that silently swallows
everything or silently swallows nothing, so all three outcomes are
asserted here.
"""

from __future__ import annotations

from typing import Any

import pytest
from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError
from homeassistant.helpers.entity_platform import async_get_platforms
from orion_sleep_api import OrionApiError, OrionAuthError

from custom_components.orion_sleep.const import DOMAIN
from tests_ha.conftest import make_entry


async def _away_switch(hass, client):
    """Set the entry up and return the Away Mode switch.

    Away Mode is only created for a single-device account, which the
    default fixture is. If that ever changes the `next()` below raises
    StopIteration rather than passing on an entity that is not the one
    under test.
    """
    entry = make_entry(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    return next(
        e
        for platform in async_get_platforms(hass, DOMAIN)
        if platform.domain == "switch"
        for e in platform.entities.values()
        if e.unique_id.endswith("_away_mode")
    )


async def test_the_already_present_no_op_is_still_swallowed(hass, patched, client):
    """The tolerated case, which the rewrite must not have lost.

    An automation that re-asserts presence calls this on a user who is
    already present, and the server answers 400. Surfacing that would put
    a red error in the UI for a toggle that changed nothing.

    What breaks this: dropping the `error_code` check, or comparing
    against the wrong code string, either of which turns a routine no-op
    back into a visible failure.
    """
    calls: list[dict[str, Any]] = []

    async def set_user_away(**kwargs: Any) -> None:
        calls.append(kwargs)
        raise OrionApiError(
            "User has no previous device to return to",
            status=400,
            error_code="user_already_present",
        )

    client.set_user_away = set_user_away
    switch = await _away_switch(hass, client)

    # No pytest.raises. Returning quietly IS the assertion.
    await switch.async_turn_off()

    assert calls, "set_user_away was never reached"


async def test_any_other_api_error_becomes_a_readable_sentence(hass, patched, client):
    """The defect. A vendor 500 must not escape as a raw OrionApiError.

    What breaks this: restoring the bare `raise` in the else arm, or
    moving the `async with orion_call(...)` back outside so it no longer
    wraps the re-raise.
    """
    async def set_user_away(**kwargs: Any) -> None:
        raise OrionApiError("internal server error", status=500)

    client.set_user_away = set_user_away
    switch = await _away_switch(hass, client)

    with pytest.raises(HomeAssistantError) as caught:
        await switch.async_turn_on()

    assert not isinstance(caught.value, OrionApiError), (
        "the raw vendor exception escaped again, which is a traceback in "
        "the Home Assistant UI rather than a sentence"
    )
    assert "Orion could not change away mode" in str(caught.value)


async def test_an_expired_token_here_also_asks_for_a_login(hass, patched, client):
    """Nesting inside `orion_call` buys this arm for free, so check it.

    The old hand-rolled handler caught `OrionApiError`, and
    `OrionAuthError` subclasses it, so an expired token on this route was
    re-raised raw. Routing through `orion_call` picks up its auth arm.

    What breaks this: catching `OrionAuthError` in the inner handler, or
    going back to a hand-rolled `except OrionApiError` outside
    `orion_call`.
    """
    async def set_user_away(**kwargs: Any) -> None:
        raise OrionAuthError("refresh token expired", status=401)

    client.set_user_away = set_user_away
    switch = await _away_switch(hass, client)

    with pytest.raises(ConfigEntryAuthFailed):
        await switch.async_turn_on()
