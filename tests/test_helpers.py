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


# ── Names that are actually credentials ───────────────────────────────
#
# The thing being defended: Home Assistant slugifies an entity's name
# into its entity_id at first registration and never revisits it on
# rename, and `orion_user_label` falls back through name, then email,
# then phone. So an account with no display name set puts a login
# credential into a permanent identifier. `tests_ha` proves the entity
# ids that result. These prove the predicate underneath them.


def test_an_email_is_never_a_display_name():
    for bad in (
        "alice@example.com",
        "  alice@example.com  ",
        "Alice <alice@example.com>",
        "a@b",
    ):
        assert helpers.is_safe_display_name(bad) is False, bad


def test_a_phone_number_is_never_a_display_name():
    """Every spelling the vendor has been seen to use, and then some."""
    for bad in (
        "+1 (555) 123-4567",
        "+15551234567",
        "555-123-4567",
        "555.123.4567",
        "5551234567",
        "(555) 123 4567",
        "  +1 555 123 4567  ",
    ):
        assert helpers.is_safe_display_name(bad) is False, bad


def test_a_non_ascii_separator_does_not_get_a_phone_number_through():
    """The three measured defeats of the old fixed-punctuation strip.

    The predicate used to remove exactly " \\t\\u00a0-.()+" and then test
    whether what remained was a digit string. Anything separated by a
    character outside that set survived as "not all digits" and was
    therefore waved through as a safe display name, which made it
    eligible to be slugified into a permanent entity_id. Home Assistant
    never revisits an entity_id on rename, so that is irreversible.

    None of these is exotic. An en dash or a non-breaking hyphen is what
    a word processor produces from a typed hyphen, and a slash-separated
    number is an ordinary way to write one in several countries.

    Parametrized-by-loop rather than by pytest so this file keeps its
    dependency-free shape.
    """
    for bad in (
        "555\u20131234567",  # en dash
        "+1\u20115551234567",  # non-breaking hyphen
        "555/123/4567",  # forward slashes
        "555\u20141234567",  # em dash, same class of miss
        "555_123_4567",  # underscore, ditto
        "\u0665\u0665\u0665\u0661\u0662\u0663\u0664\u0665\u0666\u0667",  # Arabic-Indic
        "５５５１２３４５６７",  # fullwidth digits
    ):
        assert helpers.is_safe_display_name(bad) is False, bad


def test_a_real_name_is_not_rejected():
    """The other half of the contract.

    A predicate aggressive enough to drop these would make every
    household's entities unreadable and buy no privacy at all. "R2" and
    "42" are the deliberate stress cases: short, digit-heavy, and still
    plainly names rather than phone numbers.

    This list is why the rule is "long enough AND letterless" rather than
    just "long enough". Tightening the digit floor to catch a
    slash-separated number would take "Room 101" and "42" with it, and a
    household cannot argue with that from the UI.
    """
    for good in (
        "Alex",
        "Anne-Marie",
        "O'Brien",
        "R2",
        "42",
        "Björn",
        "李雷",
        "Mary Jane Watson",
        "Room 101",
        "555",
        # A name that is long enough to be a phone number and is plainly
        # not one. Letters are what tells the two apart.
        "Zone 4 Bed 3 Sensor 12",
    ):
        assert helpers.is_safe_display_name(good) is True, good


def test_a_blank_or_missing_name_is_not_usable():
    for bad in ("", "   ", None, 0, [], {}, 12345678):
        assert helpers.is_safe_display_name(bad) is False, bad


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
    """Read the TO_REDACT set out of diagnostics.py without importing it.

    Resolves `ast.Name` elements against `const.py` as well as reading
    plain string literals, because most of TO_REDACT is now written as
    references to the constants it mirrors rather than as retyped copies
    of their strings.

    That indirection is the fix for the leak this whole section is about.
    `CONF_PARTNER_ACCOUNT_ID` shipped unredacted because the redaction
    set was a list of hand-typed strings living in a different file from
    the constants it was supposed to shadow, so adding the constant and
    adding the redaction were two separate acts and only one of them
    happened. An earlier version of this reader saw literals only, which
    means it would have silently reported an almost-empty set the moment
    the indirection landed. Anything it cannot resolve is a hard failure
    for exactly that reason.
    """
    import ast
    import pathlib

    const = _orion.load("const")
    src = pathlib.Path("custom_components/orion_sleep/diagnostics.py").read_text()
    for node in ast.parse(src).body:
        if not isinstance(node, ast.Assign) or not any(
            isinstance(t, ast.Name) and t.id == "TO_REDACT" for t in node.targets
        ):
            continue
        found: set[str] = set()
        for el in node.value.elts:
            if isinstance(el, ast.Constant) and isinstance(el.value, str):
                found.add(el.value)
                continue
            resolved = getattr(el, "id", None)
            value = getattr(const, resolved, None) if resolved else None
            assert isinstance(value, str), (
                f"TO_REDACT element {ast.dump(el)} does not resolve to a "
                "string constant in const.py. Extend this reader rather "
                "than dropping the element, because an element it cannot "
                "read is a field nobody checks the redaction of."
            )
            found.add(value)
        return found
    raise AssertionError("TO_REDACT not found in diagnostics.py")


# Identifier-shaped fields that are deliberately NOT redacted, and why.
# Adding to this list is a decision. Forgetting is not.
_DELIBERATELY_CLEAR = {
    # Zones are "zone_a" and "zone_b", not UUIDs. Redacting them makes a
    # zone-level bug report unreadable and protects nothing.
    "zone_id",
    # Home Assistant's own random local id for the config entry. It
    # identifies the install to itself and says nothing about a person.
    "entry_id",
    # Local Home Assistant config-entry id accepted by the recovery
    # service. It identifies this installation, not a vendor account.
    "config_entry_id",
}


def test_every_identifier_the_code_reads_is_redacted_or_excused():
    """A new `*_id` read from a payload must not reach diagnostics raw."""
    import pathlib
    import re

    # Both access forms. Scanning only `.get("x")` made this pass while
    # covering two fields, because most identifier reads in this codebase
    # are plain subscripts.
    fields: set[str] = set()
    for path in pathlib.Path("custom_components/orion_sleep").glob("*.py"):
        src = path.read_text()
        fields |= set(re.findall(r'\.get\(\s*"([a-z_]+_ids?)"', src))
        fields |= set(re.findall(r'\[\s*"([a-z_]+_ids?)"\s*\]', src))

    assert len(fields) >= 4, (
        "the scan found only " + str(sorted(fields)) + ", which is too few to "
        "be guarding anything. It previously passed while seeing two."
    )
    missed = fields - _to_redact() - _DELIBERATELY_CLEAR
    assert not missed, (
        f"identifier fields read from payloads but never redacted: {sorted(missed)}. "
        f"Add them to TO_REDACT, or to _DELIBERATELY_CLEAR with a reason."
    )


def test_the_excuse_list_does_not_rot():
    """An excused field that is now redacted anyway is a stale excuse."""
    both = _DELIBERATELY_CLEAR & _to_redact()
    assert not both, f"listed as deliberately clear but also redacted: {sorted(both)}"


def test_recovery_journal_and_every_phone_spelling_are_redacted():
    redacted = _to_redact()
    assert {
        "phone",
        "phone_number",
        "_account_id_v3",
        "_device_ids_v3",
        "_uid_migration_v3",
        "_uid_recovery_active_v3",
        # The partner's Orion user id, which shipped in the clear next to
        # a redacted `_account_id_v3` because the redaction set was a
        # separate hand-typed enumeration. Spelled as its literal here on
        # purpose: this file resolves constants, and asserting on the
        # constant would pass even if `const.CONF_PARTNER_ACCOUNT_ID` and
        # the entry in TO_REDACT drifted to two different strings.
        #
        # `tests_ha/test_diagnostics_redaction_real.py` carries the
        # general form of this, which requires every credential-shaped
        # and identifier-shaped CONF_* constant to be present.
        "_partner_account_id_v3",
    } <= redacted


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


# ── Diagnostics branch coverage ────────────────────────────────────────
#
# The leak this exists to stop: the coordinator gained `live_session`,
# `sleep_config` and `ws_timelines`, and the omit set was never revisited.
# So the file whose whole purpose is to be safe to attach to a public
# issue published household occupancy, both people's bedtimes and their
# chronotype. The omit set contained `timeline`, which caught the nested
# copy and not the top-level one under a different spelling.
#
# The point of this test is that adding a branch to the coordinator FAILS
# until somebody decides which side of the line it goes on.


def _coordinator_data_keys() -> set[str]:
    """Top-level keys `coordinator.py` writes into `data`."""
    import ast
    import pathlib

    src = pathlib.Path("custom_components/orion_sleep/coordinator.py").read_text()
    tree = ast.parse(src)
    keys: set[str] = set()
    for node in ast.walk(tree):
        # data = {"schedules": ..., "insights": ...}
        if isinstance(node, ast.Dict):
            for k in node.keys:
                if isinstance(k, ast.Constant) and isinstance(k.value, str):
                    keys.add(k.value)
        # data["ws_timelines"] = ...
        if (
            isinstance(node, ast.Subscript)
            and isinstance(node.value, ast.Name)
            and node.value.id == "data"
            and isinstance(node.slice, ast.Constant)
            and isinstance(node.slice.value, str)
        ):
            keys.add(node.slice.value)
    return keys


# Branches that reach a diagnostics download on purpose. Everything here
# is a deliberate decision, and the test below is what forces one.
# Keys the `ast` scan picks up from unrelated dict literals in
# coordinator.py. They are not coordinator.data branches, so they are not
# a diagnostics decision. Curated deliberately: an unknown key failing the
# test is the point.
_INCIDENTAL_DICT_KEYS = {
    "id",
    "on",
    "temp",
    "zones",
    "status",
    "serial_number",
    "response",
    "success",
    "data",
    "user_id",
    "devices",
    "name",
    "error",
}

_SAFE_IN_DIAGNOSTICS = {
    "schedules",  # omitted already, listed for completeness of intent
    "insights",
    "partner_insights",
    "live_session",
    "sleep_config",
    "ws_timelines",
}


def test_every_coordinator_branch_is_omitted_or_deliberately_allowed():
    written = _coordinator_data_keys()
    assert "live_session" in written, "the scan lost track of coordinator.data"

    omitted = helpers._SENSITIVE_DIAGNOSTIC_BRANCHES
    undecided = {
        key
        for key in written
        if key in _SAFE_IN_DIAGNOSTICS or key in omitted
    }
    # Every key we know about must be classified. The interesting failure
    # is a NEW coordinator branch nobody classified.
    # Intersecting with a hardcoded list and then subtracting a set that
    # contained all of it made this empty for every possible input, so a
    # NEW coordinator branch could never fail it. Which was the one thing
    # it existed to catch.
    unclassified = written - omitted - _SAFE_IN_DIAGNOSTICS - _INCIDENTAL_DICT_KEYS
    assert not unclassified, (
        "coordinator branches that reach diagnostics unredacted: "
        + str(sorted(unclassified))
    )
    assert undecided, "sanity: the classification sets are not wired up"


def test_occupancy_and_schedule_never_survive_the_omit_pass():
    """The concrete payload, not just the key list."""
    payload = {
        "live_session": {"response": {"is_in_bed": True, "in_bed_start": "03:41"}},
        "sleep_config": {"response": {"chronotype": "night_owl"}},
        "ws_timelines": {DEVICE: [{"label": "bedtime", "scheduled_time": "23:55"}]},
    }
    flat = repr(helpers.omit_sensitive_diagnostic_branches(payload))
    for leak in ("is_in_bed", "03:41", "night_owl", "bedtime", "23:55"):
        assert leak not in flat, leak + " reached the diagnostics download"


def test_uuids_in_lists_are_redacted_not_just_uuid_keys():
    """`/v1/auth/me` returns `devices` as a bare list of device ids.

    Home Assistant's redaction matches field NAMES, and the field is
    called `devices`. Nothing was looking at a uuid sitting in a list.
    """
    out = helpers.redact_identifier_keys({"devices": [USER_A, "not-a-uuid"]})
    assert USER_A not in repr(out)
    assert "not-a-uuid" in repr(out), "only uuids should be redacted"


# ── Reauth identity ───────────────────────────────────────────────────
#
# The guard shipped before this one compared the entry's unique_id to the
# account id and abstained when the unique_id still held the typed
# address. Every entry created before 3.0 is keyed exactly that way, so
# the abstain fired for one hundred percent of the installs that would
# ever run the check. It was inert, and the tests written alongside it
# did not notice, because none of them built a pre-3.0 entry.
#
# `config_flow.py` imports Home Assistant, so the decision is exercised
# through a stand-in with the same shape rather than the real flow.
#
# THE STAND-IN WAS UPDATED, and the reason matters more than the diff.
# Two things changed underneath it.
#
# 1. The decision reads `coordinator.recorded_account_id` instead of
#    `entry.unique_id`. The account identity is recorded twice, this
#    method guarded one copy and the coordinator guarded the other, and
#    the reauth write then overwrote the copy this method had not
#    checked. Reading one accessor also retires the `existing != typed`
#    heuristic, which existed only to guess which of the two things
#    `unique_id` was holding. `CONF_ACCOUNT_ID` means one thing on every
#    entry, so there is nothing left to guess.
#
# 2. The address comparison is `coordinator.profile_carries_address`
#    rather than a copy. The copy and the canonical version were the same
#    rule written twice, and the unverified-account option made them
#    genuinely divergent: setup could be relaxed by it, reauth could not.
#    A household that set the option could load the entry and still not
#    reauthenticate.
#
# The old drift test string-matched the copy, so it necessarily failed
# once the copy was deleted. It encoded "config_flow reimplements this",
# and the whole point of the change is that config_flow no longer does.
# `test_config_flow_delegates_rather_than_reimplementing` replaces it and
# asserts the stronger property: that the duplicate has not come back.


class _Entry:
    """A config entry as this decision sees it.

    Carries `options` because the decision now consults one, and the
    account id lives in `data` under its real key rather than in
    `unique_id`, which is what actually changed.
    """

    def __init__(self, auth_value, account_id=None, allow_unverified=False):
        self.data = {"auth_value": auth_value}
        if account_id is not None:
            self.data["_account_id_v3"] = account_id
        self.options = {"allow_unverified_account": allow_unverified}


def _recorded_account_id(entry):
    """The logic of coordinator.recorded_account_id."""
    value = entry.data.get("_account_id_v3")
    return value if isinstance(value, str) and value else None


def _carries_address(profile, typed):
    """The logic of coordinator.profile_carries_address."""
    if not isinstance(profile, dict) or not profile or not typed:
        return False
    known = {
        str(profile.get(f) or "").strip().lower()
        for f in ("email", "phone", "phone_number")
    }
    known.discard("")
    if not known:
        return False
    return typed in known


def _matches(entry, profile):
    """The logic of ConfigFlow._async_reauth_account_matches."""
    if entry is None or not isinstance(profile, dict) or not profile:
        return False
    account_id = profile.get("id")
    recorded = _recorded_account_id(entry)
    typed = (entry.data.get("auth_value") or "").strip().lower()
    if recorded is not None:
        return bool(account_id) and recorded == account_id
    if _carries_address(profile, typed):
        return True
    return bool(account_id) and isinstance(account_id, str) and (
        entry.options.get("allow_unverified_account", False) is True
    )


LEGACY = _Entry("alice@example.com")
MODERN = _Entry("alice@example.com", account_id=USER_A)


def test_a_pre_3_0_entry_still_rejects_a_foreign_account():
    """The case the previous guard let through, which was all of them."""
    assert _matches(LEGACY, {"id": USER_A, "email": "alice@example.com"}) is True
    assert _matches(LEGACY, {"id": USER_B, "email": "bob@example.com"}) is False


def test_an_account_keyed_entry_compares_on_the_account():
    assert _matches(MODERN, {"id": USER_A, "email": "alice@example.com"}) is True
    assert _matches(MODERN, {"id": USER_B, "email": "bob@example.com"}) is False


def test_a_phone_entry_matches_on_phone():
    phone = _Entry("15555550100")
    assert _matches(phone, {"id": USER_A, "phone": "15555550100"}) is True
    assert _matches(phone, {"id": USER_B, "phone": "15555550199"}) is False


def test_reauth_fails_closed_when_identity_is_unknowable():
    """Accepted credentials are not proof they belong to this entry."""
    assert _matches(LEGACY, None) is False, "no profile"
    assert _matches(LEGACY, {}) is False, "empty profile"
    assert _matches(LEGACY, {"id": USER_B}) is False, "profile carries no address"


def test_the_bypass_option_reaches_the_reauth_decision():
    """Setup and reauth have to apply one rule, or the hatch is a dead end.

    Expired tokens are the case that matters. Setup cannot run at all,
    so the option never gets consulted there, and reauth is the only
    door left. A reauth that refuses an address-less profile while
    setup accepts it locks the household out through the one path
    still available to them.
    """
    locked_out = _Entry("alice@example.com", allow_unverified=True)
    assert _matches(locked_out, {"id": USER_A}) is True


def test_the_bypass_option_cannot_ratify_a_recorded_mismatch():
    """The scope of the hatch, and the whole safety argument for it.

    A recorded account id is a real reference value, so a mismatch
    against it is a real finding rather than an absence of evidence.
    Accepting it swaps two people's sleep history.
    """
    recorded = _Entry("alice@example.com", account_id=USER_A, allow_unverified=True)
    assert _matches(recorded, {"id": USER_B, "email": "alice@example.com"}) is False
    # And an empty profile is still refused, because accepted credentials
    # are not evidence of whose they are no matter what is switched on.
    assert _matches(_Entry("alice@example.com", allow_unverified=True), {}) is False


def test_config_flow_delegates_rather_than_reimplementing():
    """The replacement for the old drift test, and a stronger claim.

    The old version asserted that config_flow contained its own copy of
    the address comparison, which is exactly the duplication that was
    removed. It could only ever fail once the copy was deleted.

    This asserts the property that actually matters now: the decision
    reads the one account-id accessor, calls the one address function,
    and does NOT carry a second copy of either. The re-inlined copy is
    what made setup and reauth able to disagree, so its absence is the
    thing worth pinning.
    """
    import pathlib

    src = pathlib.Path("custom_components/orion_sleep/config_flow.py").read_text()
    assert "from .coordinator import profile_carries_address, recorded_account_id" in src

    body = src[src.index("def _async_reauth_account_matches") :]
    body = body[: body.index("\n    async def ")]

    # Docstring stripped before the absence checks below. That docstring
    # explains what the old rule was and names `entry.unique_id` while
    # doing it, so a substring search over the whole body would match the
    # explanation of the fix and call it the bug. The absence claims are
    # about code, so they get code.
    opening = body.index('"""')
    code = body[body.index('"""', opening + 3) + 3 :]
    # Line comments are prose too, and this file is full of them.
    code = "\n".join(
        line for line in code.splitlines() if not line.strip().startswith("#")
    )

    for marker in (
        "recorded = recorded_account_id(entry)",
        "if profile_carries_address(profile, typed):",
    ):
        assert marker in code, "config_flow stopped delegating: " + marker
    for copy in (
        'for field in ("email", "phone", "phone_number")',
        "return typed in known",
        "entry.unique_id",
    ):
        assert copy not in code, (
            "the duplicated identity rule is back in config_flow, so setup "
            "and reauth can disagree again: " + copy
        )


def test_rotated_verification_tokens_are_copied_only_after_identity_probe():
    import pathlib

    src = pathlib.Path("custom_components/orion_sleep/config_flow.py").read_text()
    body = src[src.index("async def async_step_verify") :]
    body = body[: body.index("async def async_step_reauth")]
    assert body.index("identity = await self._async_account_identity(tokens)") < body.index(
        "data = {"
    )
    assert 'CONF_REFRESH_TOKEN: tokens["refresh_token"]' in body


def test_reauth_checks_both_account_and_bed_before_persisting_tokens():
    import pathlib

    src = pathlib.Path("custom_components/orion_sleep/config_flow.py").read_text()
    body = src[src.index("async def async_step_verify") :]
    body = body[: body.index("async def async_step_reauth")]
    persist = body.index("self.hass.config_entries.async_update_entry")
    for marker in (
        "_async_reauth_account_matches(profile)",
        "recorded_devices",
        "overlapping_entry_ids(",
    ):
        assert body.index(marker) < persist


# ── Every write into the client is translated ─────────────────────────


def test_no_client_call_reaches_the_user_as_a_traceback():
    """Sixteen of thirty-two call sites had no handler, or only ValueError.

    Between them that was the whole write surface: every temperature set,
    every power toggle, every schedule write, every button. A vendor 500
    on any of them surfaced in the UI as a raw traceback. Lint, compile
    and the suite were all green throughout, which is why this is checked
    rather than remembered.
    """
    import ast
    import pathlib

    clients = {"api_client", "partner_api_client", "client", "_sessions_client"}
    skip = {"coordinator.py", "config_flow.py", "migrations.py"}
    unguarded: list[str] = []

    for path in sorted(pathlib.Path("custom_components/orion_sleep").glob("*.py")):
        if path.name in skip:
            continue
        tree = ast.parse(path.read_text())
        guards: list[tuple[int, int, set[str]]] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Try):
                names = {
                    x.id
                    for h in node.handlers
                    for x in (
                        (h.type.elts if isinstance(h.type, ast.Tuple) else [h.type])
                        if h.type
                        else []
                    )
                    if isinstance(x, ast.Name)
                }
                guards.append((node.body[0].lineno, node.body[-1].end_lineno, names))
            if isinstance(node, ast.AsyncWith):
                for item in node.items:
                    call = item.context_expr
                    if (
                        isinstance(call, ast.Call)
                        and getattr(call.func, "id", None) == "orion_call"
                    ):
                        guards.append(
                            (node.body[0].lineno, node.body[-1].end_lineno, {"OrionApiError"})
                        )

        for node in ast.walk(tree):
            if not (isinstance(node, ast.Await) and isinstance(node.value, ast.Call)):
                continue
            fn = node.value.func
            if not isinstance(fn, ast.Attribute):
                continue
            owner = fn.value
            source = getattr(owner, "attr", None) or getattr(owner, "id", None)
            if isinstance(owner, ast.Call):
                source = getattr(owner.func, "attr", None)
            if source not in clients:
                continue
            covering = [g for g in guards if g[0] <= node.lineno <= g[1]]
            if not any(
                "OrionApiError" in g[2] or "Exception" in g[2] for g in covering
            ):
                unguarded.append(f"{path.name}:{node.lineno} {fn.attr}")

    assert not unguarded, (
        "client calls that would reach the user as a traceback: " + str(unguarded)
    )
