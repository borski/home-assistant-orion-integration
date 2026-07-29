"""Shared loaders for the Orion fast test suite.

This suite reaches the integration three ways, in descending order of
how much they can prove.

1. ``real("descriptions")`` imports the actual module out of the actual
   package and hands back the actual objects. Prefer this. It is the only
   technique that can catch a symbol that does not exist, a Home Assistant
   helper that got renamed, or a handler attached to the wrong class.
2. ``load("helpers")`` loads a Home-Assistant-free module straight off
   disk under a private module name. Kept for modules that genuinely have
   no package context to need, because it stays honest about the fact
   that they import nothing.
3. ``tree("coordinator")`` parses a module as source and never imports
   it. Structural only. This still earns its place: it can assert about
   code that no fixture happens to execute, such as a service registered
   on a platform that a given test never builds entities for, and it can
   assert about the ABSENCE of a call, which no import can see.

WHY REAL IMPORTS ARE AFFORDABLE HERE. The suite used to run under pipx's
pytest with neither ``homeassistant`` nor ``orion_sleep_api`` installed,
so option 1 did not exist at all and option 3 had to stand in for it.
It now runs under ``.venv-ha``, which already has both. Importing the
whole integration costs about 3 seconds once per process, essentially all
of it inside ``homeassistant.components.sensor``, which drags in
``websocket_api`` then ``http`` then ``hass_nabucasa`` then ``ssl``. That
is paid once, not per test, and it is measured rather than assumed. See
``AGENTS.md`` for the numbers and the argument.
"""

import ast
import importlib
import importlib.util
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
COMPONENT = REPO_ROOT / "custom_components" / "orion_sleep"

PACKAGE = "custom_components.orion_sleep"

# The command that provides an interpreter these imports can succeed in.
# Named in the error below rather than left to the reader, because the
# failure mode this replaced was a suite that quietly skipped its own best
# tests when run under the wrong interpreter, and a skipped test reports
# the same colour as a passing one.
_HOW_TO_RUN = ".venv-ha/bin/python -m pytest tests -q -p no:homeassistant"


def real(module_name: str):
    """Import one integration module for real, from the real package.

    No stubs, no shims, and deliberately not a
    ``spec_from_file_location`` load either. Loading a module off disk
    under a synthetic name gives it a synthetic identity, so
    ``isinstance`` against a class the production code imported normally
    returns False, and two copies of the same module can disagree about
    module-level state. Going through the package means the objects here
    are the same objects Home Assistant gets.

    This DOES execute the integration's ``__init__.py``. That was measured
    before it was accepted: it costs about 280 ms on top of importing
    ``descriptions`` alone, because both pay the same
    ``homeassistant.components.sensor`` import underneath. Paying it buys
    a real package rather than a hand-built one.
    """
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    try:
        return importlib.import_module(f"{PACKAGE}.{module_name}")
    except ImportError as err:  # pragma: no cover - environment failure
        raise ImportError(
            f"could not import {PACKAGE}.{module_name} ({err}). This suite "
            "needs Home Assistant and orion_sleep_api on the path. Run it "
            f"as: {_HOW_TO_RUN}"
        ) from err


def load(module_name: str):
    """Load one Home-Assistant-free component module by file name.

    Prefer ``real`` unless the point of the test is that the module needs
    no package context. ``helpers.py`` is the case that qualifies, and it
    qualifies on purpose: it defers every Home Assistant import into
    ``TYPE_CHECKING`` or into function bodies, and loading it in isolation
    is what proves that deferral still holds.
    """
    path = COMPONENT / f"{module_name}.py"
    spec = importlib.util.spec_from_file_location(f"orion_{module_name}", path)
    assert spec and spec.loader, f"could not build an import spec for {path}"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def module_names() -> list[str]:
    """Every component module name, ``__init__`` last.

    ``__init__`` sorts last so that a failure in a leaf module is reported
    as that module rather than as the package import that pulled it in.
    """
    names = sorted(p.stem for p in COMPONENT.glob("*.py") if p.stem != "__init__")
    return names + ["__init__"]


def source(module_name: str) -> str:
    """Read a component module as text, without importing it."""
    return (COMPONENT / f"{module_name}.py").read_text()


def tree(module_name: str) -> ast.Module:
    """Parse a component module into an AST, without importing it."""
    return ast.parse(source(module_name), filename=f"{module_name}.py")


def functions(node: ast.AST, name: str):
    """Yield every function or async function with the given name."""
    for child in ast.walk(node):
        if (
            isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
            and child.name == name
        ):
            yield child


def function(node: ast.AST, name: str):
    """Return the single function with the given name, or fail loudly."""
    found = list(functions(node, name))
    assert found, f"{name} not found"
    assert len(found) == 1, f"{name} is defined {len(found)} times"
    return found[0]


def is_docstring(node: ast.AST) -> bool:
    """Whether a statement is a bare string expression, ie a docstring.

    Tests that ban a call by substring need this. Several functions here
    name the very call they exist to avoid, in the docstring explaining
    why they avoid it.
    """
    return (
        isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
    )
