"""Behavioural tests for the integration's own helpers.

`helpers.py` imports nothing from Home Assistant, so unlike the rest of
this package it can be loaded and actually executed here rather than
inspected with `ast`.

Two of these functions decide what leaves a user's machine when they
click "download diagnostics" on the integration page: what gets redacted
and which whole branches get dropped. They arrived here from the library
with no tests attached, which for privacy code is the wrong way round.
"""

import _orion

helpers = _orion.load("helpers")

DEVICE = "dev-1"
USER_A = "11111111-1111-4111-8111-111111111111"
USER_B = "22222222-2222-4222-8222-222222222222"


# ── Diagnostics redaction ─────────────────────────────────────────────
#
# The thing being defended: a diagnostics file gets attached to a GitHub
# issue. Anything still identifying in it is public forever.


def test_uuid_keys_are_redacted_not_just_uuid_values():
    """Schedules and insights are keyed BY user id.

    Redacting values alone leaves the identifiers sitting in the keys,
    which is where this API actually puts them.
    """
    payload = {"schedules": {USER_A: [{"day": 0}], USER_B: [{"day": 1}]}}
    out = helpers.redact_identifier_keys(payload)
    flat = repr(out)
    assert USER_A not in flat
    assert USER_B not in flat
    assert len(out["schedules"]) == 2, "both people should survive, renamed"


def test_redaction_reaches_arbitrarily_deep():
    payload = {"a": {"b": {"c": {USER_A: {"d": [{USER_B: 1}]}}}}}
    assert USER_A not in repr(helpers.redact_identifier_keys(payload))
    assert USER_B not in repr(helpers.redact_identifier_keys(payload))


def test_redaction_does_not_invent_collisions():
    """Two ids must not both become the same placeholder."""
    payload = {USER_A: 1, USER_B: 2}
    out = helpers.redact_identifier_keys(payload)
    assert len(out) == 2


def test_redaction_leaves_ordinary_keys_alone():
    payload = {"serial_number": "AA11", "zones": [{"id": "zone_a"}]}
    assert helpers.redact_identifier_keys(payload) == payload


def test_redaction_survives_anything():
    for bad in (None, [], "x", 0, True, {"a": None}, [None, {"b": []}]):
        helpers.redact_identifier_keys(bad)


def test_the_sensitive_branches_are_dropped_entirely():
    """Biometrics and schedules go, not just their identifiers.

    A redacted heart rate series is still a heart rate series.
    """
    payload = {
        "insights": {"data": {"2026-07-27": {"sessions": [{"heart_rate": [60, 61]}]}}},
        "partner_insights": {"data": {}},
        "schedules": {"today_sleep_schedule": {}},
        "sensors": {"sensor1": {"heart_rate": 60}},
        "timeline": [{"label": "bedtime"}],
        "serial_number": "AA11BB22CC33",
        "zones": [{"id": "zone_a", "temp": 21}],
    }
    out = helpers.omit_sensitive_diagnostic_branches(payload)

    for gone in ("insights", "partner_insights", "schedules", "sensors", "timeline"):
        assert gone not in out, f"{gone} should not appear in a diagnostics file"
    assert out["serial_number"] == "AA11BB22CC33"
    assert out["zones"] == [{"id": "zone_a", "temp": 21}]


def test_sensitive_branches_are_dropped_at_any_depth():
    payload = {"a": {"b": {"insights": {"heart_rate": [60]}, "keep": 1}}}
    out = helpers.omit_sensitive_diagnostic_branches(payload)
    assert "heart_rate" not in repr(out)
    assert out["a"]["b"]["keep"] == 1


def test_omission_survives_anything():
    for bad in (None, [], "x", 0, {"insights": None}, [{"insights": 1}]):
        helpers.omit_sensitive_diagnostic_branches(bad)


def test_the_two_compose_without_reintroducing_anything():
    """These two run back to back in `diagnostics._redact`.

    They cover branches and mapping KEYS. Identifiers appearing as
    ordinary VALUES are Home Assistant's `async_redact_data` with
    `TO_REDACT`, which imports Home Assistant and so cannot run here.
    Asserting on a value would be asserting a guarantee these two
    functions never made.
    """
    payload = {
        "schedules": {USER_A: [{"bedtime": "23:00"}]},
        "live": {USER_B: {"temp": 21}},
    }
    out = helpers.redact_identifier_keys(
        helpers.omit_sensitive_diagnostic_branches(payload)
    )
    flat = repr(out)
    assert "23:00" not in flat, "the whole schedules branch should be gone"
    assert USER_A not in flat
    assert USER_B not in flat, "a user id used as a key anywhere must be redacted"


# ── Cooling duration ──────────────────────────────────────────────────
#
# Bounds a number sent to hardware that heats and cools a bed.


def test_cooling_duration_clamps_into_range():
    assert helpers.clamp_cooling_minutes(500, 30, 1, 240) == 240
    assert helpers.clamp_cooling_minutes(-5, 30, 1, 240) == 1
    assert helpers.clamp_cooling_minutes(45, 30, 1, 240) == 45


def test_cooling_duration_falls_back_rather_than_guessing():
    for junk in (None, "45", [], {}, True, False, float("nan")):
        assert helpers.clamp_cooling_minutes(junk, 30, 1, 240) == 30


def test_cooling_duration_treats_infinity_as_unusable():
    """Not clamped to the maximum.

    Infinity means the input was wrong, and silently turning that into
    four hours of cooling is worse than falling back to the default.
    """
    assert helpers.clamp_cooling_minutes(float("inf"), 30, 1, 240) == 30
    assert helpers.clamp_cooling_minutes(float("-inf"), 30, 1, 240) == 30


# ── Entity identifiers ────────────────────────────────────────────────


def test_schedule_ids_are_unique_per_person_and_key():
    keys = ("bedtime", "wakeup_time", "bedtime_temp", "bedtime_temp_offset")
    ids = [
        helpers.schedule_unique_id(DEVICE, k, u)
        for u in (USER_A, USER_B)
        for k in keys
    ]
    assert len(set(ids)) == len(ids)


def test_the_temperature_and_offset_ids_never_collide():
    """The near-miss: one is a sensor, the other a number."""
    a = helpers.schedule_unique_id(DEVICE, "wakeup_temp", USER_A)
    b = helpers.schedule_unique_id(DEVICE, "wakeup_temp_offset", USER_A)
    assert a != b and not a == b


def test_schedule_ids_are_stable():
    """These key entity history. Drift orphans a graph."""
    assert helpers.schedule_unique_id(DEVICE, "bedtime", USER_A) == (
        f"{DEVICE}_user_{USER_A}_bedtime"
    )


def test_two_devices_never_collide():
    assert helpers.schedule_unique_id("a", "bedtime", USER_A) != (
        helpers.schedule_unique_id("b", "bedtime", USER_A)
    )


# ── Display strings ───────────────────────────────────────────────────


def test_duration_handles_the_overnight_case():
    assert helpers.schedule_duration_text({"bedtime": "23:00", "wakeup": "07:00"}) == "8h 0m"
    assert helpers.schedule_duration_text({"bedtime": "22:30", "wakeup": "06:45"}) == "8h 15m"


def test_duration_treats_equal_times_as_a_full_day():
    assert helpers.schedule_duration_text({"bedtime": "23:00", "wakeup": "23:00"}) == "24h 0m"


def test_duration_returns_nothing_rather_than_guessing():
    for bad in ({}, None, {"bedtime": "23:00"}, {"bedtime": "25:00", "wakeup": "07:00"},
                {"bedtime": "23:00", "wakeup": None}, "x", 0):
        assert helpers.schedule_duration_text(bad) is None


# ── Options-flow plumbing ─────────────────────────────────────────────


def test_two_people_with_the_same_name_get_distinct_form_labels():
    """These become schema keys. A duplicate silently drops a field."""
    labels = helpers.unique_alias_labels(
        [{"id": USER_A, "name": "Alex"}, {"id": USER_B, "name": "Alex"}]
    )
    assert len(set(labels.values())) == 2
    assert set(labels) == {USER_A, USER_B}


def test_a_nameless_person_still_gets_a_label():
    labels = helpers.unique_alias_labels([{"id": USER_A, "name": ""}])
    assert labels[USER_A].strip()


def test_clearing_an_alias_removes_the_override():
    assert helpers.clean_alias_map({USER_A: "   "}, {USER_A}) == {}
    assert helpers.clean_alias_map({USER_A: "Ada"}, {USER_A}) == {USER_A: "Ada"}


def test_an_alias_for_somebody_who_left_is_discarded():
    assert helpers.clean_alias_map({USER_B: "Ghost"}, {USER_A}) == {}


def test_alias_map_survives_anything():
    for bad in (None, [], "x", 0, {USER_A: None}, {USER_A: 5}, {5: "x"}):
        assert helpers.clean_alias_map(bad, {USER_A}) == {}


# ── Nested reads ──────────────────────────────────────────────────────


def test_nested_mapping_stops_at_the_first_non_mapping():
    assert helpers.nested_mapping({"a": {"b": {"c": 1}}}, "a", "b") == {"c": 1}
    assert helpers.nested_mapping({"a": []}, "a", "b") == {}
    assert helpers.nested_mapping({"a": {"b": "x"}}, "a", "b") == {}


def test_nested_mapping_never_raises():
    for bad in (None, [], "x", 0, True, {"a": None}):
        assert helpers.nested_mapping(bad, "a", "b") == {}
