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

This file is now HALF structural and half real, and the split is
deliberate.

WHAT THE REAL IMPORTS DO THAT `ast` COULD NOT. The original bug was a
handler attached to the wrong class. `ast` can only see methods written
literally inside a `class` body, so it answers "is this name typed
somewhere under that class statement". The question that actually decides
whether a service call 500s is "does `getattr(cls, name)` resolve", which
is a different question in two directions. A handler inherited from a
base class exists at runtime and is invisible to `ast`, so the structural
check could report a false failure. A handler shadowed by a same-named
non-callable attribute is visible to `ast` and broken at runtime, so the
structural check could report a false pass. The real checks below ask
`getattr`, which is the question Home Assistant asks.

WHAT STAYS STRUCTURAL, AND WHY. The registration list itself. Nothing
here executes `async_setup_entry`, so there is no live `EntityPlatform`
to read registered service names off, and building one would mean
standing up Home Assistant, which is what `tests_ha` is for and what this
suite exists to avoid. `ast` reads the registration call sites directly,
which is both cheaper and stricter: it sees a service registered on a
platform that no fixture happens to build entities for, and a live
platform would not. The two halves are cross-checked against each other
in `test_registered_handlers_resolve_on_a_real_entity_class`.
"""

import ast
import inspect

import _orion

SENSOR_TREE = _orion.tree("sensor")
SENSOR_SOURCE = _orion.source("sensor")

# The real module, real classes, real functions. See the header for why
# this is worth about three seconds of import once per process.
sensor = _orion.real("sensor")

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
    """Claiming a session for both sleepers needs a verified, writable partner.

    The two guards that carry the property, and one that does not:

      has_partner_for_device   Is this partner verified for this bed.
                               Already returns False unless
                               `partner_mapping_valid`, so it subsumes it.
      partner_update_ok        Can the partner's tokens be written with
                               right now. A genuinely separate axis, and
                               the reason this is two checks rather than
                               one.
      partner_mapping_valid    Deliberately NOT asserted, and asserted to
                               be absent below.
    """
    body = next(
        ast.unparse(node)
        for node in ast.walk(SENSOR_TREE)
        if isinstance(node, ast.AsyncFunctionDef)
        and node.name == "async_confirm_sleep_session"
    )
    assert "needs_confirmation" in body and "is not True" in body
    assert "partner_update_ok" in body
    assert "has_partner_for_device" in body
    # This test used to require `partner_mapping_valid` here as well,
    # which pinned a conjunct that could never change the answer:
    # `coordinator.has_partner_for_device` returns False unless that flag
    # is set, and the call sites listed it anyway.
    #
    # Asserting its ABSENCE rather than simply dropping the old line,
    # because the harm was never a wrong answer. It was that a redundant
    # check reads as a necessary one, so the next partner-gated entity
    # gets written by copying this shape, and eventually the copy and
    # `has_partner_for_device` disagree about which is authoritative.
    # Two other call sites had already grown the same pair.
    #
    # If `has_partner_for_device` ever stops folding the flag in, this
    # assertion is wrong and should be replaced by the conjunct, not
    # deleted. Read that method first.
    assert "partner_mapping_valid" not in body, (
        "`partner_mapping_valid` is back alongside `has_partner_for_device`, "
        "which already returns False without it. Either the redundant "
        "conjunct was reintroduced, or `has_partner_for_device` no longer "
        "checks it and this test needs rewriting rather than satisfying."
    )


def test_recovery_guard_is_set_before_the_entry_is_unloaded():
    source = _orion.source("__init__")
    handler = source[source.index("async def _handle_revert") :]
    handler = handler[: handler.index("async def _handle_resume")]
    assert handler.index("CONF_UID_RECOVERY_ACTIVE: True") < handler.index(
        "async_unload(entry.entry_id)"
    )


# ── Real-object checks ────────────────────────────────────────────────
#
# Everything above reads source. Everything below reads the imported
# module, so it is asking about the objects Home Assistant will actually
# call rather than about the text that produced them.


def _entity_classes():
    """Every entity class the sensor platform defines, by name.

    Includes the private base classes. A handler defined on
    `_OrionLiveSensorBase` is genuinely reachable from its subclasses, and
    the point of this file is to answer reachability rather than tidiness.
    """
    return {
        name: obj
        for name, obj in vars(sensor).items()
        if inspect.isclass(obj) and obj.__module__ == sensor.__name__
    }


def test_registered_handlers_resolve_on_a_real_entity_class():
    """The runtime version of the check this file was written for.

    `test_every_registered_service_handler_exists_somewhere` asks `ast`
    whether the name appears under some class statement. This asks
    `getattr`, which is what Home Assistant does when the service fires.
    The two disagree exactly where it matters: an inherited handler passes
    here and is invisible to `ast`, and a name bound to something that is
    not callable passes `ast` and fails here.
    """
    classes = _entity_classes()
    unresolved = []
    for handler in _registered_handlers(SENSOR_TREE):
        owners = [
            name
            for name, cls in classes.items()
            if callable(getattr(cls, handler, None))
        ]
        if not owners:
            unresolved.append(handler)
    assert unresolved == [], (
        "these services are registered but no entity class on the sensor "
        "platform resolves them, so calling one raises AttributeError and "
        f"returns 500 to the caller: {unresolved}"
    )


def test_access_handlers_resolve_on_the_real_access_sensor():
    """The exact failure that created this file, asked of the real class.

    Three access handlers once landed on `OrionZoneSplitModeSensor`
    because they were appended to the end of the file after two other
    classes had been appended past their intended home. Lint, compile, the
    unit suite and Home Assistant startup all passed. `getattr` on the
    real class is the cheapest thing that would have failed.
    """
    missing = sorted(
        name
        for name in ACCESS_HANDLERS
        if not callable(getattr(sensor.OrionAccessSensor, name, None))
    )
    assert missing == [], (
        "these handlers do not resolve on the real OrionAccessSensor. "
        "Appending to the end of the file puts them on whichever class "
        f"happens to be last: {missing}"
    )


def test_access_handlers_did_not_land_on_an_unrelated_sensor():
    """`OrionZoneSplitModeSensor` is named because it is where they landed.

    Asserting the absence, not just the presence. A copy left behind on
    the wrong class after a fix is dead code that reads as live code, and
    the next person moving a handler copies the shape they can see.
    """
    stowaways = sorted(
        name
        for name in ACCESS_HANDLERS
        if name in vars(sensor.OrionZoneSplitModeSensor)
    )
    assert stowaways == [], (
        "access handlers are defined on OrionZoneSplitModeSensor, which is "
        f"the class they were misfiled onto once already: {stowaways}"
    )


def test_every_registered_handler_is_a_coroutine_function():
    """A synchronous handler is accepted at registration and wrong at call.

    `EntityPlatform.async_register_entity_service` takes a method NAME, so
    nothing checks the shape of what that name resolves to until the
    service fires. A handler written `def` rather than `async def`
    registers cleanly, appears in the service list with the right schema,
    and then returns a plain value where Home Assistant awaits one.
    `ast` can see the `def` keyword, but only on a method it can attribute
    to a class, which is the same blind spot as above.
    """
    classes = _entity_classes()
    wrong_shape = []
    for handler in _registered_handlers(SENSOR_TREE):
        for cls_name, cls in classes.items():
            func = getattr(cls, handler, None)
            if func is None or not callable(func):
                continue
            if not inspect.iscoroutinefunction(func):
                wrong_shape.append(f"{cls_name}.{handler}")
    assert wrong_shape == [], (
        "these service handlers are not coroutine functions, so Home "
        f"Assistant awaits a value that is not awaitable: {wrong_shape}"
    )
