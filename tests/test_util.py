"""Tests for dependency-free utility helpers."""

import importlib.util
from pathlib import Path

MODULE_PATH = Path(__file__).parent.parent / "custom_components" / "orion_sleep" / "util.py"
SPEC = importlib.util.spec_from_file_location("orion_util", MODULE_PATH)
assert SPEC and SPEC.loader
util = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(util)


def test_dedupe_devices_keeps_first_and_preserves_order():
    devices = [
        {"id": "a", "name": "first"},
        {"id": "b", "name": "other"},
        {"id": "a", "name": "second"},
    ]
    assert [item["name"] for item in util.dedupe_devices_by_id(devices)] == [
        "first",
        "other",
    ]


def test_dedupe_devices_handles_malformed_values():
    devices = [{"id": "a"}, "bad", None, {"id": "a"}, {"name": "no-id"}]
    assert util.dedupe_devices_by_id(devices) == [
        {"id": "a"},
        {"name": "no-id"},
    ]
    assert util.dedupe_devices_by_id(None) == []


INSIGHTS = {
    "2026-05-28": {
        "sessions": [
            {"session_id": "s1", "zone_id": "zone_a"},
            {"session_id": "s2", "zone_id": "zone_b"},
        ]
    },
    "2026-05-29": {"sessions": [{"session_id": "s3", "zone_id": "zone_a"}]},
}


def test_latest_session_for_zone_uses_newest_match():
    assert util.latest_session_for_zone(INSIGHTS, "zone_a")["session_id"] == "s3"
    assert util.latest_session_for_zone(INSIGHTS, "zone_b")["session_id"] == "s2"
    assert util.latest_session_for_zone(INSIGHTS, "zone_c") is None


def test_latest_session_for_zone_handles_malformed_values():
    assert util.latest_session_for_zone(None, "zone_a") is None
    assert util.latest_session_for_zone({"date": {"sessions": "bad"}}, "zone_a") is None


def test_latest_session_uses_newest_valid_session():
    assert util.latest_session(INSIGHTS)["session_id"] == "s3"
    assert util.latest_session(None) is None
    assert util.latest_session({"date": {"sessions": [None, "bad"]}}) is None


def test_shared_device_serials_uses_physical_identity():
    primary = [
        {"id": "primary-a", "serial_number": "SERIAL-A"},
        {"id": "primary-b", "serial_number": "SERIAL-B"},
    ]
    partner = [
        {"id": "different-uuid", "serial_number": "SERIAL-A"},
        {"id": "partner-c", "serial_number": "SERIAL-C"},
    ]
    assert util.shared_device_serials(primary, partner) == {"SERIAL-A"}
    assert util.shared_device_serials(None, partner) == set()


def test_user_is_away_checks_the_requested_user():
    device = {
        "zones": [
            {"id": "zone_a", "user": {"id": "user-one"}},
            {"id": "zone_b", "user": {"id": "user-two"}},
        ]
    }
    assert util.user_is_away(device, "user-one") is False
    assert util.user_is_away(device, "user-two") is False
    assert util.user_is_away(device, "someone-else") is True
    assert util.user_is_away({"zones": []}, "user-one") is None
    assert util.user_is_away({"zones": [None]}, "user-one") is None
    assert util.user_is_away({"zones": [{"user": None}, None]}, "user-one") is None
    assert util.user_is_away({"zones": [{"user": "broken"}]}, "user-one") is None


def test_redact_identifier_keys_handles_nested_uuid_maps():
    first = "11111111-1111-4111-8111-111111111111"
    second = "22222222-2222-4222-8222-222222222222"
    data = {
        "schedules": {first: [{"day": 0}], second: [{"day": 1}]},
        "ordinary": {"value": 1},
    }
    assert util.redact_identifier_keys(data) == {
        "schedules": {
            "**REDACTED_KEY_1**": [{"day": 0}],
            "**REDACTED_KEY_2**": [{"day": 1}],
        },
        "ordinary": {"value": 1},
    }


def test_redact_identifier_keys_avoids_existing_placeholder_collision():
    user_id = "11111111-1111-4111-8111-111111111111"
    data = {"**REDACTED_KEY_1**": "existing", user_id: "sensitive"}
    assert util.redact_identifier_keys(data) == {
        "**REDACTED_KEY_1**": "existing",
        "**REDACTED_KEY_2**": "sensitive",
    }


def test_omit_sensitive_diagnostic_branches_removes_health_and_occupancy():
    data = {
        "insights": {"heart_rate": {"average": 60}},
        "schedules": {"bedtime": "22:30"},
        "live": {
            "timeline": [{"scheduled_time": "22:30"}],
            "status": {
                "online": True,
                "sensors": {"sensor1": {"heart_rate": 60}},
            },
        },
    }
    assert util.omit_sensitive_diagnostic_branches(data) == {"live": {"status": {"online": True}}}


def test_safe_api_error_code_never_returns_free_form_pii():
    assert util.safe_api_error_code({"code": "invalid_request"}) == "invalid_request"
    assert (
        util.safe_api_error_code({"error": "User has no previous device to return to"})
        == "user_already_present"
    )
    assert util.safe_api_error_code({"error": "No account for me@example.com"}) is None
    assert util.safe_api_error_code({"error": []}) is None
    assert (
        util.safe_api_error_code({"message": "User has no previous device to return to"})
        == "user_already_present"
    )


def test_describe_api_error_maps_recognized_codes():
    assert util.describe_api_error({"error": "invalid_request"}) == "code: invalid_request"
    assert (
        util.describe_api_error({"message": "User has no previous device to return to"})
        == "code: user_already_present"
    )


def test_describe_api_error_never_leaks_vendor_text():
    secret = "No account exists for me@example.com or +14155551234"
    described = util.describe_api_error({"error": secret, "message": secret})
    assert secret not in described
    assert "me@example.com" not in described
    assert "14155551234" not in described
    assert described == "unrecognized error, keys: error, message"


def test_describe_api_error_handles_empty_and_non_dict_payloads():
    assert util.describe_api_error({}) == "unrecognized error, no detail"
    assert util.describe_api_error(None) == "unrecognized error, no detail"
    assert util.describe_api_error("boom") == "unrecognized error, no detail"
    assert util.describe_api_error([{"error": "invalid_request"}]) == (
        "unrecognized error, no detail"
    )


def test_auth_session_from_response_handles_only_known_shapes():
    session = {"access_token": "access", "refresh_token": "refresh"}
    assert util.auth_session_from_response({"response": {"session": session}}) == session
    assert util.auth_session_from_response(session, allow_top_level=True) == session
    assert util.auth_session_from_response(session) is None
    assert util.auth_session_from_response([]) is None
    assert util.auth_session_from_response({"response": []}) is None


# ── Alias helpers (Phase 2) ────────────────────────────────────────────


PRIMARY_USER = "11111111-1111-4111-8111-111111111111"
PARTNER_USER = "22222222-2222-4222-8222-222222222222"


def test_orion_user_label_prefers_snake_then_camel_then_contact():
    assert util.orion_user_label({"first_name": "Ada", "name": "x"}) == "Ada"
    assert util.orion_user_label({"firstName": "Grace"}) == "Grace"
    assert util.orion_user_label({"name": "Only Name"}) == "Only Name"
    assert util.orion_user_label({"email": "a@b.c"}) == "a@b.c"


def test_orion_user_label_never_raises_on_hostile_input():
    for bad in (
        None,
        "",
        0,
        [],
        {},
        True,
        {"first_name": None},
        {"first_name": "   "},
        {"first_name": 5},
        {"name": []},
    ):
        assert util.orion_user_label(bad) == ""


def test_collect_known_users_prefers_account_objects_over_zone_copies():
    devices = [{"zones": [{"user": {"id": PRIMARY_USER}}]}]
    extra = [{"id": PRIMARY_USER, "first_name": "Ada"}]
    assert util.collect_known_users(devices, extra) == [{"id": PRIMARY_USER, "name": "Ada"}]


def test_collect_known_users_dedupes_and_skips_idless_and_malformed():
    devices = [
        {"zones": [{"user": {"id": PRIMARY_USER, "first_name": "Ada"}}]},
        {"zones": [{"user": {"id": PRIMARY_USER}}, {"user": {"first_name": "no id"}}]},
        "bad",
        {"zones": "bad"},
        None,
    ]
    assert util.collect_known_users(devices) == [{"id": PRIMARY_USER, "name": "Ada"}]
    assert util.collect_known_users() == []
    assert util.collect_known_users(None, None) == []
    assert util.collect_known_users("bad", "bad") == []


def test_unique_alias_labels_disambiguates_shared_names():
    labels = util.unique_alias_labels(
        [{"id": PRIMARY_USER, "name": "Alex"}, {"id": PARTNER_USER, "name": "Alex"}]
    )
    assert labels[PRIMARY_USER] == "Alex"
    assert labels[PARTNER_USER] == "Alex (2)"
    assert len(set(labels.values())) == 2


def test_unique_alias_labels_falls_back_and_skips_unusable_records():
    labels = util.unique_alias_labels([{"id": PRIMARY_USER, "name": ""}])
    assert labels[PRIMARY_USER] == f"User {PRIMARY_USER[:8]}"
    skipped = util.unique_alias_labels(
        [None, "bad", {}, {"name": "no id"}, {"id": ""}, {"id": 5}, {"id": PARTNER_USER}]
    )
    assert list(skipped) == [PARTNER_USER]
    assert util.unique_alias_labels(None) == {}


def test_clean_alias_map_drops_blank_values_so_clearing_removes_the_override():
    for blank in ("", "   ", "\t\n"):
        assert util.clean_alias_map({PARTNER_USER: blank}) == {}
    assert util.clean_alias_map({PARTNER_USER: "  Sam  "}) == {PARTNER_USER: "Sam"}


def test_clean_alias_map_discards_unknown_ids_and_non_string_values():
    known = {PRIMARY_USER, PARTNER_USER}
    stale = "99999999-9999-4999-8999-999999999999"
    assert util.clean_alias_map({stale: "Ghost"}, known) == {}
    assert util.clean_alias_map({PRIMARY_USER: "Alex", stale: "Ghost"}, known) == {
        PRIMARY_USER: "Alex"
    }
    for bad in (None, 0, 1, [], {}, True, 3.5):
        assert util.clean_alias_map({PARTNER_USER: bad}, known) == {}
    for bad in (None, [], "x", 0, {"": "blank"}, {5: "int"}):
        assert util.clean_alias_map(bad, known) == {}


# ── Crash guards ───────────────────────────────────────────────────────


def test_nested_mapping_returns_empty_at_the_first_non_mapping_level():
    data = {"insights": {"data": {"2026-07-26": {}}}}
    assert util.nested_mapping(data, "insights", "data") == {"2026-07-26": {}}
    assert util.nested_mapping({"insights": []}, "insights", "data") == {}
    assert util.nested_mapping({"insights": {"data": []}}, "insights", "data") == {}
    assert util.nested_mapping(None, "insights") == {}
    assert util.nested_mapping([], "insights") == {}
    assert util.nested_mapping("x", "insights") == {}
    assert util.nested_mapping({}, "a", "b", "c") == {}


def test_session_subsection_never_returns_a_non_mapping():
    assert util.session_subsection({"hrv": {"average": 40}}, "hrv") == {"average": 40}
    for bad in ({"hrv": []}, {"hrv": None}, {"hrv": "x"}, {}, None, [], "x", 0):
        assert util.session_subsection(bad, "hrv") == {}


def test_auth_tokens_from_session_requires_both_tokens():
    good = {"access_token": "a", "refresh_token": "r", "expires_at": 123.5}
    assert util.auth_tokens_from_session(good) == good
    partial = [
        {"access_token": "a"},
        {"refresh_token": "r"},
        {"access_token": "a", "refresh_token": None},
        {"access_token": "a", "refresh_token": ""},
        {"access_token": "", "refresh_token": "r"},
        {"access_token": "a", "refresh_token": 5},
        {"access_token": True, "refresh_token": "r"},
        {},
        None,
        [],
        "x",
    ]
    for session in partial:
        assert util.auth_tokens_from_session(session) is None


def test_auth_tokens_from_session_defaults_a_bad_expiry_to_zero():
    for bad in (None, "soon", [], {}, True):
        tokens = util.auth_tokens_from_session(
            {"access_token": "a", "refresh_token": "r", "expires_at": bad}
        )
        assert tokens is not None and tokens["expires_at"] == 0
    tokens = util.auth_tokens_from_session({"access_token": "a", "refresh_token": "r"})
    assert tokens is not None and tokens["expires_at"] == 0


def test_should_refresh_token_matches_the_original_expression():
    now = 1_000_000.0
    for expires_at in (
        0,
        1,
        now - 3600,
        now - 1,
        now,
        now + 1,
        now + 59,
        now + 60,
        now + 61,
        now + 3600,
    ):
        for margin in (0, 1, 60, 300):
            assert util.should_refresh_token(expires_at, now, margin) is (
                now + margin >= expires_at
            )


def test_should_refresh_token_treats_unknown_expiry_as_expired():
    now = 1_000_000.0
    for bad in (None, "", "soon", [], {}, (), b"0", True, False):
        assert util.should_refresh_token(bad, now) is True
    assert isinstance(util.should_refresh_token(now + 10, now), bool)
