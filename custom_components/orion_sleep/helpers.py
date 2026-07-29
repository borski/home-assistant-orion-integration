"""Helpers that belong to this integration, not to the API client.

These came back from `orion_sleep_api` because none of them describe the
vendor's API. They describe how Home Assistant presents it: entity
registry identifiers, display strings, options-flow schema keys, and the
redaction policy for a diagnostics download.

A client library has no business choosing an entity's unique_id or
formatting a duration as "7h 30m".

The service authorization block below lives here for the same reason,
plus one more: both entity platforms and `__init__.py` need the same
answer to "may this caller do that", and three copies of an
authorization rule is how two of them drift.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine

    from homeassistant.core import Context, HomeAssistant, ServiceCall
    from homeassistant.helpers.entity import Entity
    from homeassistant.helpers.entity_platform import EntityPlatform
    from homeassistant.helpers.typing import VolDictType, VolSchemaType

# Matches the vendor's HH:mm schedule times. The library keeps its own
# copy for write validation; this one is only used to render a duration.
_SCHEDULE_TIME_RE = re.compile(r"^(?:[01][0-9]|2[0-3]):[0-5][0-9]$")


_SENSITIVE_DIAGNOSTIC_BRANCHES = frozenset(
    {
        "insights",
        "partner_insights",
        "recommendations",
        "schedules",
        "sensors",
        "timeline",
        "today_sleep_schedule",
        # Whether somebody is in bed right now, since when, and which
        # side. Occupancy over a date range also reads as "the house was
        # empty", which is why this belongs with the biometrics rather
        # than with the config.
        "live_session",
        # Chronotype and sleep targets.
        "sleep_config",
        # The same schedule as `timeline`, which the coordinator stores at
        # top level under a different name. Omitting `schedules` and
        # `today_sleep_schedule` and then letting it out under a third
        # spelling is how a deliberate omission becomes a leak.
        "ws_timelines",
    }
)


_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)


# How many digits a value needs before it is read as a phone number
# rather than as a name. Seven is the shortest real subscriber number.
# Set lower and legitimate short names like "R2", "42" or "Room 101"
# start getting rejected, which costs a household a readable entity for
# no privacy gain and cannot be argued with from the UI.
_MIN_PHONE_DIGITS = 7


# ── Service authorization ──────────────────────────────────────────────
#
# Home Assistant enforces only per-entity POLICY_CONTROL for an entity
# service, and the built-in Users group has control of every entity. So
# with no gate of our own, any non-admin household member of this HA
# instance can revoke another person's access to the bed, delete a night
# of biometric history, or rewrite the account owner's phone number,
# which is the factor the vendor's API sends login codes to.
#
# Everything in this block deliberately avoids importing Home Assistant
# at module scope. `helpers.py` is the one module in this package that
# the fast test suite loads and executes for real instead of parsing it
# with `ast`, and it can only do that because Home Assistant is not
# installed there. The runtime imports therefore sit inside the
# functions, which only ever run inside a live Home Assistant.


async def _require_admin(hass: HomeAssistant, context: Context) -> None:
    """Refuse an account-changing call from a non-admin Home Assistant user.

    Matches `_async_admin_handler` in `homeassistant/helpers/service.py`
    exactly, including the part that reads like a bug and is not.

    A context with no `user_id` is ALLOWED. Automations, scripts and
    scenes all produce a context with no user attached, and Home
    Assistant treats those as trusted because creating the automation
    was itself an admin-gated action. An earlier version of this check
    inverted that and denied them, which broke every automation touching
    one of these services. For a home automation integration that is a
    worse failure than the hole it closed.

    A `user_id` that resolves to nothing raises `UnknownUser` rather than
    `Unauthorized`, again matching core, so a caller can tell a deleted
    account apart from an under-privileged one.

    Takes the `Context` off the `ServiceCall`. Never `Entity._context`,
    which is private, nullable, and cleared five seconds after it is set
    by `CONTEXT_RECENT_TIME_SECONDS` in `helpers/entity.py`. A gate built
    on that attribute raises AttributeError on `None.user_id` instead of
    denying, and it only ever holds the right value because
    `entity_service_call` happens to set it with no await in between.
    Nothing in Home Assistant's contract promises that ordering.
    """
    from homeassistant.exceptions import Unauthorized, UnknownUser

    if not context.user_id:
        return
    user = await hass.auth.async_get_user(context.user_id)
    if user is None:
        raise UnknownUser(context=context)
    if not user.is_admin:
        raise Unauthorized(context=context)


def _admin_only(
    method_name: str,
) -> Callable[[Entity, ServiceCall], Coroutine[Any, Any, Any]]:
    """Wrap one entity service method behind `_require_admin`.

    Home Assistant hands a CALLABLE `func` the raw `ServiceCall`, where a
    method registered by NAME gets only the filtered data dict.
    `entity_service_call` picks between the two on `isinstance(func, str)`
    and `_handle_single_entity_call` then runs the job as
    `func(entity, call)`. That fork is the entire reason this indirection
    exists. A method registered by name can never see `call.context`, and
    `call.context` is the only correct place to read the caller from.

    The filtered dict is rebuilt here with the same core helper, so the
    wrapped method keeps the signature it already had and nothing about
    the call sites changes.
    """
    from homeassistant.helpers.service import remove_entity_service_fields

    async def handler(entity: Entity, call: ServiceCall) -> Any:
        await _require_admin(entity.hass, call.context)
        # Returned, not discarded. Several gated services declare
        # SupportsResponse.ONLY, and swallowing the value here would turn
        # them into services that answer with nothing.
        return await getattr(entity, method_name)(**remove_entity_service_fields(call))

    handler.__name__ = f"admin_gated_{method_name}"
    return handler


def async_register_entity_service(
    platform: EntityPlatform,
    name: str,
    # Matches what `EntityPlatform.async_register_entity_service` accepts.
    # This used to say `dict | None`, which is narrower than the truth: a
    # `vol.Schema`, a `vol.All` and a `cv.make_entity_service_schema`
    # result are all valid here and none of them is a dict. Deferred to
    # TYPE_CHECKING with the rest of the Home Assistant imports, because
    # `tests/test_helpers.py` loads and EXECUTES this module in an
    # environment where Home Assistant is not installed.
    schema: VolDictType | VolSchemaType | None,
    method: str,
    *,
    admin: bool,
    **kwargs: Any,
) -> None:
    """Register one entity service, having first decided who may call it.

    `admin` is keyword-only and has no default on purpose. The
    registration block used to say nothing about whether a service was
    destructive, so the answer lived eight separate times inside eight
    method bodies. Adding a ninth account-changing service and
    forgetting the line was a silent privilege escalation that no test
    could see. A registration now does not compile without an answer.

    Named identically to the `EntityPlatform` method it wraps, so the
    structural guard in `tests/test_service_wiring.py` keeps matching
    these call sites. That test parses the platform modules with `ast`
    and exists because three handlers once landed on the wrong class and
    nothing noticed until somebody called one. Renaming this would have
    made every registration invisible to it.
    """
    func: str | Callable[..., Any] = _admin_only(method) if admin else method
    platform.async_register_entity_service(name, schema, func, **kwargs)


def short_id(value: object) -> str:
    """Last four characters of an identifier, for logs.

    The library already does this for device serials, and says why: logs
    get pasted into bug reports, and `serial_number` and `user_id` are
    both in the diagnostics redaction set. Publishing them at INFO or
    WARNING contradicts a policy the rest of the project keeps. Four
    characters still tell a two-person household which record is meant.
    DEBUG keeps the whole value, because enabling it is deliberate.
    """
    text = value if isinstance(value, str) else str(value or "")
    return "…" + text[-4:] if len(text) > 4 else (text or "?")


def is_safe_display_name(value: object) -> bool:
    """Whether a vendor-supplied name may be used to name an entity.

    The thing being defended: `orion_sleep_api.util.orion_user_label`
    falls back through first_name, firstName, name, email, and finally
    phone. The last two are login credentials, and the vendor sends
    sign-in codes to both. An account that never had a display name set
    reaches them on the ordinary path, not as an edge case, which is why
    the test fixtures were asserting on an entity id with an email in it.

    That matters more for a name than for a recorded attribute. Home
    Assistant slugifies an entity's name into its entity_id at FIRST
    registration and never revisits it on rename, so a credential that
    reaches a name is permanent. It lands in every dashboard,
    automation, recorder row, long term statistics row, backup, and
    screenshot pasted into a bug report. A recorded attribute can be
    purged. An entity_id cannot.

    `OrionAccessSensor._people` already refuses `orion_user_label` for
    exactly this reason. This is that same policy, moved to where every
    naming path in the integration actually converges.

    Deliberately permissive about anything that is not an email or a
    phone number. Rejecting a real name costs a household a readable
    entity and gains nothing, so "R2", "42" and "Anne-Marie" all pass.
    """
    if not isinstance(value, str):
        return False
    text = value.strip()
    if not text:
        return False
    # Any "@" at all. Email is the first credential in that fallback
    # chain and the one the default fixture profile actually hits.
    if "@" in text:
        return False
    # A phone number is digits plus separators. A name that is long
    # enough to be a phone number essentially always contains a letter.
    #
    # Tested by Unicode property rather than against a fixed punctuation
    # set, because a fixed set is only ever as complete as whatever
    # somebody thought of on the day. The previous version stripped
    # exactly " \t\u00a0-.()+" and was therefore defeated by an en dash
    # (U+2013), a non-breaking hyphen (U+2011), and a forward slash, all
    # three of which sailed through as safe names and were eligible to
    # be slugified into a permanent entity_id. None of those is exotic.
    # A vendor that stores "555/123/4567" or a copy-paste out of a word
    # processor that helpfully converted the hyphen produces them, and
    # the failure is silent and irreversible.
    #
    # `str.isdigit` also covers non-ASCII digits, so an Arabic-Indic or
    # fullwidth spelling of the same number is caught by the same rule
    # rather than needing its own entry in a table.
    # The letter test is what keeps this permissive. "Room 101" and "42"
    # are short enough to pass on the digit count alone, and a genuine
    # name that does carry seven digits still passes because it has
    # letters in it. Only a bare, letterless, long-enough run of digits
    # is refused, which is exactly the shape `orion_user_label` returns
    # when it falls all the way through to the phone field.
    if sum(1 for char in text if char.isdigit()) < _MIN_PHONE_DIGITS:
        return True
    return any(char.isalpha() for char in text)


def nested_mapping(container: object, *keys: str) -> dict:
    """Walk nested mappings, returning ``{}`` at the first non-mapping level.

    Guards the coordinator accessors. ``(self.data or {}).get("insights", {})``
    happily returns a list if the vendor ever sends one, and the very next
    ``.get`` then raises AttributeError from inside a property, where
    nothing catches it and the entity goes permanently broken.
    """
    current = container
    for key in keys:
        if not isinstance(current, dict):
            return {}
        current = current.get(key)
    return current if isinstance(current, dict) else {}


def person_unique_id(
    device_id: str, key: str, user_id: str | None, *, legacy: str
) -> str:
    """Stable unique_id for any entity that belongs to one person.

    Same scheme `schedule_unique_id` has always used, generalised because
    the reasoning in its docstring was never specific to schedules. Sleep
    scores, session flags and occupancy are just as much one person's as
    a bedtime is, and those shipped keyed on the literal role "partner",
    which is the exact thing that docstring warns against.

    `legacy` is returned when the Orion user id is not known yet. That
    only happens if identity is missing at platform setup, and inventing
    a placeholder there would mint a second entity for a person who
    already has one. Returning what previously shipped keeps them on the
    entity they already have until identity resolves.
    """
    if not user_id:
        return legacy
    return f"{device_id}_user_{user_id}_{key}"


def schedule_unique_id(device_id: str, key: str, user_id: str) -> str:
    """Stable unique_id for one person's schedule entity.

    Keyed on the immutable Orion user id. Never on a role like "partner",
    which would silently swap owners if the integration were ever
    re-authenticated as the other account, grafting one person's history
    onto the other. Never on a display name, which is user-editable.

    Deliberately uniform. An earlier draft kept nine un-namespaced ids for
    the authenticated user so pre-existing history would survive, but
    nothing had been built on those entities yet, so the only thing that
    exception bought was a permanent special case in the code and an
    asymmetry between the two people on the bed.
    """
    return f"{device_id}_user_{user_id}_{key}"


def partner_changed_since_legacy_rows(
    marker: object, current_partner_id: object
) -> bool:
    """Whether the linked partner differs from the one the 2.x rows hold.

    Lives here because `migrations` may not import `coordinator` (that
    direction is already taken) and `config_flow` writes the value this
    reads. One rule, one copy, both ends.

    Takes the two VALUES rather than the entry or its data, and that is
    not squeamishness about types. This module imports nothing from the
    package, which is what lets `tests/test_helpers.py` load it straight
    off disk and exercise the redaction policy without Home Assistant
    installed. A `from .const import` here fails that test at collection
    with "attempted relative import with no known parent package", which
    is how this signature was arrived at.

    The question matters exactly once, in `partner_revert_blockers`. A
    household that upgraded from 2.x keeps its `{device}_partner_{key}`
    rows, holding whoever the partner was at upgrade time, and the forward
    migration deliberately leaves them. When they still belong to the
    current partner a downgrade is fine, because 2.x finds them where it
    left them. When the partner has since changed they hold somebody else,
    and letting 2.x write the new partner's readings into them merges two
    people's biometric history.

    `CONF_PARTNER_REPLACED` used to be a bare `True`, stamped on every
    partner write while any partner tokens existed. That made it fire on
    the one action the integration itself prescribes: a partner's refresh
    token rots, the warning says to replace the partner account in
    options, the household signs the SAME person back in, and the marker
    latched forever. The revert then named that partner's own entities and
    instructed the household to delete them, destroying genuine history to
    fix a replacement that never happened.

    So the marker now records WHO filled those rows: the partner account
    id linked immediately before the first change, written once and never
    overwritten. Never overwritten because the rows do not change hands
    when the partner does. Through P to Q and back to P, the answer for
    the rows is still P, and a marker that tracked the latest outgoing
    partner would call that a mismatch.

    Fails closed on anything it cannot read. A legacy boolean, or a change
    made when no partner id had ever been recorded, both mean a change
    happened that cannot be attributed, and refusing a downgrade costs a
    delay where allowing one costs a merge.
    """
    if marker is None or marker is False:
        return False
    if isinstance(marker, str) and marker:
        if isinstance(current_partner_id, str) and current_partner_id:
            return current_partner_id != marker
    return True


def schedule_duration_text(schedule: object) -> str | None:
    """Human "Xh Ym" between a schedule's bedtime and wakeup.

    Handles the overnight rollover. Returns None unless both times are
    present and well formed.
    """
    if not isinstance(schedule, dict):
        return None
    bedtime = schedule.get("bedtime")
    wakeup = schedule.get("wakeup")
    if not isinstance(bedtime, str) or not isinstance(wakeup, str):
        return None
    if not _SCHEDULE_TIME_RE.match(bedtime) or not _SCHEDULE_TIME_RE.match(wakeup):
        return None

    start_h, start_m = (int(part) for part in bedtime.split(":"))
    end_h, end_m = (int(part) for part in wakeup.split(":"))
    minutes = (end_h * 60 + end_m) - (start_h * 60 + start_m)
    if minutes <= 0:
        minutes += 24 * 60
    return f"{minutes // 60}h {minutes % 60}m"


def unique_alias_labels(users: object) -> dict[str, str]:
    """Map each user id to a unique, human-readable form-field label.

    Options-flow schema keys must be unique, and two people can share a
    first name. Duplicates get a numeric suffix. Users with no readable
    name fall back to a shortened id so the field is still addressable.
    """
    labels: dict[str, str] = {}
    if not isinstance(users, list):
        return labels

    used: set[str] = set()
    for user in users:
        if not isinstance(user, dict):
            continue
        user_id = user.get("id")
        if not isinstance(user_id, str) or not user_id:
            continue
        base = (user.get("name") or "").strip() or f"User {user_id[:8]}"
        label = base
        suffix = 2
        while label in used:
            label = f"{base} ({suffix})"
            suffix += 1
        used.add(label)
        labels[user_id] = label
    return labels


def clean_alias_map(value: object, known_ids: object = None) -> dict[str, str]:
    """Normalize a stored alias mapping into ``{user_id: alias}``.

    Blank aliases are dropped so clearing a field removes the override.
    When ``known_ids`` is supplied, ids outside it are discarded, which
    keeps stale entries from a previous account out of the options.
    """
    if not isinstance(value, dict):
        return {}
    allowed = set(known_ids) if isinstance(known_ids, (set, list, tuple)) else None

    cleaned: dict[str, str] = {}
    for user_id, alias in value.items():
        if not isinstance(user_id, str) or not user_id:
            continue
        if allowed is not None and user_id not in allowed:
            continue
        if not isinstance(alias, str):
            continue
        stripped = alias.strip()
        if stripped:
            cleaned[user_id] = stripped
    return cleaned


def clamp_cooling_minutes(value: object, default: int, low: int, high: int) -> int:
    """Coerce a rapid-cool duration to a usable whole number of minutes.

    The duration is chosen locally and then sent to a route that changes
    the physical bed, so this refuses to guess. A missing, malformed, or
    non-numeric value falls back to ``default`` rather than to zero,
    because a zero-minute window is a request the server has never been
    asked to honour.

    ``bool`` is rejected explicitly: it subclasses ``int``, so ``True``
    would otherwise be accepted as a one-minute window.

    Out-of-range values are clamped rather than rejected. A slider that
    silently refuses is worse than one that saturates, and both bounds
    are ours rather than the vendor's.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    try:
        minutes = int(round(float(value)))
    except (TypeError, ValueError, OverflowError):
        return default
    return max(low, min(high, minutes))


def redact_identifier_keys(value: object) -> object:
    """Redact UUIDs wherever they appear, preserving container shape.

    Keys and list elements both. Home Assistant's `async_redact_data`
    matches on field NAMES, so a uuid sitting in a plain list is invisible
    to it: `/v1/auth/me` returns `devices` as an array of device ids, and
    the field is called `devices`, not anything identifier-shaped. Neither
    defence looked at it.
    """
    if isinstance(value, list):
        return [
            "**REDACTED**"
            if isinstance(item, str) and _UUID_RE.fullmatch(item)
            else redact_identifier_keys(item)
            for item in value
        ]
    if not isinstance(value, dict):
        return value

    result: dict[object, object] = {}
    redacted_index = 0
    for key, item in value.items():
        safe_key: object = key
        if isinstance(key, str) and _UUID_RE.fullmatch(key):
            while True:
                redacted_index += 1
                candidate = f"**REDACTED_KEY_{redacted_index}**"
                if candidate not in value and candidate not in result:
                    safe_key = candidate
                    break
        result[safe_key] = redact_identifier_keys(item)
    return result


def omit_sensitive_diagnostic_branches(value: object) -> object:
    """Remove raw biometric, schedule, occupancy, and timeline branches."""
    if isinstance(value, list):
        return [omit_sensitive_diagnostic_branches(item) for item in value]
    if not isinstance(value, dict):
        return value
    return {
        key: omit_sensitive_diagnostic_branches(item)
        for key, item in value.items()
        if key not in _SENSITIVE_DIAGNOSTIC_BRANCHES
    }
