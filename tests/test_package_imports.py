"""Every module imports, and every symbol it names actually exists.

These two checks used to live as inline `python - <<'PY'` heredocs in the
`runtime` job of `.github/workflows/verify.yml`. They are tests, so they
now live where tests live.

WHY MOVING THEM MATTERS RATHER THAN BEING TIDINESS. A check embedded in a
YAML heredoc cannot be run locally without copying it out of the YAML, is
not counted in any total, has no name when it fails, and cannot be
extended without editing CI config. Nobody ran these before pushing. Both
guard against the failure mode that got the `runtime` job written in the
first place, which is that the fast suite could not import the
integration and so scored a missing symbol as a pass.

Now that the fast suite CAN import the integration, the natural home for
both is here, where they run in under a second alongside everything else.
"""

from __future__ import annotations

import ast
import importlib

import _orion
import pytest

MODULE_NAMES = _orion.module_names()


@pytest.mark.parametrize("module_name", MODULE_NAMES)
def test_every_component_module_imports(module_name):
    """A module that raises on import takes the whole integration down.

    Home Assistant imports a custom component's `__init__.py` in the
    import executor and then imports each declared platform. A NameError
    or a bad `from homeassistant... import` in any one of them fails
    setup for the entire integration, and the user sees "Error setting up
    entry" with the real cause several lines down a log they have to go
    find.

    Parametrised per module so the failure names the module rather than
    naming the first import in a loop.
    """
    _orion.real(module_name)


def test_every_symbol_imported_from_the_client_exists():
    """The integration and its client are versioned separately.

    `orion_sleep_api` is pinned in `manifest.json` and ships from its own
    repository, so a symbol can be renamed or removed there while every
    call site here still reads fine, compiles fine, and lints fine. The
    failure surfaces at integration load in somebody's house.

    Checked by parsing rather than by importing so that the message names
    the file and the symbol. An ImportError would name only the first one
    and would stop there.
    """
    client = importlib.import_module("orion_sleep_api")
    missing = []
    for name in MODULE_NAMES:
        source = _orion.source(name)
        for node in ast.walk(ast.parse(source, filename=f"{name}.py")):
            if not isinstance(node, ast.ImportFrom):
                continue
            if node.module != "orion_sleep_api":
                continue
            for alias in node.names:
                if not hasattr(client, alias.name):
                    missing.append(f"{name}.py imports {alias.name}")
    assert missing == [], (
        "these names are imported from orion_sleep_api and the installed "
        f"client does not export them: {missing}"
    )


def test_the_pinned_client_version_is_the_installed_one():
    """A stale local install makes every other test here lie.

    Everything in this suite that touches the client is only meaningful
    against the version the integration actually ships with.
    `manifest.json` is the pin Home Assistant installs from, so a
    developer environment holding a different build is testing something
    no user will ever run.
    """
    import json
    from importlib.metadata import version

    manifest = json.loads((_orion.COMPONENT / "manifest.json").read_text())
    requirements = manifest.get("requirements", [])
    pinned = [r for r in requirements if r.startswith("orion-sleep-api")]
    assert pinned, f"manifest.json no longer pins the client: {requirements}"

    assert "==" in pinned[0], (
        "the client requirement is not pinned to an exact version, so two "
        f"machines can resolve different builds: {pinned[0]}"
    )
    wanted = pinned[0].split("==", 1)[1]
    installed = version("orion-sleep-api")
    assert installed == wanted, (
        f"manifest.json pins orion-sleep-api=={wanted} and the environment "
        f"has {installed}. Every client-facing assertion in this suite is "
        "running against the wrong build."
    )
