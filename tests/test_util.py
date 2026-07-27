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


def test_timeline_label_maps_known_and_passes_through_unknown():
    assert util.timeline_label("wake_up") == "Wake Up"
    assert util.timeline_label("phase_1") == "Asleep Phase 1"
    assert util.timeline_label("turn_off") == "Turn Off"
    # An unrecognized vendor label is surfaced, not dropped.
    assert util.timeline_label("brand_new_thing") == "Brand New Thing"
    for bad in (None, "", 5, [], {}):
        assert util.timeline_label(bad) is None


def test_parse_iso_datetime_handles_z_and_offsets():
    assert util.parse_iso_datetime("2026-07-26T23:00:00Z") == datetime.datetime(
        2026, 7, 26, 23, 0, tzinfo=_UTC
    )
    assert util.parse_iso_datetime("2026-07-26T23:00:00+00:00") == datetime.datetime(
        2026, 7, 26, 23, 0, tzinfo=_UTC
    )
    # A naive timestamp is treated as UTC, never as local time.
    assert util.parse_iso_datetime("2026-07-26T23:00:00").tzinfo == _UTC


def test_parse_iso_datetime_rejects_malformed():
    for bad in (None, "", "not a date", "2026-13-01T00:00:00Z", 5, [], {}, True):
        assert util.parse_iso_datetime(bad) is None


def test_next_timeline_entry_picks_the_soonest_future_entry():
    timeline = [
        _entry("u1", "2026-07-26T23:30:00Z", "phase_1"),
        _entry("u1", "2026-07-26T23:00:00Z", "bedtime"),
        _entry("u1", "2026-07-27T07:00:00Z", "wake_up"),
    ]
    found = util.next_timeline_entry(timeline, "u1", NOW)
    assert found is not None and found["label"] == "bedtime"


def test_next_timeline_entry_ignores_the_past_and_other_people():
    timeline = [
        _entry("u1", "2026-07-26T21:00:00Z"),          # already happened
        _entry("u2", "2026-07-26T22:30:00Z"),          # somebody else
        _entry("u1", "2026-07-26T23:00:00Z", "wake_up"),
    ]
    found = util.next_timeline_entry(timeline, "u1", NOW)
    assert found is not None and found["label"] == "wake_up"
    # An entry exactly at `now` counts as already fired.
    assert util.next_timeline_entry([_entry("u1", "2026-07-26T22:00:00Z")], "u1", NOW) is None


def test_next_timeline_entry_returns_none_on_empty_or_malformed():
    assert util.next_timeline_entry([], "u1", NOW) is None
    assert util.next_timeline_entry(None, "u1", NOW) is None
    assert util.next_timeline_entry("nope", "u1", NOW) is None
    assert util.next_timeline_entry([None, "bad", {}], "u1", NOW) is None
    assert util.next_timeline_entry([_entry("u1", "garbage")], "u1", NOW) is None
    assert util.next_timeline_entry([_entry("u1", "2026-07-26T23:00:00Z")], "", NOW) is None
    assert util.next_timeline_entry([_entry("u1", "2026-07-26T23:00:00Z")], "u1", None) is None


def test_timeline_target_temps_reads_zones_and_rejects_junk():
    entry = _entry(
        "u1",
        "2026-07-26T23:00:00Z",
        zones=[{"id": "zone_a", "temp": 23.5}, {"id": "zone_b", "temp": 19}],
    )
    assert util.timeline_target_temps(entry) == {"zone_a": 23.5, "zone_b": 19.0}
    # bool is a subclass of int and must not read as a temperature.
    assert util.timeline_target_temps(_entry("u1", "x", zones=[{"id": "z", "temp": True}])) == {}
    for bad in (None, {}, {"action": None}, {"action": {"zones": "no"}}, "str"):
        assert util.timeline_target_temps(bad) == {}
