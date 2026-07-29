"""Turning a failed Orion call into something a user can read.

Separate module rather than a function in `helpers.py`, and deliberately
so. `helpers.py` imports nothing, which is the only reason the test suite
can load and execute it without Home Assistant installed. Adding a Home
Assistant import there would silently take 500-odd lines of behavioural
coverage on the diagnostics redaction path out of the run.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator

from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError
from orion_sleep_api import OrionApiError, OrionAuthError


@contextlib.asynccontextmanager
async def orion_call(what: str) -> AsyncIterator[None]:
    """Wrap a call into the Orion client.

    Sixteen of the thirty-two call sites into the client had no handler at
    all, or caught only `ValueError`. Between them that is the whole write
    surface: every temperature set, every power toggle, every schedule
    write, every button. A vendor 500 on any of them reached the user as a
    raw traceback instead of a sentence.

    `ValueError` is caught here because the client raises it bare for
    input validation. Handling that at each call site meant an incidental
    `ValueError` from a bug inside the client was reported to the user as
    a complaint about something they typed.

    `what` completes the sentence "Orion could not ...".
    """
    try:
        yield
    except ValueError as err:
        raise HomeAssistantError(str(err)) from err
    except OrionAuthError as err:
        # MUST stay ahead of the `OrionApiError` arm below. `OrionAuthError`
        # SUBCLASSES `OrionApiError`, so ordering these the other way round
        # makes this branch unreachable and silently reintroduces the bug.
        #
        # The bug: every write in this integration funnels through here, and
        # a flat `OrionApiError` arm turned an expired refresh token into
        # "Orion could not save that schedule change". That reads like the
        # vendor rejected the value. The account is simply logged out, and
        # the fix is to sign in again, which the user is never told.
        # `ConfigEntryAuthFailed` is the type Home Assistant recognises as
        # "this entry needs new credentials", and it is what every one of
        # the dozen `OrionAuthError` handlers in `coordinator.py` already
        # raises. This module was the one place that flattened them.
        #
        # Be precise about what this does NOT do. Home Assistant starts a
        # reauth flow from `ConfigEntryAuthFailed` in exactly two places,
        # `ConfigEntry.async_setup` and `DataUpdateCoordinator._async_refresh`.
        # Neither is on the path of an entity service call or an entity
        # write, so raising this here does not by itself pop the reauth
        # card. The prompt still arrives, from the coordinator's next poll
        # hitting the same dead token. What this buys is the correct type
        # today, a message that names the real problem, and a write path
        # that stays correct if any of these ever move under a coordinator
        # method.
        raise ConfigEntryAuthFailed(
            f"Orion rejected the stored credentials while trying to {what}. "
            f"Sign in again to continue: {err}"
        ) from err
    except OrionApiError as err:
        raise HomeAssistantError(f"Orion could not {what}: {err}") from err
