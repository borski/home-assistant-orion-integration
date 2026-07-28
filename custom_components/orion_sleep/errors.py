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

from homeassistant.exceptions import HomeAssistantError
from orion_sleep_api import OrionApiError


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
    except OrionApiError as err:
        raise HomeAssistantError(f"Orion could not {what}: {err}") from err
