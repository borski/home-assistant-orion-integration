"""Shared loaders for the Orion test suite.

Home Assistant and aiohttp are not installed in the test environment, and
CI runs pytest without them on purpose. So the tests reach the integration
two ways:

1. Dependency-free modules (``util``, ``live_state``) are loaded straight
   off disk with importlib and exercised normally.
2. Modules that import aiohttp or Home Assistant (``api``, ``coordinator``,
   the entity platforms) are never imported. They are parsed as source with
   the stdlib ``ast`` module and asserted against structurally.

The second technique is what catches leak regressions. It cannot check
runtime behaviour, but it can prove that no exception message interpolates
a request path, and that guarantee is worth having in CI.
"""

import ast
import importlib.util
from pathlib import Path

COMPONENT = Path(__file__).parent.parent / "custom_components" / "orion_sleep"


def load(module_name: str):
    """Load one dependency-free component module by file name."""
    path = COMPONENT / f"{module_name}.py"
    spec = importlib.util.spec_from_file_location(f"orion_{module_name}", path)
    assert spec and spec.loader, f"could not build an import spec for {path}"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


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
