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


# ── The redaction set itself ──────────────────────────────────────────
#
# `TO_REDACT` cannot be imported: diagnostics.py imports Home Assistant.
# It can be read with `ast`, which is enough to check the thing that
# actually goes wrong. Nobody forgets to redact a field they are thinking
# about. They add code that reads a NEW identifier out of a payload and
# never revisit a constant in another file.


def _to_redact() -> set[str]:
    """Read the TO_REDACT literal out of diagnostics.py without importing it."""
    import ast
    import pathlib

    src = pathlib.Path("custom_components/orion_sleep/diagnostics.py").read_text()
    for node in ast.parse(src).body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "TO_REDACT" for t in node.targets
        ):
            return {
                el.value
                for el in node.value.elts
                if isinstance(el, ast.Constant) and isinstance(el.value, str)
            }
    raise AssertionError("TO_REDACT not found in diagnostics.py")


# Identifier-shaped fields that are deliberately NOT redacted, and why.
# Adding to this list is a decision. Forgetting is not.
_DELIBERATELY_CLEAR = {
    # Zones are "zone_a" and "zone_b", not UUIDs. Redacting them makes a
    # zone-level bug report unreadable and protects nothing.
    "zone_id",
}


def test_every_identifier_the_code_reads_is_redacted_or_excused():
    """A new `*_id` read from a payload must not reach diagnostics raw."""
    import pathlib
    import re

    fields: set[str] = set()
    for path in pathlib.Path("custom_components/orion_sleep").glob("*.py"):
        fields |= set(re.findall(r'\.get\("([a-z_]+_ids?)"', path.read_text()))

    assert fields, "the scan found nothing, so it is not guarding anything"
    missed = fields - _to_redact() - _DELIBERATELY_CLEAR
    assert not missed, (
        f"identifier fields read from payloads but never redacted: {sorted(missed)}. "
        f"Add them to TO_REDACT, or to _DELIBERATELY_CLEAR with a reason."
    )


def test_the_excuse_list_does_not_rot():
    """An excused field that is now redacted anyway is a stale excuse."""
    both = _DELIBERATELY_CLEAR & _to_redact()
    assert not both, f"listed as deliberately clear but also redacted: {sorted(both)}"


# ── Whose entity is this ──────────────────────────────────────────────
#
# The failure being prevented: replace the linked partner account, and
# the new person inherits the previous person's entity, recorder history
# and long-term statistics. One human's sleep scores, heart rates and
# apnea counts filed under another human's name.


def test_a_person_id_is_keyed_on_the_account_not_the_role():
    a = helpers.person_unique_id(DEVICE, "sleep_score", USER_A, legacy="x")
    b = helpers.person_unique_id(DEVICE, "sleep_score", USER_B, legacy="x")
    assert a != b, "two people on one bed must not share an id"
    assert USER_A in a and USER_B in b
    assert "partner" not in a and "partner" not in b


def test_person_and_schedule_ids_use_one_scheme():
    """`schedule_unique_id` predates this and must not have moved."""
    assert helpers.person_unique_id(
        DEVICE, "bedtime", USER_A, legacy="x"
    ) == helpers.schedule_unique_id(DEVICE, "bedtime", USER_A)


def test_an_unknown_account_keeps_the_id_it_already_had():
    """Inventing a placeholder would mint a second entity for one person."""
    for unknown in (None, ""):
        assert (
            helpers.person_unique_id(DEVICE, "sleep_score", unknown, legacy="legacy-id")
            == "legacy-id"
        )


def test_no_entity_is_keyed_on_a_role_literal():
    """The anti-pattern, guarded at the source rather than by memory.

    `schedule_unique_id` has warned against this in its docstring since
    it was written. The insight and session entities did it anyway, in
    another file, and nothing failed.
    """
    import pathlib
    import re

    offenders = []
    for path in pathlib.Path("custom_components/orion_sleep").glob("*.py"):
        src = path.read_text()
        for m in re.finditer(r"_attr_unique_id\s*=\s*f?\"([^\"]*)\"", src):
            if re.search(r"_(partner|primary|owner|guest)_", m.group(1)):
                offenders.append(path.name + ": " + m.group(1))
    assert not offenders, (
        "unique_id built from a role instead of an account id: " + str(offenders)
    )
