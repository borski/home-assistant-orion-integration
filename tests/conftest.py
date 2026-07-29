"""Environment guards for the fast suite.

This suite runs under the same interpreter as `tests_ha`, which is
`.venv-ha`. That is deliberate. It is the only environment the repository
knows how to build that has both Home Assistant and `orion_sleep_api`, and
without those two this suite cannot import the integration at all and has
to fall back to parsing it with `ast`.

Sharing the interpreter is not the same as sharing the pytest process.
The two suites stay separate invocations because they cost different
amounts. This one imports modules and calls functions and finishes in
under a second. `tests_ha` builds a real Home Assistant instance per test
and takes about 25 seconds warm, or over two minutes on a cold bytecode
cache.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent

COMMAND = ".venv-ha/bin/python -m pytest -q -p no:homeassistant"

# `real()` in `_orion.py` imports `custom_components.orion_sleep`, which
# only resolves with the repository root on the path. Done here as well as
# in `_orion` so that a test module importing the package directly,
# without going through the helper, works the same way.
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def pytest_configure(config: pytest.Config) -> None:
    """Refuse to run in an interpreter that cannot import the integration.

    THE TRAP THIS CLOSES. `mise.toml` still declares `pipx:pytest`, and
    `mise exec pipx:pytest -- pytest -q` was the documented command for
    this suite until it moved to `.venv-ha`. That pipx interpreter has
    neither Home Assistant nor `orion_sleep_api`, so muscle memory now
    produces a handful of collection errors whose tracebacks point at
    `import homeassistant` inside various test modules. Nothing in that
    output says which interpreter was wrong or which one is right.

    Checked with `find_spec` rather than by importing, because importing
    Home Assistant here would pay its cost before pytest has even decided
    what to collect.
    """
    for package in ("homeassistant", "orion_sleep_api"):
        if importlib.util.find_spec(package) is None:
            raise pytest.UsageError(
                f"`{package}` is not importable in this interpreter, so this "
                "suite cannot import the integration and would report a pile "
                "of collection errors instead. This is what running the old "
                "`mise exec pipx:pytest` command looks like now. Use:\n\n"
                f"    {COMMAND}\n\n"
                "See the Testing section of AGENTS.md to build `.venv-ha`."
            )

    _refuse_the_home_assistant_plugin(config)


def _refuse_the_home_assistant_plugin(config: pytest.Config) -> None:
    """Refuse to run with the Home Assistant pytest plugin loaded.

    THE BUG THIS PREVENTS. `pytest-homeassistant-custom-component` is
    installed in `.venv-ha` because `tests_ha` needs it, and pytest
    autoloads it by entry point. It registers autouse fixtures, including
    an async `enable_event_loop_debug`, against every test in the session.
    Every test in THIS suite is synchronous, so each one dies at setup
    with:

        PytestRemovedIn9Warning: '<test>' requested an async fixture
        'enable_event_loop_debug' with autouse=True, with no plugin or
        hook that handled it.

    Every test in the suite errors, none of them run, and the message
    points at pytest_asyncio internals rather than at the actual cause,
    which is that a plugin meant for the other suite got loaded into this
    one. Measured at the time this was written: 91 errors, 0 passes.
    The fix is `-p no:homeassistant` on the command line. Detecting it
    here turns twenty minutes of reading a stack trace into one line that
    names the flag.
    """
    if config.pluginmanager.has_plugin("homeassistant"):
        raise pytest.UsageError(
            "the pytest-homeassistant-custom-component plugin is loaded, and "
            "its autouse async fixtures make every synchronous test in this "
            "suite error at setup. Re-run with `-p no:homeassistant`. The "
            "documented command is:\n\n"
            f"    {COMMAND}\n"
        )
