"""Sensor platform for Orion Sleep."""

from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import Any

import voluptuous as vol
from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
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
from orion_sleep_api import util

from . import helpers
from .coordinator import OrionDataUpdateCoordinator
from .descriptions import (
    _SCHEDULE_LABELS,
    INSIGHT_SENSOR_DESCRIPTIONS,
    SCHEDULE_SENSOR_DESCRIPTIONS,
    OrionSensorEntityDescription,
    _get_day_field,
    _get_partner_day_field,
    _insight_label,
)
from .entity import OrionBaseEntity
from .errors import orion_call

# The description tuples, the dataclass that types them, and the pure
# readers behind every `value_fn` now live in `descriptions.py`. They were
# moved out because `migrations.py` derives its unique_id renames from
# `INSIGHT_SENSOR_DESCRIPTIONS` and had to reach into this platform module
# to get them, which was a layering inversion papered over with two
# function-scoped imports, and which made the FIRST import of this
# 2000-line module happen inside the event loop on a cold start.
# `descriptions.py` explains the whole thing.
#
# They are re-imported here at module scope rather than referenced through
# the module, so `sensor.INSIGHT_SENSOR_DESCRIPTIONS` still resolves for
# anything that reads it off this module.

# Topper sensors exposed on every WS payload. Mapping to zone_a/zone_b
# isn't verified yet, so entities are named per sensor.
_TOPPER_SENSORS: tuple[str, ...] = ("sensor1", "sensor2")

_LOGGER = logging.getLogger(__name__)

SERVICE_LIST_SLEEP_SESSIONS = "list_sleep_sessions"
SERVICE_DELETE_SLEEP_SESSION = "delete_sleep_session"
SERVICE_CONFIRM_SLEEP_SESSION = "confirm_sleep_session"
SERVICE_EDIT_SLEEP_SESSION = "edit_sleep_session"
SERVICE_END_SLEEP_SESSION = "end_sleep_session"
SERVICE_LIST_ACCESS = "list_access"
SERVICE_LIST_INVITES = "list_invites"
SERVICE_INVITE_USER = "invite_user"
SERVICE_CANCEL_INVITE = "cancel_invite"
SERVICE_ACCEPT_INVITE = "accept_invite"
SERVICE_REMOVE_USER_ACCESS = "remove_user_access"
SERVICE_CREATE_GUEST = "create_guest"
SERVICE_UPDATE_USER_PHONE = "update_user_phone"
SERVICE_ASSIGN_ZONES = "assign_zones"
SERVICE_SET_DEVICE_NAME = "set_device_name"
SERVICE_SET_DEVICE_TIMEZONE = "set_device_timezone"

# ── Setup ─────────────────────────────────────────────────────────────────


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Orion Sleep sensor entities."""
    coordinator: OrionDataUpdateCoordinator = entry.runtime_data
    entities: list[SensorEntity] = []

    # Account-scoped, so built ONCE rather than once per bed.
    #
    # `/v2/insights` takes no device and `/v1/sleep-schedules` is keyed on
    # user id alone, so every one of these reads the same account-wide
    # response no matter which bed it is attached to. Building them inside
    # the loop below gave a two-bed household two sleep scores, two HRVs
    # and two of every schedule control, all reflecting one value, and it
    # recorded a night slept in one bed against both of them.
    #
    # They hang off `account_device_id` so they have somewhere to live in
    # the registry. That is presentation only and never reaches their
    # unique_id.
    account_device_id = coordinator.account_device_id()
    if account_device_id:
        for description in INSIGHT_SENSOR_DESCRIPTIONS:
            entities.append(
                OrionSensorEntity(coordinator, account_device_id, description)
            )
        for user_id in coordinator.schedule_user_ids():
            for description in SCHEDULE_SENSOR_DESCRIPTIONS:
                entities.append(
                    OrionScheduleSensorEntity(
                        coordinator, account_device_id, description, user_id
                    )
                )
        entities.append(OrionSchedulePhaseSensor(coordinator, account_device_id))
        entities.append(OrionZoneSplitModeSensor(coordinator, account_device_id))

        # v3 / Orion Intelligence analytics. Account-scoped like the other
        # insight sensors: day metrics as their own sensors, week and month
        # as one score sensor each with the metric breakdown as attributes.
        entities.append(OrionConsistencySensor(coordinator, account_device_id))
        entities.append(OrionSleepDebtSensor(coordinator, account_device_id))
        entities.append(
            OrionBreathingDisturbancesSensor(coordinator, account_device_id)
        )
        entities.append(OrionWeeklyScoreSensor(coordinator, account_device_id))
        entities.append(OrionMonthlyScoreSensor(coordinator, account_device_id))
        entities.append(
            OrionTemperatureRecommendationsSensor(coordinator, account_device_id)
        )
        # CONFIGURED, not verified. This gate decides whether the partner's
        # entities EXIST, which is a fact about how this entry was set up,
        # not about whether an HTTP request succeeded thirty seconds ago.
        #
        # It used to be `has_partner_for_device`, the trust predicate, and
        # that was the bug. A single failed partner fetch at cold start left
        # `partner_user` empty and `partner_mapping_valid` False, so this
        # built nothing and the partner's sleep score, heart rate, HRV and
        # apnea sensors did not exist in Home Assistant at all. Not
        # unavailable. Absent. Every card and automation referencing them
        # broke, and nothing in the log tied that to one dropped connection.
        # There was no recovery short of reloading the entry by hand.
        #
        # Trust has not been weakened, it has been moved to where it can be
        # revisited. `OrionPartnerInsightSensor.available` still requires
        # `has_partner_for_device`, so an unverified partner's entities
        # exist and report `unavailable`, and they go available on their own
        # the moment a later poll verifies the partner. `available` is a
        # property evaluated per state write, so that needs no reload.
        if coordinator.has_partner_configured_for_device(account_device_id):
            for description in INSIGHT_SENSOR_DESCRIPTIONS:
                entities.append(
                    OrionPartnerInsightSensor(
                        coordinator, account_device_id, description
                    )
                )
            # Partner v3 analytics, from the partner's own /v3/insights.
            entities.append(
                OrionConsistencySensor(
                    coordinator, account_device_id, is_partner=True
                )
            )
            entities.append(
                OrionSleepDebtSensor(coordinator, account_device_id, is_partner=True)
            )
            entities.append(
                OrionBreathingDisturbancesSensor(
                    coordinator, account_device_id, is_partner=True
                )
            )
            entities.append(
                OrionWeeklyScoreSensor(
                    coordinator, account_device_id, is_partner=True
                )
            )
            entities.append(
                OrionMonthlyScoreSensor(
                    coordinator, account_device_id, is_partner=True
                )
            )
            entities.append(
                OrionTemperatureRecommendationsSensor(
                    coordinator, account_device_id, is_partner=True
                )
            )

    for device in coordinator.devices:
        device_id = device.get("id")
        if not device_id:
            continue
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

    async_add_entities(entities)

    # Both service groups are registered on this platform because sleep
    # sessions belong to an account, and the insight sensors are the only
    # entities that know which account they speak for. Targeting one of a
    # person's insight sensors is how the caller says whose session they
    # mean, without ever handling a raw Orion user id.
    #
    # Every registration below goes through `helpers.async_register_entity_service`
    # and must state `admin=`. Home Assistant only enforces per-entity
    # POLICY_CONTROL for an entity service and the built-in Users group
    # controls every entity, so `admin=False` means "any household member
    # of this HA instance may call this". Nothing here is gated by
    # accident and nothing is left open by accident either.
    platform = async_get_current_platform()

    # ── Sleep sessions: admin only ────────────────────────────────────
    #
    # Reads as well as writes. `list_sleep_sessions` hands over session
    # ids and per-night timestamps, and the reason those are a service
    # response rather than an entity attribute is that attributes go to
    # the recorder and into every backup. "Too sensitive to keep" and
    # "any household member may fetch on demand" cannot both be true of
    # the same data. The timestamps on their own are an occupancy log,
    # which `_SENSITIVE_DIAGNOSTIC_BRANCHES` already strips from a
    # diagnostics download for exactly that reason.
    helpers.async_register_entity_service(
        platform,
        SERVICE_LIST_SLEEP_SESSIONS,
        {vol.Optional("limit", default=30): vol.All(int, vol.Range(min=1, max=200))},
        "async_list_sleep_sessions",
        admin=True,
        supports_response=SupportsResponse.ONLY,
    )
    helpers.async_register_entity_service(
        platform,
        SERVICE_DELETE_SLEEP_SESSION,
        {
            vol.Required("session_id"): cv.string,
            vol.Required("reason"): vol.In(sorted(util.SESSION_DELETE_REASONS)),
            vol.Required("confirm"): vol.All(cv.boolean, vol.Equal(True)),
        },
        "async_delete_sleep_session",
        admin=True,
    )
    # Not a relabel. The server recomputes stages, heart rate, breathing
    # and apnea from the new window, so the original night is gone.
    helpers.async_register_entity_service(
        platform,
        SERVICE_EDIT_SLEEP_SESSION,
        {
            vol.Required("session_id"): cv.string,
            vol.Required("fell_asleep"): cv.datetime,
            vol.Required("woke_up"): cv.datetime,
        },
        "async_edit_sleep_session",
        admin=True,
    )
    # `claim: both` writes a named person into a session they may not
    # have slept, which is somebody else's record being edited.
    helpers.async_register_entity_service(
        platform,
        SERVICE_CONFIRM_SLEEP_SESSION,
        {
            vol.Required("session_id"): cv.string,
            vol.Optional("claim", default="me"): vol.In(["me", "both"]),
        },
        "async_confirm_sleep_session",
        admin=True,
    )
    helpers.async_register_entity_service(
        platform,
        SERVICE_END_SLEEP_SESSION,
        {vol.Required("confirm"): cv.boolean},
        "async_end_sleep_session",
        admin=True,
    )

    # ── Bed access and device settings: admin only ────────────────────
    #
    # Access management is device-scoped rather than person-scoped, so it
    # targets the Bed Access sensor. The device owns the guest list; a
    # person's insight sensor has nothing to say about who else can use
    # the bed.
    #
    # `list_access` and `list_invites` are gated alongside the writes
    # rather than left open, because they are the reconnaissance step for
    # them. `list_access` returns the raw Orion `user_id` for every
    # household member, and those ids are the exact required input to
    # `remove_user_access`, `update_user_phone` and `assign_zones`.
    # `list_invites` is keyed on phone numbers, which for this vendor is
    # where login codes are delivered. Redacting them instead was the
    # alternative and it does not work: `list_access` minus the ids is
    # just the `people` attribute this sensor already publishes, and
    # `list_invites` minus the numbers is a count.
    helpers.async_register_entity_service(
        platform,
        SERVICE_LIST_ACCESS,
        {},
        "async_list_access",
        admin=True,
        supports_response=SupportsResponse.ONLY,
    )
    helpers.async_register_entity_service(
        platform,
        SERVICE_LIST_INVITES,
        {},
        "async_list_invites",
        admin=True,
        supports_response=SupportsResponse.ONLY,
    )
    helpers.async_register_entity_service(
        platform,
        SERVICE_INVITE_USER,
        {
            vol.Required("phone_number"): cv.string,
            vol.Required("role"): vol.In(sorted(util.INVITE_ROLES)),
        },
        "async_invite_user",
        admin=True,
    )
    helpers.async_register_entity_service(
        platform,
        SERVICE_CANCEL_INVITE,
        {vol.Required("invite_id"): cv.string},
        "async_cancel_invite",
        admin=True,
    )
    helpers.async_register_entity_service(
        platform,
        SERVICE_ACCEPT_INVITE,
        {vol.Required("code"): cv.string},
        "async_accept_invite",
        admin=True,
    )
    helpers.async_register_entity_service(
        platform,
        SERVICE_REMOVE_USER_ACCESS,
        {
            vol.Required("user_id"): cv.string,
            vol.Required("confirm"): vol.All(cv.boolean, vol.Equal(True)),
        },
        "async_remove_user_access",
        admin=True,
    )
    helpers.async_register_entity_service(
        platform,
        SERVICE_CREATE_GUEST,
        {},
        "async_create_guest",
        admin=True,
    )
    helpers.async_register_entity_service(
        platform,
        SERVICE_UPDATE_USER_PHONE,
        {
            vol.Required("user_id"): cv.string,
            vol.Required("phone"): cv.string,
        },
        "async_update_user_phone",
        admin=True,
    )
    helpers.async_register_entity_service(
        platform,
        SERVICE_ASSIGN_ZONES,
        {
            vol.Required("user_id"): cv.string,
            vol.Required("zone_ids"): vol.All(cv.ensure_list, [cv.string]),
            vol.Required("confirm"): vol.All(cv.boolean, vol.Equal(True)),
        },
        "async_assign_zones",
        admin=True,
    )
    # Cosmetic on the vendor's side and nothing here reads it, but it
    # still writes to the shared account, so it sits with the writes
    # rather than becoming the one ungated exception somebody has to
    # remember the reason for.
    helpers.async_register_entity_service(
        platform,
        SERVICE_SET_DEVICE_NAME,
        {vol.Required("name"): cv.string},
        "async_set_device_name",
        admin=True,
    )
    # The bed derives which weekday it is from this, so a wrong value
    # moves everyone's bedtime rather than relabelling it.
    helpers.async_register_entity_service(
        platform,
        SERVICE_SET_DEVICE_TIMEZONE,
        {vol.Required("timezone"): cv.string},
        "async_set_device_timezone",
        admin=True,
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
        self._attr_unique_id = helpers.account_person_unique_id(
            coordinator.config_entry.entry_id,
            description.key,
            coordinator.user_id,
            legacy=f"{device_id}_{description.key}",
        )
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
        return helpers.nested_mapping(self.coordinator.data, "insights", "data")

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
        selected = next(
            (
                row
                for row in util.summarize_sessions(self._sessions_insights(), 200)
                if row["session_id"] == session_id
            ),
            None,
        )
        if selected is None:
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
        async with orion_call("edit that session"):
            await client.edit_sleep_session(session_id, asleep_at, awake_at)
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
        async with orion_call("end that session"):
            await self._sessions_client().end_sleep_session()
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
        selected = next(
            (
                row
                for row in util.summarize_sessions(self._sessions_insights(), 200)
                if row["session_id"] == session_id
            ),
            None,
        )
        if selected is None:
            raise HomeAssistantError(
                f"No session {session_id} belongs to {self._sessions_owner()}. "
                "Run orion_sleep.list_sleep_sessions against this entity first."
            )
        if selected.get("needs_confirmation") is not True:
            raise HomeAssistantError(
                "Orion is not asking for manual ownership confirmation on "
                "that session"
            )

        own_id = self._sessions_user_id()
        if not own_id:
            raise HomeAssistantError(
                f"No Orion user id resolved for {self._sessions_owner()}"
            )

        user_ids = [own_id]
        if claim == "both":
            # `has_partner_for_device` already returns False unless
            # `partner_mapping_valid`, so re-checking that flag here said
            # nothing and read as though it did. The cost of leaving it
            # was not a wrong answer, it was that the next partner-gated
            # entity copies this belt-and-braces shape and the two
            # eventually disagree about which one is authoritative.
            #
            # `partner_update_ok` stays. That is a genuinely separate
            # axis: it is about whether the partner's tokens can be
            # WRITTEN with right now, where the other is about whether
            # this partner belongs to this bed at all.
            if (
                not self.coordinator.partner_update_ok
                or not self.coordinator.has_partner_for_device(self._device_id)
            ):
                raise HomeAssistantError(
                    "Cannot claim a session for both sleepers until the linked "
                    "partner is verified for this bed"
                )
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

        async with orion_call("confirm that session"):
            await client.confirm_sleep_session(session_id, user_ids)

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
        async with orion_call("delete that session"):
            await client.delete_sleep_session(session_id, reason)
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
        # `partner_entity_key_id` rather than `partner_user["id"]`, because
        # this entity is now built on runs where no partner fetch has
        # succeeded and `partner_user` is therefore empty. An empty id makes
        # `person_unique_id` return the 2.x role-keyed legacy string, the
        # registry keeps that id forever, and the next boot that reaches the
        # server mints the account-keyed id as a SECOND entity. The
        # household ends up with two of everything and their history split
        # across the pair. See `partner_entity_key_id` for why the recorded
        # id is the right durable answer and why it is preferred over the
        # fetched one.
        self._attr_unique_id = helpers.account_person_unique_id(
            coordinator.config_entry.entry_id,
            description.key,
            coordinator.partner_entity_key_id(),
            legacy=f"{device_id}_partner_{description.key}",
        )
        self._insight_label = _insight_label(description.key)

    @property
    def name(self) -> str:
        """The partner's display name, recomputed rather than frozen.

        A property instead of the `_attr_name` the parent sets, because
        this entity is now constructed on runs where no partner fetch has
        succeeded. `coordinator.partner_name()` returns "Partner" there,
        and freezing that at construction would leave the friendly name
        wrong until somebody reloaded the entry, even after a later poll
        had verified the partner and learned their real name.

        This fixes the FRIENDLY NAME only. Home Assistant slugifies the
        first name an entity ever registers with into its entity_id and
        never revisits it, so an entity born on a failed-fetch run keeps
        `sensor.partner_...` permanently. That is a real cost and it is
        accepted deliberately: an entity with a plain name is recoverable
        through the alias options flow, and an entity that does not exist
        is not recoverable at all. `partner_name` already reads the
        household's alias for the recorded partner id first, so anyone who
        has named this person is unaffected.
        """
        return f"{self.coordinator.partner_name()} {self._insight_label}"

    def _session(self) -> dict | None:
        if self.entity_description.completed_only:
            return self.coordinator.get_latest_completed_partner_session(self._device_id)
        return self.coordinator.get_latest_partner_session(self._device_id)

    def _day_value(self, field: str) -> Any:
        return _get_partner_day_field(self.coordinator.data or {}, field)

    def _sessions_insights(self) -> dict:
        return helpers.nested_mapping(self.coordinator.data, "partner_insights", "data")

    def _sessions_client(self):
        return self.coordinator.partner_api_client

    def _sessions_owner(self) -> str:
        return self.coordinator.partner_name()

    def _sessions_user_id(self) -> str | None:
        partner_id = (self.coordinator.partner_user or {}).get("id")
        return partner_id if isinstance(partner_id, str) and partner_id else None

    @property
    def available(self) -> bool:
        # No `partner_mapping_valid` conjunct. `has_partner_for_device`
        # already folds it in, and stating it twice invites the two to
        # drift apart. `partner_update_ok` is a different question and
        # stays.
        return (
            super().available
            and self.coordinator.has_partner_for_device(self._device_id)
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
        self._attr_unique_id = helpers.account_schedule_unique_id(
            coordinator.config_entry.entry_id, description.key, user_id
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
        # Through session_subsection like every other session read in this
        # file. A bare .get raises AttributeError inside a property the
        # moment the vendor sends a list here, which is the exact bug that
        # helper exists to stop.
        values = util.session_subsection(session, "temperature").get("values", [])
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
        return {"seconds_since_last_message": round(time.monotonic() - last_at, 1)}

    @property
    def available(self) -> bool:
        # Always show the state — that's the whole point of this sensor.
        return True


class _OrionLiveSensorBase(OrionBaseEntity, SensorEntity):
    """Shared plumbing for per-topper-sensor live entities."""

    # Fed by the live-device stream, so a fresh socket keeps it available
    # even while a polled endpoint is failing. Load-bearing: `available`
    # below chains up to `OrionBaseEntity.available`, which is the only
    # thing that reads this. An earlier version of that override dropped
    # the `super()` call, which made this line dead and the comment above
    # it a description of behaviour the class did not have.
    _live_fed = True

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
        """A live frame is necessary but not sufficient. It must be a fresh one.

        Having seen any frame ever is not a freshness test.
        `coordinator.live_devices` is only replaced inside
        `_async_update_data`, and every early raise in that method leaves
        it exactly as it was: an auth failure on a permanently invalid
        refresh token, or the ownership `UpdateFailed`. Without the
        `super()` call this property reported the last frame received as
        current forever, with no upper bound on how old it was. That is
        live heart rate and breath rate, so a stale reading is worse than
        no reading.

        `super().available` is `OrionBaseEntity.available`, which honours
        `_live_fed` through `push_is_fresh`, so a genuinely live socket
        still keeps these up while a polled endpoint is failing. That is
        the behaviour `_OrionZoneTempSensor` has always had, for entities
        fed by the same socket.
        """
        return (
            super().available
            and self.coordinator.sensor_status_text(self._device_id, self._sensor_name)
            is not None
        )

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

    # Fed by the live-device stream, so a fresh socket keeps it
    # available even while a polled endpoint is failing.
    _live_fed = True

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

    # Fed by the live-device stream, so a fresh socket keeps it
    # available even while a polled endpoint is failing.
    _live_fed = True

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

    # Fed by the live-device stream, so a fresh socket keeps it
    # available even while a polled endpoint is failing.
    _live_fed = True

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

    def _access_entries(self) -> list[dict]:
        """Unrecorded access rows used to validate destructive services."""
        return util.access_entries(self.coordinator.devices, self._device_id)

    def _people(self) -> list[dict]:
        """Who has access, named the way this household names people.

        The library returns facts and no display name, deliberately. It
        cannot know what anyone here is called. Naming is this
        integration's job, so it happens here: the alias if one is set,
        otherwise whatever the vendor has on the account.

        The vendor's raw user record arrives under `user` and is dropped
        on the way out. It carries a profile image URL and a full legal
        name, and this ends up in an entity attribute.
        """
        people = []
        for entry in self._access_entries():
            user_id = entry["user_id"]
            people.append(
                {
                    "name": (
                        self.coordinator.display_name_for_user(user_id)
                        # NOT orion_user_label: its fallback chain ends at
                        # email and then phone, so an account with no name
                        # set would put a login credential into a recorded
                        # attribute. This sensor already dropped the raw
                        # user record for carrying a legal name and a photo
                        # url, and that chain reintroduces the same thing.
                        or "User " + helpers.short_id(user_id)
                    ),
                    "role": entry["role"],
                    "away": entry["away"],
                    "expires": entry["expires"],
                }
            )
        return people

    @property
    def native_value(self) -> int:
        return len(self._people())

    @property
    def extra_state_attributes(self) -> dict:
        people = self._people()
        return {
            # `people` carries display names and roles. It deliberately
            # does NOT carry raw Orion user ids: attributes are recorded
            # and land in backups, and a household roster is not worth
            # keeping forever to satisfy a service call that already
            # takes the id as a parameter.
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

    async def async_list_access(self) -> dict:
        """Who has access, with their Orion user ids, as a response.

        The ids are here and not in `extra_state_attributes` for the same
        reason session ids are not: attributes are written to the recorder
        and to every backup, so putting a household roster there keeps it
        forever to serve a lookup done once. `remove_user_access`,
        `update_user_phone` and `assign_zones` all take a user id, and this
        is where you get it.
        """
        return {
            "people": [
                {
                    "name": self.coordinator.display_name_for_user(entry["user_id"]),
                    "user_id": entry["user_id"],
                    "role": entry["role"],
                    "away": entry["away"],
                    "expires": entry["expires"],
                }
                for entry in util.access_entries(
                    self.coordinator.devices, self._device_id
                )
            ]
        }

    async def async_list_invites(self) -> dict:
        """Pending invitations, as a service response."""
        async with orion_call("list the pending invites"):
            body = await self.coordinator.api_client.list_device_invites()
        response = body.get("response") if isinstance(body, dict) else None
        invites = response.get("invites") if isinstance(response, dict) else None
        return {"invites": invites if isinstance(invites, list) else []}

    async def async_invite_user(self, phone_number: str, role: str) -> None:
        """Invite somebody to this bed by phone number."""
        async with orion_call("send that invite"):
            await self.coordinator.api_client.invite_user(
                [self._device_id], phone_number, role
            )
        _LOGGER.info(
            "Sent an Orion %s invite for device %s",
            role,
            helpers.short_id(self._device_id),
        )
        await self.coordinator.async_request_refresh()

    async def async_cancel_invite(self, invite_id: str) -> None:
        """Withdraw an invitation that has not been accepted."""
        async with orion_call("cancel that invite"):
            await self.coordinator.api_client.cancel_device_invite(invite_id)

    async def async_accept_invite(self, code: str) -> None:
        """Redeem an invite code for the account this integration uses.

        This is the only call here that acts on the receiving side. It
        adds *this* account to somebody else's bed, so nothing local
        changes until the next poll picks the new device up.
        """
        async with orion_call("redeem that invite code"):
            await self.coordinator.api_client.accept_device_invite(code)
        await self.coordinator.async_request_refresh()

    async def async_remove_user_access(self, user_id: str, confirm: bool) -> None:
        """Revoke somebody's access to this bed.

        Not reversible from here. Getting them back means a fresh invite
        that they have to accept, which is why this asks for confirmation
        and refuses an id that is not currently on the bed.
        """
        if not confirm:
            raise HomeAssistantError("Set confirm to true to revoke access")
        known = {entry["user_id"] for entry in self._access_entries()}
        if user_id not in known:
            raise HomeAssistantError(
                f"{user_id} does not currently have access to this bed. "
                "Check the people attribute on this sensor."
            )
        async with orion_call("revoke that person's access"):
            await self.coordinator.api_client.remove_user_access(user_id)
        _LOGGER.warning(
            "Revoked Orion access for user %s", helpers.short_id(user_id)
        )
        await self.coordinator.async_request_refresh()

    async def async_create_guest(self) -> None:
        """Add an unattached guest slot to this bed.

        Different from inviting a guest by phone. This is what the app
        does before it knows who is coming: it creates the guest, and a
        number gets attached afterwards with update_user_phone.
        """
        async with orion_call("add a guest"):
            await self.coordinator.api_client.create_guest(self._device_id)
        await self.coordinator.async_request_refresh()

    async def async_update_user_phone(self, user_id: str, phone: str) -> None:
        """Attach or change a phone number for someone on this bed."""
        async with orion_call("update that phone number"):
            await self.coordinator.api_client.update_user_phone(user_id, phone)
        await self.coordinator.async_request_refresh()


    async def async_assign_zones(
        self, user_id: str, zone_ids: list[str], confirm: bool
    ) -> None:
        """Put somebody on one or more zones of this bed.

        The app calls this "Replace {name}". It is how a spare-room bed
        gets handed to whoever is staying without anyone opening the
        phone app.

        Moving somebody onto an occupied zone displaces whoever was
        there. That is deliberate on Orion's side and is what the
        `push_away_behavior` the app always sends governs.
        """
        if not confirm:
            raise HomeAssistantError(
                "Set confirm to true to replace the current zone assignment"
            )
        known = {entry["user_id"] for entry in self._access_entries()}
        primary = self.coordinator.user_id
        if primary:
            known.add(primary)
        if user_id not in known:
            raise HomeAssistantError(
                f"{user_id} is not on this bed. Invite them first, or check "
                "the people attribute on this sensor."
            )
        async with orion_call("assign those zones"):
            await self.coordinator.api_client.assign_zones(
                user_id, self._device_id, zone_ids
            )
        _LOGGER.info(
            "Assigned Orion zones %s to user %s",
            zone_ids,
            helpers.short_id(user_id),
        )
        await self.coordinator.async_request_refresh()

    async def async_set_device_name(self, name: str) -> None:
        """Rename the bed.

        Cosmetic on Orion's side. Home Assistant keeps its own device
        name, so this changes what the phone app shows and nothing here.
        """
        async with orion_call("rename the bed"):
            await self.coordinator.api_client.set_device_name(self._device_id, name)
        await self.coordinator.async_request_refresh()

    async def async_set_device_timezone(self, timezone: str) -> None:
        """Set the bed's timezone.

        Not cosmetic. Schedules are stored per weekday and the bed works
        out which day it is from this, so a wrong value moves bedtime
        rather than just relabelling it.
        """
        async with orion_call("set the bed timezone"):
            await self.coordinator.api_client.set_device_timezone(
                self._device_id, timezone
            )
        _LOGGER.warning(
            "Changed the Orion bed timezone to %s. Schedules are stored per "
            "weekday, so check tonight's bedtime is still what you expect.",
            timezone,
        )
        await self.coordinator.async_request_refresh()


class OrionSchedulePhaseSensor(OrionBaseEntity, SensorEntity):
    """Which part of tonight's schedule the bed is currently in.

    From `/v1/sleep-session`, which carries `current_phase` plus a
    `phases` map of `{start, end}` Unix seconds per phase.

    This is the data the WebSocket `timeline` was supposed to provide.
    The server sends an empty timeline array even mid-schedule, which is
    why the sensor built on it was deleted. This route has the same
    information and actually returns it.

    Reads the authenticated account's schedule. Phases are per user, so a
    linked partner would need her own fetch.
    """

    _attr_icon = "mdi:timeline-clock-outline"

    def __init__(self, coordinator, device_id: str) -> None:
        super().__init__(coordinator, device_id)
        # Reads `live_session()`, which is the AUTHENTICATED user's
        # session. This is one person's sensor, not the bed's.
        self._attr_unique_id = helpers.account_person_unique_id(
            coordinator.config_entry.entry_id,
            "current_phase",
            coordinator.user_id,
            legacy=f"{device_id}_current_phase",
        )
        self._attr_name = f"{coordinator.primary_name()} Schedule Phase"

    @property
    def native_value(self) -> str | None:
        phase = self.coordinator.live_session().get("current_phase")
        if isinstance(phase, str) and phase:
            return phase.replace("_", " ").title()
        return None

    @property
    def extra_state_attributes(self) -> dict:
        """Phase windows as ISO timestamps.

        The API gives Unix seconds. Converting here rather than shipping
        raw epochs means the attribute is readable in the UI without a
        template, and a malformed value is dropped rather than rendered
        as 1970.
        """
        phases = self.coordinator.live_session().get("phases")
        if not isinstance(phases, dict):
            return {}
        out: dict = {}
        for name, window in phases.items():
            if not isinstance(window, dict):
                continue
            for edge in ("start", "end"):
                value = window.get(edge)
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    continue
                out[f"{name}_{edge}"] = dt_util.utc_from_timestamp(value).isoformat()
        return out


class OrionZoneSplitModeSensor(OrionBaseEntity, SensorEntity):
    """Whether the two halves of the bed are driven as one.

    `combined` means one set of controls covers the whole bed. `split`
    means each side runs independently, which is what the app's Split
    Zones action produces.

    Worth having on its own: the session payload carries `is_combined`
    and `combined_zone_ids`, which the Orion app never reads and which
    this integration therefore refuses to build on. This field is the
    same question answered by a route the app does use.
    """

    _attr_icon = "mdi:bed-king-outline"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator, device_id: str) -> None:
        super().__init__(coordinator, device_id)
        self._attr_unique_id = helpers.account_unique_id(
            coordinator.config_entry.entry_id, "zone_split_mode"
        )
        self._attr_name = "Zone Mode"

    @property
    def available(self) -> bool:
        return super().available and self.coordinator.zone_split_mode() is not None

    @property
    def native_value(self) -> str | None:
        mode = self.coordinator.zone_split_mode()
        return mode.title() if isinstance(mode, str) else None


# ── v3 insights: Orion Intelligence analytics ─────────────────────────
#
# The v3 surface is pre-aggregated by period. These entities read the
# LATEST period of a granularity from the coordinator, which returns None
# for any no-data / no-subscription / calibrating case. Every value_fn
# here therefore treats a missing metric or a null value as `unknown`,
# NEVER as 0, because zero sleep debt and no data are different facts.
#
# `key` selects the primary (`insights_v3`) or partner
# (`partner_insights_v3`) payload, so one class serves both by passing a
# different key and a `partner_`-prefixed translation key. The translation
# keys match Kevin Klaes's fork so a dashboard written against his
# entity_ids works against this one; the v3 surface and these metrics were
# found by him.


def _v3_comparison(metric: dict | None, granularity: str) -> dict | None:
    """The prior-period comparison for a metric, keyed by granularity."""
    if not isinstance(metric, dict):
        return None
    comparisons = metric.get("comparisons")
    if not isinstance(comparisons, dict):
        return None
    key = {
        "day": "vs_prior_day",
        "week": "vs_prior_week",
        "month": "vs_prior_month",
    }[granularity]
    value = comparisons.get(key)
    return value if isinstance(value, dict) else None


class _OrionV3MetricSensor(OrionBaseEntity, SensorEntity):
    """One v3 day-granularity metric as a sensor.

    Subclasses set `_metric`, `_translation_key` (and its partner form),
    the unit, and any extra attributes. The base handles the shared
    unknown-not-zero gating and the comparison/insight attributes.
    """

    _metric: str = ""
    _base_translation_key: str = ""
    _live_fed = False

    def __init__(
        self,
        coordinator: OrionDataUpdateCoordinator,
        device_id: str,
        *,
        is_partner: bool = False,
    ) -> None:
        super().__init__(coordinator, device_id)
        self._is_partner = is_partner
        self._data_key = "partner_insights_v3" if is_partner else "insights_v3"
        tk = self._base_translation_key
        self._attr_translation_key = f"partner_{tk}" if is_partner else tk
        # unique_id is account-scoped and person-keyed, exactly like the v2
        # insight sensors, so it is stable and never collides with them.
        who = (
            coordinator.partner_entity_key_id() if is_partner else coordinator.user_id
        )
        self._attr_unique_id = helpers.account_person_unique_id(
            coordinator.config_entry.entry_id,
            f"v3_{self._metric}",
            who,
            legacy=f"{device_id}_v3_{self._metric}"
            + ("_partner" if is_partner else ""),
        )

    def _metric_dict(self) -> dict | None:
        return self.coordinator.v3_metric(
            self._metric, granularity="day", key=self._data_key
        )

    @property
    def available(self) -> bool:
        # Available whenever the coordinator poll is healthy. A present-but-
        # null metric is reported as `unknown` through native_value, which
        # is the correct "no data" state, not unavailable. A partner's
        # entities additionally require the partner to be verified.
        if not super().available:
            return False
        if self._is_partner:
            return self.coordinator.has_partner_for_device(self._device_id)
        return True

    @property
    def native_value(self) -> Any:
        metric = self._metric_dict()
        if metric is None:
            return None
        value = metric.get("value")
        # A null value is "calibrating" / "empty": unknown, not zero.
        return value if isinstance(value, (int, float)) else None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        metric = self._metric_dict()
        if metric is None:
            return None
        attrs: dict[str, Any] = {}
        insight = metric.get("insight")
        if isinstance(insight, str) and insight:
            attrs["insight"] = insight
        for gran, label in (
            ("day", "vs_prior_day"),
            ("week", "vs_prior_week"),
            ("month", "vs_prior_month"),
        ):
            comp = _v3_comparison(metric, gran)
            if comp is not None:
                attrs[label] = comp
        return attrs or None


class OrionConsistencySensor(_OrionV3MetricSensor):
    """Sleep consistency (%), latest day. v3 / Orion Intelligence."""

    _metric = "consistency"
    _base_translation_key = "consistency"
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:calendar-check"


class OrionSleepDebtSensor(_OrionV3MetricSensor):
    """Sleep debt (min), latest day. v3 / Orion Intelligence.

    Carries `need` (the computed baseline in minutes) and `status`
    (`balanced` / `low`) alongside the shared comparison attributes.
    """

    _metric = "sleep_debt"
    _base_translation_key = "sleep_debt"
    _attr_native_unit_of_measurement = UnitOfTime.MINUTES
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:sleep"

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        attrs = super().extra_state_attributes or {}
        metric = self._metric_dict()
        if isinstance(metric, dict):
            need = metric.get("need")
            if isinstance(need, (int, float)):
                attrs = {**attrs, "need": need}
            status = metric.get("status")
            if isinstance(status, str) and status:
                attrs = {**attrs, "status": status}
        return attrs or None


class OrionBreathingDisturbancesSensor(_OrionV3MetricSensor):
    """Breathing disturbances (sec), latest day. v3 / Orion Intelligence.

    NOT a duplicate of the apnea suite. The apnea sensors (apnea_ahi,
    apnea_obstructive_time, apnea_central_time, apnea_longest_event) come
    from the v2 per-session data and are an AHI-based clinical breakdown.
    This is Orion Intelligence's own single roll-up in seconds, with a
    low/high band, and it is the number the v3 app shows on its trend
    screen.

    They are kept separate on purpose because they are different
    measurements that can legitimately disagree, but the matching v2 AHI is
    cross-referenced as an `ahi` attribute so the two are visibly linked
    rather than silently divergent.
    """

    _metric = "breathing_disturbances"
    _base_translation_key = "breathing_disturbances"
    _attr_native_unit_of_measurement = UnitOfTime.SECONDS
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:lungs"

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        attrs = super().extra_state_attributes or {}
        metric = self._metric_dict()
        if isinstance(metric, dict):
            details = metric.get("details")
            if isinstance(details, dict):
                for k in ("low_seconds", "high_seconds"):
                    v = details.get(k)
                    if isinstance(v, (int, float)):
                        attrs = {**attrs, k: v}
            state = metric.get("state")
            if isinstance(state, str) and state:
                attrs = {**attrs, "state": state}
        # Cross-reference the AHI from the matching v2 session, so the two
        # breathing measurements are visibly linked. `apnea_ahi` is the same
        # value the apnea_index sensor reads.
        session = (
            self.coordinator.get_latest_completed_partner_session(self._device_id)
            if self._is_partner
            else self.coordinator.get_latest_completed_session()
        )
        if isinstance(session, dict):
            from .descriptions import _get_apnea

            ahi = _get_apnea(session).get("ahi")
            if isinstance(ahi, (int, float)):
                attrs = {**attrs, "ahi": ahi}
        return attrs or None


class _OrionV3ScoreSensor(OrionBaseEntity, SensorEntity):
    """A whole granularity's overview score, with metrics as attributes.

    One sensor per granularity (week, month) rather than eight metric
    entities per granularity. State is the period's `overview.score`;
    attributes carry the rating/award/dates plus one entry per metric,
    each `{value, unit, insight, comparison}` where comparison is the
    prior-period one for that granularity.
    """

    _granularity: str = ""
    _base_translation_key: str = ""
    _live_fed = False
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = "points"
    _attr_icon = "mdi:medal-outline"

    _METRIC_KEYS = (
        "sleep_duration",
        "body_movements",
        "breathing_disturbances",
        "consistency",
        "sleep_debt",
        "hrv",
        "heart_rate",
        "breath_rate",
    )

    def __init__(
        self,
        coordinator: OrionDataUpdateCoordinator,
        device_id: str,
        *,
        is_partner: bool = False,
    ) -> None:
        super().__init__(coordinator, device_id)
        self._is_partner = is_partner
        self._data_key = "partner_insights_v3" if is_partner else "insights_v3"
        tk = self._base_translation_key
        self._attr_translation_key = f"partner_{tk}" if is_partner else tk
        who = (
            coordinator.partner_entity_key_id() if is_partner else coordinator.user_id
        )
        self._attr_unique_id = helpers.account_person_unique_id(
            coordinator.config_entry.entry_id,
            f"v3_{self._granularity}_score",
            who,
            legacy=f"{device_id}_v3_{self._granularity}_score"
            + ("_partner" if is_partner else ""),
        )

    def _overview(self) -> dict | None:
        return self.coordinator.v3_overview(self._granularity, key=self._data_key)

    def _period(self) -> dict | None:
        return self.coordinator._latest_v3_period(self._data_key, self._granularity)

    @property
    def available(self) -> bool:
        if not super().available:
            return False
        if self._is_partner:
            return self.coordinator.has_partner_for_device(self._device_id)
        return True

    @property
    def native_value(self) -> Any:
        overview = self._overview()
        if not isinstance(overview, dict):
            return None
        score = overview.get("score")
        return score if isinstance(score, (int, float)) else None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        period = self._period()
        if not isinstance(period, dict):
            return None
        attrs: dict[str, Any] = {}
        overview = period.get("overview")
        if isinstance(overview, dict):
            for k in ("rating", "color", "award", "state"):
                v = overview.get(k)
                if v is not None:
                    attrs[k] = v
        for k in ("start_date", "end_date", "days_with_data"):
            v = period.get(k)
            if v is not None:
                attrs[k] = v
        metrics = period.get("metrics")
        comparison_key = {
            "week": "vs_prior_week",
            "month": "vs_prior_month",
        }[self._granularity]
        if isinstance(metrics, dict):
            for mk in self._METRIC_KEYS:
                metric = metrics.get(mk)
                if not isinstance(metric, dict):
                    continue
                comp = _v3_comparison(metric, self._granularity)
                attrs[mk] = {
                    "value": metric.get("value"),
                    "unit": metric.get("unit"),
                    "insight": metric.get("insight"),
                    "comparison": comp,
                }
                # Name the comparison key so the shape is self-describing.
                if comp is not None:
                    attrs[mk]["comparison_key"] = comparison_key
        return attrs or None


class OrionTemperatureRecommendationsSensor(OrionBaseEntity, SensorEntity):
    """Orion Intelligence temperature recommendations for one sleeper.

    State is the number of recommendations (0 is a valid state, not an
    error: it means Orion has none for this user yet). The typed items ride
    as the `recommendations` attribute. The sensor is unavailable only when
    the recommendations key is absent entirely.

    MEASURED item schema (probed 2026-07-30 against a live account that,
    unlike Kevin Klaes's, had a populated recommendation): `bedtime_temp`,
    `phase_1_temp`, `phase_2_temp`, `wakeup_temp` (all numeric, per-phase),
    `thermal_classification`, `source`, `version`, `created_at`. This
    closes the open question in Kevin's fork, which only ever saw the list
    empty. Recommendations ride in `/v1/sleep-schedules`, which he found.
    """

    _live_fed = False
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:thermometer-auto"

    def __init__(
        self,
        coordinator: OrionDataUpdateCoordinator,
        device_id: str,
        *,
        is_partner: bool = False,
    ) -> None:
        super().__init__(coordinator, device_id)
        self._is_partner = is_partner
        self._attr_translation_key = (
            "partner_temperature_recommendations"
            if is_partner
            else "temperature_recommendations"
        )
        who = (
            coordinator.partner_entity_key_id() if is_partner else coordinator.user_id
        )
        self._attr_unique_id = helpers.account_person_unique_id(
            coordinator.config_entry.entry_id,
            "temperature_recommendations",
            who,
            legacy=f"{device_id}_temperature_recommendations"
            + ("_partner" if is_partner else ""),
        )

    def _user_id(self) -> str | None:
        if self._is_partner:
            return self.coordinator.partner_entity_key_id()
        return self.coordinator.user_id

    @property
    def available(self) -> bool:
        if not super().available:
            return False
        if self._is_partner and not self.coordinator.has_partner_for_device(
            self._device_id
        ):
            return False
        # Unavailable only when the key is absent. An empty list is a
        # present-and-known state of "zero recommendations".
        return self.coordinator.has_temperature_recommendations_key(self._user_id())

    @property
    def native_value(self) -> int:
        return len(self.coordinator.temperature_recommendations(self._user_id()))

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        items = self.coordinator.temperature_recommendations(self._user_id())
        return {"recommendations": items} if items else None


class OrionWeeklyScoreSensor(_OrionV3ScoreSensor):
    """Latest week's Orion Intelligence sleep score, metrics as attributes."""

    _granularity = "week"
    _base_translation_key = "weekly_sleep_score"


class OrionMonthlyScoreSensor(_OrionV3ScoreSensor):
    """Latest month's Orion Intelligence sleep score, metrics as attributes."""

    _granularity = "month"
    _base_translation_key = "monthly_sleep_score"
