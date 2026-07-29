"""Sensor entity descriptions and the pure readers that feed them.

This module exists to break a layering inversion, not for tidiness.

THE CYCLE IT REMOVES. `migrations.py` derives every unique_id rename from
`INSIGHT_SENSOR_DESCRIPTIONS` rather than from a string pattern, which is
deliberate and documented at the top of that file. But the list used to
live in `sensor.py`, and `sensor.py` imports `coordinator`, `entity` and
`helpers`, while `__init__.py` imports `migrations` at module scope. So
`migrations` importing `sensor` at module scope was a cycle, and the two
call sites dodged it with function-scoped imports instead. A deferred
import is the standard tell for a cycle being avoided rather than
resolved. The descriptions live here now, this module imports nothing
from the platform layer, and both call sites are ordinary module-scope
imports again.

THE EVENT-LOOP IMPORT IT REMOVES. `async_setup_entry` calls
`async_migrate_unique_ids` BEFORE `async_forward_entry_setups`. On a cold
start nothing has imported `custom_components.orion_sleep.sensor` yet, so
the deferred import inside `_planned_renames` performed the first import
of a 2000+ line module from inside the event loop: a disk stat, a read, a
compile, and the transitive import of `homeassistant.components.sensor`
underneath it. Home Assistant ships `async_import_module` precisely
because that is an anti-pattern, and its loop protection flags it. It was
doing all of that to read a list of `key` strings.

Moving the import to module scope in `migrations.py` moves it off the
event loop entirely. Home Assistant loads a custom component's
`__init__.py` in the import executor, so everything reachable from it at
module scope is imported in a worker thread where blocking IO is allowed,
not in the loop.

RESIDUAL COST, STATED HONESTLY. This module still imports
`homeassistant.components.sensor`, measured at roughly 434 ms and 784
transitive modules on a cold interpreter. It cannot avoid that while
`OrionSensorEntityDescription` subclasses `SensorEntityDescription`, and
it has to subclass it: Home Assistant's `SensorEntity` reads
`entity_description.native_unit_of_measurement`, `.state_class`,
`.suggested_display_precision`, `.device_class` and the whole
`EntityDescription` field set off that object, and a hand-rolled
lookalike would silently drift the next time Home Assistant adds a field.
Duck-typing the base class would be a redesign, and this is a move.

What that residual cost is NOT is a problem, because of where it now
happens. In the executor at integration load, once, milliseconds before
`async_forward_entry_setups` imports the sensor platform and pays it
anyway. The expensive thing was never the size of the import. It was
doing it inside the event loop.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import PERCENTAGE, UnitOfTemperature, UnitOfTime
from orion_sleep_api import util

from . import helpers

_INSIGHT_DISPLAY_NAMES = {
    "sleep_score": "Sleep Score",
    "total_sleep_time": "Total Sleep Time",
    "deep_sleep_time": "Deep Sleep",
    "rem_sleep_time": "REM Sleep",
    "light_sleep_time": "Light Sleep",
    "awake_time": "Awake Time",
    "heart_rate_avg": "Heart Rate",
    "breath_rate": "Breath Rate",
    "hrv": "HRV",
    "body_movement_rate": "Body Movement Rate",
    "restless_time": "Restless Time",
    # Numeric counterparts to the human-readable duration sensors above.
    # Those emit strings like "7h 53m" and so can carry no state_class,
    # which means no long-term statistics and nothing to graph. These do.
    "total_sleep_minutes": "Total Sleep Minutes",
    "deep_sleep_minutes": "Deep Sleep Minutes",
    "rem_sleep_minutes": "REM Sleep Minutes",
    "light_sleep_minutes": "Light Sleep Minutes",
    "awake_minutes": "Awake Minutes",
    "last_session_end": "Last Session End",
    "sleep_quality": "Sleep Quality",
    "time_in_bed": "Time in Bed",
    "sleep_efficiency": "Sleep Efficiency",
    "session_confidence": "Session Confidence",
    "avg_target_temperature": "Average Target Temperature",
    "avg_bed_temperature": "Average Bed Temperature",
    "apnea_ahi": "Apnea Index",
    "apnea_obstructive_time": "Obstructive Apnea Time",
    "apnea_central_time": "Central Apnea Time",
    "apnea_longest_event": "Longest Apnea Event",
}


# ── Helpers ────────────────────────────────────────────────────────────────


def _insight_label(key: str) -> str:
    """Human label for one insight metric, used to build per-person names."""
    return _INSIGHT_DISPLAY_NAMES.get(key, key.replace("_", " ").title())


def _get_sleep_summary(session: dict | None) -> dict:
    """Get sleep_summary from a session.

    Type-guarded: every caller immediately does `.get(...)` on the result,
    so returning a vendor-supplied list here would raise AttributeError
    deep inside a value_fn lambda where nothing catches it.
    """
    return util.session_subsection(session, "sleep_summary")


def _get_heart_rate(session: dict | None) -> dict:
    """Get heart_rate from a session."""
    return util.session_subsection(session, "heart_rate")


def _get_breath_rate(session: dict | None) -> dict:
    """Get breath_rate from a session."""
    return util.session_subsection(session, "breath_rate")


def _get_hrv(session: dict | None) -> dict:
    """Get hrv from a session."""
    return util.session_subsection(session, "hrv")


def _get_movement(session: dict | None) -> dict:
    """Get movement from a session."""
    return util.session_subsection(session, "movement")


def _minutes_to_hm(minutes: float | int | None) -> str | None:
    """Convert minutes to 'Xh Ym' string like the app shows."""
    if minutes is None:
        return None
    total = int(round(minutes))
    h, m = divmod(total, 60)
    if h > 0:
        return f"{h}h {m}m"
    return f"{m}m"


def _get_apnea(session: dict | None) -> dict:
    """Breathing-interruption block from a completed session.

    Only populated once a night finishes. Mid-session this is null, so
    every apnea sensor reads unknown while someone is still asleep.
    """
    return util.session_subsection(session, "apnea")


def _minutes_value(minutes: Any) -> float | None:
    """Return a sleep-stage duration as a plain number of minutes.

    Rejects bool explicitly: it subclasses int, so ``True`` would
    otherwise be recorded as a one-minute sleep stage.
    """
    if isinstance(minutes, bool) or not isinstance(minutes, (int, float)):
        return None
    return float(minutes)


def _session_end(session: dict | None) -> Any:
    """Timezone-aware end time of a session, or None.

    Only meaningful on a session already known to be finished. The
    vendor fills end_time in while a night is still running.
    """
    if not isinstance(session, dict):
        return None
    return util.parse_iso_datetime(session.get("end_time"))


def _seconds_to_ms(seconds: float | int | None) -> str | None:
    """Convert seconds to 'Xm Ys' string like the app shows."""
    if seconds is None:
        return None
    total = int(round(seconds))
    m, s = divmod(total, 60)
    if m > 0:
        return f"{m}m {s}s"
    return f"{s}s"


def _temp_stats(session: dict | None, block: str) -> dict | None:
    """Reduce one per-session temperature series to average/min/max.

    `temperature_setpoint` is what the bed was aiming for through the
    night and `temperature` is what it measured. Both arrive as a few
    hundred samples, which no Home Assistant state can hold, so they get
    reduced to scalars here.
    """
    return util.series_stats(util.session_subsection(session, block).get("values"))


def _temp_attrs(session: dict | None, block: str) -> dict:
    stats = _temp_stats(session, block)
    if stats is None:
        return {}
    # Home Assistant converts a sensor's state to the user's preferred
    # unit but leaves attributes exactly as given. Naming these plainly
    # "min" and "max" put 17.5 next to a state of 69.9 and looked like a
    # fault. The unit is in the key instead.
    return {
        "min_celsius": stats["min"],
        "max_celsius": stats["max"],
        "samples": stats["samples"],
    }


def _time_in_bed(session: dict | None) -> float | None:
    """Minutes between getting in and getting out.

    Distinct from time asleep. On one measured night the stay ran 80
    minutes past the end of the session itself.
    """
    if not isinstance(session, dict):
        return None
    return util.duration_minutes(
        session.get("in_bed_start_time"), session.get("in_bed_end_time")
    )


def _get_day_field(coordinator_data: dict, field: str) -> Any:
    """Newest non-null day-level value for one field.

    `overview` and `data` both carry `score`, `quality`, and `color` per
    day. Overview wins because it is the summary the app itself renders,
    with `data` as a fallback for accounts where overview comes back
    empty.
    """
    insights = coordinator_data.get("insights", {})
    for source in (insights.get("overview", {}), insights.get("data", {})):
        if not isinstance(source, dict):
            continue
        for date_key in sorted(source.keys(), reverse=True):
            day = source[date_key]
            if not isinstance(day, dict):
                continue
            value = day.get(field)
            if value is not None:
                return value
    return None


def _get_partner_day_field(coordinator_data: dict, field: str) -> Any:
    """Same lookup against the partner account's own insights."""
    return _get_day_field(
        {"insights": coordinator_data.get("partner_insights", {})}, field
    )


# ── Sensor descriptions ───────────────────────────────────────────────────


@dataclass(frozen=True, kw_only=True)
class OrionSensorEntityDescription(SensorEntityDescription):
    """Describe an Orion Sleep sensor."""

    value_fn: Callable[[dict | None], Any]
    extra_attrs_fn: Callable[[dict | None], dict[str, Any]] | None = None
    icon: str | None = None
    # Read from the newest FINISHED session rather than the newest one.
    # Only set this where an in-progress night would give a wrong answer.
    completed_only: bool = False
    # Read this field off the day summary instead of a session. Day
    # values (score, quality, colour) are not session-scoped.
    day_field: str | None = None
    # Further day-level fields to hang off the entity as attributes.
    day_attrs: tuple[str, ...] = ()


# Duration sensors: we intentionally do NOT set device_class=DURATION.
# HA's DURATION device class overrides entity names on device pages with a
# generic "Duration" label, making all sleep duration sensors indistinguishable.
# Instead we format the values ourselves as human-friendly strings (7h 53m).

INSIGHT_SENSOR_DESCRIPTIONS: tuple[OrionSensorEntityDescription, ...] = (
    OrionSensorEntityDescription(
        key="sleep_score",
        translation_key="sleep_score",
        day_field="score",
        day_attrs=("quality", "color"),
        native_unit_of_measurement="points",
        suggested_display_precision=0,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:medal-outline",
        value_fn=lambda session: None,  # day-level, see day_field
        # Whether this night is the bed's own number or a correction
        # someone made in the app. Worth knowing now that the integration
        # can edit sessions itself.
        extra_attrs_fn=lambda session: {
            "edited": (session or {}).get("has_been_edited"),
            "rated": (session or {}).get("has_been_rated"),
        },
    ),
    OrionSensorEntityDescription(
        key="total_sleep_time",
        translation_key="total_sleep_time",
        icon="mdi:sleep",
        value_fn=lambda session: _minutes_to_hm(_get_sleep_summary(session).get("time_asleep")),
    ),
    OrionSensorEntityDescription(
        key="deep_sleep_time",
        translation_key="deep_sleep_time",
        icon="mdi:power-sleep",
        value_fn=lambda session: _minutes_to_hm(_get_sleep_summary(session).get("deep_sleep")),
    ),
    OrionSensorEntityDescription(
        key="rem_sleep_time",
        translation_key="rem_sleep_time",
        icon="mdi:eye-refresh-outline",
        value_fn=lambda session: _minutes_to_hm(_get_sleep_summary(session).get("rem_sleep")),
    ),
    OrionSensorEntityDescription(
        key="light_sleep_time",
        translation_key="light_sleep_time",
        icon="mdi:weather-night",
        value_fn=lambda session: _minutes_to_hm(_get_sleep_summary(session).get("light_sleep")),
    ),
    OrionSensorEntityDescription(
        key="awake_time",
        translation_key="awake_time",
        icon="mdi:eye-outline",
        value_fn=lambda session: _minutes_to_hm(_get_sleep_summary(session).get("awake_time")),
    ),
    OrionSensorEntityDescription(
        key="heart_rate_avg",
        translation_key="heart_rate_avg",
        native_unit_of_measurement="bpm",
        suggested_display_precision=0,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:heart-pulse",
        value_fn=lambda session: _get_heart_rate(session).get("average"),
        extra_attrs_fn=lambda session: {
            "min": _get_heart_rate(session).get("min"),
            "max": _get_heart_rate(session).get("max"),
            "range": (
                f"{_get_heart_rate(session).get('min')} - {_get_heart_rate(session).get('max')}"
                if _get_heart_rate(session).get("min") is not None
                and _get_heart_rate(session).get("max") is not None
                else None
            ),
        },
    ),
    OrionSensorEntityDescription(
        key="breath_rate",
        translation_key="breath_rate",
        native_unit_of_measurement="breaths/min",
        suggested_display_precision=1,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:lungs",
        value_fn=lambda session: _get_breath_rate(session).get("average"),
        extra_attrs_fn=lambda session: {
            "min": _get_breath_rate(session).get("min"),
            "max": _get_breath_rate(session).get("max"),
            "range": (
                f"{_get_breath_rate(session).get('min')} - {_get_breath_rate(session).get('max')}"
                if _get_breath_rate(session).get("min") is not None
                and _get_breath_rate(session).get("max") is not None
                else None
            ),
        },
    ),
    OrionSensorEntityDescription(
        key="hrv",
        translation_key="hrv",
        native_unit_of_measurement="ms",
        suggested_display_precision=0,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:heart-flash",
        value_fn=lambda session: _get_hrv(session).get("average"),
        extra_attrs_fn=lambda session: {
            "min": _get_hrv(session).get("min"),
            "max": _get_hrv(session).get("max"),
        },
    ),
    OrionSensorEntityDescription(
        key="body_movement_rate",
        translation_key="body_movement_rate",
        native_unit_of_measurement="/hr",
        suggested_display_precision=1,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:run",
        value_fn=lambda session: _get_movement(session).get("movement_rate"),
    ),
    OrionSensorEntityDescription(
        key="restless_time",
        translation_key="restless_time",
        icon="mdi:motion-sensor",
        # Format as human-friendly string like the app (3m 36s)
        value_fn=lambda session: _seconds_to_ms(_get_movement(session).get("total_seconds")),
    ),
    # ── Numeric sleep-stage durations ──────────────────────────────────
    #
    # The five string sensors above are the app-facing presentation and
    # stay for compatibility. These carry the same underlying values as
    # plain numbers so HA can keep long-term statistics and graph them.
    #
    # No device_class. DURATION would add minute/hour conversion nobody
    # asked for, and this codebase already avoids it on the duration
    # sensors. state_class=MEASUREMENT is deliberate: these are
    # independent per-night measurements, NOT accumulations. Using
    # total_increasing would make HA read every shorter night as a meter
    # reset and add the whole value again, permanently corrupting the
    # stored sum.
    #
    # Field names measured on the wire 2026-07-26 against a live
    # in-progress session. They had been asserted upstream since April
    # with no capture behind them.
    OrionSensorEntityDescription(
        key="total_sleep_minutes",
        native_unit_of_measurement=UnitOfTime.MINUTES,
        suggested_display_precision=0,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:sleep",
        value_fn=lambda session: _minutes_value(_get_sleep_summary(session).get("time_asleep")),
    ),
    OrionSensorEntityDescription(
        key="deep_sleep_minutes",
        native_unit_of_measurement=UnitOfTime.MINUTES,
        suggested_display_precision=0,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:power-sleep",
        value_fn=lambda session: _minutes_value(_get_sleep_summary(session).get("deep_sleep")),
    ),
    OrionSensorEntityDescription(
        key="rem_sleep_minutes",
        native_unit_of_measurement=UnitOfTime.MINUTES,
        suggested_display_precision=0,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:eye-refresh-outline",
        value_fn=lambda session: _minutes_value(_get_sleep_summary(session).get("rem_sleep")),
    ),
    OrionSensorEntityDescription(
        key="light_sleep_minutes",
        native_unit_of_measurement=UnitOfTime.MINUTES,
        suggested_display_precision=0,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:weather-night",
        value_fn=lambda session: _minutes_value(_get_sleep_summary(session).get("light_sleep")),
    ),
    OrionSensorEntityDescription(
        key="awake_minutes",
        native_unit_of_measurement=UnitOfTime.MINUTES,
        suggested_display_precision=0,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:eye-outline",
        value_fn=lambda session: _minutes_value(_get_sleep_summary(session).get("awake_time")),
    ),
    # When the last finished night actually ended. state_class is left
    # unset because HA rejects one on a non-numeric sensor.
    OrionSensorEntityDescription(
        key="sleep_quality",
        day_field="quality",
        day_attrs=("color", "score"),
        icon="mdi:star-outline",
        value_fn=lambda session: None,  # day-level, see day_field
    ),
    # ── Session shape ─────────────────────────────────────────────
    #
    # Time in bed is the whole stay, including the stretch spent awake
    # before and after the session proper. Efficiency is the share of
    # that actually slept, which is the standard way to read the pair.
    #
    # Sleep latency is deliberately absent. The obvious source fields,
    # `user_fallasleep_timestamp` and `user_wakeup_timestamp`, come back
    # null on completed sessions: they are user-supplied overrides from
    # the app's edit screen, not measurements. Deriving latency from
    # `in_bed_start_time` instead would inherit the occupancy false
    # positive and report hours of lying awake that never happened.
    OrionSensorEntityDescription(
        key="time_in_bed",
        native_unit_of_measurement=UnitOfTime.MINUTES,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
        icon="mdi:bed-clock",
        completed_only=True,
        value_fn=lambda session: _time_in_bed(session),
        extra_attrs_fn=lambda session: {
            "in_bed_start": (session or {}).get("in_bed_start_time"),
            "in_bed_end": (session or {}).get("in_bed_end_time"),
        },
    ),
    OrionSensorEntityDescription(
        key="sleep_efficiency",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
        icon="mdi:percent-outline",
        completed_only=True,
        value_fn=lambda session: util.sleep_efficiency(
            util.session_subsection(session, "sleep_summary").get("time_asleep"),
            _time_in_bed(session),
        ),
    ),
    OrionSensorEntityDescription(
        key="session_confidence",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
        icon="mdi:check-decagram-outline",
        completed_only=True,
        value_fn=lambda session: util.confidence_percent(
            (session or {}).get("confidence")
        ),
    ),
    # ── Breathing ─────────────────────────────────────────────────
    #
    # Reported per completed session and previously discarded entirely.
    # AHI is events per hour of sleep and is the figure sleep clinics
    # actually use, which makes it the most consequential number the bed
    # produces. It is also an estimate from a mattress topper, so the
    # sensors carry the vendor's numbers and nothing else. No severity
    # banding, no interpretation: see the README.
    OrionSensorEntityDescription(
        key="apnea_ahi",
        native_unit_of_measurement="events/h",
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        icon="mdi:lungs",
        value_fn=lambda session: util.apnea_number(_get_apnea(session).get("ahi")),
    ),
    OrionSensorEntityDescription(
        key="apnea_obstructive_time",
        native_unit_of_measurement=UnitOfTime.SECONDS,
        suggested_display_precision=0,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:lungs",
        value_fn=lambda session: util.apnea_number(
            _get_apnea(session).get("obstructive_total_seconds")
        ),
    ),
    OrionSensorEntityDescription(
        key="apnea_central_time",
        native_unit_of_measurement=UnitOfTime.SECONDS,
        suggested_display_precision=0,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:lungs",
        value_fn=lambda session: util.apnea_number(
            _get_apnea(session).get("central_total_seconds")
        ),
    ),
    OrionSensorEntityDescription(
        key="apnea_longest_event",
        native_unit_of_measurement=UnitOfTime.SECONDS,
        suggested_display_precision=0,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:timer-alert-outline",
        value_fn=lambda session: util.apnea_number(
            _get_apnea(session).get("longest_event_seconds")
        ),
    ),
    # ── Overnight temperature ─────────────────────────────────────
    #
    # The live climate entity already records target and measured temp
    # continuously, but only from whenever the integration was installed
    # and only until the recorder purges it. These are the vendor's own
    # series for the night, reduced to scalars and tied to the session.
    OrionSensorEntityDescription(
        key="avg_target_temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        icon="mdi:thermometer-check",
        completed_only=True,
        value_fn=lambda session: (_temp_stats(session, "temperature_setpoint") or {}).get(
            "average"
        ),
        extra_attrs_fn=lambda session: _temp_attrs(session, "temperature_setpoint"),
    ),
    OrionSensorEntityDescription(
        key="avg_bed_temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        icon="mdi:thermometer",
        completed_only=True,
        value_fn=lambda session: (_temp_stats(session, "temperature") or {}).get("average"),
        extra_attrs_fn=lambda session: _temp_attrs(session, "temperature"),
    ),
    OrionSensorEntityDescription(
        key="last_session_end",
        device_class=SensorDeviceClass.TIMESTAMP,
        icon="mdi:clock-check-outline",
        completed_only=True,
        value_fn=_session_end,
    ),
)

# Schedule sensors — derived from today_sleep_schedule, not sessions.
#
# These are per-person. Names are built imperatively from the display alias
# rather than a translation_key, because the person's name has to lead and
# translations cannot interpolate a runtime value.
_SCHEDULE_LABELS: dict[str, str] = {
    "schedule_duration": "Schedule Duration",
    "bedtime_temp": "Bedtime Temperature",
    "phase_1_temp": "Asleep Phase 1 Temperature",
    "phase_2_temp": "Asleep Phase 2 Temperature",
    "wakeup_temp": "Wake Up Temperature",
}

SCHEDULE_SENSOR_DESCRIPTIONS: tuple[OrionSensorEntityDescription, ...] = (
    OrionSensorEntityDescription(
        key="schedule_duration",
        icon="mdi:timer-sand",
        value_fn=lambda schedule: helpers.schedule_duration_text(schedule),
    ),
    # device_class=TEMPERATURE added 2026-07-26. Without it HA treats "°C"
    # as a custom unit and will NOT convert for a Fahrenheit household, so
    # these rendered as "23 °C" beside a climate card reading "78 °F".
    OrionSensorEntityDescription(
        key="bedtime_temp",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        suggested_display_precision=1,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:thermometer-lines",
        value_fn=lambda schedule: schedule.get("bedtime_temp") if schedule else None,
        extra_attrs_fn=lambda schedule: _schedule_temp_attrs(schedule),
    ),
    # phase_1_temp and phase_2_temp were previously only extra attributes on
    # the bedtime temperature sensor. Promoted to first-class sensors so they
    # graph and generate long-term statistics like the other two. New keys for
    # both people, so there is no legacy id to preserve.
    OrionSensorEntityDescription(
        key="phase_1_temp",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        suggested_display_precision=1,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:thermometer-chevron-down",
        value_fn=lambda schedule: schedule.get("phase_1_temp") if schedule else None,
    ),
    OrionSensorEntityDescription(
        key="phase_2_temp",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        suggested_display_precision=1,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:thermometer-chevron-up",
        value_fn=lambda schedule: schedule.get("phase_2_temp") if schedule else None,
    ),
    OrionSensorEntityDescription(
        key="wakeup_temp",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        suggested_display_precision=1,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:thermometer-alert",
        value_fn=lambda schedule: schedule.get("wakeup_temp") if schedule else None,
    ),
)


def _schedule_temp_attrs(schedule: dict | None) -> dict[str, Any]:
    """Extra attributes for the bedtime temp sensor showing the full temp curve."""
    if not schedule:
        return {}
    attrs: dict[str, Any] = {}
    for key in ("phase_1_temp", "phase_2_temp", "wakeup_temp"):
        val = schedule.get(key)
        if val is not None:
            attrs[key] = val
    if schedule.get("is_smart_temperature_active") is not None:
        attrs["smart_temperature"] = schedule["is_smart_temperature_active"]
    return attrs
