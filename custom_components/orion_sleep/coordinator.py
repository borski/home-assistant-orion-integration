"""DataUpdateCoordinator for Orion Sleep."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryError
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util
from orion_sleep_api import (
    OrionApiClient,
    OrionApiError,
    OrionAuthError,
    OrionConnectionError,
    OrionWebSocketManager,
    live_state,
    util,
)

from . import helpers
from .const import (
    CONF_ACCOUNT_ID,
    CONF_ALLOW_UNVERIFIED_ACCOUNT,
    CONF_AUTH_VALUE,
    CONF_DEVICE_IDS,
    CONF_DISPLAY_ALIASES,
    CONF_INSIGHTS_DAYS,
    CONF_PARTNER_ACCOUNT_ID,
    CONF_PARTNER_AUTH_VALUE,
    CONF_PARTNER_DEVICE_SERIAL,
    CONF_SCAN_INTERVAL,
    CONF_ZONE_LEFT,
    DEFAULT_ALLOW_UNVERIFIED_ACCOUNT,
    DEFAULT_COOLING_MINUTES,
    DEFAULT_INSIGHTS_DAYS,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_ZONE_LEFT,
    MAX_COOLING_MINUTES,
    MIN_COOLING_MINUTES,
)

# Module scope, not inside `_async_update_data`. The function-scoped
# import that used to sit at the bed-overlap check was defending against a
# circular import that does not exist: `migrations` imports `helpers`,
# `const` and `descriptions`, and none of those reach back here. A local
# import that looks like a cycle workaround is worse than no comment,
# because the next reader has to re-derive the whole import graph to find
# out it was never needed.
from .migrations import overlapping_entry_ids, resolve_bed_owner

_LOGGER = logging.getLogger(__name__)

OrionConfigEntry = ConfigEntry  # ConfigEntry[OrionDataUpdateCoordinator]


def recorded_account_id(entry: ConfigEntry) -> str | None:
    """The Orion account this entry is bound to, or None if not yet known.

    `unique_id` also carries this after `async_migrate_entry_identity`, but
    pre-3.0 entries hold the typed address there. This is the only field
    that means one thing on every entry.

    The one accessor exists because the account identity is recorded TWICE
    and the two copies were guarded separately. `entry.unique_id` is what
    `ConfigFlow._async_reauth_account_matches` compares. `CONF_ACCOUNT_ID`
    is what the coordinator guard below compares. Reauth then writes
    `CONF_ACCOUNT_ID` unconditionally through a dict spread, so it
    overwrites the field the coordinator guards having validated only the
    other one. That is the same "never overwrite a recorded account id"
    invariant `__init__.py` states, violated two modules away, and it is
    currently harmless only because an unrelated third check happens to
    reject the mismatch first. A single reader is what stops the two
    drifting again.

    Returns None rather than "" for an absent value on purpose. An absent
    account id and a recorded empty string mean different things: the first
    is a pre-3.0 entry that has never been told who it belongs to, and the
    second would be a corrupt record. Both are falsy, so a caller testing
    truthiness is unaffected, and a caller that needs to tell them apart
    still can.
    """
    value = entry.data.get(CONF_ACCOUNT_ID)
    return value if isinstance(value, str) and value else None


def recorded_partner_account_id(entry: ConfigEntry) -> str | None:
    """The Orion account the linked partner tokens belong to, or None.

    The partner half of `recorded_account_id`, with the same contract.
    Absent on every entry whose partner was linked before this key
    existed, which is why `_partner_identity_verified` treats an absent
    value as unverifiable rather than as a mismatch.
    """
    value = entry.data.get(CONF_PARTNER_ACCOUNT_ID)
    return value if isinstance(value, str) and value else None


def profile_carries_address(profile: object, typed: str) -> bool:
    """Whether an Orion profile names the address this entry was set up with.

    `typed` is the already-normalised `CONF_AUTH_VALUE`, or
    `CONF_PARTNER_AUTH_VALUE` on the partner path, the email or phone
    number the user actually entered in the config flow. The profile's own
    `email` / `phone` / `phone_number` fields are the only independent
    statement of identity `/v1/auth/me` returns alongside the account id, so
    they are what an unverified account id has to be checked against.

    Fails closed. A profile carrying none of the three fields is not
    evidence of a match, it is an absence of evidence, and an accepted
    token is not proof of which account issued it.

    This is the same test `ConfigFlow._async_reauth_account_matches` applies
    on the reauth path, and it applies it by CALLING this function.
    `config_flow.py` imports it at module scope, so there is one copy of
    the rule and no way for the setup path and the reauth path to drift
    into disagreeing about identity.
    """
    if not isinstance(profile, dict) or not profile or not typed:
        return False
    known = {
        str(profile.get(field) or "").strip().lower()
        for field in ("email", "phone", "phone_number")
    }
    known.discard("")
    if not known:
        return False
    return typed in known


class OrionDataUpdateCoordinator(DataUpdateCoordinator[dict]):
    """Fetch data from Orion API."""

    config_entry: OrionConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        config_entry: OrionConfigEntry,
        api_client: OrionApiClient,
        partner_api_client: OrionApiClient | None = None,
    ) -> None:
        interval = config_entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
        super().__init__(
            hass,
            _LOGGER,
            name="orion_sleep",
            config_entry=config_entry,
            update_interval=timedelta(seconds=interval),
        )
        self.api_client = api_client
        self.partner_api_client = partner_api_client
        self.options = dict(config_entry.options)
        self.reload_started = False
        self.devices: list[dict] = []
        self.partner_devices: list[dict] = []
        # Live snapshots keyed by device id (UUID). Populated from
        # GET /v1/devices/{serial}/live on each poll AND from
        # live_device.{snapshot,update} frames on the per-device WebSocket.
        # The WS stream supersedes the polled state between polls, giving
        # realtime zone on/temp + status updates without waiting for the
        # next REST poll. Note that biometric-derived fields like
        # status.sensors.*.status_text (on-bed classification) lag the
        # real event by ~30s to 1min because the topper itself is slow to
        # decide. The WS frame arrival is not the bottleneck there.
        self.live_devices: dict[str, dict] = {}
        # Per-zone rapid-cool window, in minutes. Chosen locally and held
        # here so the switch and its duration slider agree without either
        # importing the other. Not vendor state: the server is only ever
        # told the number at the moment cooling starts.
        self.rapid_cool_minutes: dict[str, int] = {}
        self.user: dict = {}
        self.user_id: str = ""
        self.partner_user: dict = {}
        self.partner_update_ok = False
        # User-facing display overrides keyed by immutable Orion user id.
        # Aliases only ever change friendly names. Unique ids and entity ids
        # are derived from device and zone ids, so renaming is non-breaking.
        self.display_aliases: dict[str, str] = helpers.clean_alias_map(
            self.options.get(CONF_DISPLAY_ALIASES)
        )
        self.partner_device_serial = str(config_entry.data.get(CONF_PARTNER_DEVICE_SERIAL, ""))
        # Whether the last SUCCESSFUL partner fetch established that these
        # two accounts still share exactly one bed AND that the partner is
        # who this entry recorded. Both halves, together.
        #
        # Starts False, for the same reason `partner_identity_confirmed`
        # sixteen lines below does. This used to initialise to
        # `bool(self.partner_device_serial)`, which is neither of those
        # things. It reports that a serial is written in the config entry,
        # which is true the instant a partner is linked and stays true
        # forever afterwards, including on a boot where the partner has
        # never once been fetched.
        #
        # That fail-open default is what made the pair
        # `partner_mapping_valid=True` with `partner_identity_confirmed=
        # False` reachable on the FIRST fetch of a setup. Within one try
        # block in `_async_refresh_partner_identity`, `get_current_user()`
        # can return and `list_devices()` can throw, which populates
        # `partner_user`, skips both assignments below it, and leaves this
        # flag holding a value invented by the constructor rather than
        # established by any fetch. `has_partner_for_device` then answers
        # yes for a partner nothing has verified.
        #
        # The "leave it alone on a transient error" rule in the
        # OrionApiError handler is not in tension with this. That rule
        # preserves a verdict a previous successful fetch established. It
        # is not a licence to invent one before the first fetch has
        # happened.
        self.partner_mapping_valid = False
        # The topology half of the verdict above, published separately.
        #
        # `_async_refresh_partner_identity` has always computed these two
        # halves apart and then thrown one of them away, so a reader asking
        # "do these accounts still share one bed" had to infer it from a
        # flag that also folds in identity, and could not.
        #
        # `partner_mapping_valid` is deliberately still a plain assigned
        # attribute rather than a property returning `partner_topology_ok
        # and partner_identity_confirmed`. That property was implemented and
        # measured, and it is wrong. Identity goes False on any transient
        # partner error, by design, so the derived flag would go False too,
        # and `has_partner_for_device` gates entity CONSTRUCTION on it. A
        # restart inside one dropped connection built 0 partner entities
        # where the current code builds 44. That is the precise failure the
        # OrionApiError handler below exists to prevent, reintroduced
        # through the back door by a refactor that reads as pure tidying.
        #
        # The three-meanings problem that motivated the property is fixed by
        # the initial value above plus this attribute, which is the part
        # that was actually broken. `partner_mapping_valid` now means one
        # thing in all three places: the verdict of the last SUCCESSFUL
        # fetch.
        self.partner_topology_ok = False
        # Whether the LAST partner fetch positively established which Orion
        # account these partner tokens belong to.
        #
        # Separate from `partner_mapping_valid`, which answers a different
        # question and answers it in a way that cannot be inverted. That
        # flag is false for a partner who was replaced, for a partner whose
        # bed topology changed, AND for a partner the server simply did not
        # answer about for 800ms. `migrations.async_migrate_unique_ids`
        # needs to tell the first of those from the last, because it decides
        # whether to DELETE the partner's downgrade records or merely
        # distrust them, and deleting them on a network blip destroyed the
        # only rollback path that person had.
        #
        # Starts False. Nothing has been confirmed before the first fetch,
        # and an entry that never reaches one must not look confirmed.
        self.partner_identity_confirmed = False
        # Whether a SUCCESSFUL fetch positively DISPROVED the partner's
        # identity. Not the inverse of the flag above, and that is the
        # whole point of it existing.
        #
        # `partner_identity_confirmed` goes false for two unrelated
        # reasons: the server named a different account, and the server
        # did not answer. Only the first is evidence. Entity CONSTRUCTION
        # needs to tell them apart, because a household whose partner is
        # configured must keep its partner entities through a dropped
        # connection and must not get them for an account that has
        # demonstrably been swapped.
        #
        # Set only in the branch that actually ran the check, and left
        # alone by both failure handlers for the same reason
        # `partner_mapping_valid` is.
        self.partner_identity_rejected = False
        self._warned_partner_topology = False
        # Separate latch from the topology one on purpose. They are
        # different findings with different fixes, and sharing a latch
        # would let whichever fired first suppress the other's message
        # for the lifetime of the entry.
        self._warned_partner_identity = False

        # Maps device serial_number -> UUID so the WS message handler
        # (which only knows the serial) can key into live_devices.
        self._serial_to_id: dict[str, str] = {}

        # Live WebSocket manager — one connection per device serial.
        self._ws_manager: OrionWebSocketManager = OrionWebSocketManager(
            session=async_get_clientsession(hass),
            api_client=api_client,
            on_message=self._handle_ws_message,
            on_state_change=self._handle_ws_state,
        )

    def _unverified_account_allowed(self) -> bool:
        """Whether the household has explicitly accepted an unverified account.

        The escape hatch for one specific lockout, and deliberately not for
        anything else.

        WHAT IT RELAXES. Exactly one assertion: the requirement that a
        profile carry the address this entry was set up with, in the branch
        where NO account id has ever been recorded. That branch fails
        closed, which is correct, but the failure it raises launches a
        reauth flow whose own check is a copy of the same test. A profile
        Orion returns without `email`, `phone` and `phone_number` therefore
        fails setup and fails every attempt to escape setup, and there is
        no supported action left. This option is that action.

        WHAT IT DOES NOT RELAX, and this is the whole safety argument:

          * The recorded-versus-returned comparison. When an account id IS
            recorded there is a real reference value, a mismatch is a real
            finding, and ratifying it is the identity swap that moves one
            person's sleep history onto another person. That branch is
            above this one and never consults this option.
          * The empty account id guard. A profile with no id still refuses
            to set up, because there is nothing to key an entity on and the
            fallback ids no longer exist in the registry.
          * Anything on the partner path. `_partner_identity_verified` has
            its own reference values and its own degraded outcome, and it
            cannot brick an entry, so it needs no hatch and is given none.

        WHY THAT IS SAFE. The only thing this option can do is let an entry
        with no recorded account id write the id the server just returned.
        That is precisely what every 3.x build before the address assertion
        did unconditionally, so the worst case is the previous behaviour,
        entered deliberately, announced at WARNING, and confined to entries
        the household has individually opted in. It is also self-limiting:
        the write it permits happens once, in `async_setup_entry`, and
        every boot after it takes the recorded-id branch instead, which
        this option cannot reach. Leaving it switched on does not leave the
        assertion switched off.

        The warning is logged on the setup path rather than the poll path,
        so it appears once per load and cannot become log spam.

        SETTABLE FROM THE UI. `OrionSleepOptionsFlow.async_step_init`
        offers `CONF_ALLOW_UNVERIFIED_ACCOUNT` as a boolean field defaulted
        from the current options, placed last and worded as a recovery
        step. Home Assistant offers the options flow on an entry that
        failed to set up, which is what makes this reachable by somebody
        who is actually locked out, so the escape hatch is a real one
        rather than a read side waiting on a writer.
        """
        if not self.config_entry.options.get(
            CONF_ALLOW_UNVERIFIED_ACCOUNT, DEFAULT_ALLOW_UNVERIFIED_ACCOUNT
        ):
            return False
        _LOGGER.warning(
            "Orion is loading this entry WITHOUT verifying which account its "
            "tokens belong to, because %s is switched on in its options. The "
            "account id this setup records will be whatever the server just "
            "returned, and the entity migration keys every person's history "
            "on it. Turn this off once the entry has loaded",
            CONF_ALLOW_UNVERIFIED_ACCOUNT,
        )
        return True

    async def _async_setup(self) -> None:
        """Load one-time data: user profile, device list."""
        try:
            self.user = await self.api_client.get_current_user()
            self.user_id = self.user.get("id", "")
            if not self.user_id:
                # Refuse to set up rather than proceed anonymously. Every
                # person-scoped entity is keyed on this id, and without
                # it they fall back to the pre-3.0 ids, which no longer
                # exist in the registry. Home Assistant would build a
                # second entity for each person, and once both ids exist
                # the migration can never merge them again. One bad
                # profile response would permanently split a household's
                # sleep history in two.
                #
                # Orion has been measured returning {"response": null}
                # here, so this is an observed shape, not a hypothetical.
                raise UpdateFailed("Orion returned a profile with no account id")
            # The account id decides which human every person-scoped entity
            # belongs to, and the migration re-keys existing history onto
            # it. `api.py` already refuses an insights payload naming the
            # wrong account. The endpoint that DECIDES the expected account
            # had no such check, so a cached or mixed-up /v1/auth/me could
            # silently move one person's history onto another identity.
            recorded = recorded_account_id(self.config_entry)
            typed = str(self.config_entry.data.get(CONF_AUTH_VALUE) or "").strip().lower()
            if recorded:
                if recorded != self.user_id:
                    raise ConfigEntryAuthFailed(
                        "Orion returned a different account than this entry was "
                        "set up for. Reauthenticate to confirm which account it is"
                    )
            elif typed:
                # No recorded account id, so there is nothing to compare the
                # returned id against. That is not a rare state: the config
                # flow only began writing CONF_ACCOUNT_ID in 3.0, so EVERY
                # entry created before then arrives here on its first 3.x
                # boot, and that same boot performs the destructive re-key.
                # `async_migrate_entry_identity` moves the entry's unique_id
                # onto whatever id this response carried, and
                # `async_migrate_unique_ids` renames the pre-3.0 registry
                # rows, with all of their recorder history, onto ids derived
                # from it. Every later boot then compares the id to itself
                # and reports agreement forever.
                #
                # The check above cannot catch that, because it only fires
                # when a recorded id already exists. Neither can anything
                # else: the id is a well-formed non-empty uuid, so the
                # emptiness guard passes, and `entry_identity_conflict` only
                # detects a collision with another Orion entry on this same
                # Home Assistant instance, not a wrong-but-unused account.
                # Writing it unverified was trust on first use over an
                # endpoint measured returning {"response": null}.
                #
                # CONF_AUTH_VALUE is the address the user typed when they
                # set the entry up, so requiring the profile to carry it
                # turns that first write into a real identity assertion,
                # using a field already present in the response in hand.
                #
                # Fails closed, which is right, and fails closed onto a
                # locked door if the assertion can never be satisfied. If
                # Orion ever returns a profile carrying none of `email`,
                # `phone` or `phone_number`, a legacy entry stops loading,
                # and the reauth flow this error launches applies the SAME
                # test in `_async_reauth_account_matches`, so completing it
                # aborts with reauth_account_mismatch. The user has no way
                # out, and this endpoint has been measured returning
                # {"response": null}, so an account-shape change is not a
                # hypothetical. `self._unverified_account_allowed()` is the
                # documented way out, and it is checked here rather than
                # around the whole block on purpose. See its docstring.
                if not profile_carries_address(
                    self.user, typed
                ) and not self._unverified_account_allowed():
                    raise ConfigEntryAuthFailed(
                        "Orion returned an account that does not match the "
                        "address this entry was set up with. Reauthenticate to "
                        "confirm which account it is"
                    )
            else:
                # Neither a recorded account id nor a typed address. There
                # is no reference value in existence to check against, so
                # any test here would be theatre. Deliberately NOT fatal:
                # refusing would brick an entry whose CONF_AUTH_VALUE was
                # lost or was never written, and those entries are not
                # suspect, merely unverifiable. Warn instead, because the
                # write that follows in async_setup_entry is genuinely
                # unverified and the re-key it feeds is not reversible
                # without the recovery action.
                _LOGGER.warning(
                    "This Orion entry records neither an account id nor the "
                    "address it was set up with, so the account behind these "
                    "tokens cannot be verified. Reauthenticate to confirm it"
                )
            self.devices = util.dedupe_devices_by_id(await self.api_client.list_devices())
        except OrionAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except (OrionApiError, OrionConnectionError) as err:
            raise UpdateFailed(f"Error fetching initial data: {err}") from err

        await self._async_refresh_partner_identity()

    def _partner_identity_verified(self, partner_id: object) -> bool:
        """Whether these partner tokens still belong to the linked partner.

        The partner half of the identity check `_async_setup` runs on the
        primary account. Before this existed the two were asymmetric in the
        worst possible direction: the primary compared a recorded account id
        against the returned one, and the partner accepted whatever
        `get_current_user()` said. `partner_mapping_valid` looked like a
        guard and is not one. It compares device serials and device counts,
        which establishes that the two accounts still share exactly one bed
        and establishes nothing at all about which partner account this is.
        `has_partner_for_device` adds only that the partner is not the
        primary.

        What that unverified id then drives is not cosmetic. It is the id
        `migrations._partner_recovery_renames` builds its pairs from, and
        those pairs become the partner records in the downgrade journal.
        2.x has exactly ONE role-keyed row per partner key, fed by whichever
        partner account is linked at the time, so reverting a journal that
        names the wrong partner hands the previous partner's entities to the
        current one. Two people's heart rate, HRV and apnea history merge
        under one identity, with every rename reporting success. That is the
        same class of damage `evict_partner_journal` exists to prevent from
        a different direction, and the primary already had a recorded-versus-
        returned check protecting the same journal.

        Never raises, and in particular never raises ConfigEntryAuthFailed.
        The reauth flow that exception launches re-verifies the PRIMARY
        account's address, so a partner problem would prompt the wrong
        person for credentials that cannot fix it, and the primary's
        entities would go down for the duration. Returning False disables
        the partner-derived entities and leaves everything else running,
        which is the correct blast radius for a partner fault.

        Three cases, in order of how much evidence exists:

        1. A recorded partner account id. Compare directly. A mismatch is a
           real finding against a real reference value.
        2. No recorded id but a recorded partner address. Apply
           `profile_carries_address`, exactly as the primary does when it
           has no recorded id. Every partner linked by the current config
           flow has this, because `_write_partner_change` writes
           CONF_PARTNER_AUTH_VALUE in the same call as the tokens and
           removes it in the same call as the tokens.
        3. Neither. Fail closed. This differs from the primary's
           equivalent branch, which warns and continues, and the difference
           is deliberate: refusing there would brick the whole entry, and
           refusing here costs the household its partner insight entities
           and nothing else. Case 3 is also not reachable from any
           supported flow, since the tokens and the address are written and
           removed together, so failing closed on it forfeits nothing that
           a working install has. Relinking the partner in Orion options
           resolves it.
        """
        if not isinstance(partner_id, str) or not partner_id:
            # No id to check. Topology already rejects this, so returning
            # False here is agreement rather than a second opinion, and it
            # keeps this method total instead of relying on its caller.
            return False

        recorded = recorded_partner_account_id(self.config_entry)
        typed = (
            str(self.config_entry.data.get(CONF_PARTNER_AUTH_VALUE) or "").strip().lower()
        )

        problem: str | None = None
        if recorded:
            if recorded != partner_id:
                problem = (
                    "Orion returned a different partner account than this entry "
                    "recorded. Partner insights are disabled and no partner "
                    "downgrade records will be kept until the partner account "
                    "is relinked in the Orion options"
                )
        elif typed:
            if not profile_carries_address(self.partner_user, typed):
                problem = (
                    "Orion returned a partner account that does not match the "
                    "address the partner was linked with. Partner insights are "
                    "disabled until the partner account is relinked in the "
                    "Orion options"
                )
        else:
            problem = (
                "This Orion entry has partner tokens but records neither a "
                "partner account id nor the address the partner was linked "
                "with, so the account behind those tokens cannot be verified. "
                "Partner insights are disabled until the partner account is "
                "relinked in the Orion options"
            )

        # Latched, for the same reason the topology warning is. This method
        # runs from every poll, so an unlatched warning would repeat once
        # per scan interval forever for any household whose partner will
        # not verify on its own.
        if problem is None:
            self._warned_partner_identity = False
            return True
        if not self._warned_partner_identity:
            self._warned_partner_identity = True
            _LOGGER.warning("%s", problem)
        return False

    async def _async_refresh_partner_identity(self) -> None:
        """Refresh partner profile and device visibility when configured."""
        if self.partner_api_client is None:
            return
        try:
            self.partner_user = await self.partner_api_client.get_current_user()
            self.partner_devices = util.dedupe_devices_by_id(
                await self.partner_api_client.list_devices()
            )
            partner_id = self.partner_user.get("id")
            # Topology says the two accounts still share exactly one bed.
            # It says nothing about WHICH partner account is behind these
            # tokens, which is a different question with a different
            # answer, so the two verdicts are computed separately and the
            # log names whichever one actually failed.
            topology_ok = (
                isinstance(partner_id, str)
                and bool(partner_id)
                and len(self.devices) == 1
                and len(self.partner_devices) == 1
                and self.devices[0].get("serial_number") == self.partner_device_serial
                and self.partner_devices[0].get("serial_number") == self.partner_device_serial
            )
            identity_ok = self._partner_identity_verified(partner_id)
            # Recorded separately from the combined verdict below. A caller
            # that needs "do we know who this partner is" must not have to
            # infer it from a flag that also folds in bed topology.
            self.partner_identity_confirmed = identity_ok
            # The one place this may be written. A fetch came back and the
            # check ran, so its verdict is evidence either way.
            self.partner_identity_rejected = not identity_ok
            self.partner_topology_ok = topology_ok
            self.partner_mapping_valid = topology_ok and identity_ok
            # Latched. This runs on every poll, so an unlatched warning
            # repeated once per scan interval forever for any household
            # whose topology genuinely will not resolve on its own.
            if not topology_ok:
                if not self._warned_partner_topology:
                    self._warned_partner_topology = True
                    _LOGGER.warning(
                        "Partner device topology changed. Partner insights are "
                        "disabled until the account is relinked"
                    )
            else:
                self._warned_partner_topology = False
        except OrionAuthError as err:
            # A rejected token IS evidence, unlike the branch below. These
            # credentials do not currently speak for anyone, so the partner
            # entities go unavailable until the account is relinked.
            self.partner_update_ok = False
            self.partner_identity_confirmed = False
            self.partner_topology_ok = False
            self.partner_mapping_valid = False
            _LOGGER.warning(
                "Partner authentication failed. Replace it in Orion options: %s",
                err,
            )
        except (OrionApiError, OrionConnectionError) as err:
            # `partner_mapping_valid` is deliberately LEFT ALONE here.
            #
            # A 500 or a dropped connection is an absence of evidence, not
            # evidence of a change. Forcing this flag false on one failed
            # request tore down every partner entity's availability, and on
            # a restart inside that window the entities were never
            # constructed at all, because `has_partner_for_device` gates
            # construction on this flag in `sensor.py` and
            # `binary_sensor.py`. One unlucky 800ms was enough to remove a
            # person's biometric entities from the system.
            #
            # Leaving it means the flag keeps whatever the last SUCCESSFUL
            # fetch established. That is safe because no consumer acts on
            # this flag alone: every one of them also requires a real id out
            # of `partner_user`, and `partner_user` is only ever replaced by
            # a successful response. On a cold start the last successful
            # fetch has not happened, `partner_user` is empty, and the
            # partner entities are correctly absent regardless of this flag.
            #
            # `partner_topology_ok` is left alone for the same reason and
            # on the same terms. It is the other half of the verdict above,
            # and a request that never arrived did not disprove it either.
            #
            # `partner_identity_confirmed` DOES go false, because we cannot
            # confirm anything right now. That is what stops the migration
            # treating this blip as proof the partner was replaced, and it
            # is also why `partner_mapping_valid` must NOT be derived from
            # it: deriving it would make this handler tear down the partner
            # entities it exists to keep alive.
            self.partner_update_ok = False
            self.partner_identity_confirmed = False
            _LOGGER.warning("Failed to initialize partner account: %s", err)

    async def _async_update_data(self) -> dict:
        """Poll mutable state."""
        try:
            await self.api_client.ensure_valid_token()
        except OrionAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except (OrionApiError, OrionConnectionError) as err:
            raise UpdateFailed(f"Error refreshing token: {err}") from err

        # Carry the previous poll's payloads forward. A failed sub-fetch
        # logs and continues, so initialising these empty would blank every
        # dependent entity for a full scan interval after one transient 500.
        # `partner_insights` already did this. `schedules` and `insights`
        # did not, which is a real bug: nine schedule entities today, and
        # thirty-two once per-person schedules land.
        data: dict = {
            "schedules": (self.data or {}).get("schedules", {}),
            "insights": (self.data or {}).get("insights", {}),
            "insights_v3": (self.data or {}).get("insights_v3", {}),
            "partner_insights": (self.data or {}).get("partner_insights", {}),
            "partner_insights_v3": (self.data or {}).get("partner_insights_v3", {}),
            "live_session": (self.data or {}).get("live_session", {}),
            "sleep_config": (self.data or {}).get("sleep_config", {}),
        }

        # Re-fetch devices each poll so zone/user changes surface.
        topology_changed = False
        try:
            refreshed_devices = util.dedupe_devices_by_id(
                await self.api_client.list_devices()
            )
        except OrionAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except (OrionApiError, OrionConnectionError) as err:
            _LOGGER.warning("Failed to refresh device list: %s", err)
        else:
            refreshed_ids = sorted(
                str(device["id"])
                for device in refreshed_devices
                if isinstance(device.get("id"), str) and device["id"]
            )
            recorded_ids = list(self.config_entry.data.get(CONF_DEVICE_IDS) or [])
            if overlapping_entry_ids(
                self.hass, self.config_entry.entry_id, set(refreshed_ids)
            ):
                # Overlap alone is not a failure: a claim left behind by
                # another entry's failed setup looks identical to one held
                # by a healthy entry, so treating any overlap as fatal let a
                # dead entry lock out the one that actually holds the
                # history. `resolve_bed_owner` is the single copy of that
                # rule, shared with `async_migrate_unique_ids`.
                owner = resolve_bed_owner(
                    self.hass, self.config_entry.entry_id, set(refreshed_ids)
                )
                if owner != self.config_entry.entry_id:
                    # ConfigEntryError, NOT UpdateFailed, and this is the
                    # message the user actually reads.
                    #
                    # This check runs inside the first refresh, which is
                    # before `async_migrate_unique_ids` gets to run its
                    # copy, so the migration's carefully worded refusal was
                    # never reached. What people saw instead was this one,
                    # which named the owner and then stopped, saying nothing
                    # about what to do next.
                    #
                    # UpdateFailed was also the wrong shape. It becomes
                    # ConfigEntryNotReady and puts the entry in SETUP_RETRY,
                    # so it re-ran this same doomed refresh on a backoff
                    # forever. Retrying cannot resolve a conflict that only
                    # the user can resolve, and the retry loop buried the
                    # one line explaining how. ConfigEntryError fails once,
                    # loudly, with the instruction attached. On a later
                    # scheduled poll Home Assistant logs it and marks the
                    # update failed without any retry storm, because
                    # `raise_on_entry_error` is only set for the first
                    # refresh.
                    raise ConfigEntryError(
                        "Another Orion config entry already holds this bed's "
                        f"entity history ({owner}). Two entries cannot both "
                        "own it. Remove whichever entry you do not want, then "
                        "reload"
                    )
            if refreshed_ids != recorded_ids:
                self.hass.config_entries.async_update_entry(
                    self.config_entry,
                    data={**self.config_entry.data, CONF_DEVICE_IDS: refreshed_ids},
                )
                topology_changed = True
            self.devices = refreshed_devices

        # Rebuild the serial -> UUID map and sync the WS connections to
        # the current device list. Starting the WS manager here (rather
        # than in _async_setup) means it survives account topology
        # changes (devices added/removed) without a full reload.
        self._serial_to_id = {
            d["serial_number"]: d["id"]
            for d in self.devices
            if d.get("serial_number") and d.get("id")
        }
        self._ws_manager.sync_to_serials(list(self._serial_to_id.keys()))

        # Fetch the live snapshot for each device (zone on/temp + status).
        # GET /v1/devices does NOT include the `on` field. GET /v1/devices/
        # {serial}/live does. The /live path uses serial_number, not UUID.
        #
        # We still poll /live even with the WS in place — the WS is best-
        # effort and the periodic REST fetch guarantees the entities have
        # fresh state if the socket ever drops between polls. When the WS
        # is healthy the coordinator state is kept up to date by
        # async_set_updated_data from _handle_ws_message, so users don't
        # wait for the next poll to see their toggles reflected.
        new_live: dict[str, dict] = {}
        # What each device's live state was when this loop decided what to
        # do about it. Read again after the awaits, because both writers of
        # `live_devices` REPLACE the per-device dict rather than mutating
        # it, so object identity is a reliable "did something land while we
        # were away".
        before_fetch: dict[str, dict | None] = {}
        for device in self.devices:
            dev_id = device.get("id")
            serial = device.get("serial_number")
            if not dev_id or not serial:
                continue
            before_fetch[dev_id] = self.live_devices.get(dev_id)
            # Keep any WS-provided state until the REST fetch replaces it
            # — this avoids a flash of stale data between polls.
            if dev_id in self.live_devices and self._ws_manager.is_fresh(serial):
                new_live[dev_id] = self.live_devices[dev_id]
                continue
            try:
                new_live[dev_id] = await self.api_client.get_live_device(serial)
            except OrionAuthError as err:
                raise ConfigEntryAuthFailed(str(err)) from err
            except (OrionApiError, OrionConnectionError) as err:
                _LOGGER.warning(
                    "Failed to fetch live state for %s: %s",
                    helpers.short_id(serial),
                    err,
                )
                # Preserve whatever we already had rather than blanking it.
                if dev_id in self.live_devices:
                    new_live[dev_id] = self.live_devices[dev_id]

        # Anything that changed WHILE the fetches were in flight wins.
        #
        # `new_live` is built across a series of awaits and then assigned
        # wholesale, so it is a snapshot of a moment that has already
        # passed. A successful `PUT .../live` landing inside that window
        # calls `apply_live_device`, and a socket frame calls
        # `_handle_ws_message`, and both wrote into `self.live_devices`
        # only for the assignment below to throw the result away.
        #
        # The socket case self-heals, because a healthy socket sends
        # another frame about two seconds later. The write case does not.
        # `OrionLiveSettingMixin` deliberately does not refresh after a
        # successful write, on the correct grounds that the PUT echo is
        # authoritative, so with a stale socket the only thing that ever
        # corrected the reverted value was the NEXT poll, up to
        # `DEFAULT_SCAN_INTERVAL` away. Ten minutes of a control showing
        # the opposite of what the bed was actually doing, after the server
        # had already acknowledged the change. The mixin's five second
        # optimistic lock hid it and then expired.
        for dev_id, before in before_fetch.items():
            current = self.live_devices.get(dev_id)
            if current is not None and current is not before:
                new_live[dev_id] = current
        self.live_devices = new_live

        try:
            data["schedules"] = await self.api_client.get_sleep_schedules()
        except OrionAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except (OrionApiError, OrionConnectionError) as err:
            _LOGGER.warning("Failed to fetch sleep schedules: %s", err)

        # Read outside the try, because it is read again by the partner
        # fetch thirty-odd lines below. Binding it inside meant the only
        # statement that could raise before the binding, the insights call
        # itself, left `insights_days` undefined for the rest of the poll.
        # It never actually blew up, purely because the failing call was
        # the second statement rather than the first, which is a property
        # of the current line order and not a guarantee. A reordering, or
        # an options lookup that ever raises, turns a logged warning into
        # an UnboundLocalError that takes the whole poll down.
        insights_days = self.config_entry.options.get(
            CONF_INSIGHTS_DAYS, DEFAULT_INSIGHTS_DAYS
        )
        try:
            data["insights"] = await self.api_client.get_insights(
                days=insights_days, expected_user_id=self.user_id
            )
        except OrionAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except (OrionApiError, OrionConnectionError) as err:
            _LOGGER.warning("Failed to fetch insights: %s", err)

        # Orion Intelligence pre-aggregated day/week/month analytics. A
        # separate, richer surface from v2 above. Fetched on its own so a
        # v3 failure never blanks the v2-backed entities and vice versa.
        try:
            data["insights_v3"] = await self.api_client.get_insights_v3(
                expected_user_id=self.user_id
            )
        except OrionAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except (OrionApiError, OrionConnectionError) as err:
            _LOGGER.warning("Failed to fetch v3 insights: %s", err)

        # Orion's own view of whether anyone is in bed, and where the
        # schedule currently is. Both are cheap, and `is_in_bed` is the
        # only occupancy signal in this API that the vendor's own app
        # actually consumes.
        try:
            data["live_session"] = await self.api_client.get_live_session()
        except OrionAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except (OrionApiError, OrionConnectionError) as err:
            _LOGGER.warning("Failed to fetch the live session: %s", err)

        try:
            data["sleep_config"] = await self.api_client.get_sleep_configurations()
        except OrionAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except (OrionApiError, OrionConnectionError) as err:
            _LOGGER.warning("Failed to fetch sleep configurations: %s", err)

        if self.partner_api_client is not None:
            await self._async_refresh_partner_identity()
            partner_id = self.partner_user.get("id")
            if self.partner_mapping_valid and isinstance(partner_id, str):
                try:
                    data["partner_insights"] = (
                        await self.partner_api_client.get_insights(
                            days=insights_days,
                            expected_user_id=partner_id,
                        )
                    )
                    self.partner_update_ok = True
                except OrionAuthError as err:
                    self.partner_update_ok = False
                    _LOGGER.warning(
                        "Partner authentication failed. Replace it in Orion options: %s",
                        err,
                    )
                except (OrionApiError, OrionConnectionError) as err:
                    self.partner_update_ok = False
                    _LOGGER.warning("Failed to fetch partner insights: %s", err)

                # Partner v3, on the partner's own token and guarded by the
                # partner's account id. Separate try so a v3 failure does
                # not flip `partner_update_ok` for the v2 fetch above.
                try:
                    data["partner_insights_v3"] = (
                        await self.partner_api_client.get_insights_v3(
                            expected_user_id=partner_id,
                        )
                    )
                except OrionAuthError as err:
                    _LOGGER.warning(
                        "Partner authentication failed. Replace it in Orion options: %s",
                        err,
                    )
                except (OrionApiError, OrionConnectionError) as err:
                    _LOGGER.warning("Failed to fetch partner v3 insights: %s", err)

        if topology_changed and not self.reload_started:
            self.reload_started = True

            async def _reload_for_topology() -> None:
                try:
                    reloaded = await self.hass.config_entries.async_reload(
                        self.config_entry.entry_id
                    )
                    if not reloaded:
                        self.reload_started = False
                except Exception:
                    self.reload_started = False
                    _LOGGER.exception("Could not reload Orion after its bed set changed")

            self.hass.async_create_task(
                _reload_for_topology(), "reload Orion after bed topology change"
            )

        return data

    # ── v3 insights (Orion Intelligence day/week/month analytics) ─────
    #
    # The v3 surface is pre-aggregated by period. Entities read the LATEST
    # period of a granularity. A metric with no data reports `value: null`
    # and a `state` like `calibrating`/`empty`, and every accessor here
    # returns None for that so the entity reports `unknown`, never 0. Zero
    # sleep debt and no data are different facts.

    def _v3_root(self, key: str) -> dict:
        """The v3 payload for the primary (`insights_v3`) or partner."""
        root = (self.data or {}).get(key)
        return root if isinstance(root, dict) else {}

    def v3_has_subscription(self, key: str = "insights_v3") -> bool:
        """Whether the account has an active Orion Intelligence subscription.

        Without it the whole metrics surface comes back empty, which the
        entities must render as unknown rather than as a real zero.
        """
        return self._v3_root(key).get("has_subscription") is True

    def _latest_v3_period(self, key: str, granularity: str) -> dict | None:
        """The most recent period dict for a granularity, or None.

        Latest by `period_key` (the date-shaped key), so it does not depend
        on dict ordering. Returns None if v3 is empty or the granularity is
        absent, which is the no-data path.
        """
        gran = self._v3_root(key).get("granularities")
        if not isinstance(gran, dict):
            return None
        block = gran.get(granularity)
        if not isinstance(block, dict):
            return None
        periods = block.get("data")
        if not isinstance(periods, dict) or not periods:
            return None
        latest_key = max(periods)
        period = periods.get(latest_key)
        return period if isinstance(period, dict) else None

    def v3_metric(
        self, metric: str, granularity: str = "day", key: str = "insights_v3"
    ) -> dict | None:
        """One metric dict from the latest period of a granularity.

        Returns the raw metric dict (value, unit, insight, comparisons,
        state, status, and metric-specific extras). None when v3 is
        unavailable, the subscription is inactive, or the metric is absent.
        The entity decides whether a present-but-null value is `unknown`.
        """
        if not self.v3_has_subscription(key):
            return None
        period = self._latest_v3_period(key, granularity)
        if period is None:
            return None
        metrics = period.get("metrics")
        if not isinstance(metrics, dict):
            return None
        value = metrics.get(metric)
        return value if isinstance(value, dict) else None

    def v3_overview(
        self, granularity: str, key: str = "insights_v3"
    ) -> dict | None:
        """The `overview` dict (score, rating, ...) of the latest period.

        Used by the week/month score sensors. None when v3 is unavailable
        or the subscription is inactive.
        """
        if not self.v3_has_subscription(key):
            return None
        period = self._latest_v3_period(key, granularity)
        if period is None:
            return None
        overview = period.get("overview")
        return overview if isinstance(overview, dict) else None

    def get_latest_session(self) -> dict | None:
        """Get the most recent sleep session from insights data."""
        return util.latest_session(helpers.nested_mapping(self.data, "insights", "data"))

    def get_latest_session_for_zone(self, zone_id: str) -> dict | None:
        """Get the most recent sleep session for one device zone."""
        return util.latest_session_for_zone(
            helpers.nested_mapping(self.data, "insights", "data"), zone_id
        )

    def get_latest_partner_session(self, device_id: str) -> dict | None:
        """Get the newest partner session for an unambiguous shared bed."""
        if not self.has_partner_for_device(device_id):
            return None
        return util.latest_session(
            helpers.nested_mapping(self.data, "partner_insights", "data")
        )

    def get_latest_completed_session(self) -> dict | None:
        """Newest FINISHED session for the authenticated account.

        Separate from get_latest_session because a night in progress
        already carries an end_time. See util.latest_completed_session.
        """
        return util.latest_completed_session(
            helpers.nested_mapping(self.data, "insights", "data")
        )

    def get_latest_completed_partner_session(self, device_id: str) -> dict | None:
        """Newest FINISHED session for the linked partner account."""
        if not self.has_partner_for_device(device_id):
            return None
        return util.latest_completed_session(
            helpers.nested_mapping(self.data, "partner_insights", "data")
        )

    def session_active(self, session: dict | None) -> bool:
        """Whether the supplied session is currently running."""
        return util.session_in_progress(session)

    def known_users(self) -> list[dict[str, str]]:
        """Every Orion user visible to this entry, for the alias options form.

        Account objects are walked before device zones so the fuller
        `/v1/auth/me` copies win over the sparser embedded ones.
        """
        return util.collect_known_users(self.devices, [self.user, self.partner_user])

    def display_name_for_user(self, user_id: object) -> str:
        """Friendly name for one Orion user.

        Resolution order: configured alias, then a vendor-supplied name,
        then a stable id-derived fallback. Never raises and never returns
        an empty string, because an entity with a blank name is worse than
        one with an ugly name.

        BOTH of the first two are filtered for login credentials, not just
        the vendor's. The filter guards a permanent identifier, and a
        permanent identifier does not become safe because the household
        typed the value itself.

        Unique ids are built from device and zone ids, never from a
        person, so an alias change cannot orphan history. The entity_id
        is a different matter and this docstring used to imply otherwise.
        Home Assistant slugifies the first name an entity registers with
        and then keeps that entity_id forever, through every later
        rename. So the vendor's label is filtered through
        `helpers.is_safe_display_name` rather than trusted: its fallback
        chain ends at email and then phone, and a login credential must
        never reach a permanent identifier.
        """
        if not isinstance(user_id, str) or not user_id:
            return "Unknown"
        alias = self.display_aliases.get(user_id)
        if (
            isinstance(alias, str)
            and alias.strip()
            and helpers.is_safe_display_name(alias)
        ):
            # The household's own choice, and the documented way to recover
            # from an ugly fallback name below. Taken as given in every
            # respect EXCEPT the credential shape, which is filtered by the
            # same predicate the vendor's label goes through a few lines
            # down.
            #
            # The filter is here because permanence does not care who typed
            # the string. Home Assistant slugifies the first name an entity
            # registers with into its entity_id and never revisits it on
            # rename, so an alias of "alice@example.com" becomes
            # `climate.alice_example_com_climate` in the entity registry,
            # in every recorder row, in every long term statistics row, and
            # in every backup, permanently. The household typed it, so it
            # is self-inflicted, but self-inflicted and irreversible is
            # still irreversible, and "you typed it" is not a remedy once
            # the id is minted.
            #
            # Without this branch the two naming paths disagreed for no
            # reason anyone could state. A vendor-supplied
            # "alice@example.com" was refused here and an alias of the
            # identical string was accepted, so the credential the filter
            # exists to keep out of a permanent identifier walked in
            # through the field next to it.
            #
            # `config_flow.async_step_aliases` refuses the same value at
            # the write boundary, which is the half that can explain itself
            # to the household and the half where the value is not yet
            # permanent. This half covers an alias that was already stored
            # before that validation existed, which the form will never see
            # again because nothing re-validates an option on read.
            #
            # Deliberately NOT filtered inside `helpers.clean_alias_map`.
            # That runs in `__init__` over already-stored options, so
            # filtering there would silently blank an alias a household is
            # relying on, with no message and no way to tell why the name
            # changed. Refusing to USE a value is recoverable. Deleting it
            # is not.
            return alias.strip()
        for record in self.known_users():
            # `record["name"]` is `orion_user_label` output, so it is an
            # email or a phone number whenever the account has no name
            # set. Filtered here rather than inside `known_users` because
            # the alias options form is the other caller and it still
            # wants the vendor's own label to tell two people apart. A
            # form field is transient. An entity_id is not.
            if record["id"] == user_id and helpers.is_safe_display_name(
                record["name"]
            ):
                return record["name"].strip()
        # Ugly but safe, and recoverable through the alias options flow.
        # Eight characters of the user id, matching `unique_alias_labels`
        # so the field in that form is recognizably the same person as
        # the entity it renames. The full id is already in every
        # `person_unique_id`, so this exposes nothing new.
        return f"User {user_id[:8]}"

    def primary_name(self) -> str:
        """Display name for the authenticated account holder.

        Feeds `_attr_name` on the insight, schedule and climate entities,
        so whatever this returns is slugified into a permanent entity_id.
        """
        if self.user_id:
            return self.display_name_for_user(self.user_id)
        # NOT orion_user_label. Its fallback chain ends at email and then
        # phone, and this branch runs precisely when identity is thin
        # enough that those are what it would return. "You" is worse to
        # read and impossible to leak.
        return "You"

    def partner_name(self) -> str:
        """Display name for the linked partner account.

        Same permanence as `primary_name`. See its comment.

        The fetched id is consulted FIRST, so a healthy entry resolves the
        vendor's own label exactly as it always has and this method's
        output is unchanged for every household whose partner fetch
        succeeded.

        The recorded-id branch below exists only for the run whose first
        partner fetch failed. `has_partner_configured_for_device` now
        builds the partner's entities on that run, so this method is
        reached with an empty `partner_user`, and whatever it returns is
        slugified into a permanent entity_id the very first time those
        entities register. A household that has already named this person
        in the alias options should keep that name rather than be given
        `sensor.partner_sleep_score` forever because of one dropped
        connection.

        `display_name_for_user` is deliberately NOT called on the recorded
        id. Its last fallback is `User 22222222`, which is worse to read
        than "Partner" and is permanent, and its middle fallback walks
        `known_users()`, which is built from `partner_user` and is empty
        in precisely the case this branch handles. Only the alias, the one
        value the household chose itself, is worth minting an id from.
        The credential filter still applies, for the reason spelled out at
        length in `display_name_for_user`: permanence does not care who
        typed the string.
        """
        partner_id = self.partner_user.get("id")
        if isinstance(partner_id, str) and partner_id:
            return self.display_name_for_user(partner_id)
        recorded = recorded_partner_account_id(self.config_entry)
        if recorded:
            alias = self.display_aliases.get(recorded)
            if (
                isinstance(alias, str)
                and alias.strip()
                and helpers.is_safe_display_name(alias)
            ):
                return alias.strip()
        return "Partner"

    def partner_entity_key_id(self) -> str | None:
        """The Orion account id the partner's entities are keyed on, or None.

        Exists because `has_partner_configured_for_device` lets a partner
        entity be BUILT before any fetch has succeeded, and an entity has
        to choose a unique_id at construction time and then keep it
        forever. `helpers.person_unique_id` falls back to the 2.x
        role-keyed `{device}_partner_{key}` string when it is handed no
        account id, so building from an empty `partner_user` would mint
        every partner entity on a legacy id. The registry would then hold
        that id permanently, the verified id would be minted as a SECOND
        entity on the next boot that reached the server, and the household
        would own two of everything with the history split between them.
        That is worse than the absent-entity bug this whole change exists
        to fix, so existence-based construction is only safe alongside a
        durable answer to "which account".

        The recorded id is preferred over the fetched one, in that order,
        and the order is the point:

          * `CONF_PARTNER_ACCOUNT_ID` is a configuration fact. It is
            written once by the config flow when the partner is linked and
            it does not depend on any request succeeding. It is also
            exactly the value `_partner_identity_verified` demands the
            fetched id equal, so on every healthy entry these two agree
            and this preference changes nothing at all.
          * They disagree in one case: the partner was REPLACED at the
            vendor and the fetch returned somebody else. Keying on the
            recorded id there is deliberate. Those registry entries belong
            to the partner this entry was set up with, and re-keying them
            onto an account this integration has explicitly refused to
            verify is the cross-person merge three waves of work went into
            preventing. The entities stay on the old id and report
            unavailable until the household relinks, which is the same
            outcome the identity check already produces everywhere else.
          * The fetched id is used only when nothing is recorded. That is
            an entry whose partner was linked before
            `CONF_PARTNER_ACCOUNT_ID` existed, and using it there
            reproduces exactly what those entries do today.

        This value NEVER reaches `_partner_identity_verified`, and it must
        not. That function's entire job is to compare the fetched id
        against the recorded one, so feeding the recorded one back in as
        the fetched one would make it agree with itself and the partner
        identity check would become a tautology.
        """
        fetched = (self.partner_user or {}).get("id")
        recorded = recorded_partner_account_id(self.config_entry)
        if recorded:
            return recorded
        if isinstance(fetched, str) and fetched:
            return fetched
        return None

    def has_partner_configured_for_device(self, device_id: str) -> bool:
        """Whether this household HAS a partner on this bed. Not whether we trust one.

        The existence half of a question `has_partner_for_device` used to
        answer on its own, split out because the two halves have different
        lifetimes and conflating them lost a household its entities.

        Read the pair together:

          * THIS predicate answers a question about CONFIGURATION. Partner
            tokens and a partner serial are written on the config entry,
            so a second person sleeps on this bed. That is durable. It was
            true before this boot and it stays true across every failed
            request.
          * `has_partner_for_device` answers a question about THIS
            SESSION. The last successful fetch established that these two
            accounts still share exactly one bed and that the partner is
            the one we recorded. That is perishable by design.

        Entity CONSTRUCTION is gated on this one, in `sensor.py` and
        `binary_sensor.py`. Entity AVAILABILITY and every piece of partner
        DATA stay gated on the trust predicate. The bug that forced the
        split was that construction was gated on trust: if the very first
        partner fetch of a run failed, `partner_user` stayed empty,
        `partner_mapping_valid` stayed False, and the partner's sleep,
        heart rate, HRV and apnea entities were never built. Not
        unavailable. Absent. Every dashboard card and automation naming
        them broke, with nothing in the log connecting one dropped
        connection to a missing half of the household, and no recovery
        until somebody reloaded the entry by hand.

        Reporting `unavailable` is the correct answer to "this exists and
        I cannot currently speak for it", and it is what Home Assistant
        expects. Deleting the entity is not a stronger version of the same
        statement, it is a different and much more destructive one.

        Note what is NOT relaxed. This still requires a durable account id
        out of `partner_entity_key_id`, still refuses an account linked as
        its own partner, and still requires the bed to be the single
        device this entry owns. It only drops the requirement that a
        request succeeded recently.
        """
        if self.partner_api_client is None or not self.partner_device_serial:
            return False
        # An account linked as its own partner is one person, not two.
        # Same guard, same reasoning, as the trust predicate below. Left
        # duplicated rather than factored out because the two predicates
        # are meant to be readable side by side, and a shared private
        # helper would hide that they agree here on purpose.
        partner_id = self.partner_entity_key_id()
        if not partner_id:
            return False
        if self.user_id and partner_id == self.user_id:
            return False
        primary = next((d for d in self.devices if d.get("id") == device_id), None)
        if not primary:
            return False
        serial = primary.get("serial_number")
        if not serial:
            return False
        return len(self.devices) == 1 and serial == self.partner_device_serial

    def has_partner_for_device(self, device_id: str) -> bool:
        """Return whether the partner account was verified for this bed.

        The TRUST predicate. Deliberately unchanged by the existence split
        described in `has_partner_configured_for_device`, because three
        separate consumers depend on it meaning exactly this and one of
        them is the downgrade journal.

        `migrations._partner_recovery_renames` calls this, and the pairs it
        returns become the partner records that `async_revert_unique_ids`
        applies. Loosening this method would let an unverified partner's id
        reach those records, which is the cross-person biometric merge.
        Anything that wants "does a partner exist" wants the other
        predicate. Nothing that writes a journal record or performs a
        rename may use it.
        """
        if (
            self.partner_api_client is None
            or not self.partner_device_serial
            or not self.partner_mapping_valid
        ):
            return False
        # An account linked as its own partner is one person, not two.
        # `schedule_user_ids` has always guarded this and this did not,
        # so the insight and session entities were built twice for the
        # same human while the schedule entities were built once. Both
        # copies then wanted the same account-keyed id, one of them lost,
        # and the loser sat on a dead id nothing would ever claim again.
        partner_id = (self.partner_user or {}).get("id")
        if not isinstance(partner_id, str) or not partner_id:
            return False
        if self.user_id and partner_id == self.user_id:
            return False
        primary = next((d for d in self.devices if d.get("id") == device_id), None)
        if not primary:
            return False
        serial = primary.get("serial_number")
        if not serial:
            return False
        return len(self.devices) == 1 and serial == self.partner_device_serial

    # ── Live session and account configuration ────────────────────

    def live_session(self) -> dict:
        """Orion's own live-session record for the authenticated user."""
        return helpers.nested_mapping(self.data, "live_session", "response") or (
            helpers.nested_mapping(self.data, "live_session")
        )

    def server_says_in_bed(self) -> bool | None:
        """Whether Orion thinks this user is in bed right now.

        Deliberately separate from the topper's own occupancy reading.
        The two disagree, and the vendor's app trusts this one: it never
        reads `status_text` at all. Returns None when the field is
        missing so an absent answer is not mistaken for a no.
        """
        value = self.live_session().get("is_in_bed")
        return value if isinstance(value, bool) else None

    def sleep_config(self) -> dict:
        """Account-level configuration from /v1/sleep-configurations."""
        return helpers.nested_mapping(self.data, "sleep_config", "response") or (
            helpers.nested_mapping(self.data, "sleep_config")
        )

    def zone_split_mode(self) -> str | None:
        """Whether the two halves of the bed are driven as one.

        `combined` or `split`. Answers a question this project spent a
        long time unable to settle from the session payload alone.
        """
        value = self.sleep_config().get("zone_split_mode")
        return value if isinstance(value, str) and value else None

    def temperature_display_unit(self) -> str | None:
        """The scale the Orion app shows, `relative` or `fahrenheit`."""
        temperature = self.sleep_config().get("temperature")
        if not isinstance(temperature, dict):
            return None
        value = temperature.get("display_unit")
        return value if isinstance(value, str) and value else None

    def get_today_schedule(self, user_id: str | None = None) -> dict | None:
        """Today's sleep schedule row for one Orion user.

        ``user_id=None`` means the authenticated user, which is what every
        caller meant before per-person entities existed. The API returns
        rows for every user on the bed in a single fetch with the primary
        token, so reading a partner's row costs no extra request.
        """
        target = user_id or self.user_id
        if not target:
            return None
        row = helpers.nested_mapping(self.data, "schedules", "today_sleep_schedule").get(
            target
        )
        return row if isinstance(row, dict) else None

    def temperature_recommendations(self, user_id: str | None = None) -> list[dict]:
        """Orion Intelligence temperature recommendations for one user.

        Rides in the `/v1/sleep-schedules` response under
        `recommendations.{user_id}`, so it costs no extra request. Returns
        a list of recommendation items (possibly empty). Each measured item
        carries `bedtime_temp`, `phase_1_temp`, `phase_2_temp`,
        `wakeup_temp`, `thermal_classification`, `source`, `version`, and
        `created_at`.

        An EMPTY list is a real state, not an error: it means Orion has no
        recommendation for this user yet. The list being ABSENT entirely
        (the key missing) is different and the sensor reports unavailable
        for that.
        """
        target = user_id or self.user_id
        if not target:
            return []
        recs = helpers.nested_mapping(self.data, "schedules", "recommendations").get(
            target
        )
        return [item for item in recs if isinstance(item, dict)] if isinstance(
            recs, list
        ) else []

    def has_temperature_recommendations_key(self, user_id: str | None = None) -> bool:
        """Whether the recommendations key exists for this user at all.

        Distinguishes "the server returned a (possibly empty) list for this
        user" from "the key is absent", which is the availability signal:
        a count of 0 is a valid state, a missing key is unavailable.
        """
        target = user_id or self.user_id
        if not target:
            return False
        recs = helpers.nested_mapping(self.data, "schedules", "recommendations")
        return isinstance(recs.get(target), list)

    def account_device_id(self) -> str | None:
        """The bed that PRESENTS the account's own entities.

        Insights, schedules, the live session and the account
        configuration belong to the account, not to a bed, so their
        identity is the config entry. They still need somewhere to live
        in the device registry, and Home Assistant has no concept of an
        entity that belongs to an entry alone.

        Lowest id wins, deterministically. Using whatever the vendor
        array happened to list first meant a reordered response re-parented
        these entities to the other bed across a restart, and the
        equivalent bug in `_planned_renames` planned a rename onto an id
        the surviving row already held.

        This is presentation only. Nothing here reaches a unique_id, which
        is why selling this bed moves the entities to another one without
        touching their history.
        """
        return min((str(d["id"]) for d in self.devices if d.get("id")), default=None)

    def schedule_user_ids(self) -> list[str]:
        """Orion user ids this integration owns a schedule for.

        Derived from account identity, NOT from the schedule response.
        A failed schedule fetch leaves that response empty, and building
        the entity list from it would silently create zero entities after
        one transient error, with no recovery until a reload.

        Deliberately an allowlist. If the server ever returns a third id
        (a guest, or a stale ex-partner) we do not spawn a full entity
        family for someone we can neither name nor reliably write to.

        The partner half reads CONFIGURATION, not this session's trust
        verdict, and that is the same split `sensor.py` and
        `binary_sensor.py` already make when they decide whether a
        partner's entities EXIST.

        This function was left out of that fix and kept gating on
        `partner_mapping_valid` and the FETCHED `partner_user`. Both are
        empty on a cold start whose first partner request failed, so a
        single dropped connection at boot built no partner bedtime, wake
        up, temperature offset, schedule flag or override entities at all.
        Not unavailable. Absent, with every card and automation naming
        them broken, and no recovery short of reloading the entry by hand.
        The insight sensors survived that exact failure and the schedule
        entities did not, which is not a distinction anyone designed.

        `partner_entity_key_id` prefers the RECORDED partner account id,
        so the entities are keyed on durable configuration rather than on
        whatever the last response happened to say. Writes carry that same
        recorded id and travel on the PRIMARY token, so nothing here
        depends on the partner's own credentials working.

        What is NOT relaxed: a partner the server has positively named as
        a different account is still excluded. That is
        `partner_identity_rejected`, which only a completed check can set,
        so it separates "we were told this is someone else" from "we could
        not ask". Availability stays gated on `has_schedule_for_user`, so
        a person the bed does not return a row for reports unavailable and
        cannot be written to.
        """
        ids: list[str] = []
        if self.user_id:
            ids.append(self.user_id)

        partner_id = self.partner_entity_key_id()
        if (
            isinstance(partner_id, str)
            and partner_id
            and partner_id != self.user_id
            and self.partner_api_client is not None
            and self.partner_device_serial
            and not self.partner_identity_rejected
            # Same single-bed requirement the configuration predicate
            # applies. A partner is only ever linked against one shared
            # bed, and building their schedule against a second one would
            # mint entities for a bed they are not on.
            and len(self.devices) == 1
        ):
            ids.append(partner_id)
        return ids

    def has_schedule_for_user(self, user_id: str) -> bool:
        """Whether today's response carries a schedule row for this user.

        Read availability only. Reads use the primary token for everyone,
        so a partner whose own token has expired still has working
        schedule entities. That asymmetry is deliberate.
        """
        return self.get_today_schedule(user_id) is not None

    def schedule_day_for_user(self, user_id: str) -> int | None:
        """Today's day-of-week index from that person's own schedule row.

        Never computed locally. Devices carry their own ``timezone``, so a
        local weekday() would be wrong near midnight for anyone whose
        Home Assistant timezone differs from the bed's.
        """
        schedule = self.get_today_schedule(user_id)
        if not schedule:
            return None
        day = schedule.get("day")
        if isinstance(day, bool) or not isinstance(day, int):
            return None
        return day if day in range(7) else None

    # ── WebSocket integration ─────────────────────────────────────────

    @callback
    def _handle_ws_message(self, serial: str, msg_type: str, payload: dict[str, Any]) -> None:
        """Merge a ``live_device.{snapshot,update}`` frame into state.

        Called from the WS receive loop. Both event types carry the same
        payload shape, so we treat them identically: the payload IS the
        new live state for the device. We also extract the today's
        schedule timeline when present, since it arrives only via WS.
        """
        if msg_type not in ("live_device.snapshot", "live_device.update"):
            # Do not log vendor-controlled event names or payload keys.
            _LOGGER.debug(
                "Orion WS received an unsupported event for %s",
                helpers.short_id(serial),
            )
            return

        dev_id = self._serial_to_id.get(serial)
        if not dev_id:
            _LOGGER.debug(
                "Orion WS message for unknown serial %s; ignoring",
                helpers.short_id(serial),
            )
            return

        # Merge in place so any fields present in the prior snapshot that
        # aren't repeated in this frame are preserved. In practice the
        # server includes the full payload every time, so this is mostly
        # a belt-and-suspenders guard.
        previous = self.live_devices.get(dev_id, {})
        merged = {**previous, **payload}
        self.live_devices[dev_id] = merged

        # Stash the timeline (today's scheduled actions) on the coordinator
        # data so sensors can read it without polling /v1/sleep-schedules
        # more aggressively. Only live_device.update carries this field.
        data = dict(self.data or {})
        if msg_type == "live_device.update" and "timeline" in payload:
            timelines = dict(data.get("ws_timelines", {}))
            timelines[dev_id] = payload.get("timeline") or []
            data["ws_timelines"] = timelines
        self._async_push_without_rescheduling(data)

    @callback
    def apply_live_device(self, serial: str, payload: object) -> None:
        """Merge a live-device payload returned by a write into local state.

        `PUT /v1/devices/{serial}/live` returns the full live device
        object, so a successful write does not need a follow-up GET. The
        vendor app pipes the response straight into its store and we do
        the same. Same merge semantics as the WebSocket handler.
        """
        if not isinstance(payload, dict):
            return
        dev_id = self._serial_to_id.get(serial)
        if not dev_id:
            return
        previous = self.live_devices.get(dev_id, {})
        self.live_devices[dev_id] = {**previous, **payload}
        self._async_push_without_rescheduling(dict(self.data or {}))

    @callback
    def _async_push_without_rescheduling(self, data: dict) -> None:
        """Publish pushed state without moving the next poll.

        `async_set_updated_data` is documented as resetting the refresh
        interval, and it does: it unsubscribes the pending refresh and
        schedules a fresh one. The bed pushes a frame roughly every two
        seconds and the default scan interval is ten minutes, so calling
        it per frame meant `_async_update_data` never ran at all while a
        socket was healthy. Insights, schedules, the live session and the
        device list all froze at whatever the first poll returned, and the
        REST fallback this integration relies on became unreachable
        precisely when the socket was working.

        Publishing directly keeps the poll on its own clock.

        Deliberately does NOT touch `last_update_success` or
        `last_exception`. A frame proves one bed's live state is fresh. It
        says nothing about `/v2/insights`, the schedule endpoint, the live
        session, or the account still being authenticated, all of which are
        carried forward from the last successful poll. Marking the whole
        coordinator healthy on a frame made every insight sensor report
        available while serving days-old data, and erased the record of
        why the poll failed before anyone could read it.

        Entities that genuinely are socket-fed opt in through
        `OrionBaseEntity._live_fed` instead, which is a per-entity claim
        rather than a global one.
        """
        self.data = data
        self.async_update_listeners()

    def push_is_fresh(self, serial: str) -> bool:
        """Whether this bed's socket has delivered a frame recently."""
        return self._ws_manager.is_fresh(serial)

    @callback
    def _handle_ws_state(self, serial: str, state: str) -> None:
        """Log WS connection-state transitions for diagnostics."""
        _LOGGER.debug("Orion WS %s -> %s", helpers.short_id(serial), state)

    def ws_state(self, serial: str) -> str:
        """Return the current WS state for a device (for diagnostics)."""
        return self._ws_manager.state(serial)

    def ws_last_message_at(self, serial: str) -> float:
        """Monotonic timestamp of the most recent WS frame, or 0."""
        return self._ws_manager.last_message_at(serial)

    async def async_shutdown(self) -> None:
        """Stop the WS manager before the coordinator is disposed."""
        await self._ws_manager.async_stop()
        await super().async_shutdown()

    # ── Live per-sensor helpers (fed by the WebSocket stream) ─────────
    #
    # ``live_device.{snapshot,update}`` payloads expose two in-topper
    # sensors at ``status.sensors.sensor1`` and ``status.sensors.sensor2``.
    # The zone->sensor mapping (sensor1 ~ zone_a vs. zone_b) has not yet
    # been verified on the wire, so we key on the raw sensor name and let
    # the user map them to sides in their automations.
    #
    # Observed payload shape (see openapi.yaml WsSensor):
    #   status, status_text, heart_rate, breath_rate, sign_of_asleep,
    #   sign_of_wake_up, timestamp, uptime, is_working, firmware_version,
    #   hardware_version
    #
    # Observed status_text values: "left_bed" (nobody on the topper) and
    # "normal" (someone on it, readings tracking). heart_rate/breath_rate
    # use 255 as a "no reading yet" sentinel and 0 when the bed is empty.

    # Sentinel value the topper reports for HR/BR when it has no reading
    # yet (e.g. the first second or two after someone sits down).
    _SENSOR_SENTINEL = 255

    def _sensor_block(self, device_id: str, sensor_name: str) -> dict[str, Any] | None:
        """Return the raw sensor payload or None if not yet seen."""
        live = self.live_devices.get(device_id)
        if not live:
            return None
        sensors = (live.get("status") or {}).get("sensors") or {}
        block = sensors.get(sensor_name)
        if not isinstance(block, dict):
            return None
        return block

    def sensor_status_text(self, device_id: str, sensor_name: str) -> str | None:
        block = self._sensor_block(device_id, sensor_name)
        if not block:
            return None
        text = block.get("status_text")
        return text if isinstance(text, str) else None

    def sensor_is_on_bed(self, device_id: str, sensor_name: str) -> bool | None:
        """Return occupancy for one topper sensor.

        ``status_text == "left_bed"`` -> empty; any other value means a
        person is on the bed. If we've never seen a frame yet, return
        None so HA shows the sensor as unknown rather than guessing.
        """
        text = self.sensor_status_text(device_id, sensor_name)
        if text is None:
            return None
        return text != "left_bed"

    def sensor_heart_rate(self, device_id: str, sensor_name: str) -> int | None:
        """Return the live HR for one sensor, mapping sentinels to None.

        * ``0`` when the bed is empty -> None (the value would mislead
          automations looking at raw BPM).
        * ``255`` is the topper's "no reading yet" sentinel -> None.
        * Any other value is returned as-is.
        """
        block = self._sensor_block(device_id, sensor_name)
        if not block:
            return None
        hr = block.get("heart_rate")
        if not isinstance(hr, (int, float)):
            return None
        hr = int(hr)
        if hr == 0 or hr == self._SENSOR_SENTINEL:
            return None
        return hr

    def sensor_breath_rate(self, device_id: str, sensor_name: str) -> int | None:
        """Return the live breath rate for one sensor, with sentinel handling."""
        block = self._sensor_block(device_id, sensor_name)
        if not block:
            return None
        br = block.get("breath_rate")
        if not isinstance(br, (int, float)):
            return None
        br = int(br)
        if br == 0 or br == self._SENSOR_SENTINEL:
            return None
        return br

    def sensor_is_working(self, device_id: str, sensor_name: str) -> bool | None:
        block = self._sensor_block(device_id, sensor_name)
        if not block:
            return None
        val = block.get("is_working")
        return bool(val) if val is not None else None

    def sensor_diagnostics(
        self, device_id: str, sensor_name: str
    ) -> dict[str, Any] | None:
        """Raw per-sensor fields the entities otherwise hide or drop.

        Exists to diagnose a measured defect: the topper has been observed
        reporting ``status_text == "normal"`` on a side that was provably
        empty, so ``sensor_is_on_bed`` returns a false positive. The
        vendor's own app never reads ``status_text`` at all (zero hits
        across the decompiled bundle), so there is no upstream logic to
        copy and the replacement has to be designed from observation.

        ``heart_rate`` and ``breath_rate`` are returned RAW. The sensor
        entities map the ``0`` and ``255`` sentinels to None, which is
        right for automations but destroys the distinction between "empty
        bed" and "no reading yet" — exactly the distinction needed here.

        ``timestamp`` and ``uptime`` are deliberately excluded. They change
        on every frame (~2s), and attributes are written to the recorder,
        so including them would cost tens of thousands of rows a day to
        answer a question ``is_working`` already answers.
        """
        block = self._sensor_block(device_id, sensor_name)
        if not block:
            return None

        out: dict[str, Any] = {}
        for key in (
            "status",
            "is_working",
            "sign_of_asleep",
            "sign_of_wake_up",
        ):
            if key in block:
                out[key] = block[key]
        for key in ("heart_rate", "breath_rate"):
            if key in block:
                out[f"raw_{key}"] = block[key]
        return out or None

    def is_user_away(self, device_id: str) -> bool | None:
        """Check whether the authenticated user is away from this device.

        The server signals away mode by removing that user from the zone
        assignment returned by ``GET /v1/devices``. Other users may remain
        assigned, so checking whether any zone has any user gives the wrong
        answer on shared beds.

        This is **distinct from device power state** (``is_device_on``).
        The mattress can be powered off while the user is still present
        (e.g. outside the schedule window), so deriving away-mode from
        the power state produces a desynced switch and makes
        ``set_user_away(is_away=False)`` fail with
        ``400 "User has no previous device to return to"`` when the user
        was already present.
        """
        for device in self.devices:
            if device.get("id") != device_id:
                continue
            return util.user_is_away(device, self.user_id)
        return None

    def is_device_on(self, device_id: str) -> bool | None:
        """Check if the device is on.

        Reads the per-zone `on` field from the live snapshot
        (`GET /v1/devices/{serial}/live`). Returns True if any zone is
        on, False if all zones report off, and None if no live snapshot
        is available yet.
        """
        live = self.live_devices.get(device_id)
        if not live:
            return None
        zones = live.get("zones", [])
        if not zones:
            return None
        saw_any = False
        any_on = False
        for zone in zones:
            if "on" in zone:
                saw_any = True
                if zone.get("on"):
                    any_on = True
        return any_on if saw_any else None

    # ── Per-zone live state ───────────────────────────────────────────
    #
    # Two different zone lists live in the same snapshot and they do NOT
    # mean the same thing:
    #
    #   live["zones"][]           -> {id, on, temp}    temp is the SETPOINT
    #   live["status"]["zones"][] -> {id, temp,
    #                                 thermal_state}   temp is MEASURED
    #
    # Mixing them up silently reports the target as the actual, which
    # looks plausible on a dashboard and is wrong. Keep them separate.

    def zone_setpoint(self, device_id: str, zone_id: str) -> float | None:
        """Target temperature (°C) for one zone, from `live.zones[].temp`."""
        return live_state.zone_setpoint(self.live_devices.get(device_id), zone_id)

    def zone_measured_temp(self, device_id: str, zone_id: str) -> float | None:
        """Measured temperature (°C), from `live.status.zones[].temp`."""
        return live_state.zone_measured_temp(self.live_devices.get(device_id), zone_id)

    def zone_is_on(self, device_id: str, zone_id: str) -> bool | None:
        """Power state for one zone, from `live.zones[].on`."""
        return live_state.zone_is_on(self.live_devices.get(device_id), zone_id)

    def zone_thermal_relief(self, device_id: str, zone_id: str) -> dict | None:
        """Raw thermal-relief block for one zone, or None."""
        return live_state.zone_thermal_relief(self.live_devices.get(device_id), zone_id)

    def thermal_relief_until(self, device_id: str, zone_id: str) -> datetime | None:
        """When active hot flash relief on this zone ends, else None.

        Returns an aware datetime. Mirrors the app's own test: relief
        counts as running only when `end_time` is a finite number still
        in the future, so a stale block the server never cleared does
        not read as an active session.
        """
        relief = self.zone_thermal_relief(device_id, zone_id)
        end_ms = live_state.thermal_relief_end_ms(relief)
        if end_ms is None:
            return None
        ends = dt_util.utc_from_timestamp(end_ms / 1000)
        return ends if ends > dt_util.utcnow() else None

    def thermal_relief_active(self, device_id: str, zone_id: str) -> bool:
        """Whether hot flash relief is currently running on this zone."""
        return self.thermal_relief_until(device_id, zone_id) is not None

    def rapid_cool_duration(self, zone_id: str) -> int:
        """Minutes of cooling to request for this zone.

        Falls back to the default whenever nothing has been chosen yet,
        which is the state on a fresh install and after a restart if the
        slider has never been touched.
        """
        return helpers.clamp_cooling_minutes(
            self.rapid_cool_minutes.get(zone_id),
            DEFAULT_COOLING_MINUTES,
            MIN_COOLING_MINUTES,
            MAX_COOLING_MINUTES,
        )

    def set_rapid_cool_duration(self, zone_id: str, minutes: object) -> int:
        """Record the chosen window for a zone and return what was stored."""
        value = helpers.clamp_cooling_minutes(
            minutes, DEFAULT_COOLING_MINUTES, MIN_COOLING_MINUTES, MAX_COOLING_MINUTES
        )
        self.rapid_cool_minutes[zone_id] = value
        return value

    def zone_thermal_state(self, device_id: str, zone_id: str) -> str | None:
        """Raw `thermal_state` for one zone.

        Only ``"standby"`` has actually been observed on the wire; heating
        and cooling values are inferred from the field name and have never
        been captured. Callers must treat any unrecognised value as unknown
        rather than guessing a direction.
        """
        return live_state.zone_thermal_state(self.live_devices.get(device_id), zone_id)

    def device_zone_ids(self, device_id: str) -> list[str]:
        """Zone ids for a device, preferring the live snapshot.

        `GET /v1/devices` also carries a `zones[]`, but it is the
        *membership* list (zone -> user) and is not guaranteed to be
        ordered or populated the same way as the live runtime list.
        """
        live = self.live_devices.get(device_id) or {}
        ids = [z.get("id") for z in live.get("zones", []) or [] if z.get("id")]
        if ids:
            return ids
        for device in self.devices:
            if device.get("id") == device_id:
                return [z.get("id") for z in device.get("zones", []) or [] if z.get("id")]
        return []

    def zone_label(self, device_id: str, zone_id: str) -> str:
        """Human label for one zone, preferring the assigned user's name.

        Routes through `display_name_for_user` so a configured alias wins
        over the vendor's own first name, and so the credential filter in
        that method covers this path too. Falls back to a title-cased
        zone id.

        Deliberately NOT left/right. The device carries an `orientation`
        field, but the zone -> physical side mapping has never been
        verified against a split-occupancy capture, and a confidently
        mislabelled side is worse than a neutral one.
        """
        for device in self.devices:
            if device.get("id") != device_id:
                continue
            for zone in device.get("zones") or []:
                if not isinstance(zone, dict) or zone.get("id") != zone_id:
                    continue
                user = zone.get("user")
                if isinstance(user, dict):
                    user_id = user.get("id")
                    if isinstance(user_id, str) and user_id:
                        return self.display_name_for_user(user_id)
                    # No id means no alias key and no way to look the
                    # person up, so there is deliberately no fallback to
                    # orion_user_label here. A zone's embedded user object
                    # is the sparsest copy this API sends, which makes it
                    # the likeliest of all of them to fall through that
                    # chain to email or phone, and this label is slugified
                    # into a permanent entity_id.
                break
            break
        return zone_id.replace("_", " ").title()

    def zone_side(self, zone_id: str) -> str | None:
        """Left/right for one zone, from the CONF_ZONE_LEFT option.

        Unlike `zone_label`, this deliberately DOES answer left/right,
        because it is never slugified into an entity_id. It only labels a
        side-anchored controller (a bedside dial). The dangerous case the
        `zone_label` docstring guards against, a confidently mislabelled
        side burned into a permanent entity_id, cannot happen here: this is
        a user-set option that only feeds an attribute, and flipping it is
        a one-line options change with no migration.

        Returns None for a zone that is neither the configured left zone
        nor its opposite, so a three-zone bed (were one to exist) does not
        get a bogus side.
        """
        zone_left = self.config_entry.options.get(CONF_ZONE_LEFT, DEFAULT_ZONE_LEFT)
        zone_right = "zone_b" if zone_left == "zone_a" else "zone_a"
        if zone_id == zone_left:
            return "left"
        if zone_id == zone_right:
            return "right"
        return None

    # ── Device-level capabilities and diagnostics ─────────────────────

    def device_allowed_actions(self, device_id: str) -> set[str]:
        """UI capabilities the server exposes for this account.

        Sourced from `permissions.allowed_actions` on `GET /v1/devices`.
        These values determine which controls the app renders. They are not
        accepted verbatim by the `/action` endpoint.
        """
        for device in self.devices:
            if device.get("id") == device_id:
                perms = device.get("permissions") or {}
                return set(perms.get("allowed_actions") or [])
        return set()

    def device_quiet_mode(self, device_id: str) -> bool | None:
        """Quiet-mode state from the live snapshot."""
        live = self.live_devices.get(device_id)
        if not live or "quiet_mode" not in live:
            return None
        return bool(live.get("quiet_mode"))

    def device_led_brightness(self, device_id: str) -> int | None:
        """LED brightness (0-100) from the live snapshot."""
        return live_state.led_brightness(self.live_devices.get(device_id))

    def firmware(self, device_id: str) -> dict | None:
        """Return device firmware details from the live snapshot."""
        return live_state.firmware(self.live_devices.get(device_id))

    def device_online(self, device_id: str) -> bool | None:
        """Server-reported reachability for this device."""
        return live_state.device_online(self.live_devices.get(device_id))

    def device_timeline(self, device_id: str) -> list:
        """Today's scheduled actions, as pushed over the WebSocket.

        Only `live_device.update` carries this, never the snapshot, so it
        is empty until the first update frame arrives after a reconnect.
        """
        # Measured 2026-07-27: the server sends `timeline: []` even minutes
        # before a scheduled transition, with the socket confirmed live.
        # Nothing consumes this any more. It is kept because an empty
        # array is itself the finding, and diagnostics should show it.
        timelines = helpers.nested_mapping(self.data, "ws_timelines")
        entries = timelines.get(device_id)
        return entries if isinstance(entries, list) else []

    def pending_update_available(self, device_id: str) -> bool | None:
        """Return whether a firmware update is being advertised."""
        return live_state.pending_update_available(self.live_devices.get(device_id))

    def pending_update_info(self, device_id: str) -> dict | None:
        """Return the full pending-update block from the live snapshot."""
        return live_state.pending_update_info(self.live_devices.get(device_id))

    def firmware_update_info(self, device_id: str) -> dict | None:
        """Return the in-flight firmware update block."""
        return live_state.firmware_update_info(self.live_devices.get(device_id))

    def firmware_update_in_progress(self, device_id: str) -> bool:
        """Whether the device says a firmware update is running right now."""
        info = self.firmware_update_info(device_id) or {}
        return info.get("in_progress") is True

    def network_info(self, device_id: str) -> dict | None:
        """Return device network details from the live snapshot."""
        return live_state.network_info(self.live_devices.get(device_id))

    def wifi_rssi(self, device_id: str) -> int | None:
        """Return device Wi-Fi RSSI from the live snapshot."""
        return live_state.wifi_rssi(self.live_devices.get(device_id))

    def safety_info(self, device_id: str) -> dict | None:
        """Return device safety details from the live snapshot."""
        return live_state.safety_info(self.live_devices.get(device_id))

    def has_safety_error(self, device_id: str) -> bool | None:
        """Return whether the device reports a safety problem."""
        return live_state.safety_error(self.live_devices.get(device_id))
