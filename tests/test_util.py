"""Tests for dependency-free utility helpers."""

import datetime
from datetime import time as _dt_time

import _orion

util = _orion.load("util")


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


def test_session_in_progress_only_trusts_an_explicit_true():
    assert util.session_in_progress({"is_in_progress": True}) is True
    assert util.session_in_progress({"is_in_progress": False}) is False
    # A missing or malformed flag reads as finished. Hiding a completed
    # night behind a field the vendor forgot to send is the worse failure.
    assert util.session_in_progress({}) is False
    assert util.session_in_progress({"is_in_progress": None}) is False
    assert util.session_in_progress({"is_in_progress": "true"}) is False
    assert util.session_in_progress({"is_in_progress": 1}) is False
    assert util.session_in_progress(None) is False
    assert util.session_in_progress([]) is False


def test_latest_completed_session_ignores_a_night_in_progress():
    # Measured shape: the vendor fills end_time in WHILE is_in_progress
    # is still true, so an end_time check would report this as complete.
    data = {
        "2026-07-25": {
            "sessions": [
                {"session_id": "old", "is_in_progress": False, "end_time": "2026-07-25T14:00:00Z"}
            ]
        },
        "2026-07-26": {
            "sessions": [
                {"session_id": "live", "is_in_progress": True, "end_time": "2026-07-27T06:00:00Z"}
            ]
        },
    }
    assert util.latest_session(data)["session_id"] == "live"
    assert util.latest_completed_session(data)["session_id"] == "old"


def test_latest_completed_session_prefers_the_newest_finished_one():
    data = {
        "2026-07-24": {"sessions": [{"session_id": "older", "is_in_progress": False}]},
        "2026-07-25": {"sessions": [{"session_id": "newer", "is_in_progress": False}]},
    }
    assert util.latest_completed_session(data)["session_id"] == "newer"


def test_latest_completed_session_handles_empty_and_malformed():
    assert util.latest_completed_session(None) is None
    assert util.latest_completed_session({}) is None
    assert util.latest_completed_session([]) is None
    assert util.latest_completed_session({"2026-07-26": None}) is None
    assert util.latest_completed_session({"2026-07-26": {"sessions": "bad"}}) is None
    assert util.latest_completed_session({"2026-07-26": {"sessions": [None, "x"]}}) is None
    # Every session still running means there is nothing completed yet.
    running = {"2026-07-26": {"sessions": [{"is_in_progress": True}]}}
    assert util.latest_completed_session(running) is None


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
    assert util.clean_alias_map({PARTNER_USER: "  Grace  "}) == {PARTNER_USER: "Grace"}


def test_clean_alias_map_discards_unknown_ids_and_non_string_values():
    known = {PRIMARY_USER, PARTNER_USER}
    stale = "99999999-9999-4999-8999-999999999999"
    assert util.clean_alias_map({stale: "Ghost"}, known) == {}
    assert util.clean_alias_map({PRIMARY_USER: "Ada", stale: "Ghost"}, known) == {
        PRIMARY_USER: "Ada"
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


def test_schedule_field_sets_are_disjoint_and_complete():
    """The three groups must not overlap: each field has exactly one validator."""
    temps = util.SCHEDULE_TEMPERATURE_FIELDS
    times = util.SCHEDULE_TIME_FIELDS
    flags = util.SCHEDULE_FLAG_FIELDS
    assert temps.isdisjoint(times)
    assert temps.isdisjoint(flags)
    assert times.isdisjoint(flags)
    assert util.SCHEDULE_WRITABLE_FIELDS == temps | times | flags
    assert len(util.SCHEDULE_WRITABLE_FIELDS) == 10


def test_validate_schedule_write_accepts_each_field_group():
    util.validate_schedule_write(0, "bedtime_temp", 23.0)
    util.validate_schedule_write(6, "phase_1_temp", 17)
    util.validate_schedule_write(3, "wakeup", "07:00")
    util.validate_schedule_write(3, "bedtime", "23:59")
    util.validate_schedule_write(0, "bedtime", "00:00")
    util.validate_schedule_write(1, "auto_turn_off", True)
    util.validate_schedule_write(1, "is_smart_temperature_active", False)


def test_validate_schedule_write_rejects_unknown_fields():
    for field in ("override_date", "day", "is_override_applied", "", None, 5):
        try:
            util.validate_schedule_write(0, field, 1)
        except ValueError:
            continue
        raise AssertionError(f"{field!r} should not be writable")


def test_validate_schedule_write_rejects_bad_days():
    for day in (-1, 7, 100, None, "0", 1.5, True, False):
        try:
            util.validate_schedule_write(day, "bedtime_temp", 23)
        except ValueError:
            continue
        raise AssertionError(f"day={day!r} should be rejected")


def test_validate_schedule_write_rejects_malformed_times():
    # 24:00 and 07:60 are the off-by-one cases a naive regex lets through.
    for value in ("24:00", "07:60", "7:00", "0700", "07:00:00", "", None, 700, True):
        try:
            util.validate_schedule_write(0, "wakeup", value)
        except ValueError:
            continue
        raise AssertionError(f"wakeup={value!r} should be rejected")


def test_validate_schedule_write_rejects_bool_for_numeric_fields():
    """bool subclasses int, so True would silently become 1 degree Celsius."""
    for value in (True, False):
        try:
            util.validate_schedule_write(0, "bedtime_temp", value)
        except ValueError:
            continue
        raise AssertionError(f"bedtime_temp={value!r} should be rejected")


def test_validate_schedule_write_rejects_non_bool_for_flag_fields():
    for value in (1, 0, "true", None, [], 1.0):
        try:
            util.validate_schedule_write(0, "auto_turn_off", value)
        except ValueError:
            continue
        raise AssertionError(f"auto_turn_off={value!r} should be rejected")


# ── Schedule unique_id scheme ────────────────────────────────────────
#
# The failure this guards: two entities resolving to one unique_id, or an
# id that moves when something cosmetic changes. Either registers a
# duplicate in Home Assistant with `_2` appended, and both keep working.
#
# The scheme is deliberately uniform across everyone on the bed. There is
# no special case for the authenticated account.

_DEVICE = "dev-1"
_PRIMARY = "11111111-1111-4111-8111-111111111111"
_PARTNER = "22222222-2222-4222-8222-222222222222"

# Every schedule entity key across all four platforms.
_SCHEDULE_KEYS = (
    # sensor
    "schedule_duration",
    "bedtime_temp",
    "phase_1_temp",
    "phase_2_temp",
    "wakeup_temp",
    # number
    "bedtime_temp_offset",
    "phase_1_temp_offset",
    "phase_2_temp_offset",
    "wakeup_temp_offset",
    # time
    "bedtime",
    "wakeup_time",
    # switch
    "bedtime_is_active",
    "wakeup_is_active",
    "auto_turn_off",
    "is_smart_temperature_active",
    # binary_sensor
    "is_override_applied",
)


def _all_ids(users=(_PRIMARY, _PARTNER)):
    return [
        util.schedule_unique_id(_DEVICE, key, user_id)
        for user_id in users
        for key in _SCHEDULE_KEYS
    ]


def test_every_schedule_id_is_namespaced_by_user():
    """No special case for the authenticated account."""
    for key in _SCHEDULE_KEYS:
        assert (
            util.schedule_unique_id(_DEVICE, key, _PRIMARY)
            == f"{_DEVICE}_user_{_PRIMARY}_{key}"
        )
        assert (
            util.schedule_unique_id(_DEVICE, key, _PARTNER)
            == f"{_DEVICE}_user_{_PARTNER}_{key}"
        )


def test_no_duplicate_ids_across_two_or_three_people():
    two = _all_ids()
    assert len(two) == len(set(two)) == len(_SCHEDULE_KEYS) * 2
    third = "33333333-3333-4333-8333-333333333333"
    three = _all_ids((_PRIMARY, _PARTNER, third))
    assert len(three) == len(set(three)) == len(_SCHEDULE_KEYS) * 3


def test_the_two_people_never_share_an_id():
    mine = {util.schedule_unique_id(_DEVICE, k, _PRIMARY) for k in _SCHEDULE_KEYS}
    theirs = {util.schedule_unique_id(_DEVICE, k, _PARTNER) for k in _SCHEDULE_KEYS}
    assert mine.isdisjoint(theirs)


def test_two_devices_never_collide():
    a = {util.schedule_unique_id("dev-a", k, _PRIMARY) for k in _SCHEDULE_KEYS}
    b = {util.schedule_unique_id("dev-b", k, _PRIMARY) for k in _SCHEDULE_KEYS}
    assert a.isdisjoint(b)


def test_temperature_and_offset_keys_resolve_to_distinct_ids():
    """`wakeup_temp` is a sensor, `wakeup_temp_offset` is a number.

    Copying the wrong key into the wrong platform is the single most
    likely way a duplicate ships.
    """
    for base in ("wakeup_temp", "bedtime_temp", "phase_1_temp", "phase_2_temp"):
        plain = util.schedule_unique_id(_DEVICE, base, _PRIMARY)
        offset = util.schedule_unique_id(_DEVICE, f"{base}_offset", _PRIMARY)
        assert plain != offset
        assert plain.endswith(f"_{base}")
        assert offset.endswith(f"_{base}_offset")


def test_unique_id_is_deterministic():
    assert _all_ids() == _all_ids()


# ── Schedule time parsing ────────────────────────────────────────────


def test_parse_schedule_time_accepts_wall_clock():
    assert util.parse_schedule_time("23:00") == _dt_time(23, 0)
    assert util.parse_schedule_time("00:00") == _dt_time(0, 0)
    assert util.parse_schedule_time("07:45") == _dt_time(7, 45)
    assert util.parse_schedule_time("23:59") == _dt_time(23, 59)


def test_parse_schedule_time_rejects_anything_malformed():
    for bad in (
        None, "", "nope", "24:00", "23:60", "7:00", "0700", "23:00:00",
        2300, [], {}, True, "-1:00", "23:0",
    ):
        assert util.parse_schedule_time(bad) is None


# ── Schedule duration ────────────────────────────────────────────────


def test_schedule_duration_handles_the_overnight_rollover():
    assert util.schedule_duration_text({"bedtime": "23:00", "wakeup": "07:00"}) == "8h 0m"
    assert util.schedule_duration_text({"bedtime": "22:15", "wakeup": "06:45"}) == "8h 30m"
    assert util.schedule_duration_text({"bedtime": "01:00", "wakeup": "09:20"}) == "8h 20m"


def test_schedule_duration_treats_equal_times_as_a_full_day():
    assert util.schedule_duration_text({"bedtime": "23:00", "wakeup": "23:00"}) == "24h 0m"


def test_schedule_duration_rejects_malformed_input():
    for bad in (
        None,
        {},
        "nope",
        {"bedtime": "23:00"},
        {"wakeup": "07:00"},
        {"bedtime": "24:00", "wakeup": "07:00"},
        {"bedtime": "23:00", "wakeup": "07:60"},
        {"bedtime": "2300", "wakeup": "07:00"},
        {"bedtime": 2300, "wakeup": "07:00"},
        {"bedtime": "23:00", "wakeup": None},
    ):
        assert util.schedule_duration_text(bad) is None


# ── Timeline helpers ──────────────────────────────────────────────────

_UTC = datetime.timezone.utc
NOW = datetime.datetime(2026, 7, 26, 22, 0, tzinfo=_UTC)


def _entry(user, when, label="bedtime", zones=None):
    return {
        "user_id": user,
        "label": label,
        "scheduled_time": when,
        "action": {"zones": zones or []},
    }


# ── Rapid cool duration ───────────────────────────────────────────────
#
# This number is sent to a route that changes the physical bed, so the
# failure that matters is not a wrong value, it is a plausible-looking
# wrong value. Zero, True, and None all have to land on the default
# rather than on something the server has never been asked to honour.

_D, _LO, _HI = 30, 1, 240


def test_clamp_cooling_minutes_accepts_sensible_values():
    assert util.clamp_cooling_minutes(45, _D, _LO, _HI) == 45
    assert util.clamp_cooling_minutes(45.0, _D, _LO, _HI) == 45
    assert util.clamp_cooling_minutes(_LO, _D, _LO, _HI) == _LO
    assert util.clamp_cooling_minutes(_HI, _D, _LO, _HI) == _HI


def test_clamp_cooling_minutes_rounds_rather_than_truncates():
    assert util.clamp_cooling_minutes(29.6, _D, _LO, _HI) == 30
    assert util.clamp_cooling_minutes(29.4, _D, _LO, _HI) == 29


def test_clamp_cooling_minutes_saturates_out_of_range():
    assert util.clamp_cooling_minutes(0, _D, _LO, _HI) == _LO
    assert util.clamp_cooling_minutes(-500, _D, _LO, _HI) == _LO
    assert util.clamp_cooling_minutes(99999, _D, _LO, _HI) == _HI


def test_clamp_cooling_minutes_rejects_bool_rather_than_reading_it_as_one():
    """True is an int. A one-minute cooling window is not what was meant."""
    assert util.clamp_cooling_minutes(True, _D, _LO, _HI) == _D
    assert util.clamp_cooling_minutes(False, _D, _LO, _HI) == _D


def test_clamp_cooling_minutes_falls_back_on_anything_unusable():
    for bad in (None, "", "30", [], {}, (), object(), b"30", float("nan")):
        got = util.clamp_cooling_minutes(bad, _D, _LO, _HI)
        assert got == _D, f"{bad!r} produced {got}"


def test_clamp_cooling_minutes_treats_infinity_as_unusable_not_as_the_maximum():
    """Infinity falls back to the default rather than clamping to the top.

    Clamping would be the tidier arithmetic and the worse outcome: it
    turns a nonsense value into the longest cooling window the bed will
    accept. Nothing legitimate produces infinity here, so the safe read
    is that the input is broken, not that someone wanted four hours.
    """
    assert util.clamp_cooling_minutes(float("inf"), _D, _LO, _HI) == _D
    assert util.clamp_cooling_minutes(float("-inf"), _D, _LO, _HI) == _D


def test_clamp_cooling_minutes_always_returns_a_real_int():
    for value in (45, 45.7, None, True, 99999, -1):
        got = util.clamp_cooling_minutes(value, _D, _LO, _HI)
        assert isinstance(got, int) and not isinstance(got, bool)


# ── Session deletion ──────────────────────────────────────────────────
#
# This is the only irreversible call in the integration. The tests below
# are deliberately paranoid about what gets through, because the failure
# mode is a permanently destroyed night rather than an error message.


def test_delete_reasons_are_exactly_the_two_the_app_sends():
    assert util.SESSION_DELETE_REASONS == {"not_real_session", "no_longer_needed"}


def test_validate_session_delete_reason_accepts_only_those_two():
    for good in ("not_real_session", "no_longer_needed"):
        assert util.validate_session_delete_reason(good) == good


def test_validate_session_delete_reason_rejects_everything_else():
    bad = [
        "", " ", "NOT_REAL_SESSION", "not real session", "phantom",
        "delete", None, 0, 1, True, [], {}, ["not_real_session"],
        " not_real_session", "not_real_session ",
    ]
    for value in bad:
        try:
            util.validate_session_delete_reason(value)
        except ValueError:
            continue
        raise AssertionError(f"{value!r} was accepted and should not have been")


def test_validate_session_delete_reason_error_names_the_allowed_values():
    """A rejection has to say what would work, or it just blocks the user."""
    try:
        util.validate_session_delete_reason("nope")
    except ValueError as err:
        assert "not_real_session" in str(err)
        assert "no_longer_needed" in str(err)
    else:
        raise AssertionError("expected ValueError")


_INSIGHTS = {
    "2026-07-26": {
        "score": 71,
        "sessions": [
            {
                "session_id": "sess-old",
                "zone_id": "zone_a",
                "start_time": "2026-07-26T23:10:00Z",
                "end_time": "2026-07-27T06:50:00Z",
                "is_in_progress": False,
                "sleep_summary": {"time_asleep": 412.4},
            }
        ],
    },
    "2026-07-27": {
        "score": 68,
        "sessions": [
            {
                "session_id": "sess-phantom",
                "zone_id": "zone_a",
                "start_time": "2026-07-27T01:00:00Z",
                "end_time": "2026-07-27T02:00:00Z",
                "is_in_progress": False,
                "sleep_summary": {"time_asleep": 60},
            },
            {
                "session_id": "sess-running",
                "zone_id": "zone_b",
                "start_time": "2026-07-27T03:00:00Z",
                "end_time": None,
                "is_in_progress": True,
                "sleep_summary": {},
            },
        ],
    },
}


def test_summarize_sessions_is_newest_first():
    rows = util.summarize_sessions(_INSIGHTS)
    assert [r["session_id"] for r in rows] == [
        "sess-phantom",
        "sess-running",
        "sess-old",
    ]


def test_summarize_sessions_carries_what_is_needed_to_pick_one():
    rows = util.summarize_sessions(_INSIGHTS)
    phantom = rows[0]
    assert phantom["session_id"] == "sess-phantom"
    assert phantom["date"] == "2026-07-27"
    assert phantom["zone_id"] == "zone_a"
    assert phantom["in_progress"] is False
    assert phantom["minutes_asleep"] == 60
    assert phantom["day_score"] == 68


def test_summarize_sessions_flags_a_running_session_rather_than_hiding_it():
    """A running session is exactly the kind someone may want deleted."""
    rows = util.summarize_sessions(_INSIGHTS)
    running = [r for r in rows if r["session_id"] == "sess-running"][0]
    assert running["in_progress"] is True
    assert "end_time" not in running


def test_summarize_sessions_rounds_minutes_and_rejects_bool():
    rows = util.summarize_sessions(_INSIGHTS)
    assert rows[2]["minutes_asleep"] == 412
    weird = {"d": {"sessions": [{"session_id": "s", "sleep_summary": {"time_asleep": True}}]}}
    assert "minutes_asleep" not in util.summarize_sessions(weird)[0]


def test_summarize_sessions_skips_rows_with_no_usable_id():
    """No id means nothing can be deleted with it, so it is noise."""
    payload = {
        "2026-07-27": {
            "sessions": [
                {"session_id": ""},
                {"session_id": None},
                {"session_id": 123},
                {"no_id": True},
                None,
                "junk",
                {"session_id": "keep-me"},
            ]
        }
    }
    rows = util.summarize_sessions(payload)
    assert [r["session_id"] for r in rows] == ["keep-me"]


def test_summarize_sessions_honours_the_limit():
    payload = {
        f"2026-07-{d:02d}": {"sessions": [{"session_id": f"s{d}"}]} for d in range(1, 21)
    }
    assert len(util.summarize_sessions(payload, limit=5)) == 5
    assert len(util.summarize_sessions(payload)) == 20


def test_summarize_sessions_never_raises_on_hostile_input():
    for bad in (None, [], "x", 0, True, {"d": None}, {"d": {"sessions": "no"}}, {}):
        assert util.summarize_sessions(bad) == []


# ── Apnea figures ─────────────────────────────────────────────────────
#
# Zero is the answer most nights. A coercion that treats it as missing
# would make a healthy night indistinguishable from a broken sensor.


def test_apnea_number_keeps_zero():
    assert util.apnea_number(0) == 0.0
    assert util.apnea_number(0.0) == 0.0


def test_apnea_number_accepts_real_readings():
    assert util.apnea_number(0.3) == 0.3
    assert util.apnea_number(60) == 60.0
    assert util.apnea_number(31) == 31.0


def test_apnea_number_rejects_bool_so_false_is_not_a_zero_reading():
    assert util.apnea_number(False) is None
    assert util.apnea_number(True) is None


def test_apnea_number_rejects_everything_unusable():
    for bad in (None, "", "0", "0.3", [], {}, (), object(), b"0"):
        assert util.apnea_number(bad) is None


def test_apnea_number_always_returns_float_or_none():
    for value in (0, 1, 0.3, 60):
        assert isinstance(util.apnea_number(value), float)


# ── Time in bed, efficiency, confidence ───────────────────────────────


def test_duration_minutes_basic():
    assert util.duration_minutes(
        "2026-07-27T06:00:00Z", "2026-07-27T07:30:00Z"
    ) == 90.0


def test_duration_minutes_handles_millis_and_offsets():
    assert util.duration_minutes(
        "2026-07-27T06:09:49.776Z", "2026-07-27T06:39:49.776Z"
    ) == 30.0
    assert util.duration_minutes(
        "2026-07-27T00:00:00-07:00", "2026-07-27T01:00:00-07:00"
    ) == 60.0


def test_duration_minutes_rejects_reversed_pairs():
    assert util.duration_minutes(
        "2026-07-27T07:00:00Z", "2026-07-27T06:00:00Z"
    ) is None


def test_duration_minutes_rejects_unusable_input():
    for bad in (None, "", "not a time", 0, [], {}, True):
        assert util.duration_minutes(bad, "2026-07-27T07:00:00Z") is None
        assert util.duration_minutes("2026-07-27T06:00:00Z", bad) is None


def test_sleep_efficiency_basic():
    assert util.sleep_efficiency(480, 600) == 80.0
    assert util.sleep_efficiency(600, 600) == 100.0


def test_sleep_efficiency_rejects_impossible_ratios():
    # Asleep longer than in bed means one figure is wrong. Reading it as
    # a tidy 100% would hide the fault.
    assert util.sleep_efficiency(700, 600) is None


def test_sleep_efficiency_rejects_zero_and_negative_time_in_bed():
    assert util.sleep_efficiency(480, 0) is None
    assert util.sleep_efficiency(480, -10) is None


def test_sleep_efficiency_rejects_bool_and_junk():
    for bad in (True, False, None, "480", [], {}):
        assert util.sleep_efficiency(bad, 600) is None
        assert util.sleep_efficiency(480, bad) is None


def test_confidence_percent_scales_the_vendor_float():
    assert util.confidence_percent(0.8) == 80.0
    assert util.confidence_percent(0) == 0.0
    assert util.confidence_percent(1) == 100.0


def test_confidence_percent_refuses_out_of_contract_values():
    # If the vendor ever switches to 0-100, rescaling would turn 80 into
    # 8000%. Better to read unknown than to be confidently wrong.
    for bad in (1.5, 80, -0.1, 100):
        assert util.confidence_percent(bad) is None


def test_confidence_percent_rejects_bool_and_junk():
    for bad in (True, False, None, "0.8", [], {}):
        assert util.confidence_percent(bad) is None


# ── Session edit window ───────────────────────────────────────────────
#
# The server recomputes an entire night from these two values, so a
# timezone mistake here is not cosmetic. A naive datetime is refused
# rather than assumed local: the caller knows the zone, util does not.

_UTC = datetime.timezone.utc


def test_session_edit_window_formats_to_the_wire_shape():
    start = datetime.datetime(2026, 7, 27, 10, 30, 36, tzinfo=_UTC)
    end = datetime.datetime(2026, 7, 27, 14, 23, 36, tzinfo=_UTC)
    assert util.session_edit_window(start, end) == (
        "2026-07-27T10:30:36Z",
        "2026-07-27T14:23:36Z",
    )


def test_session_edit_window_converts_other_zones_to_utc():
    offset = datetime.timezone(datetime.timedelta(hours=-7))
    start = datetime.datetime(2026, 7, 27, 3, 30, 36, tzinfo=offset)
    end = datetime.datetime(2026, 7, 27, 7, 23, 36, tzinfo=offset)
    assert util.session_edit_window(start, end) == (
        "2026-07-27T10:30:36Z",
        "2026-07-27T14:23:36Z",
    )


def test_session_edit_window_drops_sub_second_precision():
    start = datetime.datetime(2026, 7, 27, 10, 30, 36, 830000, tzinfo=_UTC)
    end = datetime.datetime(2026, 7, 27, 14, 23, 36, 5000, tzinfo=_UTC)
    assert util.session_edit_window(start, end) == (
        "2026-07-27T10:30:36Z",
        "2026-07-27T14:23:36Z",
    )


def test_session_edit_window_refuses_naive_datetimes():
    naive = datetime.datetime(2026, 7, 27, 10, 30, 36)
    aware = datetime.datetime(2026, 7, 27, 14, 23, 36, tzinfo=_UTC)
    for pair in ((naive, aware), (aware, naive), (naive, naive)):
        try:
            util.session_edit_window(*pair)
        except ValueError as err:
            assert "timezone" in str(err)
        else:
            raise AssertionError("naive datetime must be refused")


def test_session_edit_window_refuses_a_backwards_window():
    start = datetime.datetime(2026, 7, 27, 14, 0, 0, tzinfo=_UTC)
    end = datetime.datetime(2026, 7, 27, 10, 0, 0, tzinfo=_UTC)
    for pair in ((start, end), (start, start)):
        try:
            util.session_edit_window(*pair)
        except ValueError as err:
            assert "after" in str(err)
        else:
            raise AssertionError("a backwards window must be refused")


def test_session_edit_window_refuses_non_datetimes():
    aware = datetime.datetime(2026, 7, 27, 14, 23, 36, tzinfo=_UTC)
    for bad in (None, "2026-07-27T10:30:36Z", 0, [], {}, True, datetime.date(2026, 7, 27)):
        for pair in ((bad, aware), (aware, bad)):
            try:
                util.session_edit_window(*pair)
            except ValueError:
                pass
            else:
                raise AssertionError(f"{bad!r} must be refused")
