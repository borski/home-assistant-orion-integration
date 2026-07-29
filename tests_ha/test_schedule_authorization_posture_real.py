"""The schedule authorization posture, pinned so it cannot be flipped back.

A decision was made and written down at the registration in `time.py`:
per-person schedule control is POLICY_CONTROL-governed, uniformly, and
`orion_sleep.override_schedule` is NOT admin-gated.

The reasoning, in one paragraph. `override_schedule` applies a one-day
self-expiring override. Four other paths onto the same schedule rows are
permanent and ungatable: `time.set_value` rewrites the stored weekday
bedtime, `switch.turn_on/off` flips `bedtime_is_active` and
`auto_turn_off`, `number.set_value` rewrites the overnight temperature
curve, and `select.select_option` re-attributes sleep data between
sleepers. All four carry the partner's `user_id` on the partner's
entities. None can be gated, because an entity write receives no
`ServiceCall` and therefore no `Context` to read a caller from, and
`helpers._require_admin` documents why `Entity._context` is not a
substitute. Gating only the service meant a non-admin household member
could not override tonight's bedtime but could permanently delete it,
which is a promise the code did not keep rather than a boundary.

This file exists because that posture is easy to un-decide by accident.
Someone reads the `admin=False` in `time.py`, reads the words "rewrites a
NAMED person's bedtime", and flips it to True in a one-line commit that
looks like a security fix. It is not one. It restores an asymmetry that
protects the reversible door and leaves four irreversible ones open.

If the posture is deliberately changed to the other option, which is to
expose the partner's schedule read-only and route partner writes through
an admin-gated service, then these tests SHOULD fail. They are here to
make that a decision, not a drift.
"""

from __future__ import annotations

import ast
from datetime import time as dt_time
from pathlib import Path
from typing import Any

import pytest
from homeassistant.core import Context
from homeassistant.helpers.entity_platform import async_get_platforms

from custom_components.orion_sleep.const import DOMAIN
from tests_ha.conftest import ACCOUNT, PARTNER, make_entry

_COMPONENT = Path(__file__).resolve().parents[1] / "custom_components" / "orion_sleep"

# Every write path onto a person's stored schedule, as (platform, method).
# Read as: "these are the doors the gate on `override_schedule` did not
# cover". A new one appearing here is not a problem. A new one appearing
# here while `override_schedule` is gated again is the exact inconsistency
# this file is about.
_SCHEDULE_WRITE_METHODS: tuple[tuple[str, str], ...] = (
    ("time", "async_set_value"),
    ("switch", "async_turn_on"),
    ("switch", "async_turn_off"),
    ("number", "async_set_native_value"),
    ("select", "async_select_option"),
)


def _seed_schedule(client) -> None:
    """Give BOTH people a schedule row.

    The partner matters here and nowhere else in this file's setup. The
    whole argument turns on the partner's entities being writable by the
    same non-admin user, so a fixture that only ever built the account
    owner's entities would let every assertion below pass while proving
    nothing about the case that motivated the decision.
    """

    async def get_sleep_schedules() -> dict[str, Any]:
        row = {"day": 1, "bedtime": "22:30", "wakeup": "06:30"}
        return {"today_sleep_schedule": {ACCOUNT: dict(row), PARTNER: dict(row)}}

    client.get_sleep_schedules = get_sleep_schedules


async def _setup(hass, client):
    _seed_schedule(client)
    entry = make_entry(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def _non_admin(hass):
    """A user who is emphatically not an admin.

    The FIRST user created in a fresh Home Assistant becomes the owner and
    an owner is admin whatever `group_ids` asks for. Burn one, then assert,
    because a fixture that silently produced an admin would make the
    positive assertions below pass for the wrong reason.
    """
    await hass.auth.async_create_user("Owner", group_ids=["system-admin"])
    user = await hass.auth.async_create_user("Housemate", group_ids=["system-users"])
    assert not user.is_admin, "fixture failed to build a non-admin user"
    return user


def _override_schedule_admin_flag() -> bool:
    """Read the `admin=` literal off the registration in `time.py`.

    Read from source rather than asserted against behaviour so this fails
    with a clear message the moment the flag changes, rather than one test
    down in a call that raises `Unauthorized` for reasons a reader then has
    to reconstruct.
    """
    source = (_COMPONENT / "time.py").read_text(encoding="utf-8")
    tree = ast.parse(source, filename="time.py")
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if (
            not isinstance(func, ast.Attribute)
            or func.attr != "async_register_entity_service"
        ):
            continue
        for keyword in node.keywords:
            if keyword.arg != "admin":
                continue
            assert isinstance(keyword.value, ast.Constant) and isinstance(
                keyword.value.value, bool
            ), "`admin=` on override_schedule is not a literal True or False"
            return bool(keyword.value.value)
    raise AssertionError(
        "no `async_register_entity_service(..., admin=...)` call found in "
        "time.py. The registration moved or was rewritten, and this file "
        "can no longer see the flag it exists to pin."
    )


def _schedule_entities(hass) -> list:
    """Every entity in the integration that writes a person's schedule."""
    found = []
    for platform in async_get_platforms(hass, DOMAIN):
        for entity in platform.entities.values():
            if not entity.available:
                continue
            if any(
                platform.domain == domain and hasattr(entity, method)
                for domain, method in _SCHEDULE_WRITE_METHODS
            ):
                found.append(entity)
    return found


def test_override_schedule_is_not_admin_gated():
    """The decision itself, as a single readable assertion.

    What breaks this: changing `admin=False` back to `admin=True` on the
    `override_schedule` registration in `time.py`.
    """
    assert _override_schedule_admin_flag() is False, (
        "override_schedule is admin-gated again. That gate covers a "
        "one-day self-expiring override while leaving time.set_value, "
        "switch.turn_on, number.set_value and select.select_option "
        "permanently rewriting the same rows for the same non-admin user. "
        "If the posture really is changing, the other half has to change "
        "with it: stop creating writable per-person entities for anyone "
        "but the authenticated account. See the argument at the "
        "registration in time.py."
    )


async def test_a_non_admin_can_still_reach_every_schedule_write(hass, patched, client):
    """The posture, measured rather than read off the source.

    Asserts the state the decision accepts: a non-admin household member
    can drive every schedule write path, including the service. That is
    the user-facing contract this integration exists to provide, and a
    household that wants it locked uses Home Assistant's per-entity
    permissions, which cover all five paths at once.

    What breaks this: gating `override_schedule` again, or moving any of
    the entity writes behind a check that denies a non-admin. Either
    change makes the surface inconsistent again, and inconsistency is the
    defect, not the openness.
    """
    await _setup(hass, client)
    user = await _non_admin(hass)

    entity_id = next(
        e.entity_id
        for e in _schedule_entities(hass)
        if hasattr(e, "async_override_schedule")
    )
    calls: list[dict[str, Any]] = []

    async def recorder(**kwargs: Any) -> None:
        calls.append(kwargs)

    entity = next(
        e for e in _schedule_entities(hass) if e.entity_id == entity_id
    )
    entity.async_override_schedule = recorder

    # No pytest.raises. Reaching the method IS the assertion.
    await hass.services.async_call(
        DOMAIN,
        "override_schedule",
        {"entity_id": entity_id, "bedtime": "22:30:00"},
        blocking=True,
        context=Context(user_id=user.id),
    )

    assert calls, (
        "a non-admin could not call override_schedule. The registration "
        "has been gated again. Read the argument in time.py before "
        "changing this test."
    )


async def test_a_schedule_write_targets_a_named_person_not_the_caller(
    hass, patched, client
):
    """The fact the whole argument rests on, asserted rather than assumed.

    The old `admin=True` justification was that `override_schedule`
    rewrites a NAMED person's schedule. True, and the reason the gate was
    pointless is that the entity writes do exactly the same thing: each
    one carries an explicit `user_id` and rewrites THAT person's stored
    row, permanently, with no gate available to it.

    Asserted through the mechanism rather than by linking a partner. A
    partner requires partner tokens and a verified account mapping, both
    of which live in the config entry and coordinator, and coupling this
    file to that setup would make the posture pin fail for reasons that
    have nothing to do with the posture. What matters is that the write
    is addressed to whoever the entity was built for, which is visible
    with one person present.

    What breaks this: making schedule entities write the authenticated
    account's row regardless of which person they represent, or dropping
    `user_id` from the write. Either is a move toward the read-only
    partner posture, and `override_schedule` should be gated again if it
    lands.
    """
    await _setup(hass, client)

    sent: list[dict[str, Any]] = []

    async def update_schedule_field(**kwargs: Any) -> None:
        sent.append(kwargs)

    client.update_schedule_field = update_schedule_field

    # The `time` platform specifically. `NumberEntity` also carries an
    # `async_set_value` from its base class, so a search by method name
    # picked a number entity whose real write is
    # `async_set_native_value`, and the base implementation raised
    # NotImplementedError. Selecting by platform says which door is under
    # test instead of guessing from a method name two platforms share.
    entity = next(
        e
        for platform in async_get_platforms(hass, DOMAIN)
        if platform.domain == "time"
        for e in platform.entities.values()
        if e.available
    )
    # `_user_id` is what the entity was built for. Read off the instance
    # rather than assumed to be ACCOUNT, so this keeps meaning the same
    # thing on the day a partner is present in the fixture.
    target = entity._user_id

    await entity.async_set_value(dt_time(23, 15))

    assert sent, "the schedule write never reached the client"
    assert sent[0].get("user_id") == target, (
        "a schedule entity write did not carry the user_id of the person "
        f"it represents. Sent {sent[0].get('user_id')!r}, expected "
        f"{target!r}. If writes are now scoped to the authenticated "
        "account only, the read-only-partner posture has arrived and "
        "override_schedule should be gated again."
    )


def _calls_by_name(source: str, name: str) -> bool:
    """Whether `source` CALLS `name`, ignoring mentions in comments.

    A substring search is wrong here and was tried first. `time.py` names
    `helpers._require_admin` in the comment explaining why a gate is not
    reachable at an entity write, which is the very thing this check
    wants to encourage, and the substring version failed on it. Parsing
    means the test can tell a reference from a call.
    """
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr == name:
            return True
        if isinstance(func, ast.Name) and func.id == name:
            return True
    return False


@pytest.mark.parametrize("domain,method", _SCHEDULE_WRITE_METHODS)
def test_no_schedule_write_tries_to_gate_itself(domain: str, method: str):
    """No entity write may grow a home-made authorization check.

    An entity write has no `ServiceCall` and therefore no `Context`. The
    only caller-shaped thing in reach is `Entity._context`, which is
    private, nullable, and cleared five seconds after it is set by
    `CONTEXT_RECENT_TIME_SECONDS`. A gate built on it raises
    AttributeError on `None.user_id` instead of denying, which fails OPEN.
    `helpers._require_admin` says all of this in its own docstring.

    So this asserts the absence of the tempting fix. Somebody who reads
    the posture decision and wants the protection anyway will reach for
    `self._context` first, and it will appear to work in manual testing
    because the attribute happens to be populated for a few seconds after
    a UI click.

    What breaks this: a platform module reading `self._context`, or
    calling `_require_admin` outside
    `helpers.async_register_entity_service`.
    """
    source = (_COMPONENT / f"{domain}.py").read_text(encoding="utf-8")
    assert "self._context" not in source, (
        f"{domain}.py reads Entity._context. That attribute is private, "
        "nullable and time-limited, so an authorization check built on it "
        "fails OPEN rather than denying. See helpers._require_admin."
    )
    assert not _calls_by_name(source, "_require_admin"), (
        f"{domain}.py calls _require_admin directly. That helper needs a "
        "ServiceCall context, which an entity write does not have. It is "
        "only correct behind helpers.async_register_entity_service."
    )
