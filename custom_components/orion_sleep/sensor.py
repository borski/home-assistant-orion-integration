"""Sensor platform for Orion Sleep."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import voluptuous as vol
from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE, UnitOfTemperature, UnitOfTime
from homeassistant.core import HomeAssistant, SupportsResponse
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import (
    AddEntitiesCallback,
    async_get_current_platform,
)
from homeassistant.util import dt as dt_util

from . import util
from .api import OrionApiError
from .coordinator import OrionDataUpdateCoordinator
from .entity import OrionBaseEntity

# Topper sensors exposed on every WS payload. Mapping to zone_a/zone_b
# isn't verified yet, so entities are named per sensor.
_TOPPER_SENSORS: tuple[str, ...] = ("sensor1", "sensor2")

_LOGGER = logging.getLogger(__name__)

SERVICE_LIST_SLEEP_SESSIONS = "list_sleep_sessions"
SERVICE_DELETE_SLEEP_SESSION = "delete_sleep_session"
SERVICE_CONFIRM_SLEEP_SESSION = "confirm_sleep_session"
SERVICE_EDIT_SLEEP_SESSION = "edit_sleep_session"
SERVICE_END_SLEEP_SESSION = "end_sleep_session"
SERVICE_LIST_INVITES = "list_invites"
SERVICE_INVITE_USER = "invite_user"
SERVICE_CANCEL_INVITE = "cancel_invite"
SERVICE_ACCEPT_INVITE = "accept_invite"
SERVICE_REMOVE_USER_ACCESS = "remove_user_access"
SERVICE_CREATE_GUEST = "create_guest"
SERVICE_UPDATE_USER_PHONE = "update_user_phone"

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
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:sleep",
        value_fn=lambda session: _minutes_value(_get_sleep_summary(session).get("time_asleep")),
    ),
    OrionSensorEntityDescription(
        key="deep_sleep_minutes",
        native_unit_of_measurement=UnitOfTime.MINUTES,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:power-sleep",
        value_fn=lambda session: _minutes_value(_get_sleep_summary(session).get("deep_sleep")),
    ),
    OrionSensorEntityDescription(
        key="rem_sleep_minutes",
        native_unit_of_measurement=UnitOfTime.MINUTES,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:eye-refresh-outline",
        value_fn=lambda session: _minutes_value(_get_sleep_summary(session).get("rem_sleep")),
    ),
    OrionSensorEntityDescription(
        key="light_sleep_minutes",
        native_unit_of_measurement=UnitOfTime.MINUTES,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:weather-night",
        value_fn=lambda session: _minutes_value(_get_sleep_summary(session).get("light_sleep")),
    ),
    OrionSensorEntityDescription(
        key="awake_minutes",
        native_unit_of_measurement=UnitOfTime.MINUTES,
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
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:lungs",
        value_fn=lambda session: util.apnea_number(
            _get_apnea(session).get("obstructive_total_seconds")
        ),
    ),
    OrionSensorEntityDescription(
        key="apnea_central_time",
        native_unit_of_measurement=UnitOfTime.SECONDS,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:lungs",
        value_fn=lambda session: util.apnea_number(
            _get_apnea(session).get("central_total_seconds")
        ),
    ),
    OrionSensorEntityDescription(
        key="apnea_longest_event",
        native_unit_of_measurement=UnitOfTime.SECONDS,
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
        value_fn=lambda schedule: util.schedule_duration_text(schedule),
    ),
    # device_class=TEMPERATURE added 2026-07-26. Without it HA treats "°C"
    # as a custom unit and will NOT convert for a Fahrenheit household, so
    # these rendered as "23 °C" beside a climate card reading "78 °F".
    OrionSensorEntityDescription(
        key="bedtime_temp",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
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
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:thermometer-chevron-down",
        value_fn=lambda schedule: schedule.get("phase_1_temp") if schedule else None,
    ),
    OrionSensorEntityDescription(
        key="phase_2_temp",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:thermometer-chevron-up",
        value_fn=lambda schedule: schedule.get("phase_2_temp") if schedule else None,
    ),
    OrionSensorEntityDescription(
        key="wakeup_temp",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
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


# ── Setup ─────────────────────────────────────────────────────────────────


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Orion Sleep sensor entities."""
    coordinator: OrionDataUpdateCoordinator = entry.runtime_data
    entities: list[SensorEntity] = []

    for device in coordinator.devices:
        device_id = device.get("id")
        if not device_id:
            continue
        for description in INSIGHT_SENSOR_DESCRIPTIONS:
            entities.append(OrionSensorEntity(coordinator, device_id, description))
        for user_id in coordinator.schedule_user_ids():
            for description in SCHEDULE_SENSOR_DESCRIPTIONS:
                entities.append(
                    OrionScheduleSensorEntity(coordinator, device_id, description, user_id)
                )
        entities.append(OrionCurrentTempOffsetSensor(coordinator, device_id))
        entities.append(OrionWebSocketStateSensor(coordinator, device_id))
        for zone_id in coordinator.device_zone_ids(device_id):
            entities.append(OrionZoneMeasuredTempSensor(coordinator, device_id, zone_id))
            entities.append(OrionZoneTargetTempSensor(coordinator, device_id, zone_id))
            entities.append(OrionCoolingEndsSensor(coordinator, device_id, zone_id))
        for sensor_name in _TOPPER_SENSORS:
            entities.append(OrionLiveHeartRateSensor(coordinator, device_id, sensor_name))
            entities.append(OrionLiveBreathRateSensor(coordinator, device_id, sensor_name))
            entities.append(OrionSensorStatusTextSensor(coordinator, device_id, sensor_name))
        entities.append(OrionLedBrightnessSensor(coordinator, device_id))
        entities.append(OrionFirmwareSensor(coordinator, device_id))
        entities.append(OrionWifiSignalSensor(coordinator, device_id))
        entities.append(OrionAccessSensor(coordinator, device_id))
        if coordinator.has_partner_for_device(device_id):
            for description in INSIGHT_SENSOR_DESCRIPTIONS:
                entities.append(OrionPartnerInsightSensor(coordinator, device_id, description))

    async_add_entities(entities)

    # Both services are registered on this platform because sleep sessions
    # belong to an account, and the insight sensors are the only entities
    # that know which account they speak for. Targeting one of a person's
    # insight sensors is how the caller says whose session they mean,
    # without ever handling a raw Orion user id.
    platform = async_get_current_platform()
    platform.async_register_entity_service(
        SERVICE_LIST_SLEEP_SESSIONS,
        {vol.Optional("limit", default=30): vol.All(int, vol.Range(min=1, max=200))},
        "async_list_sleep_sessions",
        supports_response=SupportsResponse.ONLY,
    )
    platform.async_register_entity_service(
        SERVICE_DELETE_SLEEP_SESSION,
        {
            vol.Required("session_id"): cv.string,
            vol.Required("reason"): vol.In(sorted(util.SESSION_DELETE_REASONS)),
            vol.Required("confirm"): vol.All(cv.boolean, vol.Equal(True)),
        },
        "async_delete_sleep_session",
    )
    platform.async_register_entity_service(
        SERVICE_EDIT_SLEEP_SESSION,
        {
            vol.Required("session_id"): cv.string,
            vol.Required("fell_asleep"): cv.datetime,
            vol.Required("woke_up"): cv.datetime,
        },
        "async_edit_sleep_session",
    )
    platform.async_register_entity_service(
        SERVICE_CONFIRM_SLEEP_SESSION,
        {
            vol.Required("session_id"): cv.string,
            vol.Optional("claim", default="me"): vol.In(["me", "both"]),
        },
        "async_confirm_sleep_session",
    )
    platform.async_register_entity_service(
        SERVICE_END_SLEEP_SESSION,
        {vol.Required("confirm"): cv.boolean},
        "async_end_sleep_session",
    )

    # Access management is device-scoped rather than person-scoped, so it
    # targets the Bed Access sensor. The device owns the guest list; a
    # person's insight sensor has nothing to say about who else can use
    # the bed.
    platform.async_register_entity_service(
        SERVICE_LIST_INVITES,
        {},
        "async_list_invites",
        supports_response=SupportsResponse.ONLY,
    )
    platform.async_register_entity_service(
        SERVICE_INVITE_USER,
        {
            vol.Required("phone_number"): cv.string,
            vol.Required("role"): vol.In(sorted(util.INVITE_ROLES)),
        },
        "async_invite_user",
    )
    platform.async_register_entity_service(
        SERVICE_CANCEL_INVITE,
        {vol.Required("invite_id"): cv.string},
        "async_cancel_invite",
    )
    platform.async_register_entity_service(
        SERVICE_ACCEPT_INVITE,
        {vol.Required("code"): cv.string},
        "async_accept_invite",
    )
    platform.async_register_entity_service(
        SERVICE_REMOVE_USER_ACCESS,
        {
            vol.Required("user_id"): cv.string,
            vol.Required("confirm"): vol.All(cv.boolean, vol.Equal(True)),
        },
        "async_remove_user_access",
    )
    platform.async_register_entity_service(
        SERVICE_CREATE_GUEST,
        {},
        "async_create_guest",
    )
    platform.async_register_entity_service(
        SERVICE_UPDATE_USER_PHONE,
        {
            vol.Required("user_id"): cv.string,
            vol.Required("phone"): cv.string,
        },
        "async_update_user_phone",
    )


# ── Entities ──────────────────────────────────────────────────────────────


class OrionSensorEntity(OrionBaseEntity, SensorEntity):
    """Sensor entity for Orion Sleep insights."""

    entity_description: OrionSensorEntityDescription

    def __init__(
        self,
        coordinator: OrionDataUpdateCoordinator,
        device_id: str,
        description: OrionSensorEntityDescription,
    ) -> None:
        super().__init__(coordinator, device_id)
        self.entity_description = description
        self._attr_unique_id = f"{device_id}_{description.key}"
        # These are the AUTHENTICATED account holder's insights, not a
        # device aggregate. Naming them explicitly keeps them symmetric
        # with the partner set instead of leaving one side unlabelled.
        self._attr_name = f"{coordinator.primary_name()} {_insight_label(description.key)}"

    def _session(self) -> dict | None:
        if self.entity_description.completed_only:
            return self.coordinator.get_latest_completed_session()
        return self.coordinator.get_latest_session()

    def _day_value(self, field: str) -> Any:
        return _get_day_field(self.coordinator.data or {}, field)

    # ── Session management ────────────────────────────────────────
    #
    # Sessions belong to the account that recorded them, so both of
    # these resolve their own client rather than reaching for the
    # primary one. The partner subclass overrides both.

    def _sessions_insights(self) -> dict:
        return util.nested_mapping(self.coordinator.data, "insights", "data")

    def _sessions_client(self):
        return self.coordinator.api_client

    def _sessions_owner(self) -> str:
        return self.coordinator.primary_name()

    def _sessions_user_id(self) -> str | None:
        return self.coordinator.user_id

    async def async_list_sleep_sessions(self, limit: int = 30) -> dict:
        """Return this person's recent sessions, newest first.

        Read-only. Exists so a session id can be found without ever
        putting one into entity state, where it would be recorded
        forever for the sake of a lookup done once.
        """
        sessions = util.summarize_sessions(self._sessions_insights(), limit)
        return {
            "owner": self._sessions_owner(),
            "count": len(sessions),
            "sessions": sessions,
        }

    async def async_edit_sleep_session(
        self, session_id: str, fell_asleep: datetime, woke_up: datetime
    ) -> None:
        """Move a session's boundaries and let the bed reanalyse it.

        This does not relabel a night. The server recomputes sleep
        stages, heart rate, breathing, apnea, movement, and temperature
        from the new window, so the numbers afterwards are genuinely
        different numbers rather than the same ones shifted.

        It is reversible: calling this again with the original pair
        restores every derived metric exactly. That is measured, not
        hoped for.

        A naive datetime is refused. Home Assistant hands one over in the
        user's local time when a UI datetime selector is used, so it is
        attached here where the timezone is actually known, rather than
        guessed at deeper down.
        """
        known = {
            row["session_id"]
            for row in util.summarize_sessions(self._sessions_insights(), 200)
        }
        if session_id not in known:
            raise HomeAssistantError(
                f"No session {session_id} belongs to {self._sessions_owner()}. "
                "Run orion_sleep.list_sleep_sessions against this entity first."
            )

        local = dt_util.DEFAULT_TIME_ZONE
        aware = []
        for value in (fell_asleep, woke_up):
            aware.append(value.replace(tzinfo=local) if value.tzinfo is None else value)

        try:
            asleep_at, awake_at = util.session_edit_window(aware[0], aware[1])
        except ValueError as err:
            raise HomeAssistantError(str(err)) from err

        client = self._sessions_client()
        if client is None:
            raise HomeAssistantError(
                f"No Orion client available for {self._sessions_owner()}"
            )

        _LOGGER.warning(
            "Editing an Orion sleep session for %s. The bed will reanalyse "
            "the night, so stages and vitals will change.",
            self._sessions_owner(),
        )
        try:
            await client.edit_sleep_session(session_id, asleep_at, awake_at)
        except OrionApiError as err:
            raise HomeAssistantError(f"Orion refused the session edit: {err}") from err
        await self.coordinator.async_request_refresh()

    async def async_end_sleep_session(self, confirm: bool) -> None:
        """Stop the session the bed currently thinks is running.

        For the case where the topper has decided someone is asleep who
        is not. Ending it now beats deleting a fabricated night in the
        morning, because the bad window stops growing and everything
        downstream of it never gets computed.

        There is no session id here. The route ends whatever is open for
        this entity's account, which is why `confirm` is required: it is
        not addressed at a specific night and cannot be aimed.
        """
        if not confirm:
            raise HomeAssistantError(
                "Refusing to end a sleep session without confirm: true"
            )

        session = self._session()
        if not self.coordinator.session_active(session):
            raise HomeAssistantError(
                f"No sleep session is in progress for {self._sessions_owner()}"
            )

        _LOGGER.warning(
            "Ending the in-progress Orion sleep session for %s",
            self._sessions_owner(),
        )
        try:
            await self._sessions_client().end_sleep_session()
        except OrionApiError as err:
            raise HomeAssistantError(f"Orion rejected ending the session: {err}") from err
        await self.coordinator.async_request_refresh()

    async def async_confirm_sleep_session(
        self, session_id: str, claim: str = "me"
    ) -> None:
        """Tell the bed who a session belongs to.

        `claim="me"` attributes the night to this entity's person.
        `claim="both"` attributes it to both sleepers, which is the
        vendor app's "both of us" option and is what a shared night
        genuinely looks like.

        Same ownership check as delete: the id has to be one of this
        person's own sessions. Unlike delete this is recoverable, so the
        check is about catching a typo rather than preventing loss.
        """
        known = {
            row["session_id"]
            for row in util.summarize_sessions(self._sessions_insights(), 200)
        }
        if session_id not in known:
            raise HomeAssistantError(
                f"No session {session_id} belongs to {self._sessions_owner()}. "
                "Run orion_sleep.list_sleep_sessions against this entity first."
            )

        own_id = self._sessions_user_id()
        if not own_id:
            raise HomeAssistantError(
                f"No Orion user id resolved for {self._sessions_owner()}"
            )

        user_ids = [own_id]
        if claim == "both":
            other = self.coordinator.user_id
            partner = (self.coordinator.partner_user or {}).get("id")
            for candidate in (other, partner):
                if isinstance(candidate, str) and candidate and candidate not in user_ids:
                    user_ids.append(candidate)
            if len(user_ids) < 2:
                raise HomeAssistantError(
                    "Cannot claim a session for both sleepers: only one "
                    "account is linked."
                )

        client = self._sessions_client()
        if client is None:
            raise HomeAssistantError(
                f"No API client available for {self._sessions_owner()}"
            )

        try:
            await client.confirm_sleep_session(session_id, user_ids)
        except ValueError as err:
            raise HomeAssistantError(str(err)) from err

        await self.coordinator.async_request_refresh()

    async def async_delete_sleep_session(
        self, session_id: str, reason: str, confirm: bool
    ) -> None:
        """Permanently delete one sleep session. There is no undo.

        `confirm` is required and must be true. It buys nothing against
        a deliberate mistake, but it does stop a half-finished service
        call in the UI from destroying a night, which is the realistic
        failure here.

        The id is checked against this person's own sessions first. The
        server would presumably reject someone else's id, but "presumably"
        is not good enough for the one call that cannot be taken back,
        and a typo'd id that happens to be real is exactly the case worth
        catching locally.
        """
        if not confirm:
            raise HomeAssistantError("Refusing to delete: confirm was not set")

        known = {
            row["session_id"] for row in util.summarize_sessions(self._sessions_insights(), 200)
        }
        if session_id not in known:
            raise HomeAssistantError(
                f"No session {session_id} belongs to {self._sessions_owner()}. "
                "Run orion_sleep.list_sleep_sessions against this entity to see "
                "which sessions exist and who owns them."
            )

        client = self._sessions_client()
        if client is None:
            raise HomeAssistantError(
                f"No API client available for {self._sessions_owner()}"
            )

        _LOGGER.warning(
            "Deleting sleep session for %s, reason %s. This cannot be undone",
            self._sessions_owner(),
            reason,
        )
        try:
            await client.delete_sleep_session(session_id, reason)
        except ValueError as err:
            raise HomeAssistantError(str(err)) from err
        await self.coordinator.async_request_refresh()

    @property
    def native_value(self) -> Any:
        """Return the sensor value."""
        if not self.coordinator.data:
            return None

        if self.entity_description.day_field:
            return self._day_value(self.entity_description.day_field)

        return self.entity_description.value_fn(self._session())

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return extra state attributes."""
        if not self.coordinator.data:
            return None

        # A day-level sensor can still carry session attributes. Sleep
        # score is both: the number comes from the day bucket, while
        # "was this night edited" comes from the session inside it.
        attrs: dict[str, Any] = {
            name: self._day_value(name)
            for name in self.entity_description.day_attrs
        }
        if self.entity_description.extra_attrs_fn is not None:
            attrs.update(self.entity_description.extra_attrs_fn(self._session()))
        # Filter out None values
        return {k: v for k, v in attrs.items() if v is not None} or None


class OrionPartnerInsightSensor(OrionSensorEntity):
    """Sleep insight from the independently authenticated partner account."""

    entity_description: OrionSensorEntityDescription

    def __init__(
        self,
        coordinator: OrionDataUpdateCoordinator,
        device_id: str,
        description: OrionSensorEntityDescription,
    ) -> None:
        super().__init__(coordinator, device_id, description)
        self._attr_unique_id = f"{device_id}_partner_{description.key}"
        self._attr_name = f"{coordinator.partner_name()} {_insight_label(description.key)}"

    def _session(self) -> dict | None:
        if self.entity_description.completed_only:
            return self.coordinator.get_latest_completed_partner_session(self._device_id)
        return self.coordinator.get_latest_partner_session(self._device_id)

    def _day_value(self, field: str) -> Any:
        return _get_partner_day_field(self.coordinator.data or {}, field)

    def _sessions_insights(self) -> dict:
        return util.nested_mapping(self.coordinator.data, "partner_insights", "data")

    def _sessions_client(self):
        return self.coordinator.partner_api_client

    def _sessions_owner(self) -> str:
        return self.coordinator.partner_name()

    def _sessions_user_id(self) -> str | None:
        partner_id = (self.coordinator.partner_user or {}).get("id")
        return partner_id if isinstance(partner_id, str) and partner_id else None


    @property
    def available(self) -> bool:
        return (
            super().available
            and self.coordinator.has_partner_for_device(self._device_id)
            and self.coordinator.partner_mapping_valid
            and self.coordinator.partner_update_ok
        )


class OrionScheduleSensorEntity(OrionBaseEntity, SensorEntity):
    """One person's schedule sensor.

    The API returns rows for everyone on the bed in a single fetch with
    the primary token, so a partner's row costs no extra request and
    stays readable even if their own token has expired.
    """

    entity_description: OrionSensorEntityDescription

    def __init__(
        self,
        coordinator: OrionDataUpdateCoordinator,
        device_id: str,
        description: OrionSensorEntityDescription,
        user_id: str,
    ) -> None:
        super().__init__(coordinator, device_id)
        self.entity_description = description
        self._user_id = user_id
        self._attr_unique_id = util.schedule_unique_id(
            device_id, description.key, user_id
        )
        self._attr_translation_key = None
        self._attr_name = (
            f"{coordinator.display_name_for_user(user_id)} {_SCHEDULE_LABELS[description.key]}"
        )

    @property
    def available(self) -> bool:
        """Only available once this person's row is present."""
        return super().available and self.coordinator.has_schedule_for_user(self._user_id)

    @property
    def native_value(self) -> Any:
        """Return the sensor value from today's schedule."""
        schedule = self.coordinator.get_today_schedule(self._user_id)
        return self.entity_description.value_fn(schedule)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return extra state attributes."""
        if self.entity_description.extra_attrs_fn is None:
            return None
        schedule = self.coordinator.get_today_schedule(self._user_id)
        attrs = self.entity_description.extra_attrs_fn(schedule)
        return {k: v for k, v in attrs.items() if v is not None} or None


class OrionCurrentTempOffsetSensor(OrionBaseEntity, SensorEntity):
    """Sensor showing the current measured bed temperature as an app-style offset.

    The Orion app displays bed temperature as a relative offset,
    e.g. -3, 0, +5. This sensor shows the actual measured temperature
    offset from the latest sleep session — the value labeled "Now" in
    the app's temperature curve.

    Uses the device's temperature_scale.relative lookup table for
    accurate non-linear conversion.
    """

    _attr_translation_key = "current_temp_offset"
    _attr_icon = "mdi:thermometer"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(
        self,
        coordinator: OrionDataUpdateCoordinator,
        device_id: str,
    ) -> None:
        super().__init__(coordinator, device_id)
        self._attr_unique_id = f"{device_id}_current_temp_offset"

    @property
    def native_value(self) -> float | None:
        """Return the current measured temperature offset."""
        session = self.coordinator.get_latest_session()
        if not session:
            return None
        temp_data = session.get("temperature", {})
        values = temp_data.get("values", [])
        if values:
            return self._celsius_to_offset(values[-1])
        return None


class OrionWebSocketStateSensor(OrionBaseEntity, SensorEntity):
    """Diagnostic sensor exposing the live-device WebSocket state.

    Mirrors the Android app's ``connectionState`` enum. Useful for
    automations that should pause when the device is unreachable.
    """

    _attr_translation_key = "websocket_state"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:lan-connect"

    def __init__(
        self,
        coordinator: OrionDataUpdateCoordinator,
        device_id: str,
    ) -> None:
        super().__init__(coordinator, device_id)
        self._attr_unique_id = f"{device_id}_websocket_state"

    def _serial(self) -> str | None:
        device = self._get_device()
        return device.get("serial_number")

    @property
    def native_value(self) -> str | None:
        serial = self._serial()
        if not serial:
            return None
        return self.coordinator.ws_state(serial)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        serial = self._serial()
        if not serial:
            return None
        last_at = self.coordinator.ws_last_message_at(serial)
        if not last_at:
            return {"seconds_since_last_message": None}
        import time

        return {"seconds_since_last_message": round(time.monotonic() - last_at, 1)}

    @property
    def available(self) -> bool:
        # Always show the state — that's the whole point of this sensor.
        return True


class _OrionLiveSensorBase(OrionBaseEntity, SensorEntity):
    """Shared plumbing for per-topper-sensor live entities."""

    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(
        self,
        coordinator: OrionDataUpdateCoordinator,
        device_id: str,
        sensor_name: str,
        unique_suffix: str,
    ) -> None:
        super().__init__(coordinator, device_id)
        self._sensor_name = sensor_name
        self._attr_unique_id = f"{device_id}_{sensor_name}_{unique_suffix}"

    @property
    def available(self) -> bool:
        # Available whenever we've seen any live frame for this device.
        return self.coordinator.sensor_status_text(self._device_id, self._sensor_name) is not None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        block = self.coordinator._sensor_block(  # noqa: SLF001
            self._device_id, self._sensor_name
        )
        if not block:
            return None
        return {
            "status_text": block.get("status_text"),
            "is_working": block.get("is_working"),
            "firmware_version": block.get("firmware_version"),
            "hardware_version": block.get("hardware_version"),
        }


class OrionLiveHeartRateSensor(_OrionLiveSensorBase):
    """Realtime heart-rate reading from one topper sensor.

    Sourced from the WS ``status.sensors.<sensor>.heart_rate`` field.
    The raw value is 0 when the bed is empty and 255 when the sensor
    has no reading yet — both are mapped to ``None`` so automations
    don't react to sentinels. This is distinct from the post-session
    ``heart_rate_avg`` insight sensor, which only updates after Orion's
    cloud aggregates a completed session.
    """

    # HR isn't one of HA's built-in sensor device classes, so leave
    # device_class unset and surface the value + unit only.
    _attr_native_unit_of_measurement = "bpm"
    _attr_icon = "mdi:heart-pulse"

    def __init__(
        self,
        coordinator: OrionDataUpdateCoordinator,
        device_id: str,
        sensor_name: str,
    ) -> None:
        super().__init__(coordinator, device_id, sensor_name, "live_heart_rate")
        self._attr_translation_key = f"{sensor_name}_live_heart_rate"

    @property
    def native_value(self) -> int | None:
        return self.coordinator.sensor_heart_rate(self._device_id, self._sensor_name)


class OrionLiveBreathRateSensor(_OrionLiveSensorBase):
    """Realtime breath-rate reading from one topper sensor."""

    _attr_native_unit_of_measurement = "br/min"
    _attr_icon = "mdi:lungs"

    def __init__(
        self,
        coordinator: OrionDataUpdateCoordinator,
        device_id: str,
        sensor_name: str,
    ) -> None:
        super().__init__(coordinator, device_id, sensor_name, "live_breath_rate")
        self._attr_translation_key = f"{sensor_name}_live_breath_rate"

    @property
    def native_value(self) -> int | None:
        return self.coordinator.sensor_breath_rate(self._device_id, self._sensor_name)


class OrionSensorStatusTextSensor(_OrionLiveSensorBase):
    """Diagnostic sensor exposing the raw ``status_text`` field.

    Observed values: ``left_bed``, ``normal``. Other values likely exist
    in the app's string tables (e.g. error states) but haven't been seen
    on the wire yet — surfacing the raw value makes it easy to catch new
    values without another integration release.
    """

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:sleep"
    _attr_state_class = None  # categorical, not numeric

    def __init__(
        self,
        coordinator: OrionDataUpdateCoordinator,
        device_id: str,
        sensor_name: str,
    ) -> None:
        super().__init__(coordinator, device_id, sensor_name, "sensor_status")
        self._attr_translation_key = f"{sensor_name}_status_text"

    @property
    def native_value(self) -> str | None:
        return self.coordinator.sensor_status_text(self._device_id, self._sensor_name)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Raw sensor fields, kept for diagnosing the occupancy defect.

        ``status_text`` has been seen reading ``normal`` on a provably
        empty side, so the occupancy binary sensor built on it produces
        false positives. These attributes carry the undocumented
        ``status`` integer, the sleep/wake sign flags, and the unmapped
        heart and breath rates so the real discriminator can be found in
        recorded history rather than guessed at from one observation.
        """
        return self.coordinator.sensor_diagnostics(self._device_id, self._sensor_name)


class _OrionZoneTempSensor(OrionBaseEntity, SensorEntity):
    """Shared plumbing for the per-zone temperature sensors.

    These duplicate values already carried on the climate entity, and that
    is the point. A climate entity's `current_temperature` and
    `target_temperature` are attributes, so they are graphable from the
    recorder but are not retained as long-term statistics past the purge
    window. A `sensor` with a `state_class` is.
    """

    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_state_class = SensorStateClass.MEASUREMENT

    _suffix = ""
    _label = ""

    def __init__(
        self,
        coordinator: OrionDataUpdateCoordinator,
        device_id: str,
        zone_id: str,
    ) -> None:
        super().__init__(coordinator, device_id)
        self._zone_id = zone_id
        self._attr_unique_id = f"{device_id}_{zone_id}_{self._suffix}"
        self._attr_name = f"{coordinator.zone_label(device_id, zone_id)} {self._label}"

    def _read(self) -> float | None:
        raise NotImplementedError

    @property
    def available(self) -> bool:
        return super().available and self._read() is not None

    @property
    def native_value(self) -> float | None:
        return self._read()


class OrionZoneMeasuredTempSensor(_OrionZoneTempSensor):
    """Measured temperature at one zone, from `status.zones[].temp`."""

    _suffix = "measured_temp"
    _label = "Measured Temperature"
    _attr_icon = "mdi:thermometer"

    def _read(self) -> float | None:
        return self.coordinator.zone_measured_temp(self._device_id, self._zone_id)


class OrionZoneTargetTempSensor(_OrionZoneTempSensor):
    """Target temperature for one zone, from `zones[].temp`.

    This is the LIVE setpoint, not the scheduled one. The
    `today_sleep_schedule.*_temp` sensors report schedule intent, which
    diverges from this the moment anyone nudges a zone by hand.
    """

    _suffix = "target_temp"
    _label = "Target Temperature"
    _attr_icon = "mdi:thermometer-check"

    def _read(self) -> float | None:
        return self.coordinator.zone_setpoint(self._device_id, self._zone_id)


class OrionCoolingEndsSensor(OrionBaseEntity, SensorEntity):
    """When rapid cooling ends on one side.

    A TIMESTAMP sensor rather than an attribute so Home Assistant renders
    it as a live countdown ("in 24 minutes") that ticks on its own. The
    same value sits on the Rapid Cool switch as `ends_at`, but an
    attribute is static text.

    Reads `zones[].thermal_relief.end_time`, a Unix millisecond stamp.
    Returns None whenever cooling is not running, which is also how a
    stale window that the server never cleared reads, because
    `thermal_relief_until` only returns a time that is still in the
    future.

    No `state_class`: Home Assistant rejects one on a non-numeric sensor.
    """

    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_icon = "mdi:timer-sand"

    def __init__(
        self,
        coordinator: OrionDataUpdateCoordinator,
        device_id: str,
        zone_id: str,
    ) -> None:
        super().__init__(coordinator, device_id)
        self._zone_id = zone_id
        self._attr_unique_id = f"{device_id}_{zone_id}_cooling_ends"
        self._attr_name = (
            f"{coordinator.zone_label(device_id, zone_id)} Cooling Ends"
        )

    @property
    def native_value(self):
        """Cooling end time, or None when nothing is running."""
        return self.coordinator.thermal_relief_until(self._device_id, self._zone_id)


class OrionLedBrightnessSensor(OrionBaseEntity, SensorEntity):
    """Control Tower LED brightness (0-100), read side.

    The WRITE side lives on `number.<device>_led_brightness`
    (`PUT /v1/devices/{serial}/live` with `{"led_brightness": int}`,
    measured 2026-07-26). This sensor is kept alongside it deliberately:
    a `number` entity produces no long-term statistics, so without this
    the history would be lost.

    `led_color` {r,g,b} is referenced in the app but absent from the
    documented live payload, so no `light` entity is modelled.
    """

    _attr_name = "LED Brightness"
    _attr_icon = "mdi:brightness-6"
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self,
        coordinator: OrionDataUpdateCoordinator,
        device_id: str,
    ) -> None:
        super().__init__(coordinator, device_id)
        self._attr_unique_id = f"{device_id}_led_brightness_state"

    @property
    def available(self) -> bool:
        return (
            super().available
            and self.coordinator.device_led_brightness(self._device_id) is not None
        )

    @property
    def native_value(self) -> int | None:
        return self.coordinator.device_led_brightness(self._device_id)


class OrionFirmwareSensor(OrionBaseEntity, SensorEntity):
    """Control board firmware with interface and topper details."""

    _attr_translation_key = "firmware_version"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:chip"

    def __init__(self, coordinator: OrionDataUpdateCoordinator, device_id: str) -> None:
        super().__init__(coordinator, device_id)
        self._attr_unique_id = f"{device_id}_firmware_version"

    @property
    def native_value(self) -> str | None:
        firmware = self.coordinator.firmware(self._device_id)
        return str(firmware["cb"]) if firmware and firmware.get("cb") else None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        attrs: dict[str, Any] = {}
        firmware = self.coordinator.firmware(self._device_id)
        if firmware and firmware.get("ib") is not None:
            attrs["interface_board"] = firmware["ib"]
        for sensor_name in _TOPPER_SENSORS:
            block = self.coordinator._sensor_block(self._device_id, sensor_name)
            if not block:
                continue
            if block.get("firmware_version") is not None:
                attrs[f"{sensor_name}_firmware"] = block["firmware_version"]
            if block.get("hardware_version") is not None:
                attrs[f"{sensor_name}_hardware"] = block["hardware_version"]
        return attrs or None


class OrionWifiSignalSensor(OrionBaseEntity, SensorEntity):
    """Control Tower Wi-Fi signal and connection details."""

    _attr_translation_key = "wifi_signal"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_device_class = SensorDeviceClass.SIGNAL_STRENGTH
    _attr_native_unit_of_measurement = "dBm"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator: OrionDataUpdateCoordinator, device_id: str) -> None:
        super().__init__(coordinator, device_id)
        self._attr_unique_id = f"{device_id}_wifi_signal"

    @property
    def native_value(self) -> int | None:
        return self.coordinator.wifi_rssi(self._device_id)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        network = self.coordinator.network_info(self._device_id)
        if not network:
            return None
        attrs = {
            "ssid": network.get("name"),
            "ip": network.get("ip"),
            "mac": network.get("mac"),
            "uptime": network.get("uptime"),
            "last_seen": network.get("last_seen"),
        }
        return {key: value for key, value in attrs.items() if value is not None} or None


class OrionAccessSensor(OrionBaseEntity, SensorEntity):
    """Who can use this bed, and in what capacity.

    An Orion bed is shared. One account owns it, a partner is a member,
    and a guest gets their own sleep insights without control of the
    device. None of that was visible from Home Assistant before.

    The state is the number of people with access. The useful part is the
    `people` attribute, which carries a name, a role, and the Orion user
    id for each. The id is there because revoking access needs it and
    there is no other way to obtain one without going back to the API.

    Profile image URLs are dropped on the way through. They add nothing
    here and they are the sort of thing that ends up in a screenshot.
    """

    _attr_icon = "mdi:account-group"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator, device_id: str) -> None:
        super().__init__(coordinator, device_id)
        self._attr_unique_id = f"{device_id}_access"
        self._attr_name = "Bed Access"

    def _people(self) -> list[dict]:
        """Access records, with names routed through the alias layer.

        `summarize_access` returns the vendor's own name because util has
        no idea what the user renamed anybody to. Every other entity in
        this integration shows the alias, so this one does too, and a
        a household that renamed the vendor's name to something they
        actually use does not get one sensor still using the old one.
        """
        people = util.summarize_access(self.coordinator.devices, self._device_id)
        for person in people:
            alias = self.coordinator.display_name_for_user(person["user_id"])
            if alias:
                person["name"] = alias
        return people

    @property
    def native_value(self) -> int:
        return len(self._people())

    @property
    def extra_state_attributes(self) -> dict:
        people = self._people()
        return {
            "people": people,
            # Built from the normalised records rather than raw API
            # values. A set comprehension over unvalidated data is how
            # this sensor crashed on its first run: `access` turned out
            # to be an object, and a dict cannot go in a set.
            "roles": sorted({str(p["role"]) for p in people}),
        }

    # ── Services ──────────────────────────────────────────────────────
    #
    # Every one of these acts on the account that owns the device, so
    # they all go through the primary client. A partner's token cannot
    # manage access it was granted.

    async def async_list_invites(self) -> dict:
        """Pending invitations, as a service response."""
        try:
            body = await self.coordinator.api_client.list_device_invites()
        except OrionApiError as err:
            raise HomeAssistantError(f"Orion could not list invites: {err}") from err
        response = body.get("response") if isinstance(body, dict) else None
        invites = response.get("invites") if isinstance(response, dict) else None
        return {"invites": invites if isinstance(invites, list) else []}

    async def async_invite_user(self, phone_number: str, role: str) -> None:
        """Invite somebody to this bed by phone number."""
        try:
            await self.coordinator.api_client.invite_user(
                [self._device_id], phone_number, role
            )
        except ValueError as err:
            raise HomeAssistantError(str(err)) from err
        except OrionApiError as err:
            raise HomeAssistantError(f"Orion rejected the invite: {err}") from err
        _LOGGER.info("Sent an Orion %s invite for device %s", role, self._device_id)
        await self.coordinator.async_request_refresh()

    async def async_cancel_invite(self, invite_id: str) -> None:
        """Withdraw an invitation that has not been accepted."""
        try:
            await self.coordinator.api_client.cancel_device_invite(invite_id)
        except ValueError as err:
            raise HomeAssistantError(str(err)) from err
        except OrionApiError as err:
            raise HomeAssistantError(f"Orion could not cancel the invite: {err}") from err

    async def async_accept_invite(self, code: str) -> None:
        """Redeem an invite code for the account this integration uses.

        This is the only call here that acts on the receiving side. It
        adds *this* account to somebody else's bed, so nothing local
        changes until the next poll picks the new device up.
        """
        try:
            await self.coordinator.api_client.accept_device_invite(code)
        except ValueError as err:
            raise HomeAssistantError(str(err)) from err
        except OrionApiError as err:
            raise HomeAssistantError(f"Orion rejected the invite code: {err}") from err
        await self.coordinator.async_request_refresh()

    async def async_remove_user_access(self, user_id: str, confirm: bool) -> None:
        """Revoke somebody's access to this bed.

        Not reversible from here. Getting them back means a fresh invite
        that they have to accept, which is why this asks for confirmation
        and refuses an id that is not currently on the bed.
        """
        if not confirm:
            raise HomeAssistantError("Set confirm to true to revoke access")
        known = {p["user_id"] for p in self._people()}
        if user_id not in known:
            raise HomeAssistantError(
                f"{user_id} does not currently have access to this bed. "
                "Check the people attribute on this sensor."
            )
        try:
            await self.coordinator.api_client.remove_user_access(user_id)
        except ValueError as err:
            raise HomeAssistantError(str(err)) from err
        except OrionApiError as err:
            raise HomeAssistantError(f"Orion could not revoke access: {err}") from err
        _LOGGER.warning("Revoked Orion access for user %s", user_id)
        await self.coordinator.async_request_refresh()

    async def async_create_guest(self) -> None:
        """Add an unattached guest slot to this bed.

        Different from inviting a guest by phone. This is what the app
        does before it knows who is coming: it creates the guest, and a
        number gets attached afterwards with update_user_phone.
        """
        try:
            await self.coordinator.api_client.create_guest(self._device_id)
        except ValueError as err:
            raise HomeAssistantError(str(err)) from err
        except OrionApiError as err:
            raise HomeAssistantError(f"Orion could not add a guest: {err}") from err
        await self.coordinator.async_request_refresh()

    async def async_update_user_phone(self, user_id: str, phone: str) -> None:
        """Attach or change a phone number for someone on this bed."""
        try:
            await self.coordinator.api_client.update_user_phone(user_id, phone)
        except ValueError as err:
            raise HomeAssistantError(str(err)) from err
        except OrionApiError as err:
            raise HomeAssistantError(
                f"Orion could not update that phone number: {err}"
            ) from err
        await self.coordinator.async_request_refresh()
