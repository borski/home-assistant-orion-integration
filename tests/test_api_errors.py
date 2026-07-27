"""API failure handling: nothing crashes, nothing leaks.

`api.py` imports aiohttp, which is deliberately absent from the test
environment, so this file never imports it. Instead it parses the module as
source and asserts structurally, plus exercises the pure helpers in `util`
that the error paths delegate to.

The structural half exists because of a real regression. Exception messages
once carried the full request path, and live and action paths contain the
device serial number. That was fixed. These tests are what stop it coming
back: they fail the moment anyone interpolates a path, a URL, a body, or a
vendor-supplied string into an exception.
"""

import _orion

util = _orion.load("util")
API = _orion.tree("api")
API_SOURCE = _orion.source("api")

ORION_EXCEPTIONS = {"OrionApiError", "OrionAuthError", "OrionConnectionError"}

# Exception messages may interpolate ONLY these expressions.
#
# An allowlist, not a blocklist, and that choice is the whole point. A new
# substitution fails this test until a human looks at it. A blocklist would
# silently pass anything nobody thought to ban.
#
#   method            HTTP verb. No identifiers.
#   resp.status       Integer status code.
#   type(err).__name__  Exception CLASS name, never its message, which for
#                     aiohttp errors routinely contains the full URL.
#   keys              Sorted top-level response KEY names. Never values.
ALLOWED_INTERPOLATIONS = {
    "method",
    "resp.status",
    "type(err).__name__",
    "keys",
}

EXPECTED_RAISE_SITES = 6


def _orion_raises():
    return list(_orion.raise_sites(API, ORION_EXCEPTIONS))


# ── Structural leak guards ────────────────────────────────────────────


def test_raise_site_count_is_pinned():
    """A new raise site must be reviewed, not silently inherited."""
    sites = [(lineno, name) for lineno, name, _ in _orion_raises()]
    assert len(sites) == EXPECTED_RAISE_SITES, (
        f"expected {EXPECTED_RAISE_SITES} Orion raise sites, found {len(sites)}: "
        f"{sites}. If you added one, confirm its message leaks nothing and "
        "bump EXPECTED_RAISE_SITES."
    )


def test_exception_messages_interpolate_only_allowlisted_expressions():
    offenders = []
    for lineno, name, call in _orion_raises():
        for expr in _orion.interpolations(call):
            if expr not in ALLOWED_INTERPOLATIONS:
                offenders.append((lineno, name, expr))
    assert offenders == [], (
        f"exception messages may only interpolate {sorted(ALLOWED_INTERPOLATIONS)}. "
        f"Found: {offenders}"
    )


def test_no_exception_message_interpolates_an_identifier():
    """The specific regression: request paths carried the device serial."""
    banned = (
        "path",
        "url",
        "json_data",
        "body",
        "params",
        "serial",
        "device_id",
        "user_id",
        "email",
        "phone",
        "token",
        "resp.text",
        "str(err)",
        "payload",
        "data",
        "value",
    )
    offenders = []
    for lineno, name, call in _orion_raises():
        for expr in _orion.interpolations(call):
            lowered = expr.lower()
            for token in banned:
                if token in lowered:
                    offenders.append((lineno, name, expr, token))
    assert offenders == [], f"identifier leaked into an exception message: {offenders}"


def test_request_helper_never_formats_the_path_into_an_error():
    """Guards `_request` specifically, where `path` and `url` are in scope."""
    request_fn = _orion.function(API, "_request")
    for lineno, name, call in _orion.raise_sites(request_fn, ORION_EXCEPTIONS):
        for expr in _orion.interpolations(call):
            assert expr in ALLOWED_INTERPOLATIONS, (
                f"_request leaks `{expr}` at line {lineno} via {name}"
            )


def test_every_exception_message_has_a_literal_prefix():
    """Catches `raise OrionApiError(await resp.text())` style leaks."""
    for lineno, name, call in _orion_raises():
        message = call.args[0] if call.args else None
        assert message is not None, f"line {lineno}: {name} raised with no message"
        if isinstance(message, __import__("ast").Constant):
            assert isinstance(message.value, str) and message.value.strip()
            continue
        literals = [
            part.value
            for part in getattr(message, "values", [])
            if isinstance(part, __import__("ast").Constant)
        ]
        assert any(text.strip() for text in literals), (
            f"line {lineno}: {name} message is bare interpolation with no literal text"
        )


def test_connection_errors_report_only_an_exception_class_name():
    """aiohttp error strings routinely contain the full URL. Never use them."""
    assert "type(err).__name__" in API_SOURCE
    assert "str(err)" not in API_SOURCE


def test_response_bodies_are_never_awaited_into_a_message():
    for _, _, call in _orion_raises():
        for expr in _orion.interpolations(call):
            assert "text()" not in expr
            assert "json()" not in expr


# ── Malformed vendor payloads never raise ─────────────────────────────

MALFORMED = [
    {"error": []},
    {"error": None},
    {"error": {}},
    {"error": 0},
    {"error": True},
    {"error": {"nested": "dict"}},
    {"code": None},
    {"message": []},
    {"error": [], "code": {}, "message": None},
    [],
    ["error"],
    "a bare string",
    b"bytes",
    0,
    None,
    True,
    3.5,
    {},
]


def test_safe_api_error_code_never_raises():
    for payload in MALFORMED:
        result = util.safe_api_error_code(payload)
        assert result is None or isinstance(result, str)


def test_describe_api_error_never_raises_and_always_returns_text():
    for payload in MALFORMED:
        described = util.describe_api_error(payload)
        assert isinstance(described, str) and described.strip()


def test_unhashable_error_values_survive_membership_checks():
    """`KNOWN_PHRASE in candidates` must tolerate dicts and lists."""
    for payload in ({"error": {"a": 1}}, {"code": [1, 2]}, {"message": {"b": []}}):
        assert util.safe_api_error_code(payload) is None


# ── Only allowlisted codes escape, never vendor prose ─────────────────

VENDOR_SECRETS = [
    "No account exists for someone@example.com",
    "Device AA11BB22CC33 not found",
    "Invalid code for +14155551234",
    "Bearer eyJhbGciOiJIUzI1NiJ9.payload.signature",
    "user 11111111-1111-4111-8111-111111111111 has no schedule",
]


def test_safe_api_error_code_never_echoes_vendor_text():
    for secret in VENDOR_SECRETS:
        for key in ("error", "code", "message"):
            assert util.safe_api_error_code({key: secret}) is None


def test_describe_api_error_never_echoes_vendor_text():
    for secret in VENDOR_SECRETS:
        described = util.describe_api_error({"error": secret, "message": secret})
        assert secret not in described
        for fragment in ("@", "AA11B", "+1", "eyJ", "Bearer", "11111111"):
            assert fragment not in described
        assert described == "unrecognized error, keys: error, message"


def test_describe_api_error_returns_key_names_only():
    described = util.describe_api_error(
        {"detail": "leak me", "trace_id": "abc", "serial_number": "AA11BB22CC33"}
    )
    assert described == "unrecognized error, keys: detail, serial_number, trace_id"
    assert "leak me" not in described
    assert "AA11BB22CC33" not in described


def test_describe_api_error_is_deterministic():
    assert util.describe_api_error({"z": 1, "a": 2}) == util.describe_api_error(
        {"a": 2, "z": 1}
    )


# ── Malformed successful auth responses ───────────────────────────────

AUTH_SHAPES = [
    {},
    None,
    [],
    "string",
    0,
    {"response": None},
    {"response": []},
    {"response": "string"},
    {"response": {}},
    {"response": {"session": None}},
    {"response": {"session": []}},
    {"response": {"session": "string"}},
    {"session": {"access_token": "a"}},
    {"access_token": "a"},
]


def test_auth_session_from_response_never_raises():
    for shape in AUTH_SHAPES:
        for allow_top_level in (False, True):
            result = util.auth_session_from_response(
                shape, allow_top_level=allow_top_level
            )
            assert result is None or isinstance(result, dict)


PARTIAL_SESSIONS = [
    {"access_token": "a"},
    {"refresh_token": "r"},
    {"access_token": "a", "refresh_token": None},
    {"access_token": "a", "refresh_token": ""},
    {"access_token": "", "refresh_token": "r"},
    {"access_token": "a", "refresh_token": 5},
    {"access_token": [], "refresh_token": "r"},
    {"access_token": True, "refresh_token": "r"},
    {},
]


def test_auth_tokens_from_session_returns_none_instead_of_raising():
    """The real bug was a KeyError, which is not an OrionApiError.

    `auth_session_from_response` only validated `access_token`, then both
    callers subscripted `session["refresh_token"]`. A successful response
    missing that key raised KeyError, which bypassed every coordinator
    handler because it is not part of the Orion exception hierarchy.
    """
    for session in PARTIAL_SESSIONS:
        assert util.auth_tokens_from_session(session) is None


def test_api_uses_the_shared_token_extractor():
    """Guards the fix. Direct subscripting reintroduces the KeyError."""
    assert "auth_tokens_from_session" in API_SOURCE
    assert 'session["refresh_token"]' not in API_SOURCE
    assert 'session["access_token"]' not in API_SOURCE


def test_api_delegates_expiry_arithmetic_to_util():
    """Keeps one definition of "is this token stale", not two that drift."""
    assert "should_refresh_token" in API_SOURCE
