"""Who may call the destructive entity services, decided by the service registry.

Every call here goes through `hass.services.async_call`. Nothing calls an
entity method directly, because the layer the hole lived at is the one
between the registry and the method: Home Assistant enforces per-entity
POLICY_CONTROL for an entity service and the built-in Users group holds
control of every entity, so before `helpers._admin_only` existed any
non-admin household member could revoke somebody's bed access, delete a
night of biometrics, or rewrite the account owner's phone number.

Gating eight of those broke zero tests. `tests/test_service_wiring.py`
parses the platform modules with `ast` and structurally cannot see
authorization. This file is the answer to both.

The gated set is derived from the source rather than typed out, and a
completeness guard cross-checks it against what actually landed in the
registry, so a nineteenth service that forgets `admin=True` fails here
instead of shipping.
"""

from __future__ import annotations

import ast
import importlib
from pathlib import Path
from typing import Any

import pytest
from homeassistant.core import Context, SupportsResponse
from homeassistant.exceptions import Unauthorized, UnknownUser
from homeassistant.helpers.entity_platform import async_get_platforms

from custom_components.orion_sleep.const import DOMAIN
from tests_ha.conftest import ACCOUNT, make_entry

_COMPONENT = Path(__file__).resolve().parents[1] / "custom_components" / "orion_sleep"

# Entity services this integration deliberately leaves open to any
# household member, listed here so the completeness guard below can tell
# "considered and allowed" apart from "nobody thought about it".
#
# `start_cooling` and `stop_cooling` are per-zone hot flash relief on the
# climate platform. They change a temperature for a few minutes and nothing
# else, which is the same authority the climate entity itself already hands
# to anyone who can see it. Gating them would mean a non-admin can set the
# bed to 45 degrees through `climate.set_temperature` but cannot cool it
# down. That reasoning now also lives at the registration in `climate.py`,
# which passes `admin=False` explicitly rather than using the raw platform
# method, so a reader there sees the decision without coming here.
#
# `override_schedule` is here because the schedule entities it competes
# with are POLICY_CONTROL-governed and strictly more destructive. It
# applies a self-expiring one-day override, while `time.set_value`,
# `switch.turn_on`, `number.set_value` and `select.select_option` all
# rewrite the same person's stored rows permanently and none of them can
# be gated, because an entity write receives no `ServiceCall` and so no
# `Context` to read a caller from. Gating only this one meant a non-admin
# could not override tonight's bedtime but could permanently delete it.
# The full argument is written down at the registration in `time.py`.
# `test_schedule_authorization_posture_real.py` pins the whole posture so
# it cannot be flipped back one file at a time.
_INTENTIONALLY_UNGATED = frozenset(
    {"start_cooling", "stop_cooling", "override_schedule"}
)

# Registered in `__init__.py` against the domain rather than a platform,
# so they are not entity services and are not what this file is about.
# They do their own admin check inside the handler, covered by
# `test_services_real.py`.
_DOMAIN_SERVICES = frozenset({"revert_unique_ids", "resume_unique_ids"})


def _string_value(module: Any, node: ast.expr, where: str) -> str:
    """Resolve a service name or method name argument to its string."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name):
        value = getattr(module, node.id, None)
        if isinstance(value, str):
            return value
    raise AssertionError(
        f"{where}: cannot resolve this argument to a string. Extend "
        "_string_value rather than dropping the registration, because a "
        "registration this scanner cannot read is a registration nobody "
        "checks the admin flag on."
    )


def _entity_service_registrations() -> list[dict[str, Any]]:
    """Every entity service this integration registers, read off the source.

    Derived rather than typed out on purpose. A hardcoded list of gated
    services grows stale the moment somebody adds the next one, and a
    stale list is exactly how the ungated service ships.

    Two shapes are recognised. `helpers.async_register_entity_service`,
    which takes the platform first and requires a keyword-only `admin`,
    and the raw `platform.async_register_entity_service`, which has no
    admin concept at all and therefore always means ungated.
    """
    found: list[dict[str, Any]] = []
    for path in sorted(_COMPONENT.glob("*.py")):
        source = path.read_text(encoding="utf-8")
        if "async_register_entity_service" not in source:
            continue
        if path.stem == "helpers":
            # Where the wrapper is defined, not where services are declared.
            continue
        module = importlib.import_module(f"custom_components.orion_sleep.{path.stem}")
        tree = ast.parse(source, filename=str(path))

        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if (
                not isinstance(func, ast.Attribute)
                or func.attr != "async_register_entity_service"
            ):
                continue
            owner = func.value
            if not isinstance(owner, ast.Name):
                continue

            where = f"{path.name}:{node.lineno}"
            if owner.id == "helpers":
                name_node, method_node = node.args[1], node.args[3]
                admin_kw = next(
                    (kw for kw in node.keywords if kw.arg == "admin"), None
                )
                assert admin_kw is not None, (
                    f"{where}: registration has no `admin=`. The keyword is "
                    "keyword-only with no default precisely so this cannot "
                    "happen, so this means the scanner is looking at "
                    "something it does not understand."
                )
                assert isinstance(admin_kw.value, ast.Constant) and isinstance(
                    admin_kw.value.value, bool
                ), (
                    f"{where}: `admin=` is not a literal True or False. Who "
                    "may call a service must be readable without running it."
                )
                admin = bool(admin_kw.value.value)
            else:
                # `platform.async_register_entity_service(name, schema, method)`.
                name_node, method_node = node.args[0], node.args[2]
                admin = False

            found.append(
                {
                    "module": path.stem,
                    "service": _string_value(module, name_node, where),
                    "method": _string_value(module, method_node, where),
                    "admin": admin,
                    "where": where,
                }
            )
    return found


_REGISTRATIONS = _entity_service_registrations()
_GATED = {r["service"]: r for r in _REGISTRATIONS if r["admin"]}
_GATED_NAMES = sorted(_GATED)


# One valid payload per gated service, minus `entity_id`, which every call
# below adds. Home Assistant validates the schema before the handler runs,
# so an invalid payload never reaches the admin gate and the test would
# pass having proved nothing.
#
# `test_every_gated_service_has_a_payload` asserts this covers the derived
# set exactly, so a newly gated service fails loudly here rather than
# quietly dropping out of the parametrization.
_PAYLOADS: dict[str, dict[str, Any]] = {
    "list_sleep_sessions": {"limit": 5},
    "delete_sleep_session": {
        "session_id": "session-1",
        "reason": "not_real_session",
        "confirm": True,
    },
    "edit_sleep_session": {
        "session_id": "session-1",
        "fell_asleep": "2026-07-27 23:00:00",
        "woke_up": "2026-07-28 07:00:00",
    },
    "confirm_sleep_session": {"session_id": "session-1", "claim": "both"},
    "end_sleep_session": {"confirm": True},
    "list_access": {},
    "list_invites": {},
    "invite_user": {"phone_number": "+15550000000", "role": "guest"},
    "cancel_invite": {"invite_id": "invite-1"},
    "accept_invite": {"code": "123456"},
    "remove_user_access": {"user_id": ACCOUNT, "confirm": True},
    "create_guest": {},
    "update_user_phone": {"user_id": ACCOUNT, "phone": "+15550000001"},
    "assign_zones": {"user_id": ACCOUNT, "zone_ids": ["zone_a"], "confirm": True},
    "set_device_name": {"name": "Bed"},
    "set_device_timezone": {"timezone": "Antarctica/Troll"},
}


class _Recorder:
    """Stands in for the real entity method so only the gate is under test.

    Every gated service is patched with one of these before the call. That
    keeps the parametrized tests honest about what they measure: whether
    the registry reached the method, and whether the gate let it. It also
    means a positive-path test cannot pass or fail for reasons buried in a
    vendor call none of these tests are about.
    """

    def __init__(self, response: bool) -> None:
        self.calls: list[dict[str, Any]] = []
        self._response = response

    async def __call__(self, **kwargs: Any) -> dict[str, Any] | None:
        self.calls.append(kwargs)
        return {"ok": True} if self._response else None


def _seed_schedule(client) -> None:
    """Give the account a schedule row so the time entities are available.

    Home Assistant drops unavailable entities from an entity service call
    before any handler runs, and returns quietly. Without this the
    `override_schedule` case would target an entity that was filtered out,
    reach no gate at all, raise nothing, and pass as though authorization
    had been enforced.
    """

    async def get_sleep_schedules() -> dict[str, Any]:
        return {
            "today_sleep_schedule": {
                ACCOUNT: {"day": 1, "bedtime": "22:30", "wakeup": "06:30"}
            }
        }

    client.get_sleep_schedules = get_sleep_schedules


async def _setup(hass, client):
    _seed_schedule(client)
    entry = make_entry(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


def _find_target(hass, method: str):
    """The first available entity offering `method`, which is what a call hits.

    Which entity a given service targets is read off the live platforms
    rather than written down, so this keeps working when a service moves
    from the access sensor to somewhere else.

    Availability is part of the search on purpose. Home Assistant drops
    unavailable entities from an entity service call before any handler
    runs and returns quietly, so an unavailable target would make every
    authorization test below pass without reaching a gate.
    """
    for platform in async_get_platforms(hass, DOMAIN):
        for entity in platform.entities.values():
            if entity.available and hasattr(entity, method):
                return entity
    raise AssertionError(
        f"no available entity offers `{method}`, so the service registered "
        "against it can never run. Either the handler landed on the wrong "
        "class or the entity that carries it is unavailable in this fixture."
    )


def _patch_target(hass, method: str, *, response: bool) -> tuple[str, _Recorder]:
    """Swap in a recorder for `method` on the entity a call would reach."""
    entity = _find_target(hass, method)
    recorder = _Recorder(response)
    setattr(entity, method, recorder)
    return entity.entity_id, recorder


async def _call(hass, service: str, context: Context, *, entity_id: str, extra=None):
    """Invoke one gated service the way a caller does, with its own payload."""
    registered = hass.services.async_services_for_domain(DOMAIN)[service]
    wants_response = registered.supports_response is SupportsResponse.ONLY
    return await hass.services.async_call(
        DOMAIN,
        service,
        {"entity_id": entity_id, **_PAYLOADS[service], **(extra or {})},
        blocking=True,
        context=context,
        return_response=wants_response,
    )


async def _non_admin(hass):
    """A user who is emphatically not an admin.

    The FIRST user created in a fresh Home Assistant becomes the owner,
    and an owner is admin whatever `group_ids` asks for. Burn one, then
    assert, because a fixture that silently produced an admin would make
    every negative test below pass while proving the opposite.
    """
    await hass.auth.async_create_user("Owner", group_ids=["system-admin"])
    user = await hass.auth.async_create_user("Housemate", group_ids=["system-users"])
    assert not user.is_admin, "fixture failed to build a non-admin user"
    return user


async def _admin(hass):
    await hass.auth.async_create_user("Owner", group_ids=["system-admin"])
    user = await hass.auth.async_create_user("Second Admin", group_ids=["system-admin"])
    assert user.is_admin, "fixture failed to build an admin user"
    return user


# ── The derivation itself ─────────────────────────────────────────────


def test_the_source_scan_found_the_registrations():
    """A scanner that silently matched nothing would green-light everything.

    Every parametrized test below draws its cases from `_GATED`. An empty
    `_GATED` turns all of them into zero-case no-ops that report as
    passing, which is the one failure mode a derived list has and a
    hardcoded one does not.

    Deliberately only a non-empty check rather than a magic count. A
    partially broken scan is caught by
    `test_every_entity_service_declares_whether_it_is_admin_only`, which
    compares the scan against the live registry and needs no number of
    its own to go stale.
    """
    assert _REGISTRATIONS, "the source scan found no entity services at all"
    assert _GATED, (
        "the source scan found entity services but none marked admin=True, "
        "so every authorization test below is a no-op"
    )


def test_every_ungated_registration_was_a_decision():
    """The blind spot between the two guards below and above.

    The source scan records a raw `platform.async_register_entity_service`
    call as `admin=False`, because that call has no admin concept at all.
    Nothing then compared that set against `_INTENTIONALLY_UNGATED`.

    The completeness guard does not close it either, because that one
    runs against the LIVE registry. A raw ungated registration on a
    platform that happens to build no entities in this fixture never
    reaches the registry, so it is absent from `registered`, absent from
    `unaccounted`, and passes both checks while shipping open to every
    non-admin household member.

    Purely structural on purpose. It reads the source, so a platform
    building zero entities changes nothing about whether this fires.
    """
    ungated = {r["service"] for r in _REGISTRATIONS if not r["admin"]}
    assert ungated <= _INTENTIONALLY_UNGATED, (
        "entity services registered with no admin gate that nobody has "
        f"decided about: {sorted(ungated - _INTENTIONALLY_UNGATED)}. These "
        "went through the raw platform.async_register_entity_service, which "
        "has no admin keyword, so the decision was never made rather than "
        "made and recorded. Register through "
        "helpers.async_register_entity_service with admin=True, or add the "
        "name to _INTENTIONALLY_UNGATED with the reason."
    )


def test_every_gated_service_has_a_payload():
    """A newly gated service must arrive with a way to exercise it."""
    missing = sorted(set(_GATED_NAMES) - set(_PAYLOADS))
    assert not missing, (
        f"gated services with no test payload: {missing}. Add one to "
        "_PAYLOADS so the authorization tests actually cover them, rather "
        "than skipping them and reporting green."
    )
    stale = sorted(set(_PAYLOADS) - set(_GATED_NAMES))
    assert not stale, f"payloads for services that are no longer gated: {stale}"


async def test_every_entity_service_declares_whether_it_is_admin_only(
    hass, patched, client
):
    """The guard that makes a nineteenth ungated service fail CI.

    Compares what actually reached the service registry against what the
    source scan says is gated, plus the short list of services this
    integration deliberately leaves open. A new entity service is in
    neither set, so it fails here with its own name in the message.
    """
    await _setup(hass, client)
    registered = set(hass.services.async_services_for_domain(DOMAIN)) - _DOMAIN_SERVICES
    accounted = set(_GATED_NAMES) | _INTENTIONALLY_UNGATED

    unaccounted = sorted(registered - accounted)
    assert not unaccounted, (
        f"entity services nobody has decided about: {unaccounted}. Register "
        "through helpers.async_register_entity_service with admin=True, or "
        "add the name to _INTENTIONALLY_UNGATED with the reason. Home "
        "Assistant lets any non-admin household member call an ungated "
        "entity service."
    )
    # Only the gated half. An entry in _INTENTIONALLY_UNGATED that never
    # registers is a platform that built no entities in this fixture,
    # which is not what this test is about.
    vanished = sorted(set(_GATED_NAMES) - registered)
    assert not vanished, (
        f"declared gated but never registered: {vanished}. The scan is "
        "reading a registration that no longer runs, so the parametrized "
        "tests are exercising a service that does not exist."
    )


# ── Authorization ─────────────────────────────────────────────────────


@pytest.mark.parametrize("service", _GATED_NAMES)
async def test_a_non_admin_cannot_call_a_destructive_service(
    hass, patched, client, service
):
    """The hole itself. Every gated service, refused for a non-admin.

    Note what core does NOT do here. `system-users` carries
    `USER_POLICY = {"entities": True}`, so Home Assistant's own per-entity
    POLICY_CONTROL check passes for this user on every one of these
    entities. Nothing but `helpers._require_admin` stands between them and
    the method, which is why removing it broke no test before this one.
    """
    await _setup(hass, client)
    entity_id, recorder = _patch_target(
        hass, _GATED[service]["method"], response=True
    )
    user = await _non_admin(hass)

    with pytest.raises(Unauthorized):
        await _call(hass, service, Context(user_id=user.id), entity_id=entity_id)

    assert not recorder.calls, (
        f"{service} raised Unauthorized but ran anyway. The gate has to "
        "refuse before the method, not after it."
    )


@pytest.mark.parametrize("service", _GATED_NAMES)
async def test_an_automation_context_is_allowed(hass, patched, client, service):
    """A context with no user attached is a trusted internal caller.

    This reads like a hole and is not. Automations, scripts and scenes all
    produce a `Context` with no `user_id`, and Home Assistant's own
    `_async_admin_handler` allows exactly that, because creating the
    automation was itself an admin-gated action. Denying it instead breaks
    every automation that touches one of these services, which for a home
    automation integration is a worse outcome than the hole it closes.

    Anybody tempted to "fix" the falsy check in `_require_admin` into a
    denial should fail this test first.
    """
    await _setup(hass, client)
    entity_id, recorder = _patch_target(
        hass, _GATED[service]["method"], response=True
    )

    await _call(hass, service, Context(), entity_id=entity_id)

    assert recorder.calls, (
        f"{service} was not reached from a context with no user_id, so "
        "automations calling it are now broken."
    )


@pytest.mark.parametrize("service", _GATED_NAMES)
async def test_an_admin_can_call_a_destructive_service(hass, patched, client, service):
    """The positive path, which nothing previously proved at all.

    A gate that denied everybody would satisfy every negative test in this
    file. So would a handler attached to a class no entity instantiates,
    which is the exact defect `tests/test_service_wiring.py` was written
    for after it happened three times. That test reads the AST and cannot
    tell whether the service is reachable. This one calls it.
    """
    await _setup(hass, client)
    entity_id, recorder = _patch_target(
        hass, _GATED[service]["method"], response=True
    )
    user = await _admin(hass)

    await _call(hass, service, Context(user_id=user.id), entity_id=entity_id)

    assert recorder.calls, f"an admin call to {service} never reached the method"


@pytest.mark.parametrize("service", _GATED_NAMES)
async def test_an_unknown_user_id_raises_unknown_user(hass, patched, client, service):
    """A deleted account is distinguishable from an under-privileged one.

    `UnknownUser` subclasses `Unauthorized`, so this asserts the specific
    type. Collapsing the two would tell an operator whose account was
    removed that they lack permission, and they would go looking in the
    wrong place.

    Be clear about what this does and does not prove. Home Assistant
    resolves the user in `_resolve_entity_service_call_entities` before
    any handler runs, so on the current wiring core answers first and
    `_require_admin` never sees the call. Gutting our gate entirely does
    not break this test, and that was verified rather than assumed.

    It is kept because it pins the contract at the boundary a caller
    actually sees, and that contract is only inherited for as long as
    these stay entity services. Moving any of them to a plain
    `hass.services.async_register` wrapper, which is the obvious shape
    for the ones that no longer need entity targeting, drops the core
    check and leaves `_require_admin` as the only thing raising here.
    This test is what notices the difference on that day.
    """
    await _setup(hass, client)
    entity_id, recorder = _patch_target(
        hass, _GATED[service]["method"], response=True
    )

    with pytest.raises(UnknownUser):
        await _call(
            hass,
            service,
            Context(user_id="00000000000000000000000000000000"),
            entity_id=entity_id,
        )

    assert not recorder.calls, f"{service} ran for a user id that resolves to nobody"


# ── What the wrapper does to the call on the way through ──────────────


async def test_service_fields_are_forwarded_and_entity_id_is_stripped(
    hass, patched, client
):
    """`_admin_only` rebuilds the filtered payload itself, so check it did.

    A method registered by NAME gets the filtered data dict from core. A
    CALLABLE gets the raw `ServiceCall`, and the wrapper has to be a
    callable to see `call.context` at all. It therefore reconstructs the
    dict with `remove_entity_service_fields` and splats it. Get that wrong
    and every gated service either loses its arguments or gains an
    `entity_id` keyword its method has no parameter for.
    """
    await _setup(hass, client)
    entity_id, recorder = _patch_target(hass, "async_assign_zones", response=False)
    user = await _admin(hass)

    await _call(hass, "assign_zones", Context(user_id=user.id), entity_id=entity_id)

    assert recorder.calls, "assign_zones never reached the method"
    received = recorder.calls[0]
    assert received == {
        "user_id": ACCOUNT,
        "zone_ids": ["zone_a"],
        "confirm": True,
    }, received
    assert "entity_id" not in received, (
        "entity_id survived into the method call, so every gated service "
        "raises TypeError on an argument its signature does not have"
    )


async def test_a_service_with_a_response_still_returns_it(hass, patched, client):
    """The wrapper has to `return`, not just `await`.

    `list_access` is `SupportsResponse.ONLY`, and it is the documented way
    to get the Orion user ids that `remove_user_access`, `assign_zones`
    and `update_user_phone` all require as input. Dropping the return
    value turns it into a service that answers with nothing, and the ids
    are deliberately kept out of entity attributes so there is no other
    way to read them.

    Deliberately NOT patched. The real method is local and needs no
    network, so this also proves the response survives the whole path.
    """
    await _setup(hass, client)
    entity_id = _find_target(hass, "async_list_access").entity_id
    user = await _admin(hass)
    response = await _call(
        hass, "list_access", Context(user_id=user.id), entity_id=entity_id
    )

    assert response, (
        "list_access returned nothing. If the wrapper awaits without "
        "returning, every SupportsResponse.ONLY service silently answers "
        "with an empty payload."
    )
    assert entity_id in response, response
    assert "people" in response[entity_id], response[entity_id]
