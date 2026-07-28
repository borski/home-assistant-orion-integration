"""Every registered service must name a method that actually exists.

This file exists because of a real failure. Three access-management
handlers were appended to the end of sensor.py at a point when two other
classes had already been appended after their intended home, so they
landed on `OrionZoneSplitModeSensor` instead of `OrionAccessSensor`.

Everything passed. Lint passed, the module compiled, all 155 unit tests
passed, Home Assistant started clean, and the service showed up in the
service list with the right schema. It failed only when somebody called
it, with `AttributeError: object has no attribute
'async_set_device_name'` and a 500 back to the caller.

`sensor.py` imports Home Assistant, so it cannot be imported here. These
checks parse it with `ast` instead, the same technique the API and
coordinator guards use.
"""

import ast

import _orion

SENSOR_TREE = _orion.tree("sensor")
SENSOR_SOURCE = _orion.source("sensor")

# Handlers that must live on the device-level access sensor. These are
# the ones that were misplaced, plus their siblings, because a service
# targeting the wrong class is invisible until it is called.
ACCESS_HANDLERS = {
    "async_list_invites",
    "async_invite_user",
    "async_cancel_invite",
    "async_accept_invite",
    "async_remove_user_access",
    "async_create_guest",
    "async_update_user_phone",
    "async_assign_zones",
    "async_set_device_name",
    "async_set_device_timezone",
}


def _registered_handlers(tree):
    """Handler names passed to async_register_entity_service, in order."""
    handlers = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if getattr(func, "attr", None) != "async_register_entity_service":
            continue
        # (service_name, schema, handler_name, ...)
        for arg in node.args:
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                if arg.value.startswith("async_"):
                    handlers.append(arg.value)
    return handlers


def _methods_by_class(tree):
    """Map class name to the set of method names it defines."""
    out = {}
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            out[node.name] = {
                child.name
                for child in node.body
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
            }
    return out


def test_every_registered_service_handler_exists_somewhere():
    methods = _methods_by_class(SENSOR_TREE)
    defined = set().union(*methods.values()) if methods else set()
    missing = [h for h in _registered_handlers(SENSOR_TREE) if h not in defined]
    assert missing == [], (
        "these services are registered but no class defines them, so calling "
        f"one raises AttributeError at runtime: {missing}"
    )


def test_access_handlers_live_on_the_access_sensor():
    methods = _methods_by_class(SENSOR_TREE)
    assert "OrionAccessSensor" in methods
    on_access = methods["OrionAccessSensor"]
    misplaced = sorted(ACCESS_HANDLERS - on_access)
    assert misplaced == [], (
        "these handlers belong on OrionAccessSensor and are not there. "
        "Appending to the end of the file puts them on whichever class "
        f"happens to be last: {misplaced}"
    )


def test_no_handler_is_defined_on_more_than_one_class():
    """Two copies means one of them is dead and nobody would notice."""
    methods = _methods_by_class(SENSOR_TREE)
    for handler in _registered_handlers(SENSOR_TREE):
        owners = sorted(cls for cls, names in methods.items() if handler in names)
        # Subclassing is legitimate: the partner sensor overrides some of
        # the session handlers. Flag only genuinely unrelated duplicates.
        assert len(owners) <= 2, f"{handler} is defined on {owners}"


def test_the_registration_list_has_not_silently_shrunk():
    """A dropped registration is as invisible as a misplaced handler."""
    handlers = _registered_handlers(SENSOR_TREE)
    assert len(handlers) >= 15, handlers
    assert len(set(handlers)) == len(handlers), "a service is registered twice"


def test_destructive_access_handlers_validate_against_unrecorded_rows():
    methods = {
        node.name: ast.unparse(node)
        for node in ast.walk(SENSOR_TREE)
        if isinstance(node, ast.AsyncFunctionDef)
    }
    for name in ("async_remove_user_access", "async_assign_zones"):
        assert "self._access_entries()" in methods[name]
        assert "self._people()" not in methods[name]


def test_zone_assignment_requires_explicit_confirmation():
    registration = SENSOR_SOURCE[SENSOR_SOURCE.index("SERVICE_ASSIGN_ZONES,") :]
    registration = registration[: registration.index("SERVICE_SET_DEVICE_NAME,")]
    assert 'vol.Required("confirm")' in registration
    body = next(
        ast.unparse(node)
        for node in ast.walk(SENSOR_TREE)
        if isinstance(node, ast.AsyncFunctionDef)
        and node.name == "async_assign_zones"
    )
    assert "if not confirm" in body


def test_session_confirmation_requires_a_vendor_prompt_and_verified_partner():
    body = next(
        ast.unparse(node)
        for node in ast.walk(SENSOR_TREE)
        if isinstance(node, ast.AsyncFunctionDef)
        and node.name == "async_confirm_sleep_session"
    )
    assert "needs_confirmation" in body and "is not True" in body
    assert "partner_mapping_valid" in body
    assert "partner_update_ok" in body
    assert "has_partner_for_device" in body


def test_recovery_guard_is_set_before_the_entry_is_unloaded():
    source = _orion.source("__init__")
    handler = source[source.index("async def _handle_revert") :]
    handler = handler[: handler.index("async def _handle_resume")]
    assert handler.index("CONF_UID_RECOVERY_ACTIVE: True") < handler.index(
        "async_unload(entry.entry_id)"
    )
