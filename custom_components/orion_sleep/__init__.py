"""The Orion Sleep integration."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import (
    ConfigEntryError,
    ConfigEntryNotReady,
    HomeAssistantError,
)
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from orion_sleep_api import OrionApiClient

from .const import (
    CONF_ACCESS_TOKEN,
    CONF_ACCOUNT_ID,
    CONF_DEVICE_IDS,
    CONF_EXPIRES_AT,
    CONF_PARTNER_ACCESS_TOKEN,
    CONF_PARTNER_EXPIRES_AT,
    CONF_PARTNER_REFRESH_TOKEN,
    CONF_REFRESH_TOKEN,
    CONF_UID_RECOVERY_ACTIVE,
    DOMAIN,
)
from .coordinator import OrionDataUpdateCoordinator
from .helpers import _require_admin
from .migrations import (
    UnmigratedEntities,
    async_migrate_entry_identity,
    async_migrate_unique_ids,
    async_revert_unique_ids,
    partner_revert_blockers,
    unresolved_device_entries,
)

_LOGGER = logging.getLogger(__name__)

SERVICE_REVERT_UNIQUE_IDS = "revert_unique_ids"
SERVICE_RESUME_UNIQUE_IDS = "resume_unique_ids"
# Repair issue raised when a rename was declined. Keyed per entry, because
# two entries can be blocked by different things at the same time and a
# shared id would let whichever ran last erase the other's report.
ISSUE_UNIQUE_ID_CONFLICT = "unique_id_conflict"
# States Home Assistant will accept an unload from.
_UNLOADABLE_STATES = frozenset(
    {
        ConfigEntryState.LOADED,
        ConfigEntryState.SETUP_RETRY,
        ConfigEntryState.SETUP_ERROR,
    }
)
# States the recovery services will act from at all. Anything else is
# mid-transition and would race the forward migration over the same rows.
_RECOVERABLE_STATES = _UNLOADABLE_STATES | {ConfigEntryState.NOT_LOADED}
SERVICE_REVERT_SCHEMA = vol.Schema(
    {
        vol.Required("config_entry_id"): str,
        vol.Required("confirm"): vol.All(bool, vol.In([True])),
    }
)


def _partner_stale_error() -> HomeAssistantError:
    """The refusal for records no setup could vouch for.

    A DIFFERENT failure from the two below, with different words, because
    the user's situation is different and the generic message sent them to
    relink an account that had never changed.

    Nothing has been lost here. The partner's records are still in the
    journal, untouched, and a setup that reaches the server clears them
    for use.
    """
    return HomeAssistantError(
        "Orion could not confirm which account the partner tokens belong "
        "to on the last setup, so the partner's entity mappings were kept "
        "but not applied. Nothing has been lost. Run "
        "orion_sleep.resume_unique_ids to load 3.x again so the partner "
        "verifies, then run this once more before installing 2.x"
    )


def _partner_replacement_error(rows: tuple[str, ...]) -> HomeAssistantError:
    """The refusal for a replaced partner whose pre-3.0 rows survive.

    Needs the OPPOSITE instruction from the generic one below.

    Both refusals mean "a partner is linked and no partner mappings were
    recorded". Only this one also means the pre-3.0
    `{device}_partner_{key}` rows are still sitting on the old ids, and
    that changes everything about what the household should do.

    Reloading cannot resolve it. The journalling loop records a pair only
    when the old id is FREE, and these rows are occupying precisely those
    ids, so every reload lands back here. Telling somebody to reload is an
    instruction that can be followed forever without ever succeeding.

    And the rows are not empty. They hold the PREVIOUS partner's sleep and
    biometric history, because they were built by 2.x before the partner
    was replaced and the forward migration deliberately leaves role-keyed
    rows alone. Install 2.x with them in place and it writes the CURRENT
    partner's heart rate and apnea into them, which merges two people's
    history under one identity with every check green on the way there.

    So the remedy is deletion, it costs the previous partner's history,
    and saying so is the point. This message existed as a `migrations`
    WARNING that no household ever reads, while the service raised the
    generic text below. The refusal was right and the remedy it named was
    wrong.
    """
    # Capped. A real 2.x install built one of these per insight sensor, so
    # the full list runs to a couple of dozen and a service error that long
    # is not read at all. Enough to search the entity registry with, plus a
    # count so nobody stops after the ones that are named.
    shown = ", ".join(rows[:6])
    more = f" and {len(rows) - 6} more" if len(rows) > 6 else ""
    return HomeAssistantError(
        "Orion cannot prepare this entry for downgrade. It still holds "
        "pre-3.0 partner entities, and the partner account has been "
        "changed since they were written, so those entities hold the "
        "PREVIOUS partner's sleep and biometric history. Installing 2.x "
        "would write the current partner's readings into them and merge "
        "two people's history under one identity. Reloading will not clear "
        "this and running it again will not either. Delete these entities "
        f"from the entity registry: {shown}{more}. That permanently "
        "discards the previous partner's history, which is the cost of not "
        "merging it with the current partner's. Then reload Orion and run "
        "this again"
    )


def _partner_unmapped_error() -> HomeAssistantError:
    """The generic refusal, for a partner whose old ids are free.

    Reached only when no legacy partner rows are in the way, which is what
    makes "reload" a remedy that can actually work here: the old ids are
    free, so the next setup journals the current partner and the revert has
    records to apply.
    """
    return HomeAssistantError(
        "A partner account is linked but no partner entity mappings were "
        "recorded, so a downgrade would strand the partner's history on ids "
        "2.x never asks for. Reload Orion with "
        "orion_sleep.resume_unique_ids so the partner verifies, then run "
        "this again"
    )


PLATFORMS: list[Platform] = [
    Platform.BUTTON,
    Platform.CLIMATE,
    Platform.NUMBER,
    Platform.SELECT,
    Platform.SENSOR,
    Platform.BINARY_SENSOR,
    Platform.SWITCH,
    Platform.TIME,
    Platform.UPDATE,
]


async def async_setup(hass: HomeAssistant, _config: dict[str, Any]) -> bool:
    """Register recovery independently of any entry's health."""
    _async_register_recovery_service(hass)
    return True


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Move a 2.x entry to version 2, which is what fails a downgrade closed.

    The entry data is not touched. Nothing about it changed shape, and the
    version is doing a different job here than it usually does.

    Home Assistant refuses to load an entry whose stored version is HIGHER
    than the running integration's, and that refusal is the only
    mechanical protection against installing 2.x on a registry that has
    been re-keyed. Both 2.x and 3.0 shipped `VERSION = 1`, so that check
    could never fire, and an unprepared downgrade loaded normally: 2.x
    asked for its old unique ids, did not find them, and built a second
    entity for every key. History stayed on the 3.x rows while new data
    went to the replacements, splitting each person's record in two with
    nothing said about it.

    `revert_unique_ids` puts the version back to 1 as part of preparing a
    downgrade, so the supported path still works and only the unsupported
    one is blocked. That is the whole design: the guard is not there to
    stop downgrades, it is there to stop downgrades that skipped the step
    which makes them safe.
    """
    if entry.version < 2:
        hass.config_entries.async_update_entry(entry, version=2)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Orion Sleep from a config entry."""
    if entry.data.get(CONF_UID_RECOVERY_ACTIVE):
        raise ConfigEntryError(
            "Orion entity ids were prepared for a downgrade. Install 2.x, or "
            "run the orion_sleep.resume_unique_ids action to return to 3.x"
        )
    session = async_get_clientsession(hass)
    api_client = OrionApiClient(
        session=session,
        access_token=entry.data[CONF_ACCESS_TOKEN],
        refresh_token=entry.data[CONF_REFRESH_TOKEN],
        expires_at=entry.data[CONF_EXPIRES_AT],
    )

    # Register token refresh callback to persist new tokens
    expected_primary_refresh_token = entry.data[CONF_REFRESH_TOKEN]

    def on_token_refresh(
        access_token: str, refresh_token: str, expires_at: float
    ) -> None:
        nonlocal expected_primary_refresh_token
        if entry.data.get(CONF_REFRESH_TOKEN) != expected_primary_refresh_token:
            return
        expected_primary_refresh_token = refresh_token
        hass.config_entries.async_update_entry(
            entry,
            data={
                **entry.data,
                CONF_ACCESS_TOKEN: access_token,
                CONF_REFRESH_TOKEN: refresh_token,
                CONF_EXPIRES_AT: expires_at,
            },
        )

    api_client.set_token_refresh_callback(on_token_refresh)

    partner_api_client: OrionApiClient | None = None
    if entry.data.get(CONF_PARTNER_ACCESS_TOKEN):
        expected_partner_refresh_token = entry.data.get(CONF_PARTNER_REFRESH_TOKEN, "")
        partner_api_client = OrionApiClient(
            session=session,
            access_token=entry.data[CONF_PARTNER_ACCESS_TOKEN],
            refresh_token=entry.data.get(CONF_PARTNER_REFRESH_TOKEN, ""),
            expires_at=entry.data.get(CONF_PARTNER_EXPIRES_AT, 0),
        )

        def on_partner_token_refresh(
            access_token: str, refresh_token: str, expires_at: float
        ) -> None:
            nonlocal expected_partner_refresh_token
            if (
                entry.data.get(CONF_PARTNER_REFRESH_TOKEN)
                != expected_partner_refresh_token
            ):
                return
            expected_partner_refresh_token = refresh_token
            hass.config_entries.async_update_entry(
                entry,
                data={
                    **entry.data,
                    CONF_PARTNER_ACCESS_TOKEN: access_token,
                    CONF_PARTNER_REFRESH_TOKEN: refresh_token,
                    CONF_PARTNER_EXPIRES_AT: expires_at,
                },
            )

        partner_api_client.set_token_refresh_callback(on_partner_token_refresh)

    coordinator = OrionDataUpdateCoordinator(
        hass,
        entry,
        api_client,
        partner_api_client=partner_api_client,
    )
    # The first refresh starts a WebSocket client per device. If it, or
    # the platform setup after it, raises, Home Assistant never calls
    # async_unload_entry, because as far as it is concerned setup never
    # succeeded. Nothing would stop those sockets, and they reconnect on
    # a backoff forever with no config entry left to own them. A reauth
    # loop would stack a fresh set on every attempt.
    platforms_loaded = False
    completed = False
    try:
        await coordinator.async_config_entry_first_refresh()
        entry.runtime_data = coordinator
        device_ids = sorted(
            {
                str(device["id"])
                for device in coordinator.devices
                if isinstance(device, dict) and device.get("id")
            }
        )
        # Written BEFORE the sibling barrier below, deliberately. The
        # barrier waits on entries that have not yet named their beds, so
        # if nobody wrote first, two entries starting together would wait
        # on each other forever. A claim left behind by a setup that then
        # failed is harmless now, because the claim is not what decides
        # ownership. See `bed_owner`.
        identity_data = {CONF_DEVICE_IDS: device_ids}
        # Only ever populate an absent account id, never overwrite one.
        #
        # The two cases are verified by DIFFERENT rules, and an earlier
        # version of this comment claimed only the first one. A recorded id
        # is checked against the returned id, so overwriting it would ratify
        # a mismatch instead of surfacing it. An ABSENT id has no recorded
        # value to compare, so that check cannot fire, and this branch is
        # the only one that runs in exactly that case. Every pre-3.0 entry
        # reaches it, on the same boot that re-keys its registry rows.
        # `OrionDataUpdateCoordinator._async_setup` is what makes this write
        # safe: it requires the returned profile to carry the address the
        # entry was set up with before it lets setup get this far. Without
        # that, this line was trust on first use.
        if not entry.data.get(CONF_ACCOUNT_ID):
            identity_data[CONF_ACCOUNT_ID] = coordinator.user_id
        if any(entry.data.get(key) != value for key, value in identity_data.items()):
            hass.config_entries.async_update_entry(
                entry,
                data={**entry.data, **identity_data},
            )
        unresolved = unresolved_device_entries(hass, entry.entry_id)
        if unresolved:
            raise ConfigEntryNotReady(
                "Waiting for every Orion entry that is still starting to name "
                "its beds before any history is migrated"
            )
        # Identity first, then entities. Both refuse to run into another
        # entry, but only this order fails before anything is renamed: the
        # account collision used to be discovered after every entity had
        # already moved. The two are independent, so ordering is free.
        # `async_migrate_unique_ids` opens with its own bed-overlap check,
        # which is why there is no third copy of either here.
        async_migrate_entry_identity(hass, entry, coordinator)
        _migrate_unique_ids(hass, entry, coordinator)
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
        platforms_loaded = True
        # A fresh 3.0 install had no registry rows during the first pass.
        # Record the rows the platforms just created so downgrade recovery
        # works immediately rather than only after another restart.
        #
        # This pass, not the one above, decides the repair issue. It runs
        # last and therefore sees the final registry. A row the first pass
        # could not move still holds its old id here, so it is planned and
        # declined again, which means this result is never a smaller set
        # than the first one saw. A conflict that HAS cleared in between is
        # genuinely gone, and reporting it anyway would leave the user
        # staring at a repair for an entity that already migrated.
        _async_report_unique_id_conflicts(
            hass, entry, _migrate_unique_ids(hass, entry, coordinator)
        )
        completed = True
    finally:
        # NOT `except Exception`. CancelledError inherits from
        # BaseException, and Home Assistant cancels in-flight setup on
        # shutdown. The first refresh above is where the per-device sockets
        # start, so a cancellation there is exactly the leak this block
        # exists to prevent: sockets reconnecting on a backoff forever with
        # no config entry left to own them.
        if not completed:
            if platforms_loaded:
                await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
            await coordinator.async_shutdown()
            # A dead coordinator left in runtime_data is read by the
            # options flow, including the partner device-serial decision.
            if hasattr(entry, "runtime_data"):
                del entry.runtime_data

    # Reload on options change
    # Registered AFTER the migration, deliberately. The migration records
    # its rename map into options, and a listener attached before that
    # would see the write as a user options change and reload the entry
    # mid-setup.
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))

    return True


def _migrate_unique_ids(
    hass: HomeAssistant, entry: ConfigEntry, coordinator: OrionDataUpdateCoordinator
) -> list[str]:
    """Re-key what can be re-keyed, and report what could not.

    The whole point of this wrapper. `async_migrate_unique_ids` raises
    when something else already holds an id a rename needs, and that
    used to happen HERE, before `async_forward_entry_setups`. One
    squatted unique_id therefore took down all nine platforms: every
    climate control, every biometric sensor, and every automation
    referencing them, indefinitely, with no way out of it that did not
    involve hand-deleting entity registry rows named in a log line.

    The registry decision is unchanged and still correct. A rename onto
    an occupied id is refused rather than forced, because forcing it
    merges two entities' recorder history. What changed is that refusing
    a rename no longer refuses the integration. The entities that could
    not move keep working on their previous ids, and the conflict comes
    back as a repair issue the user can actually act on.
    """
    try:
        async_migrate_unique_ids(hass, entry, coordinator)
    except UnmigratedEntities as err:
        return err.conflicts
    return []


def _unique_id_conflict_issue(entry: ConfigEntry) -> str:
    return f"{ISSUE_UNIQUE_ID_CONFLICT}_{entry.entry_id}"


def _async_report_unique_id_conflicts(
    hass: HomeAssistant, entry: ConfigEntry, conflicts: list[str]
) -> None:
    """Surface declined renames in the UI, or clear a report that is stale.

    WARNING rather than ERROR on purpose. Nothing is broken: every
    entity in the conflict is still present, still updating, and still
    attached to whatever automations already reference it. The only loss
    is that its history is filed under a bed rather than an account, and
    a downgrade is the one operation that makes that visible.

    Not fixable in place, so `is_fixable` is False. The remedy is to
    delete a registry row that belongs to some other integration, or to
    run the revert action, and neither is something a repair flow can do
    on the user's behalf without deciding whose history to discard.

    The delete is not conditional on an issue existing. Home Assistant
    treats removing an absent issue as a no-op, and making it conditional
    would need a read of the issue registry whose only purpose is to
    skip a call that already does nothing.

    `entry.title` is deliberately NOT a placeholder here, and must not be
    added back. The config flow builds the title as
    `f"Orion Sleep ({self._auth_value})"`, so it carries the literal email
    address or phone number that was typed at setup. That string is a
    login credential: `diagnostics.py` redacts it three separate ways
    (`auth_value`, `email`, `phone`) and `helpers.short_id` exists purely
    to keep it out of log lines.
    Repair issues are not a safe place to put one.
    `repairs/list_issues` in
    `homeassistant/components/repairs/websocket_api.py` carries no
    `require_admin` decorator, unlike `RepairsFlowIndexView.post` in the
    same file, and it returns `translation_placeholders` verbatim to any
    authenticated user. That is the non-admin household member every
    admin check in this integration is written around, and the repairs
    dashboard is a page people screenshot when they ask for help.
    Nothing is lost by dropping it. The issue id is already scoped per
    entry and the issue is already scoped to this domain, so the title
    only ever restated which integration raised it.
    """
    if conflicts:
        ir.async_create_issue(
            hass,
            DOMAIN,
            _unique_id_conflict_issue(entry),
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key=ISSUE_UNIQUE_ID_CONFLICT,
            translation_placeholders={
                # Newline separated, because these are entity ids and a
                # comma joined run of them is unreadable at the width the
                # repairs dialog gives you.
                "conflicts": "\n".join(f"- {line}" for line in conflicts),
            },
        )
        return
    ir.async_delete_issue(hass, DOMAIN, _unique_id_conflict_issue(entry))


async def _async_options_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload only when options changed, not when tokens were persisted.

    Every guard here fails CLOSED, meaning an unclear situation declines
    to reload. That direction is not a preference. This listener is
    dispatched as a task by `async_update_entry` rather than awaited, so
    it can and does run after the entry has already left LOADED, and the
    thing it would collide with is `async_revert_unique_ids` renaming the
    same registry rows.
    """
    if entry.data.get(CONF_UID_RECOVERY_ACTIVE):
        # The recovery services own the entry lifecycle while this is set,
        # and this listener is scheduled BY the write that sets it.
        #
        # `_handle_revert` sets the latch before unloading, precisely so
        # that nothing restarts the forward migration in the gap. That
        # write goes through `async_update_entry`, which dispatches update
        # listeners as tasks instead of awaiting them, so the guard being
        # set is itself what schedules this coroutine. It then ran against
        # an entry the service was in the middle of unloading and
        # reverting. Reloading before the `finally` pops the latch makes
        # setup raise and leaves the entry broken. Reloading after it
        # succeeds and runs the forward migration on top of a half
        # finished revert, which is the two-generation registry the
        # migration documents as the one shape a revert cannot resolve.
        return

    coordinator: OrionDataUpdateCoordinator | None = getattr(
        entry, "runtime_data", None
    )
    if coordinator is None:
        # A missing coordinator is a reason NOT to reload, never a reason
        # to reload. Home Assistant clears `runtime_data` as soon as an
        # entry leaves LOADED, so None here means the entry is unloaded or
        # mid-transition. The previous shape conditioned every early
        # return on `coordinator is not None`, so exactly that case fell
        # through all three guards into the reload.
        #
        # Nothing is lost by returning. An entry that is not loaded has no
        # running coordinator to reconfigure, and whatever unloaded it is
        # responsible for what happens next.
        return
    if entry.options == coordinator.options or coordinator.reload_started:
        return

    coordinator.reload_started = True
    try:
        if not await hass.config_entries.async_reload(entry.entry_id):
            coordinator.reload_started = False
    except Exception:
        coordinator.reload_started = False
        raise


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        coordinator: OrionDataUpdateCoordinator | None = getattr(
            entry, "runtime_data", None
        )
        if coordinator is not None:
            # Close the live-device WebSockets cleanly (code 1001), matching
            # the Android app's background-shutdown behavior.
            await coordinator.async_shutdown()
    return unloaded


def _async_register_recovery_service(hass: HomeAssistant) -> None:
    """Expose the one-click undo for the 3.0 entity re-key.

    Domain-level and idempotent. Rolling back to 2.x leaves every re-keyed
    entity unavailable with its history stranded on it, because 2.x asks
    for ids that no longer exist and builds fresh ones instead. It must run
    under 3.x before Home Assistant is restarted into 2.x.
    """
    async def _admin_entry(call: ServiceCall) -> ConfigEntry:
        """Resolve the target entry, refusing anyone who is not an admin.

        The admin rule itself lives in `helpers._require_admin`, which is
        also what gates every entity service on the sensor platform. This
        used to hold its own copy of the same twelve lines. Two copies of
        an authorisation rule is one copy too many: a future change to it
        reaches one call site and misses the other, and the one it misses
        is the one nobody notices until it is being exploited.

        Read `helpers._require_admin` for why a call with no
        `context.user_id` is ALLOWED. Short version: that is Home
        Assistant itself, which is how every automation, script and scene
        calls a service, and denying it made the only supported rollback
        off the 3.x entity ids impossible to script for no security gain.
        """
        await _require_admin(hass, call.context)

        entry = hass.config_entries.async_get_entry(call.data["config_entry_id"])
        if entry is None or entry.domain != DOMAIN:
            raise HomeAssistantError("Orion config entry not found")
        return entry

    async def _handle_revert(call: ServiceCall) -> None:
        entry = await _admin_entry(call)
        # Refuse rather than race. `_UNLOADABLE_STATES` deliberately omits
        # SETUP_IN_PROGRESS because Home Assistant will not unload then,
        # but treating "cannot unload" as "safe to proceed" ran the reverse
        # registry transaction against rows the forward migration was
        # renaming at that moment.
        if entry.state not in _RECOVERABLE_STATES:
            raise HomeAssistantError(
                f"Orion is currently {entry.state}. Wait for it to settle, "
                "then run this again"
            )

        # Read BEFORE the guard is armed below, because arming it makes a
        # second run indistinguishable from a first one.
        #
        # A completed revert rewrites the journal as `remaining + stale`,
        # which on success is empty. So running this action again finds
        # nothing to do and takes the "nothing changed" exit, whose
        # `finally` pops the latch because this run prepared nothing. The
        # message said "Nothing changed, and nothing is prepared for a
        # downgrade" while every entity sat on its 2.x id, and popping the
        # latch un-prepared the entry: starting 3.x then re-ran the forward
        # migration and put everything back on 3.x ids, silently undoing
        # the preparation the household had just been told to trust.
        #
        # Running it twice is not exotic. It is what someone does to
        # confirm the first run worked.
        was_prepared = bool(entry.data.get(CONF_UID_RECOVERY_ACTIVE))

        # Set the guard before unloading. A pending retry or an update
        # listener must not start the forward migration between unload and
        # the reverse registry transaction.
        hass.config_entries.async_update_entry(
            entry,
            data={**entry.data, CONF_UID_RECOVERY_ACTIVE: True},
        )
        prepared = False
        try:
            # Also unload from SETUP_RETRY and SETUP_ERROR. Guarding on
            # LOADED alone left a pending retry timer armed and running
            # against the registry while the reverse transaction was in
            # flight.
            if entry.state in _UNLOADABLE_STATES:
                unloaded = await hass.config_entries.async_unload(entry.entry_id)
                if not unloaded:
                    raise HomeAssistantError(
                        "Could not unload Orion before recovery"
                    )

            # PREFLIGHT. Every partner refusal is decided here, from pure
            # reads, before a single row moves.
            #
            # These three used to be evaluated on the RETURNED
            # `RevertResult`, which meant every one of them raised AFTER
            # `async_revert_unique_ids` had renamed rows and rewritten the
            # journal. The refusal was correct and the state it described
            # no longer existed by the time the household read it.
            #
            # `partner_stale` is the one where that is actively dangerous
            # rather than merely confusing. The revert applies non-stale
            # records and skips stale ones, so a journal holding both,
            # which is exactly the split-brain state
            # `_partner_recovery_renames` documents, got a PARTIAL partner
            # rename and only then a refusal. Half of one person's entities
            # land on the role-keyed ids 2.x feeds from the other person,
            # and the message tells the user to go fix something while the
            # registry has already been half moved.
            #
            # The order below is stale, then replacement, then generic, and
            # it now sits ahead of the `result.complete` check rather than
            # straddling it. That is deliberate. The argument the
            # `partner_stale` branch already makes applies unchanged to the
            # other two: every partner remedy ends in a reload, a reload
            # re-runs the forward migration, and a registry conflict that
            # has cleared in the meantime then resolves itself and never
            # has to be reported at all. Resolving a registry conflict does
            # nothing whatsoever for a partner problem, so the reverse
            # ordering has no equivalent benefit. The replacement case also
            # outranks a squatted row on danger alone: one is a
            # cross-person history merge, the other is a name collision.
            blockers = partner_revert_blockers(hass, entry)
            if blockers.partner_stale:
                raise _partner_stale_error()
            if blockers.partner_rows_outrank_journal:
                raise _partner_replacement_error(blockers.legacy_partner_entity_ids)
            if blockers.partner_unmapped and blockers.pending_renames:
                # Gated on there being work to do, and the gate is what
                # keeps this refusal off households in no danger.
                #
                # Plain `partner_unmapped` was deliberately never added to
                # the "nothing recorded to undo" early return below,
                # because an entry whose journal is empty because nothing
                # ever migrated would then be refused. Running this action
                # speculatively is a supported thing to do.
                #
                # A preflight has no `reverted` to gate on, so moving the
                # refusal here without a gate would have reintroduced
                # exactly that. `pending_renames` is the same gate computed
                # ahead of time: it counts non-stale records whose 3.x id a
                # row of ours currently holds, which is precisely the set
                # the loop turns into `reverted` plus `remaining`. Zero of
                # them means the run below would have taken the early
                # return, so this one does not fire either.
                raise _partner_unmapped_error()

            result = async_revert_unique_ids(hass, entry)
            # `partner_stale` is in this condition, and leaving it out was a
            # silent data-withholding bug rather than a cosmetic one.
            #
            # Stale records count in NEITHER `reverted` nor `remaining`.
            # That is deliberate and documented on `RevertResult`: nothing
            # is blocking them, so counting them as remaining would send
            # the user hunting for a squatted registry row that does not
            # exist. The consequence here was that a journal holding only
            # stale records satisfied this early return and the
            # `partner_stale` branch below became unreachable.
            #
            # The sequence is ordinary, not contrived. Revert once with
            # primary records plus stale partner records. The primaries
            # revert, so `reverted > 0`, and the partner branch raises.
            # `async_revert_unique_ids` then rewrites the journal as
            # `remaining + stale`, which at that point is stale partner
            # records and nothing else. The user does the obvious thing and
            # runs the action a second time. Now `reverted == 0` and
            # `remaining == 0`, so they were told "No recorded Orion renames
            # to undo. Nothing changed, and nothing is prepared for a
            # downgrade" while the partner's only rollback record was being
            # withheld. Told nothing is withheld, at the exact moment
            # something is, and in flat contradiction to the message the
            # same action gave them thirty seconds earlier.
            # `partner_rows_outrank_journal` is in this condition for the
            # same reason `partner_stale` is, and it arrives by the same
            # route. It counts in NEITHER `reverted` nor `remaining`,
            # because the thing it describes is a set of registry rows this
            # version never journalled and never renames.
            #
            # The sequence, and it is the ordinary one rather than a
            # contrived one. A household with a replaced partner runs the
            # action. The primary records revert, so `reverted > 0`, and the
            # replacement branch below raises with the list of rows to
            # delete. `async_revert_unique_ids` has already rewritten the
            # journal as `remaining + stale`, which is now empty. The
            # household does not delete the rows, for whatever reason, and
            # runs the action again. Second time through `reverted == 0`,
            # `remaining == 0` and `partner_stale` is False, so without this
            # clause they are told "No recorded Orion renames to undo.
            # Nothing changed, and nothing is prepared for a downgrade" and
            # the refusal they saw thirty seconds ago never appears again.
            #
            # The legacy rows are still sitting in the registry at that
            # moment, still holding the PREVIOUS partner's history, and 2.x
            # will still write the current partner's readings into them. The
            # danger here does not live in the journal, so an empty journal
            # says nothing about whether it is gone.
            if not (
                result.reverted
                or result.remaining
                or result.partner_stale
                or result.partner_rows_outrank_journal
            ):
                # Nothing recorded to undo. Not an error, but the entry
                # must not stay latched: running the action speculatively
                # would otherwise make 3.x refuse to load forever.
                #
                # RETURNS rather than falling through, and that is the
                # whole point. The success log at the end of this handler
                # used to be shared by every path, so this one announced
                # "Reverted 0 Orion entities. The entry is unloaded and
                # ready for downgrade" immediately after saying nothing
                # had changed. Both lines were wrong, they contradicted
                # each other, and the second one told a user it was safe
                # to install 2.x when every entity was still on a 3.x id.
                #
                # The `finally` below still runs on the way out, so the
                # latch is popped exactly as it would be on any other
                # non-prepared exit.
                if was_prepared:
                    # Already done, by an earlier run of this same action.
                    # `prepared` is set so the `finally` leaves the latch
                    # alone: popping it here would re-arm the forward
                    # migration and put every entity back on a 3.x id.
                    prepared = True
                    _LOGGER.info(
                        "Orion entity ids were already prepared for a "
                        "downgrade by an earlier run, and nothing further "
                        "was needed. The entry is unloaded. Install 2.x, or "
                        "run resume_unique_ids to go back to 3.x"
                    )
                    return
                _LOGGER.info(
                    "No recorded Orion renames to undo. Nothing changed, and "
                    "nothing is prepared for a downgrade. If this entry has "
                    "3.x entity ids, reload Orion so the migration records "
                    "them, then run this again"
                )
                return
            # SECOND LINE OF DEFENCE. The preflight above already raised on
            # each of these, computed from the same journal and the same
            # registry by the same function `async_revert_unique_ids` now
            # calls, so none of them can be true here while the preflight
            # sits where it does.
            #
            # They are kept rather than deleted for two reasons. A future
            # edit that moves, weakens or short-circuits the preflight
            # would otherwise turn a refusal into a silent success, which
            # is the failure mode this whole area treats as worst-case, and
            # these branches degrade it back to the old behaviour of
            # refusing late instead. And they are the only place the
            # ordering argument against `result.complete` is written down,
            # which is knowledge worth more than the three lines it costs.
            #
            # `test_partner_replacement_message_real.py` also drives these
            # from a stubbed `RevertResult` on purpose, because the generic
            # unmapped cause cannot be produced end to end: with no legacy
            # rows in the registry the old ids are free, so setup journals
            # the current partner and the flag is false by construction.
            if result.partner_stale:
                # Checked BEFORE `result.complete`, and the order is
                # load-bearing in two ways.
                #
                # First, reachability. A stale-only journal produces
                # `remaining == 0`, so `complete` is True and this branch
                # was reached only because primary work also happened. The
                # early return above used to swallow the stale-only case
                # entirely. Putting this first means the stale condition is
                # evaluated on its own merits rather than as a leftover of
                # whatever the primary records did.
                #
                # Second, and this is the part that changes an existing
                # outcome: when a run is BOTH incomplete and carrying stale
                # partner records, the incomplete message is the one that
                # used to win. That message talks only about conflicting
                # registry rows. It never mentions that a second, unrelated
                # set of mappings was also withheld, so the user resolves
                # the conflict, runs the action again, and only then
                # discovers the partner problem. Two withholdings, reported
                # one per attempt, with the first message implying it named
                # the only thing standing in the way.
                #
                # Reporting the partner first is also the cheaper
                # instruction. Clearing this is one service call plus a
                # reload, which a user can run unattended, whereas clearing
                # `remaining` means hand-deleting entity registry rows. The
                # reload it asks for re-runs the forward migration, so a
                # registry conflict that has cleared in the meantime
                # resolves itself and never has to be reported at all. The
                # reverse ordering has no equivalent benefit: resolving a
                # conflict does nothing whatsoever for a stale partner.
                raise _partner_stale_error()
            if not result.complete:
                raise HomeAssistantError(
                    f"Orion recovery is incomplete: {result.remaining} entity "
                    "mappings could not be returned to their pre-3.0 ids. "
                    "Resolve the conflicting entity registry rows and run this "
                    "again before installing 2.x, or run "
                    "orion_sleep.resume_unique_ids to go back to 3.x"
                )
            if result.partner_rows_outrank_journal:
                raise _partner_replacement_error(result.legacy_partner_entity_ids)
            if result.partner_unmapped:
                # Inside the try, so the latch rolls back like every other
                # failure. It used to sit after the handler, which left the
                # entry unloadable while telling the user to reload it.
                #
                # Deliberately NOT gated on `pending_renames` the way the
                # preflight copy is. By the time this line is reached the
                # early return above has already proved that `reverted` or
                # `remaining` is non-zero, which is the same gate arrived at
                # by the other route.
                raise _partner_unmapped_error()
            # Put the entry back on version 1, which is what lets 2.x load
            # it at all. `async_migrate_entry` raises the version so an
            # UNPREPARED downgrade fails closed, and this is the other end
            # of that: preparing a downgrade is exactly when 2.x is
            # supposed to be able to take the entry.
            #
            # Last, after every refusal above. A run that raised must leave
            # the guard standing, because the registry it would have been
            # protecting is still on 3.x ids.
            if entry.version != 1:
                hass.config_entries.async_update_entry(entry, version=1)
            prepared = True
        finally:
            # The latch survives only a run that genuinely left the entry
            # ready for 2.x. Anything else, including doing nothing, has to
            # leave 3.x able to load.
            if not prepared:
                data = dict(entry.data)
                data.pop(CONF_UID_RECOVERY_ACTIVE, None)
                hass.config_entries.async_update_entry(entry, data=data)
        # Only reachable once `prepared` is True. Every other path either
        # raised or returned above, so these two lines now describe a run
        # that genuinely left the entry ready for 2.x rather than being the
        # shared tail of every outcome including the ones that did nothing.
        if not result.identity_restored:
            # Cosmetic to 2.x, so it does not fail the run. Worth saying,
            # because the entry will not be recognised by its typed address.
            _LOGGER.warning(
                "Orion entities are ready for downgrade, but this entry kept "
                "its account-based identity because another entry already "
                "holds the address it was set up with"
            )
        _LOGGER.info(
            "Reverted %d Orion entities to their pre-3.0 ids. The entry is "
            "unloaded and ready for downgrade",
            result.reverted,
        )

    async def _handle_resume(call: ServiceCall) -> None:
        entry = await _admin_entry(call)
        # No bed-ownership check here, deliberately. There used to be one,
        # and it was the second dead end in a row for the same entry.
        #
        # This action exists to get OUT of recovery mode. While the latch
        # is set, `async_setup_entry` refuses and `revert_unique_ids` has
        # nothing left to undo, so refusing here too left the non-owner
        # entry latched with no supported way back, which is precisely the
        # state this action was added to make escapable.
        #
        # The reload below still enforces it. `coordinator._async_update`
        # and `async_migrate_unique_ids` both apply the same
        # `resolve_bed_owner` rule during setup, so a non-owner does not
        # quietly start. The copy that used to be here added no enforcement
        # at all. It only changed WHICH error the user saw, and it fired
        # before the latch was popped, so the entry stayed stuck behind an
        # error whose only instruction was to run the action that had just
        # refused.
        #
        # There is no account-identity check here either, for exactly the
        # same reason, and it was removed second because it had exactly the
        # same shape. `async_migrate_entry_identity` already performs it,
        # during the reload below, with a message that says to remove the
        # duplicate entry. The copy here raised before the latch was
        # popped, so a latched entry whose account is claimed elsewhere hit
        # it on every attempt and stayed latched permanently. Two dead ends
        # in a row in the one action whose entire purpose is to be the way
        # out.
        data = dict(entry.data)
        data.pop(CONF_UID_RECOVERY_ACTIVE, None)
        hass.config_entries.async_update_entry(entry, data=data)
        # The entry version is deliberately NOT written back here.
        # `revert_unique_ids` lowered it to 1 so 2.x could take the entry,
        # and the reload below runs `async_migrate_entry`, which raises it
        # again. Writing it here as well would be a second copy of that
        # rule, in the one file that already carries the argument for why
        # duplicated recovery logic is how these two ends drift apart.
        #
        # Reload, not setup. `async_setup` refuses anything that is not
        # NOT_LOADED, and the likeliest way to reach this action is to
        # prepare a downgrade, restart Home Assistant, and change your
        # mind. That restart leaves the entry in SETUP_ERROR, where the
        # documented escape hatch used to raise OperationNotAllowed and
        # strand the entry with no supported way back.
        if entry.state is not ConfigEntryState.LOADED:
            if not await hass.config_entries.async_reload(entry.entry_id):
                raise HomeAssistantError("Orion could not resume 3.x setup")

    if not hass.services.has_service(DOMAIN, SERVICE_REVERT_UNIQUE_IDS):
        hass.services.async_register(
            DOMAIN,
            SERVICE_REVERT_UNIQUE_IDS,
            _handle_revert,
            schema=SERVICE_REVERT_SCHEMA,
        )
    if not hass.services.has_service(DOMAIN, SERVICE_RESUME_UNIQUE_IDS):
        hass.services.async_register(
            DOMAIN,
            SERVICE_RESUME_UNIQUE_IDS,
            _handle_resume,
            schema=SERVICE_REVERT_SCHEMA,
        )
