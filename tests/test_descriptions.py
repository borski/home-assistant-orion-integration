"""Behavioural tests for the sensor entity descriptions and their readers.

WHY THIS FILE DID NOT EXIST BEFORE. `descriptions.py` imports
`homeassistant.components.sensor`, and until this suite got an
interpreter with Home Assistant on it, importing this module here was
impossible. The fallback everywhere else in `tests/` is to parse the
source with `ast`, and `ast` is useless against this module: almost
everything in it is a lambda inside a tuple literal, so the only thing a
parser can say is that the lambda is present. Whether it returns the
right number, or raises, is exactly what could not be checked.

WHY IT IS WORTH HAVING. These readers are the largest body of pure
functions in the integration. Every one of them takes a dict that came
off the network from a vendor API and returns something Home Assistant
puts in a state. They had no tests at all.

WHAT THE HOSTILE-INPUT TESTS DEFEND. A `value_fn` runs inside
`SensorEntity.native_value`, which Home Assistant calls while writing
state. An exception there does not fail politely. It propagates out of
the state write, the entity is left holding its previous value with no
indication anything went wrong, and the traceback lands in the log
pointing at a lambda with no name. The vendor payload is not
schema-checked anywhere, and this integration has already had to add
`session_subsection` and `apnea_number` because a list arrived where a
dict was expected. So every description is driven through a battery of
malformed payloads below, generically, so that a sensor added later is
covered by the same net without anybody remembering to add it.
"""

from __future__ import annotations

import datetime

import _orion
import pytest

descriptions = _orion.real("descriptions")

INSIGHTS = descriptions.INSIGHT_SENSOR_DESCRIPTIONS
SCHEDULES = descriptions.SCHEDULE_SENSOR_DESCRIPTIONS
ALL_DESCRIPTIONS = INSIGHTS + SCHEDULES


# A session that is wrong in every way a vendor payload has actually been
# wrong, plus the shapes that are merely plausible. `None` and `{}` are
# the normal cases before the first night syncs. The rest are the ones
# that produced AttributeError in the field: a block that arrives as a
# list, a scalar where a block was expected, and a bool where a number
# was expected.

# A FIXED DEFECT, kept as a regression test.
#
# `_minutes_to_hm` and `_seconds_to_ms` did `int(round(value))` with no
# type guard, so a numeric field arriving as a JSON string raised
# TypeError inside `native_value`, which breaks the state write for the
# whole entity rather than leaving one value unknown. Their numeric
# sibling `_minutes_value` already guarded exactly this and said why in
# its docstring, which is what made it an oversight rather than a
# decision: the same payload was safe on `total_sleep_time_minutes` and
# fatal on `total_sleep_time`.
#
# Blast radius was every human-readable duration sensor, so
# `total_sleep_time`, `deep_sleep_time`, `rem_sleep_time`,
# `light_sleep_time` and `awake_time` through `_minutes_to_hm`, plus
# `restless_time` through `_seconds_to_ms`. Both now route through
# `_duration_number`, which rejects a non-number and rejects bool.
#
# These payloads carried an xfail(strict=True) marker while the bug was
# open. Adding the guard turned them into XPASS failures, which is what
# forced the marker out. Kept here unmarked so the guard cannot be
# removed without the suite noticing.
STRING_NUMBER_PAYLOADS = [
    {"sleep_summary": {"time_asleep": "420"}},
    {"movement": {"total_seconds": "125"}},
]

HOSTILE_PAYLOADS = [
    None,
    {},
    {"sleep_summary": None},
    {"sleep_summary": []},
    {"sleep_summary": "unavailable"},
    {"sleep_summary": {"time_asleep": None}},
    {"sleep_summary": {"time_asleep": True}},
    {"heart_rate": [], "breath_rate": [], "hrv": [], "movement": []},
    {"apnea": None},
    {"apnea": {"ahi": "n/a"}},
    {"temperature": {"values": None}},
    {"temperature": {"values": ["hot"]}},
    {"temperature_setpoint": {"values": []}},
    {"confidence": "high"},
    {"end_time": "not-a-timestamp"},
    {"end_time": None},
    {"in_bed_start_time": "2026-07-01T23:00:00Z", "in_bed_end_time": None},
    {"in_bed_start_time": "2026-07-02T07:00:00Z", "in_bed_end_time": "2026-07-01T23:00:00Z"},
    # Not a dict at all. `_planned_renames` and the platform both assume a
    # session is a mapping, and the coordinator hands through whatever the
    # vendor sent.
    [],
    "",
    0,
]

# Both formatter paths handle the string-number payloads now.
VALUE_FN_PAYLOADS = HOSTILE_PAYLOADS + STRING_NUMBER_PAYLOADS
ATTRS_FN_PAYLOADS = HOSTILE_PAYLOADS + STRING_NUMBER_PAYLOADS


# ── Description table invariants ──────────────────────────────────────


def test_every_description_key_is_unique_across_both_tables():
    """A duplicate key builds two entities on one unique_id.

    The second one loses. Home Assistant refuses the registration and
    logs it once at startup, which nobody reads, and the household gets a
    sensor that is simply absent. The two tables are checked together
    because both are built per person against the same device, so a key
    appearing in each collides just as hard as a key appearing twice in
    one.
    """
    keys = [d.key for d in ALL_DESCRIPTIONS]
    duplicates = sorted({k for k in keys if keys.count(k) > 1})
    assert duplicates == [], f"duplicate description keys: {duplicates}"


def test_no_description_key_is_a_suffix_of_another():
    """Unique ids are built by concatenation, so suffixes can collide.

    `helpers.person_unique_id` joins device, user and key with
    underscores. Two keys where one ends with the other, such as
    `sleep_time` beside `total_sleep_time`, do not collide on their own,
    but the migration's reverse lookup matches by suffix and would
    attribute a row to the wrong sensor. Cheaper to forbid the shape than
    to make every consumer disambiguate.
    """
    keys = sorted(d.key for d in ALL_DESCRIPTIONS)
    clashes = [
        (a, b)
        for a in keys
        for b in keys
        if a != b and b.endswith(f"_{a}")
    ]
    assert clashes == [], f"one key is an underscore-suffix of another: {clashes}"


def test_every_description_carries_a_callable_value_fn():
    """`value_fn` has no default, but a wrong type still type-checks past."""
    bad = [d.key for d in ALL_DESCRIPTIONS if not callable(d.value_fn)]
    assert bad == [], f"these descriptions have a non-callable value_fn: {bad}"


def test_extra_attrs_fn_is_either_absent_or_callable():
    bad = [
        d.key
        for d in ALL_DESCRIPTIONS
        if d.extra_attrs_fn is not None and not callable(d.extra_attrs_fn)
    ]
    assert bad == [], f"these descriptions have a non-callable extra_attrs_fn: {bad}"


def test_day_level_sensors_do_not_also_claim_a_session_value():
    """`day_field` and a real `value_fn` are mutually exclusive.

    The platform reads `day_field` off the day summary when it is set and
    ignores the session entirely. A description that sets `day_field` AND
    returns something from `value_fn` is stating two sources for one
    state, and which one wins is a detail of the platform rather than a
    decision anybody made. The existing day-level descriptions all spell
    this as `lambda session: None` on purpose.
    """
    conflicted = [
        d.key
        for d in INSIGHTS
        if d.day_field is not None and d.value_fn({"sleep_summary": {}}) is not None
    ]
    assert conflicted == [], (
        "these descriptions name a day_field and also return a session "
        f"value, so the state has two sources: {conflicted}"
    )


def test_temperature_sensors_declare_a_unit_home_assistant_can_convert():
    """Without device_class, HA treats a unit as opaque and will not convert.

    This is a real regression that shipped. The schedule temperature
    sensors carried `native_unit_of_measurement` of Celsius and no
    `device_class`, so a Fahrenheit household saw "23 °C" next to a
    climate card reading "78 °F". The fix was adding
    `SensorDeviceClass.TEMPERATURE`, and the note recording that is in
    `descriptions.py` above `bedtime_temp`. The pairing is asserted here
    so the next temperature sensor cannot ship without it.
    """
    from homeassistant.components.sensor import SensorDeviceClass
    from homeassistant.const import UnitOfTemperature

    celsius = {UnitOfTemperature.CELSIUS, UnitOfTemperature.FAHRENHEIT}
    unconvertible = [
        d.key
        for d in ALL_DESCRIPTIONS
        if d.native_unit_of_measurement in celsius
        and d.device_class is not SensorDeviceClass.TEMPERATURE
    ]
    assert unconvertible == [], (
        "these sensors report a temperature unit without "
        "device_class=TEMPERATURE, so Home Assistant will not convert them "
        f"for a Fahrenheit household: {unconvertible}"
    )


def test_duration_string_sensors_carry_no_state_class():
    """A state_class on a string state makes the recorder log errors nightly.

    The human-readable duration sensors emit "7h 53m". Long-term
    statistics require a number, so declaring MEASUREMENT on one of these
    produces a recorder warning on every single state write, forever, and
    no statistics. The numeric counterparts exist precisely so the string
    ones do not need a state_class.
    """
    sample = {"sleep_summary": {"time_asleep": 473}}
    offenders = []
    for description in INSIGHTS:
        if description.day_field is not None:
            continue
        if isinstance(description.value_fn(sample), str) and description.state_class:
            offenders.append(description.key)
    assert offenders == [], (
        "these sensors return a string state and declare a state_class, "
        f"which the recorder cannot turn into statistics: {offenders}"
    )


# ── Hostile input, applied to every description generically ───────────


@pytest.mark.parametrize("payload", VALUE_FN_PAYLOADS, ids=repr)
def test_no_value_fn_raises_on_a_malformed_payload(payload):
    """An exception here is written into the entity's state update.

    Parametrised over the descriptions rather than written per sensor so
    that adding a sensor gets this coverage without anybody remembering.
    """
    for description in INSIGHTS:
        try:
            description.value_fn(payload)
        except Exception as err:  # noqa: BLE001 - the point is that none escape
            pytest.fail(
                f"{description.key}.value_fn raised {err!r} on {payload!r}. This "
                "runs inside SensorEntity.native_value, so it breaks the state "
                "write and leaves the entity holding a stale value."
            )


@pytest.mark.parametrize("payload", ATTRS_FN_PAYLOADS, ids=repr)
def test_no_extra_attrs_fn_raises_on_a_malformed_payload(payload):
    for description in INSIGHTS:
        if description.extra_attrs_fn is None:
            continue
        try:
            result = description.extra_attrs_fn(payload)
        except Exception as err:  # noqa: BLE001 - the point is that none escape
            pytest.fail(
                f"{description.key}.extra_attrs_fn raised {err!r} on {payload!r}"
            )
        assert isinstance(result, dict), (
            f"{description.key}.extra_attrs_fn returned {type(result).__name__} "
            "and Home Assistant needs a mapping for extra_state_attributes"
        )


@pytest.mark.parametrize("schedule", [None, {}, {"bedtime_temp": None}, []], ids=repr)
def test_no_schedule_value_fn_raises_on_a_missing_schedule(schedule):
    """No schedule set is the normal state, not an error state.

    A household that has never opened the schedule screen has no
    `today_sleep_schedule` at all, so every one of these runs against
    None on the very first refresh.
    """
    for description in SCHEDULES:
        try:
            description.value_fn(schedule)
        except Exception as err:  # noqa: BLE001 - the point is that none escape
            pytest.fail(f"{description.key}.value_fn raised {err!r} on {schedule!r}")


# ── The readers, individually ─────────────────────────────────────────


@pytest.mark.parametrize(
    ("minutes", "expected"),
    [
        (None, None),
        (0, "0m"),
        (59, "59m"),
        (60, "1h 0m"),
        (473, "7h 53m"),
        (59.6, "1h 0m"),
        (1440, "24h 0m"),
    ],
)
def test_minutes_to_hm(minutes, expected):
    """The app's own format. 59.6 rounds up and carries into the hour."""
    assert descriptions._minutes_to_hm(minutes) == expected


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [(None, None), (0, "0s"), (59, "59s"), (60, "1m 0s"), (125, "2m 5s")],
)
def test_seconds_to_ms(seconds, expected):
    assert descriptions._seconds_to_ms(seconds) == expected


def test_minutes_value_rejects_bool_because_bool_is_an_int():
    """`True` would otherwise be recorded as a one-minute sleep stage.

    This is not hypothetical tidiness. The numeric duration sensors feed
    long-term statistics, so a single bool arriving in a nightly payload
    would put a 1.0 in the recorder permanently, and statistics rows are
    not something a user can go back and delete.
    """
    assert descriptions._minutes_value(True) is None
    assert descriptions._minutes_value(False) is None
    assert descriptions._minutes_value(1) == 1.0
    assert descriptions._minutes_value(0) == 0.0


@pytest.mark.parametrize("value", ["420", None, [], {}, object()])
def test_minutes_value_rejects_everything_that_is_not_a_number(value):
    assert descriptions._minutes_value(value) is None


def test_time_in_bed_is_none_when_the_pair_is_out_of_order():
    """A negative stay is a data fault and must read unknown.

    Plotting below zero would be worse than plotting nothing, because a
    negative value is still a value and gets into statistics.
    """
    backwards = {
        "in_bed_start_time": "2026-07-02T07:00:00Z",
        "in_bed_end_time": "2026-07-01T23:00:00Z",
    }
    assert descriptions._time_in_bed(backwards) is None


def test_time_in_bed_measures_the_whole_stay():
    session = {
        "in_bed_start_time": "2026-07-01T23:00:00Z",
        "in_bed_end_time": "2026-07-02T07:30:00Z",
    }
    assert descriptions._time_in_bed(session) == 510.0


@pytest.mark.parametrize("session", [None, {}, [], "", {"in_bed_start_time": None}])
def test_time_in_bed_is_none_without_a_usable_pair(session):
    assert descriptions._time_in_bed(session) is None


def test_temp_attrs_names_the_unit_in_the_key():
    """Attributes are not unit-converted, so an unqualified "min" lied.

    Home Assistant converts a sensor's STATE into the user's preferred
    unit and leaves attributes exactly as given. A Fahrenheit household
    therefore saw a state of 69.9 sitting next to a "min" of 17.5, which
    reads as a fault rather than as two units.
    """
    session = {"temperature": {"values": [20.0, 22.0, 24.0]}}
    assert descriptions._temp_attrs(session, "temperature") == {
        "min_celsius": 20.0,
        "max_celsius": 24.0,
        "samples": 3,
    }


@pytest.mark.parametrize(
    "session",
    [None, {}, {"temperature": {}}, {"temperature": {"values": []}}, {"temperature": []}],
    ids=repr,
)
def test_temp_attrs_is_an_empty_mapping_when_there_is_no_series(session):
    """Empty dict, never None. The platform spreads this into attributes."""
    assert descriptions._temp_attrs(session, "temperature") == {}


def test_temp_stats_skips_dropouts_rather_than_counting_them_as_zero():
    """Nulls in the series are missing samples, not cold readings.

    Folding a dropout in as zero would drag a 22 degree average toward
    freezing, and the vendor punches gaps out as nulls routinely.
    """
    session = {"temperature": {"values": [20.0, None, 24.0, "x", True]}}
    stats = descriptions._temp_stats(session, "temperature")
    assert stats == {"average": 22.0, "min": 20.0, "max": 24.0, "samples": 2}


def test_session_end_parses_to_an_aware_datetime():
    """Naive datetimes are rejected by Home Assistant's timestamp sensors."""
    value = descriptions._session_end({"end_time": "2026-07-02T07:30:00+00:00"})
    assert isinstance(value, datetime.datetime)
    assert value.tzinfo is not None


@pytest.mark.parametrize(
    "session", [None, {}, [], {"end_time": None}, {"end_time": "garbage"}], ids=repr
)
def test_session_end_is_none_for_anything_unparseable(session):
    assert descriptions._session_end(session) is None


# ── Day-level lookup ──────────────────────────────────────────────────


def test_day_field_prefers_overview_over_data():
    """Overview is the summary the app itself renders.

    `data` is the fallback for accounts where overview comes back empty,
    so the two disagreeing must resolve to overview. Reversing this would
    show a different score than the vendor's own app for the same night,
    which is the single most confusing thing this integration could do.
    """
    payload = {
        "insights": {
            "overview": {"2026-07-02": {"score": 88}},
            "data": {"2026-07-02": {"score": 41}},
        }
    }
    assert descriptions._get_day_field(payload, "score") == 88


def test_day_field_takes_the_newest_date():
    payload = {
        "insights": {
            "overview": {
                "2026-07-01": {"score": 70},
                "2026-07-02": {"score": 88},
                "2026-06-30": {"score": 51},
            }
        }
    }
    assert descriptions._get_day_field(payload, "score") == 88


def test_day_field_skips_a_newer_day_that_has_no_value_for_this_field():
    """A night can be scored before it is coloured.

    Returning None because the newest day lacks `color` would blank a
    sensor that has a perfectly good answer one day back, and it would do
    it every morning until the vendor filled the field in.
    """
    payload = {
        "insights": {
            "overview": {
                "2026-07-02": {"score": 88},
                "2026-07-01": {"score": 70, "color": "green"},
            }
        }
    }
    assert descriptions._get_day_field(payload, "color") == "green"


def test_day_field_falls_back_to_data_when_overview_is_empty():
    payload = {"insights": {"overview": {}, "data": {"2026-07-02": {"score": 41}}}}
    assert descriptions._get_day_field(payload, "score") == 41


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"insights": {}},
        {"insights": {"overview": None}},
        {"insights": {"overview": []}},
        {"insights": {"overview": {"2026-07-02": None}}},
        {"insights": {"overview": {"2026-07-02": "broken"}}},
    ],
    ids=repr,
)
def test_day_field_is_none_for_every_malformed_insights_shape(payload):
    assert descriptions._get_day_field(payload, "score") is None


def test_partner_day_field_reads_the_partner_account_not_the_primary():
    """Reading the wrong account here publishes one person's sleep as another's.

    This is the same class of defect as the unique_id migration: the
    number is plausible, nothing raises, and the household simply gets
    told the wrong person slept badly.
    """
    payload = {
        "insights": {"overview": {"2026-07-02": {"score": 88}}},
        "partner_insights": {"overview": {"2026-07-02": {"score": 41}}},
    }
    assert descriptions._get_partner_day_field(payload, "score") == 41


def test_partner_day_field_is_none_when_there_is_no_partner():
    assert descriptions._get_partner_day_field({}, "score") is None


# ── Labels ────────────────────────────────────────────────────────────


def test_every_insight_key_has_a_readable_label():
    """The fallback title-cases the key, which is a poor last resort.

    Per-person sensor names are built from these because a translation
    cannot interpolate a runtime alias. A key that falls through to the
    fallback reads as "Apnea Ahi" rather than "AHI", so the mapping is
    asserted to be complete rather than merely present.
    """
    missing = [
        d.key
        for d in INSIGHTS
        if d.key not in descriptions._INSIGHT_DISPLAY_NAMES
    ]
    assert missing == [], (
        "these insight keys have no display name and will fall back to a "
        f"title-cased key: {missing}"
    )


def test_insight_label_falls_back_without_raising():
    assert descriptions._insight_label("some_new_metric") == "Some New Metric"


def test_schedule_labels_cover_every_schedule_description():
    missing = [d.key for d in SCHEDULES if d.key not in descriptions._SCHEDULE_LABELS]
    assert missing == [], f"schedule keys with no label: {missing}"


# ── Schedule attributes ───────────────────────────────────────────────


def test_schedule_temp_attrs_omits_missing_phases_rather_than_showing_null():
    """A null attribute renders as "None" in the UI, which reads as a value."""
    assert descriptions._schedule_temp_attrs(
        {"phase_1_temp": 22.0, "phase_2_temp": None, "wakeup_temp": 24.0}
    ) == {"phase_1_temp": 22.0, "wakeup_temp": 24.0}


def test_schedule_temp_attrs_keeps_a_false_smart_temperature_flag():
    """False is an answer. Only None means the vendor did not say.

    A truthiness check here would drop `is_smart_temperature_active:
    False` and leave the user unable to tell "smart temperature is off"
    from "this bed does not report it".
    """
    attrs = descriptions._schedule_temp_attrs({"is_smart_temperature_active": False})
    assert attrs == {"smart_temperature": False}
