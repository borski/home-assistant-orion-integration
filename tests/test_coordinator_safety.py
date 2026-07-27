"""Coordinator safety: account isolation, handler ordering, malformed data.

`coordinator.py` imports Home Assistant, so it is never imported here. Its
control flow is asserted structurally with the stdlib `ast` module, and the
pure logic it delegates to is exercised directly through `util`.

Three invariants matter enough to be enforced mechanically, because all
three fail silently rather than loudly.
"""

import ast

import _orion

util = _orion.load("util")
COORD = _orion.tree("coordinator")
COORD_SOURCE = _orion.source("coordinator")

AUTH_EXC = "OrionAuthError"
GENERAL_EXC = {"OrionApiError", "OrionConnectionError"}


def _handler_names(handler: ast.ExceptHandler) -> list[str]:
    if handler.type is None:
        return ["bare"]
    if isinstance(handler.type, ast.Tuple):
        return [ast.unparse(e) for e in handler.type.elts]
    return [ast.unparse(handler.type)]


def _try_blocks():
    return [node for node in ast.walk(COORD) if isinstance(node, ast.Try)]


# ── Invariant 1: subclass ordering ────────────────────────────────────
#
# OrionAuthError SUBCLASSES OrionApiError. Python matches handlers top to
# bottom, so listing the parent first swallows every auth error into the
# generic branch. The integration then logs a warning and carries on with a
# dead token instead of prompting for reauth, and the only symptom is that
# data quietly stops updating.


def test_auth_handler_always_precedes_the_general_handler():
    offenders = []
    for block in _try_blocks():
        names = [n for handler in block.handlers for n in _handler_names(handler)]
        if AUTH_EXC not in names:
            continue
        auth_at = names.index(AUTH_EXC)
        general_at = [i for i, n in enumerate(names) if n in GENERAL_EXC]
        if general_at and min(general_at) < auth_at:
            offenders.append((block.lineno, names))
    assert offenders == [], (
        "OrionAuthError subclasses OrionApiError, so it must be caught FIRST. "
        f"These blocks would swallow auth failures: {offenders}"
    )


def test_no_bare_except_swallows_an_orion_error():
    offenders = [
        (block.lineno, _handler_names(handler))
        for block in _try_blocks()
        for handler in block.handlers
        if handler.type is None
    ]
    assert offenders == [], f"bare except in coordinator: {offenders}"


# ── Invariant 2: a partner failure must not reauth the primary ────────
#
# ConfigEntryAuthFailed launches Home Assistant's reauth flow, and that flow
# re-verifies CONF_AUTH_VALUE, the PRIMARY account's email or phone. Raising
# it because the PARTNER token expired prompts the wrong person, and
# completing the flow cannot fix the partner token anyway.


def _raises_config_entry_auth_failed(node: ast.AST) -> bool:
    for child in ast.walk(node):
        if isinstance(child, ast.Raise) and isinstance(child.exc, ast.Call):
            func = child.exc.func
            name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
            if name == "ConfigEntryAuthFailed":
                return True
    return False


def test_partner_client_calls_never_raise_config_entry_auth_failed():
    offenders = []
    for block in _try_blocks():
        body = "\n".join(ast.unparse(stmt) for stmt in block.body)
        if "partner_api_client" not in body:
            continue
        for handler in block.handlers:
            if _raises_config_entry_auth_failed(handler):
                offenders.append((block.lineno, _handler_names(handler)))
    assert offenders == [], (
        "a partner auth failure must not launch the primary account's reauth "
        f"flow. Offending blocks: {offenders}"
    )


def test_config_entry_auth_failed_only_guards_primary_client_calls():
    offenders = []
    for block in _try_blocks():
        if not any(_raises_config_entry_auth_failed(h) for h in block.handlers):
            continue
        body = "\n".join(ast.unparse(stmt) for stmt in block.body)
        if "self.api_client" not in body:
            offenders.append((block.lineno, body.splitlines()[:2]))
    assert offenders == [], (
        f"ConfigEntryAuthFailed raised outside a primary-client call: {offenders}"
    )


def test_partner_failure_records_state_instead_of_raising():
    """The partner path must degrade, not explode."""
    assert "partner_update_ok = False" in COORD_SOURCE


# ── Invariant 3: a transient fetch failure must not blank entities ────
#
# `_async_update_data` builds a fresh dict every poll. Initialising a key to
# {} instead of carrying the previous value forward means one 502 turns every
# dependent entity `unknown` until the next successful poll, which by default
# is ten minutes away.

CARRIED_FORWARD_KEYS = ("schedules", "insights", "partner_insights")


def test_poll_data_carries_forward_instead_of_blanking():
    update_fn = _orion.function(COORD, "_async_update_data")
    body = ast.unparse(update_fn)
    missing = []
    for key in CARRIED_FORWARD_KEYS:
        initialiser = f"'{key}': (self.data or {{}}).get('{key}', {{}})"
        if initialiser not in body:
            missing.append(key)
    assert missing == [], (
        "these keys are re-initialised empty each poll, so one transient "
        f"failure blanks every entity that reads them: {missing}"
    )


# ── Malformed vendor data never raises ────────────────────────────────

HOSTILE = [
    None,
    {},
    [],
    "string",
    0,
    True,
    {"insights": None},
    {"insights": []},
    {"insights": "string"},
    {"insights": {"data": None}},
    {"insights": {"data": []}},
    {"insights": {"data": "string"}},
    {"schedules": None},
    {"schedules": []},
    {"schedules": {"today_sleep_schedule": None}},
    {"schedules": {"today_sleep_schedule": []}},
]


def test_nested_mapping_never_raises_on_hostile_shapes():
    for payload in HOSTILE:
        assert util.nested_mapping(payload, "insights", "data") == {} or isinstance(
            util.nested_mapping(payload, "insights", "data"), dict
        )
        assert isinstance(
            util.nested_mapping(payload, "schedules", "today_sleep_schedule"), dict
        )


def test_nested_mapping_returns_empty_at_the_first_non_mapping_level():
    assert util.nested_mapping({"a": {"b": {"c": 1}}}, "a", "b") == {"c": 1}
    assert util.nested_mapping({"a": {"b": []}}, "a", "b") == {}
    assert util.nested_mapping({"a": []}, "a", "b") == {}
    assert util.nested_mapping({}, "a") == {}
    assert util.nested_mapping(None, "a") == {}


def test_session_subsection_never_raises():
    """sensor.py chains .get() twice. A list in the middle used to crash."""
    for session in (None, [], "s", 0, {}, {"sleep_summary": None},
                    {"sleep_summary": []}, {"sleep_summary": "x"}):
        assert util.session_subsection(session, "sleep_summary") == {}
    assert util.session_subsection({"sleep_summary": {"deep_sleep": 5}},
                                   "sleep_summary") == {"deep_sleep": 5}


def test_no_completed_session_is_handled_everywhere():
    """The real state of this bed until somebody finishes a night."""
    for payload in ({}, None, [], {"2026-07-26": {"sessions": []}}):
        assert util.latest_session(payload) is None
        assert util.latest_completed_session(payload) is None


def test_schedule_helpers_survive_malformed_rows():
    for row in (None, [], "x", 0, {}, {"bedtime": None}, {"bedtime": "25:00"}):
        assert util.schedule_duration_text(row) is None
    for value in (None, [], 0, True, "24:00", "7:00", "0700", ""):
        assert util.parse_schedule_time(value) is None


def test_away_state_is_unknown_rather_than_guessed_on_bad_zones():
    """A malformed zone must not resolve to a confident 'away'."""
    assert util.user_is_away({"zones": [None]}, "u1") is None
    assert util.user_is_away({"zones": [{"user": None}, None]}, "u1") is None
    assert util.user_is_away({"zones": []}, "u1") is None
    assert util.user_is_away({"zones": [{"user": {"id": "u1"}}]}, "u1") is False
    assert util.user_is_away({"zones": [{"user": {"id": "u2"}}]}, "u1") is True
