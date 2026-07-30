"""Config flow for Orion Sleep integration."""

from __future__ import annotations

import logging
from typing import Any
from uuid import uuid4

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from orion_sleep_api import (
    OrionApiClient,
    OrionApiError,
    OrionAuthError,
    OrionConnectionError,
    util,
)

from . import helpers
from .const import (
    CONF_ACCESS_TOKEN,
    CONF_ACCOUNT_ID,
    CONF_ALLOW_UNVERIFIED_ACCOUNT,
    CONF_API_KEY,
    CONF_AUTH_METHOD,
    CONF_AUTH_VALUE,
    CONF_DEVICE_IDS,
    CONF_DISPLAY_ALIASES,
    CONF_EXPIRES_AT,
    CONF_INSIGHTS_DAYS,
    CONF_PARTNER_ACCESS_TOKEN,
    CONF_PARTNER_ACCOUNT_ID,
    CONF_PARTNER_API_KEY,
    CONF_PARTNER_AUTH_METHOD,
    CONF_PARTNER_AUTH_VALUE,
    CONF_PARTNER_CONFIGURED,
    CONF_PARTNER_DEVICE_SERIAL,
    CONF_PARTNER_EXPIRES_AT,
    CONF_PARTNER_REFRESH_TOKEN,
    CONF_PARTNER_REPLACED,
    CONF_PARTNER_REVISION,
    CONF_REFRESH_TOKEN,
    CONF_SCAN_INTERVAL,
    CONF_UID_MIGRATION,
    CONF_ZONE_LEFT,
    DEFAULT_ALLOW_UNVERIFIED_ACCOUNT,
    DEFAULT_INSIGHTS_DAYS,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_ZONE_LEFT,
    DOMAIN,
)
from .coordinator import profile_carries_address, recorded_account_id

# Module scope. `overlapping_entry_ids` used to be imported inside
# `async_step_verify`, beside a module-scope import from the very same
# module, which reads as a deliberate cycle break and is not one.
# `migrations` imports `helpers`, `const` and `descriptions` only.
from .migrations import evict_partner_journal, overlapping_entry_ids

_LOGGER = logging.getLogger(__name__)

AUTH_METHOD_EMAIL = "email"
AUTH_METHOD_PHONE = "phone"
AUTH_METHOD_API_KEY = "api_key"


class OrionSleepConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Orion Sleep."""

    # 2 since 3.1. See `async_migrate_entry` for why this is the downgrade
    # guard rather than a statement about the shape of `entry.data`, which
    # has not changed.
    VERSION = 2

    def __init__(self) -> None:
        """Initialize the config flow."""
        self._auth_method: str | None = None
        self._auth_value: str | None = None
        self._reauth_entry: ConfigEntry | None = None

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OrionSleepOptionsFlow:
        """Return the options flow handler."""
        return OrionSleepOptionsFlow(config_entry)

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Step 1: User picks login method (email, phone, or API key)."""
        if user_input is not None:
            self._auth_method = user_input[CONF_AUTH_METHOD]
            if self._auth_method == AUTH_METHOD_EMAIL:
                return await self.async_step_email()
            if self._auth_method == AUTH_METHOD_API_KEY:
                return await self.async_step_api_key()
            return await self.async_step_phone()

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_AUTH_METHOD, default=AUTH_METHOD_EMAIL): vol.In(
                        {
                            AUTH_METHOD_EMAIL: "Email",
                            AUTH_METHOD_PHONE: "Phone",
                            AUTH_METHOD_API_KEY: "API key",
                        }
                    ),
                }
            ),
        )



    @callback
    def _async_reauth_account_matches(self, profile: dict | None) -> bool:
        """Whether these credentials belong to the entry being reauthed.

        Decorated `@callback` rather than renamed. In Home Assistant an
        `_async_` prefix on a plain `def` reads as a coroutine somebody
        forgot to await, and a caller acting on that assumption gets a
        truthy object back from an identity check that is supposed to be
        able to say no. The decorator states the real contract, which is
        that this is synchronous and safe to call from the event loop.

        The first version of this compared the entry's unique_id to the
        account id and gave up when the unique_id still held the typed
        address, reasoning that there was nothing to compare. Every entry
        created before 3.0 is keyed exactly that way, because the flow set
        `unique_id = auth_value.lower()` and stored the same string in
        `CONF_AUTH_VALUE`. So the early return fired for one hundred
        percent of the installs that would ever run this check, and the
        guard did nothing for anyone.

        There is something to compare. The profile carries the account's
        own email and phone, so match those against the address the entry
        was set up with.

        Identity must be positively established. A timeout after Orion
        accepts the verification code cannot be treated as permission to
        replace another account's credentials.

        READS `recorded_account_id`, NOT `entry.unique_id`. The account
        identity is recorded twice and the two copies were guarded
        separately. This method used to compare `unique_id` while the
        coordinator compared `CONF_ACCOUNT_ID`, and the reauth write below
        then overwrote `CONF_ACCOUNT_ID` through a dict spread, having
        validated only the other field. That is the "never overwrite a
        recorded account id" invariant `__init__.py` states, violated two
        modules away, and it was harmless only because the bed-overlap
        check happened to reject the mismatch first. One accessor, read by
        both layers, is what stops the two drifting again.

        It also removes a heuristic that no longer has to exist. Deciding
        whether `unique_id` held an account id or a typed address meant
        testing `existing != typed`, which is a guess. `CONF_ACCOUNT_ID`
        means exactly one thing on every entry, and absent means absent.

        DELEGATES the address comparison to `profile_carries_address`
        rather than reimplementing it. The copy that used to live here and
        the coordinator's canonical version were the same rule written
        twice, and the bypass option made them genuinely divergent: setup
        could be relaxed by it and reauth could not. A household that set
        the option could load the entry and still not reauthenticate, which
        defeats the escape hatch in the one case it is needed most. Expired
        tokens mean setup cannot run at all, so reauth is the ONLY door,
        and a reauth that refuses is a locked-out household with nowhere
        left to go.

        The option relaxes exactly what it relaxes in the coordinator, and
        no more. A recorded account id is a real reference value, a
        mismatch against it is a real finding, and ratifying that is the
        identity swap that moves one person's sleep history onto another
        person. The recorded branch below never consults the option. A
        missing or empty profile never reaches the option either, because
        accepted credentials are not evidence of whose they are.
        """
        entry = self._reauth_entry
        if entry is None or not isinstance(profile, dict) or not profile:
            return False

        account_id = profile.get("id")
        recorded = recorded_account_id(entry)
        typed = (entry.data.get(CONF_AUTH_VALUE) or "").strip().lower()

        if recorded is not None:
            # A real reference value exists. Compare against it and stop.
            # No option may widen this branch.
            return bool(account_id) and recorded == account_id

        # No recorded account id, which is every pre-3.0 entry. The
        # address the entry was set up with is the only reference value
        # left, and it is the same one the coordinator falls back to.
        if profile_carries_address(profile, typed):
            return True

        # The escape hatch, and the reason setup and reauth now agree.
        # Requires a usable account id even here, because the coordinator's
        # empty-id guard is not something this option relaxes either.
        # `isinstance` first. `bool(account_id)` was the leading term and
        # is the weaker test: it passes for any truthy value, including a
        # dict or a list the vendor might return in that field, and the
        # real requirement is that the id is a usable string. Ordering the
        # weak guard first meant the strong one only ever ran as a
        # formality after the answer was already decided.
        return isinstance(account_id, str) and bool(account_id) and (
            entry.options.get(
                CONF_ALLOW_UNVERIFIED_ACCOUNT, DEFAULT_ALLOW_UNVERIFIED_ACCOUNT
            )
            is True
        )

    async def _async_account_identity(
        self, tokens: dict
    ) -> tuple[dict, list[str]] | None:
        """The Orion profile and beds behind newly issued tokens.

        The whole profile, not just the id. The reauth check needs the
        email and phone to compare against an entry that predates
        account-keyed unique ids.

        Verification proves the code was valid. It does not prove which
        account issued it. Neither creation nor reauthentication persists
        tokens until this second request provides a stable account id.
        """
        session = async_get_clientsession(self.hass)
        client = OrionApiClient(
            session=session,
            access_token=tokens["access_token"],
            refresh_token=tokens["refresh_token"],
            expires_at=tokens["expires_at"],
        )
        # If this probe rotates the tokens, the rotation has to reach the
        # dict the caller persists. Otherwise the refresh token we just
        # spent is the one written to the entry, and the entry works until
        # its first real refresh and then dies into a reauth loop that
        # repeats the same mistake. `util.auth_tokens_from_session` coerces
        # a missing `expires_at` to 0 and 0 reads as expired, so any verify
        # response without that field arms this on the first call.
        client.set_token_refresh_callback(
            lambda access, refresh, expires_at: tokens.update(
                access_token=access, refresh_token=refresh, expires_at=expires_at
            )
        )
        try:
            profile = await client.get_current_user()
            devices = await client.list_devices()
        except (OrionApiError, OrionConnectionError, OrionAuthError):
            return None
        if (
            not isinstance(profile, dict)
            or not isinstance(profile.get("id"), str)
            or not profile["id"]
        ):
            return None
        device_ids = sorted(
            {
                str(device["id"])
                for device in devices
                if isinstance(device, dict) and device.get("id")
            }
        )
        return profile, device_ids

    async def _async_send_code(self, auth_value: str) -> None:
        """Send a verification code, or raise.

        Every failure path here raises, so callers proceed to the next
        step unconditionally. It previously claimed to return a step
        result and never did, which left both callers branching on a
        condition that was always true.
        """
        self._auth_value = auth_value.strip()

        unique_id = self._auth_value.lower()
        await self.async_set_unique_id(unique_id)
        self._abort_if_unique_id_configured()

        session = async_get_clientsession(self.hass)
        client = OrionApiClient(session=session)

        email = self._auth_value if self._auth_method == AUTH_METHOD_EMAIL else None
        phone = self._auth_value if self._auth_method == AUTH_METHOD_PHONE else None
        success = await client.request_auth_code(email=email, phone=phone)
        if not success:
            raise OrionConnectionError("API returned success=false")

    async def async_step_email(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Step 1a: User enters email address."""
        errors: dict[str, str] = {}

        if user_input is not None:
            try:
                await self._async_send_code(user_input["email"])
                return await self.async_step_verify()
            except OrionConnectionError:
                errors["base"] = "cannot_connect"
            except OrionApiError:
                errors["base"] = "unknown"

        return self.async_show_form(
            step_id="email",
            data_schema=vol.Schema(
                {
                    vol.Required("email"): str,
                }
            ),
            errors=errors,
        )

    async def async_step_phone(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Step 1b: User enters phone number."""
        errors: dict[str, str] = {}
        phone_default = ""

        if user_input is not None:
            raw = user_input.get("phone", "")
            phone_default = raw
            try:
                phone = util.normalize_phone(raw)
            except ValueError as err:
                _LOGGER.debug("Rejected phone number: %s", err)
                errors["base"] = "invalid_phone"
            else:
                try:
                    await self._async_send_code(phone)
                    return await self.async_step_verify()
                except OrionConnectionError:
                    errors["base"] = "cannot_connect"
                except OrionApiError:
                    errors["base"] = "unknown"

        return self.async_show_form(
            step_id="phone",
            data_schema=vol.Schema(
                {
                    vol.Required("phone", default=phone_default): str,
                }
            ),
            errors=errors,
        )

    async def async_step_verify(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Step 2: User enters the verification code."""
        errors: dict[str, str] = {}

        if user_input is not None:
            code = user_input["code"].strip()

            session = async_get_clientsession(self.hass)
            client = OrionApiClient(session=session)

            try:
                email = (
                    self._auth_value if self._auth_method == AUTH_METHOD_EMAIL else None
                )
                phone = (
                    self._auth_value if self._auth_method == AUTH_METHOD_PHONE else None
                )
                tokens = await client.verify_auth_code(
                    code=code, email=email, phone=phone
                )
            except OrionAuthError:
                errors["base"] = "invalid_code"
            except OrionConnectionError:
                errors["base"] = "cannot_connect"
            except OrionApiError:
                errors["base"] = "unknown"
            else:
                # Identify the entry by the ACCOUNT, not by the string
                # that was typed. One Orion account reached by email and
                # by phone is still one account, and keying on the typed
                # value let it be added twice: two pollers, two
                # WebSockets, and two sets of entities fighting over the
                # same unique ids. The account id is only knowable here,
                # after the code has been accepted.
                identity = await self._async_account_identity(tokens)
                if identity is None:
                    # Abort rather than re-showing the code form. The code
                    # was already spent by verify_auth_code above, so the
                    # form would invite a retry that can only ever return
                    # invalid_code.
                    return self.async_abort(reason="identity_unavailable")
                profile, device_ids = identity
                account_id = profile["id"]
                if self._reauth_entry:
                    # Reauth was the one door into this flow with no
                    # identity check on it, and it is the dangerous one.
                    # Finishing account A's reauth prompt with account B's
                    # credentials swapped primary and partner, and the
                    # entity migration then re-keyed each person's history
                    # onto the OTHER person. Two people's sleep scores,
                    # heart rates and apnea counts exchanged, silently,
                    # with recorder history and statistics attached. No
                    # error anywhere, because both renames succeed.
                    if not self._async_reauth_account_matches(profile):
                        return self.async_abort(reason="reauth_account_mismatch")
                    # The bed set is CORROBORATING evidence, and only where
                    # the identity proof is weak. When an account id is
                    # recorded, the check above already proved the returned
                    # id equals it, and no option can widen that branch. The
                    # bed set adds nothing to a proof that is already exact.
                    #
                    # Requiring it anyway was a permanent lockout. Expired
                    # tokens mean setup raises before any poll, and a poll is
                    # the only thing that ever updates `CONF_DEVICE_IDS`
                    # (`coordinator._async_update_data`, which rewrites it
                    # without ceremony on every run). So an entry that dies
                    # on its credentials and then loses, gains or replaces a
                    # bed at the vendor can never satisfy this test again.
                    # Reauth is the ONLY door once tokens are dead, and this
                    # aborted it on every attempt, forever. The only exit was
                    # delete-and-re-add, which discards options, aliases, the
                    # partner link, the replacement marker and the downgrade
                    # journal.
                    #
                    # Nothing is weakened by dropping it here. Unique ids
                    # embed the device id, so history cannot follow the
                    # account onto a different bed, and `overlapping_entry_ids`
                    # below still refuses a bed another entry owns.
                    #
                    # Kept for the pre-3.0 branches. There the proof is a
                    # typed address, or the unverified escape hatch, and the
                    # bed set is the only other reference value in play.
                    if recorded_account_id(self._reauth_entry) is None:
                        recorded_devices = self._reauth_entry.data.get(CONF_DEVICE_IDS)
                        if recorded_devices is not None and set(device_ids) != {
                            str(value) for value in recorded_devices
                        }:
                            return self.async_abort(reason="reauth_bed_mismatch")
                    if overlapping_entry_ids(
                        self.hass,
                        self._reauth_entry.entry_id,
                        set(device_ids),
                    ):
                        return self.async_abort(reason="bed_already_configured")
                else:
                    await self.async_set_unique_id(account_id)
                    self._abort_if_unique_id_configured()

                    if overlapping_entry_ids(self.hass, None, set(device_ids)):
                        return self.async_abort(reason="bed_already_configured")

                # `_async_account_identity` can rotate an expiry-less token.
                # Build this only after that call so the fresh refresh token,
                # not the one the probe just spent, is persisted.
                data = {
                    CONF_AUTH_METHOD: self._auth_method,
                    CONF_AUTH_VALUE: self._auth_value,
                    CONF_ACCESS_TOKEN: tokens["access_token"],
                    CONF_REFRESH_TOKEN: tokens["refresh_token"],
                    CONF_EXPIRES_AT: tokens["expires_at"],
                    CONF_ACCOUNT_ID: account_id,
                    CONF_DEVICE_IDS: device_ids,
                }

                if self._reauth_entry:
                    updated = dict(data)
                    if recorded_account_id(self._reauth_entry) is not None:
                        # Only ever POPULATE an absent account id, never
                        # overwrite a recorded one. This is the invariant
                        # `__init__.py` states and holds on the setup path,
                        # and the dict spread below used to violate it here.
                        #
                        # `CONF_ACCOUNT_ID` was spread in unconditionally,
                        # so a reauth wrote the id the server just returned
                        # over the id the entry already recorded. The guard
                        # above validated `entry.unique_id`, a DIFFERENT
                        # field, so the write landed on the copy nothing in
                        # this flow had checked. Overwriting a recorded id
                        # does not surface a mismatch, it ratifies one, and
                        # the coordinator then compares the id to itself and
                        # reports agreement forever while the entity
                        # migration re-keys one person's history onto the
                        # other person's identity.
                        #
                        # It was survivable only by accident. The bed
                        # overlap check a few lines up rejects most
                        # mismatches first, which is an unrelated third
                        # check doing this one's job. Two accounts that
                        # share a bed, which is exactly the household this
                        # integration is built for, do not trip it.
                        #
                        # Nothing is lost by dropping the key. When an id is
                        # recorded, `_async_reauth_account_matches` has
                        # already proven the returned id equals it, so the
                        # write would have been a no-op in every case where
                        # it was safe. Popping is a no-op on success and a
                        # refusal on the case the guard exists for.
                        updated.pop(CONF_ACCOUNT_ID, None)
                    self.hass.config_entries.async_update_entry(
                        self._reauth_entry,
                        data={**self._reauth_entry.data, **updated},
                    )
                    await self.hass.config_entries.async_reload(
                        self._reauth_entry.entry_id
                    )
                    return self.async_abort(reason="reauth_successful")

                return self.async_create_entry(
                    title=f"Orion Sleep ({self._auth_value})",
                    data=data,
                )

        return self.async_show_form(
            step_id="verify",
            data_schema=vol.Schema(
                {
                    vol.Required("code"): str,
                }
            ),
            errors=errors,
        )

    async def _async_key_identity(
        self, api_key: str
    ) -> tuple[dict, list[str]] | None:
        """Validate an API key and return its account profile and beds.

        The key half of `_async_account_identity`. There is no code to
        verify and no token to rotate: the key IS the credential, so this
        just authenticates a key-mode client against `/v1/auth/me` (the
        endpoint this codebase already uses for identity) and lists the
        beds. Returns None on any auth or transport failure so the caller
        shows a form error rather than accepting an unvalidated key.
        """
        session = async_get_clientsession(self.hass)
        client = OrionApiClient(
            session=session, access_token=api_key, is_api_key=True
        )
        try:
            profile = await client.get_current_user()
            devices = await client.list_devices()
        except (OrionApiError, OrionConnectionError, OrionAuthError):
            return None
        if (
            not isinstance(profile, dict)
            or not isinstance(profile.get("id"), str)
            or not profile["id"]
        ):
            return None
        device_ids = sorted(
            {
                str(device["id"])
                for device in devices
                if isinstance(device, dict) and device.get("id")
            }
        )
        return profile, device_ids

    async def async_step_api_key(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """API key path: paste a key, validate it, done. No OTP step.

        An Orion API key authenticates directly, so this skips the
        send-code / verify-code round trip entirely. The key is validated
        against `/v1/auth/me` before anything is persisted, and a bad key
        surfaces as `invalid_api_key` rather than being accepted.
        """
        errors: dict[str, str] = {}

        if user_input is not None:
            self._auth_method = AUTH_METHOD_API_KEY
            api_key = user_input[CONF_API_KEY].strip()

            identity = await self._async_key_identity(api_key)
            if identity is None:
                errors["base"] = "invalid_api_key"
            else:
                profile, device_ids = identity
                account_id = profile["id"]

                if self._reauth_entry:
                    if not self._async_reauth_account_matches(profile):
                        return self.async_abort(reason="reauth_account_mismatch")
                    if recorded_account_id(self._reauth_entry) is None:
                        recorded_devices = self._reauth_entry.data.get(CONF_DEVICE_IDS)
                        if recorded_devices is not None and set(device_ids) != {
                            str(value) for value in recorded_devices
                        }:
                            return self.async_abort(reason="reauth_bed_mismatch")
                    if overlapping_entry_ids(
                        self.hass, self._reauth_entry.entry_id, set(device_ids)
                    ):
                        return self.async_abort(reason="bed_already_configured")
                else:
                    await self.async_set_unique_id(account_id)
                    self._abort_if_unique_id_configured()
                    if overlapping_entry_ids(self.hass, None, set(device_ids)):
                        return self.async_abort(reason="bed_already_configured")

                # An API-key entry stores the key as the access token, with
                # the auth method recorded so setup rebuilds a key-mode
                # client. No refresh token, no expiry: the key is static.
                data = {
                    CONF_AUTH_METHOD: AUTH_METHOD_API_KEY,
                    CONF_API_KEY: api_key,
                    CONF_ACCESS_TOKEN: api_key,
                    CONF_ACCOUNT_ID: account_id,
                    CONF_DEVICE_IDS: device_ids,
                }

                if self._reauth_entry:
                    updated = dict(data)
                    if recorded_account_id(self._reauth_entry) is not None:
                        # Same invariant as the OTP reauth path: only ever
                        # populate an absent account id, never overwrite a
                        # recorded one.
                        updated.pop(CONF_ACCOUNT_ID, None)
                    # Drop any stale OTP-era credential keys so an entry
                    # that used to be email/phone and is now key-authed does
                    # not carry a dead refresh token beside its key.
                    merged = {**self._reauth_entry.data, **updated}
                    for stale in (CONF_REFRESH_TOKEN, CONF_EXPIRES_AT):
                        merged.pop(stale, None)
                    self.hass.config_entries.async_update_entry(
                        self._reauth_entry, data=merged
                    )
                    await self.hass.config_entries.async_reload(
                        self._reauth_entry.entry_id
                    )
                    return self.async_abort(reason="reauth_successful")

                # Title carries the account label, not the key. The key must
                # never appear in an entry title (it would leak into logs and
                # the UI). A short profile-derived label is used instead.
                label = util.orion_user_label(profile) or account_id
                return self.async_create_entry(
                    title=f"Orion Sleep ({label})",
                    data=data,
                )

        return self.async_show_form(
            step_id="api_key",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_API_KEY): str,
                }
            ),
            errors=errors,
        )

    async def async_step_reauth(self, entry_data: dict[str, Any]) -> ConfigFlowResult:
        """Handle reauth triggered by ConfigEntryAuthFailed."""
        self._reauth_entry = self.hass.config_entries.async_get_entry(
            self.context["entry_id"]
        )
        self._auth_method = entry_data.get(CONF_AUTH_METHOD)
        self._auth_value = entry_data.get(CONF_AUTH_VALUE)
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Confirm reauth and send a new verification code.

        A key-authed entry reauths by pasting a NEW key, not by receiving
        an OTP code. A revoked key cannot be recovered with an emailed
        code, so routing it to the code path would be a dead end. The key
        step handles reauth on its own because `self._reauth_entry` is set.
        """
        if self._auth_method == AUTH_METHOD_API_KEY:
            return await self.async_step_api_key()

        errors: dict[str, str] = {}

        if user_input is not None:
            session = async_get_clientsession(self.hass)
            client = OrionApiClient(session=session)

            try:
                email = (
                    self._auth_value if self._auth_method == AUTH_METHOD_EMAIL else None
                )
                phone = (
                    self._auth_value if self._auth_method == AUTH_METHOD_PHONE else None
                )
                success = await client.request_auth_code(email=email, phone=phone)
                if success:
                    return await self.async_step_verify()
                errors["base"] = "cannot_connect"
            except OrionConnectionError:
                errors["base"] = "cannot_connect"
            except OrionApiError:
                errors["base"] = "unknown"

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema({}),
            errors=errors,
        )


class OrionSleepOptionsFlow(OptionsFlow):
    """Handle options flow for Orion Sleep."""

    def __init__(self, config_entry: ConfigEntry) -> None:
        """Initialize options flow."""
        self._config_entry = config_entry
        self._partner_auth_method: str | None = None
        self._partner_auth_value: str | None = None
        self._pending_options: dict[str, Any] = {}

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Manage polling and an optional partner account."""
        has_partner = bool(self._config_entry.data.get(CONF_PARTNER_ACCESS_TOKEN))
        if user_input is not None:
            options = dict(user_input)
            # Preserve aliases across an options save that did not touch them.
            options.setdefault(
                CONF_DISPLAY_ALIASES,
                dict(self._config_entry.options.get(CONF_DISPLAY_ALIASES) or {}),
            )
            if options.pop("edit_aliases", False):
                self._pending_options = options
                return await self.async_step_aliases()
            return await self._async_finish_init(options)

        current_interval = self._config_entry.options.get(
            CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL
        )
        current_insights_days = self._config_entry.options.get(
            CONF_INSIGHTS_DAYS, DEFAULT_INSIGHTS_DAYS
        )
        # Defaulted from the entry rather than from the constant, so that
        # re-saving options for an unrelated reason cannot silently switch
        # the escape hatch back off underneath a household that is using it
        # to keep a locked-out entry loading.
        current_allow_unverified = self._config_entry.options.get(
            CONF_ALLOW_UNVERIFIED_ACCOUNT, DEFAULT_ALLOW_UNVERIFIED_ACCOUNT
        )
        current_zone_left = self._config_entry.options.get(
            CONF_ZONE_LEFT, DEFAULT_ZONE_LEFT
        )
        # Which zone is the LEFT side of the bed, for side-anchored
        # controllers like a bedside dial. Labelled by what each choice
        # means rather than by the raw zone id, because "zone_a" tells a
        # user nothing about which nightstand to reach for. Flipping it only
        # relabels a climate attribute and a voice alias, so it is safe to
        # expose here with no migration and no warning.
        zone_left_choices = {
            "zone_a": "Zone A is the left side",
            "zone_b": "Zone B is the left side",
        }
        partner_actions = {
            "keep": "Keep linked partner" if has_partner else "No partner account",
            "add": "Replace partner account" if has_partner else "Add partner account",
        }
        if has_partner:
            partner_actions["remove"] = "Remove partner account"

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_SCAN_INTERVAL, default=current_interval): vol.All(
                        vol.Coerce(int), vol.Range(min=60, max=3600)
                    ),
                    vol.Required(
                        CONF_INSIGHTS_DAYS, default=current_insights_days
                    ): vol.All(vol.Coerce(int), vol.Range(min=1, max=30)),
                    vol.Required("partner_action", default="keep"): vol.In(
                        partner_actions
                    ),
                    vol.Required("edit_aliases", default=False): bool,
                    vol.Required(
                        CONF_ZONE_LEFT, default=current_zone_left
                    ): vol.In(zone_left_choices),
                    # The recovery escape hatch, and the only way to reach
                    # `CONF_ALLOW_UNVERIFIED_ACCOUNT` at all.
                    #
                    # `coordinator._unverified_account_allowed` has read this
                    # option since it was defined, and nothing has ever
                    # written it, so the documented way out of one specific
                    # lockout did not exist. The lockout: setup requires the
                    # profile to carry the address the entry was set up with,
                    # a profile carrying none of `email`, `phone` and
                    # `phone_number` fails that, and the reauth flow the
                    # failure launches applies the same test, so the entry
                    # fails to load and every attempt to escape aborts. That
                    # endpoint has been measured returning `{"response":
                    # null}`, so it is an observed shape rather than a
                    # hypothesis.
                    #
                    # Home Assistant offers the options flow on an entry that
                    # failed to set up, which is what makes this reachable by
                    # somebody who is actually locked out. It is placed last
                    # and worded as a recovery step because it relaxes an
                    # identity assertion, and the scope of what it relaxes is
                    # argued in full in the coordinator docstring. It cannot
                    # ratify a mismatch against a recorded account id.
                    #
                    # Saving a change to this field reloads the entry, which
                    # is what makes the escape hatch escape anything. The
                    # locked-out entry has no options listener to reload it,
                    # because setup raised before the listener was
                    # registered. See `_async_reload_for_escape_hatch`.
                    vol.Required(
                        CONF_ALLOW_UNVERIFIED_ACCOUNT,
                        default=current_allow_unverified,
                    ): bool,
                }
            ),
        )

    def _known_users(self) -> list[dict[str, str]]:
        """Users discovered from the loaded entry, empty if it is not loaded."""
        coordinator = getattr(self._config_entry, "runtime_data", None)
        if coordinator is None:
            return []
        try:
            return coordinator.known_users()
        except Exception:  # noqa: BLE001 - options UI must never hard-fail
            _LOGGER.debug("Could not enumerate Orion users for the alias form")
            return []

    async def async_step_aliases(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Set a display name per person.

        Keyed on the immutable Orion user id. Form field labels are the
        vendor's own names so the mapping is obvious, but nothing about a
        name reaches a unique id, so renaming is always non-breaking.

        NON-BREAKING IS NOT THE SAME AS NON-PERMANENT, and reading the
        sentence above as licence to accept any string is what put a
        credential filter in this method. A rename never orphans history,
        because unique ids are built from device and zone ids. It does
        decide the entity_id the FIRST time an entity registers, and Home
        Assistant never revisits that. See the refusal below.
        """
        users = self._known_users()
        labels = helpers.unique_alias_labels(users)
        label_to_id = {label: user_id for user_id, label in labels.items()}
        existing = dict(self._config_entry.options.get(CONF_DISPLAY_ALIASES) or {})
        errors: dict[str, str] = {}

        if user_input is not None:
            aliases = {
                label_to_id[label]: value
                for label, value in user_input.items()
                if label in label_to_id
            }
            # The write boundary for a household-typed display name, and
            # the only place in the integration that can refuse one and
            # explain why.
            #
            # What is being refused: an alias that looks like a login
            # credential. Home Assistant slugifies an entity's name into
            # its entity_id at FIRST registration and never revisits it on
            # rename, so an alias of "alice@example.com" mints
            # `climate.alice_example_com_climate` and that string then sits
            # in the entity registry, every recorder row, every long term
            # statistics row, every backup, and every screenshot pasted
            # into a bug report. Forever. Clearing the alias afterwards
            # changes the friendly name and nothing else.
            #
            # `coordinator.display_name_for_user` already refused the
            # vendor's own label for exactly this reason, and refused the
            # alias for none, which left the credential a clear path into
            # the permanent identifier through the field beside the one
            # that was guarded. Same predicate, both paths.
            #
            # Refused HERE rather than in `helpers.clean_alias_map` on
            # purpose. That helper runs from `coordinator.__init__` over
            # options that are already stored, so a filter there would
            # silently delete an alias a household is relying on, on the
            # next reload, with nothing said. Here the value is not yet
            # permanent, nothing has been written, and the person who
            # typed it is looking at the form.
            #
            # A blank field is NOT an error. Clearing a field is the
            # documented way to drop an override and fall back to the
            # account name, and `clean_alias_map` removes blanks anyway.
            for label, value in user_input.items():
                if label not in label_to_id:
                    continue
                if not isinstance(value, str) or not value.strip():
                    continue
                if not helpers.is_safe_display_name(value):
                    errors[label] = "unsafe_alias"

            if not errors:
                options = dict(self._pending_options)
                options[CONF_DISPLAY_ALIASES] = helpers.clean_alias_map(
                    aliases, set(labels)
                )
                return await self._async_finish_init(options)

            # Re-show with what was typed rather than with what is stored,
            # so the household can see and edit the offending value. A form
            # that silently reverts to the previous alias reads as if the
            # rejection came from somewhere else.
            existing = {
                user_id: user_input.get(label, existing.get(user_id, ""))
                for user_id, label in labels.items()
            }

        if not labels:
            # Nothing to rename yet. Save what the first step collected
            # rather than dead-ending on an empty form.
            return await self._async_finish_init(dict(self._pending_options))

        return self.async_show_form(
            step_id="aliases",
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        label, default=existing.get(user_id, "")
                    ): str
                    for user_id, label in labels.items()
                }
            ),
            description_placeholders={"people": ", ".join(labels.values())},
            errors=errors,
        )

    def _async_save_options(self, options: dict[str, Any]) -> ConfigFlowResult:
        """Write the options, carrying an unmigrated pair journal forward.

        Every options save in this flow goes through here, and that is the
        point. `async_create_entry` REPLACES `entry.options` wholesale with
        whatever the form collected, and the form has never collected
        `CONF_UID_MIGRATION`, so any options save silently discarded it.

        That key is the ORIGINAL `[old, new]` pair journal, and it is the
        map back from 3.x unique ids to the ids 2.x asks for. Losing it
        does not fail. `async_revert_unique_ids` reports "no recorded Orion
        renames to undo" and exits clean while every entity sits on an id
        2.x never asks for, which is the stranded-history outcome this
        project treats as the worst one available.

        Only reachable before a migration pass has run, because
        `migrations._write_journal` pops this key from options once it
        expands the pairs into `data`. The window is small and it is
        exactly the window a fresh upgrade sits in, which is also when
        somebody is most likely to open options and change the polling
        interval.

        Read from `self._config_entry.options` at call time rather than
        from a value captured earlier, and that ordering is load bearing.
        `_write_partner_change` runs `evict_partner_journal` and writes the
        pruned options back to the entry BEFORE the flow reaches this
        method. Carrying a captured copy forward would put the previous
        partner's pairs back after the eviction removed them, which is the
        history merge `evict_partner_journal` exists to prevent, performed
        by the thing meant to prevent data loss.

        Nothing is preserved when the journal is absent or empty, so this
        cannot resurrect the key as an empty list. Downstream readers treat
        a present-but-empty journal as "a journal exists here and it is
        complete", which is the most confident possible way to be wrong
        about whether a downgrade has anything to undo.

        ALSO THE ONLY PLACE THE UNVERIFIED-ACCOUNT ESCAPE HATCH CAN APPLY
        ITSELF. See `_async_reload_for_escape_hatch`.
        """
        journal = self._config_entry.options.get(CONF_UID_MIGRATION)
        if journal and CONF_UID_MIGRATION not in options:
            options = {**options, CONF_UID_MIGRATION: journal}
        self._async_reload_for_escape_hatch(options)
        return self.async_create_entry(title="", data=options)

    def _async_reload_for_escape_hatch(self, options: dict[str, Any]) -> None:
        """Reload the entry when `CONF_ALLOW_UNVERIFIED_ACCOUNT` changed.

        The option exists to rescue an entry that cannot load, and until
        now saving it did nothing on the entry that needed it most.

        Why nothing happened. Every other options change applies through
        the update listener that `async_setup_entry` registers, and that
        registration is the LAST statement of a successful setup. An entry
        locked out by the account check never reaches it, because setup
        raised long before. So `entry.update_listeners` is empty, the write
        fires nothing, and the household is left with the setting saved and
        unapplied on the one entry it was written for. The previous
        shipped remedy was a paragraph of `strings.json` telling them to
        reload by hand.

        `async_schedule_reload` rather than `async_reload`. It cancels the
        pending SETUP_RETRY first, and a locked-out entry can be sitting in
        exactly that state with a retry timer armed. Reloading without
        cancelling it races the retry over the same setup.

        GUARDED ON AN ACTUAL CHANGE, and that guard is not cosmetic. This
        method runs on EVERY options save, including a scan interval edit
        on a perfectly healthy entry. The field is `vol.Required` with a
        default read from the entry, so it is submitted every time whether
        or not anybody touched it, and testing presence instead of value
        would make every save a hatch change.

        Compared against the entry's stored value with the same default
        the options form defaults the field from, so the first save on an
        entry that predates the option does not read as a change. A
        missing key in `options` also reads as unchanged rather than as a
        switch to the default, because a caller that did not collect the
        field is not expressing an opinion about it.

        THE OPTIONS ARE WRITTEN HERE, BEFORE THE RELOAD IS SCHEDULED, and
        that ordering is the whole thing working. It was originally left
        to `OptionsFlowManager.async_finish_flow`, which writes them once
        this step returns, on the reasoning that a task cannot start
        before its scheduler yields. That reasoning was wrong when
        measured. The scheduled reload ran first, set the entry up against
        the options as they were BEFORE the save, failed the identical
        account check, and left the hatch saved and unapplied, which is
        indistinguishable from the bug being fixed. Writing first makes
        the ordering ours instead of a property of somebody else's call
        stack.

        The second write is free. `async_finish_flow` calls
        `async_update_entry` with the same dict, and `async_update_entry`
        returns without doing anything when nothing changed.

        SKIPPED ENTIRELY WHEN THE ENTRY HAS AN UPDATE LISTENER. A loaded
        entry already reloads on any options change through the listener
        `async_setup_entry` registers, so scheduling a second reload would
        tear down and rebuild every per-device socket again for nothing.
        The absence of that listener is not incidental here, it IS the
        lockout: the registration is the last statement of a successful
        setup, so the entries that need this are exactly the entries that
        have nobody watching.

        The alternatives are closed, and were checked rather than assumed.
        Registering the listener early through `entry.async_on_unload` is
        defeated by `config_entries.py` calling `_async_process_on_unload`
        in the `finally` of a failed setup, which unregisters it on the way
        out. Registering it bare leaks one listener per SETUP_RETRY with
        nothing owning the unsubscribe. `OptionsFlowWithReload` refuses to
        run at all when `entry.update_listeners` is non-empty, and this
        integration registers one.
        """
        previous = bool(
            self._config_entry.options.get(
                CONF_ALLOW_UNVERIFIED_ACCOUNT, DEFAULT_ALLOW_UNVERIFIED_ACCOUNT
            )
        )
        requested = bool(options.get(CONF_ALLOW_UNVERIFIED_ACCOUNT, previous))
        if requested == previous:
            return
        if self._config_entry.update_listeners:
            return
        self.hass.config_entries.async_update_entry(
            self._config_entry, options=options
        )
        self.hass.config_entries.async_schedule_reload(
            self._config_entry.entry_id
        )

    def _write_partner_change(self, data: dict[str, Any]) -> None:
        """Persist a partner change and evict both partner journals at once.

        One `async_update_entry`, never two. The update fires the options
        listener and can reload the entry, so writing the new partner
        tokens first and clearing the journal second opens a window where
        a reload sees the PREVIOUS partner's rename records next to the
        CURRENT partner's tokens. That pair is exactly what merges two
        people's history, and it is the pair `evict_partner_journal`
        exists to break.

        `options` is passed only when it actually changed. The pair format
        journal it can hold is long gone from most entries, and handing
        `async_update_entry` an identical options dict would turn a token
        write into an options change and reload the entry for nothing.

        ALSO RECORDS THAT A PARTNER CHANGED AT ALL. Evicting the journal
        removes this integration's own records of the previous partner. It
        does nothing about the pre-3.0 `{device}_partner_{key}` rows still
        sitting in the entity registry, which the forward migration leaves
        alone on purpose and which still hold the PREVIOUS partner's
        history.

        Those rows are read as reassurance by `async_revert_unique_ids`:
        their presence suppresses the refusal that would otherwise block a
        downgrade, on the grounds that 2.x will find them where it left
        them. That is correct for a household that upgraded and never
        touched its partner, and it is a two-person history merge for one
        that replaced them. 2.x reads the role-keyed row, finds the
        previous partner's history, and writes the current partner's heart
        rate into it. Every check passes on the way there.

        The marker is set whenever a partner is linked at the moment this
        runs, which covers a straight replacement and also removal followed
        much later by a new partner. It is never cleared, because nothing
        that happens afterwards makes those registry rows belong to the
        current partner again. Only deleting them does, and that is what
        the revert now tells the household to do.
        """
        if self._config_entry.data.get(CONF_PARTNER_ACCESS_TOKEN):
            # Record WHO filled the pre-3.0 `_partner_` rows, once.
            #
            # This used to store a bare `True` on every partner write while
            # any partner tokens existed, which made it fire on the one
            # remedy this integration prescribes. A partner refresh token
            # rots, `coordinator` warns "replace it in Orion options", the
            # household signs the SAME person back in, and the marker
            # latched with no way to clear it. `revert_unique_ids` then read
            # it as proof of a replacement, named that partner's own
            # entities, and told the household to delete them and accept the
            # loss. It was destroying real history to undo a change that had
            # not happened, and the flow held the proof of that at this exact
            # line: the new partner id was already known and already equal to
            # the recorded one.
            #
            # Written once and never overwritten, because the rows do not
            # change hands when the partner does. They still hold whoever
            # was linked at upgrade time. Tracking the LATEST outgoing
            # partner would call P to Q and back to P a mismatch, when the
            # rows have belonged to P throughout.
            #
            # `True` remains the value when no partner id was ever recorded,
            # which is an entry whose partner predates `CONF_PARTNER_ACCOUNT_ID`.
            # A change happened and cannot be attributed, so it fails closed
            # exactly as before.
            if self._config_entry.data.get(CONF_PARTNER_REPLACED) is None:
                outgoing = self._config_entry.data.get(CONF_PARTNER_ACCOUNT_ID)
                data = {
                    **data,
                    CONF_PARTNER_REPLACED: (
                        outgoing if isinstance(outgoing, str) and outgoing else True
                    ),
                }
        data, options = evict_partner_journal(data, self._config_entry.options)
        changes: dict[str, Any] = {"data": data}
        if options != self._config_entry.options:
            changes["options"] = options
        self.hass.config_entries.async_update_entry(self._config_entry, **changes)

    async def _async_finish_init(
        self, options: dict[str, Any]
    ) -> ConfigFlowResult:
        """Apply the partner action and write the options entry."""
        has_partner = bool(self._config_entry.data.get(CONF_PARTNER_ACCESS_TOKEN))
        partner_action = options.pop("partner_action", "keep")

        if partner_action == "remove" and has_partner:
            # Every key that describes the linked partner goes in one write.
            #
            # CONF_PARTNER_ACCOUNT_ID belongs in this set for the same reason
            # it belongs beside the tokens when a partner is linked. Leaving
            # it behind meant remove-then-re-add did not clear it either, so
            # an entry that had ever recorded partner A rejected partner B
            # through both of the flows the household actually has. It also
            # left a stale identity claim sitting in `data` describing tokens
            # that are no longer there, and `_partner_identity_verified`
            # prefers a recorded id over the linked address, so the next
            # partner would have been judged against the previous one.
            partner_keys = {
                CONF_PARTNER_AUTH_METHOD,
                CONF_PARTNER_AUTH_VALUE,
                CONF_PARTNER_ACCESS_TOKEN,
                # A key-authed partner stores the raw key here. Removing the
                # partner must clear it too, or a revoked partnership leaves
                # a live credential in the entry.
                CONF_PARTNER_API_KEY,
                CONF_PARTNER_REFRESH_TOKEN,
                CONF_PARTNER_EXPIRES_AT,
                CONF_PARTNER_DEVICE_SERIAL,
                CONF_PARTNER_ACCOUNT_ID,
            }
            self._write_partner_change(
                {
                    key: value
                    for key, value in self._config_entry.data.items()
                    if key not in partner_keys
                }
            )
            options[CONF_PARTNER_CONFIGURED] = False
            return self._async_save_options(options)

        if partner_action == "add":
            options[CONF_PARTNER_CONFIGURED] = True
            options[CONF_PARTNER_REVISION] = uuid4().hex
            self._pending_options = options
            return await self.async_step_partner_method()

        options[CONF_PARTNER_CONFIGURED] = has_partner
        return self._async_save_options(options)

    async def async_step_partner_method(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Choose the partner login method.

        Independent of the primary account's method, so a household can run
        one side on OTP and the other on an API key.
        """
        if user_input is not None:
            self._partner_auth_method = user_input[CONF_AUTH_METHOD]
            if self._partner_auth_method == AUTH_METHOD_EMAIL:
                return await self.async_step_partner_email()
            if self._partner_auth_method == AUTH_METHOD_API_KEY:
                return await self.async_step_partner_api_key()
            return await self.async_step_partner_phone()

        return self.async_show_form(
            step_id="partner_method",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_AUTH_METHOD, default=AUTH_METHOD_EMAIL): vol.In(
                        {
                            AUTH_METHOD_EMAIL: "Email",
                            AUTH_METHOD_PHONE: "Phone",
                            AUTH_METHOD_API_KEY: "API key",
                        }
                    )
                }
            ),
        )

    async def async_step_partner_email(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Send a verification code to the partner email."""
        errors: dict[str, str] = {}
        if user_input is not None:
            self._partner_auth_value = user_input["email"].strip()
            try:
                client = OrionApiClient(session=async_get_clientsession(self.hass))
                if not await client.request_auth_code(email=self._partner_auth_value):
                    raise OrionConnectionError("API returned success=false")
                return await self.async_step_partner_verify()
            except OrionConnectionError:
                errors["base"] = "cannot_connect"
            except OrionApiError:
                errors["base"] = "unknown"

        return self.async_show_form(
            step_id="partner_email",
            data_schema=vol.Schema({vol.Required("email"): str}),
            errors=errors,
        )

    async def async_step_partner_phone(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Send a verification code to the partner phone."""
        errors: dict[str, str] = {}
        phone_default = ""
        if user_input is not None:
            phone_default = user_input.get("phone", "")
            try:
                phone = util.normalize_phone(phone_default)
            except ValueError:
                errors["base"] = "invalid_phone"
            else:
                self._partner_auth_value = phone
                try:
                    client = OrionApiClient(session=async_get_clientsession(self.hass))
                    if not await client.request_auth_code(phone=phone):
                        raise OrionConnectionError("API returned success=false")
                    return await self.async_step_partner_verify()
                except OrionConnectionError:
                    errors["base"] = "cannot_connect"
                except OrionApiError:
                    errors["base"] = "unknown"

        return self.async_show_form(
            step_id="partner_phone",
            data_schema=vol.Schema({vol.Required("phone", default=phone_default): str}),
            errors=errors,
        )

    async def async_step_partner_verify(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Verify the partner code and persist its tokens."""
        errors: dict[str, str] = {}
        if user_input is not None:
            client = OrionApiClient(session=async_get_clientsession(self.hass))
            email = (
                self._partner_auth_value
                if self._partner_auth_method == AUTH_METHOD_EMAIL
                else None
            )
            phone = (
                self._partner_auth_value
                if self._partner_auth_method == AUTH_METHOD_PHONE
                else None
            )
            try:
                tokens = await client.verify_auth_code(
                    code=user_input["code"].strip(),
                    email=email,
                    phone=phone,
                )
            except OrionAuthError:
                errors["base"] = "invalid_code"
            except OrionConnectionError:
                errors["base"] = "cannot_connect"
            except OrionApiError:
                errors["base"] = "unknown"
            else:
                authenticated_client = OrionApiClient(
                    session=async_get_clientsession(self.hass),
                    access_token=tokens["access_token"],
                    refresh_token=tokens["refresh_token"],
                    expires_at=tokens["expires_at"],
                )
                # Same reason as `_async_account_id`: this client calls
                # ensure_valid_token before its first request, and a
                # rotation here would otherwise be thrown away while the
                # spent refresh token is what gets persisted below.
                authenticated_client.set_token_refresh_callback(
                    lambda access, refresh, expires_at: tokens.update(
                        access_token=access,
                        refresh_token=refresh,
                        expires_at=expires_at,
                    )
                )
                try:
                    partner_profile = await authenticated_client.get_current_user()
                    partner_devices = util.dedupe_devices_by_id(
                        await authenticated_client.list_devices()
                    )
                # Every branch from here aborts rather than re-showing the
                # form. `verify_auth_code` above has spent the one-time
                # code, so offering the box again can only ever return
                # invalid_code, and closing the dialog is the user's only
                # exit. It also discards `_pending_options`, which was
                # collected two steps earlier.
                except OrionAuthError:
                    return self.async_abort(reason="identity_unavailable")
                except (OrionApiError, OrionConnectionError):
                    return self.async_abort(reason="cannot_connect")
                else:
                    return self._finalize_partner_link(
                        partner_profile,
                        partner_devices,
                        {
                            CONF_PARTNER_AUTH_METHOD: self._partner_auth_method,
                            CONF_PARTNER_AUTH_VALUE: self._partner_auth_value,
                            CONF_PARTNER_ACCESS_TOKEN: tokens["access_token"],
                            CONF_PARTNER_REFRESH_TOKEN: tokens["refresh_token"],
                            CONF_PARTNER_EXPIRES_AT: tokens["expires_at"],
                        },
                    )

        return self.async_show_form(
            step_id="partner_verify",
            data_schema=vol.Schema({vol.Required("code"): str}),
            errors=errors,
        )

    async def async_step_partner_api_key(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Link a partner account with an API key. No OTP step.

        The partner equivalent of `async_step_api_key`. The key is
        validated against the partner's own `/v1/auth/me`, then the same
        shared-bed and identity checks run as for the OTP partner path.
        """
        errors: dict[str, str] = {}

        if user_input is not None:
            self._partner_auth_method = AUTH_METHOD_API_KEY
            api_key = user_input[CONF_PARTNER_API_KEY].strip()

            client = OrionApiClient(
                session=async_get_clientsession(self.hass),
                access_token=api_key,
                is_api_key=True,
            )
            try:
                partner_profile = await client.get_current_user()
                partner_devices = util.dedupe_devices_by_id(
                    await client.list_devices()
                )
            except OrionAuthError:
                errors["base"] = "invalid_api_key"
            except (OrionApiError, OrionConnectionError):
                errors["base"] = "cannot_connect"
            else:
                return self._finalize_partner_link(
                    partner_profile,
                    partner_devices,
                    {
                        CONF_PARTNER_AUTH_METHOD: AUTH_METHOD_API_KEY,
                        CONF_PARTNER_API_KEY: api_key,
                        CONF_PARTNER_ACCESS_TOKEN: api_key,
                    },
                )

        return self.async_show_form(
            step_id="partner_api_key",
            data_schema=vol.Schema({vol.Required(CONF_PARTNER_API_KEY): str}),
            errors=errors,
        )

    def _finalize_partner_link(
        self,
        partner_profile: object,
        partner_devices: list,
        credential: dict[str, Any],
    ) -> ConfigFlowResult:
        """Shared partner-link finalize for both OTP and API-key paths.

        Runs the identity and shared-bed checks that must hold regardless
        of how the partner authenticated, then writes the partner
        credential (`credential`, whichever shape) alongside the recorded
        partner account id and device serial in a single
        `_write_partner_change`.
        """
        if not isinstance(partner_profile, dict):
            partner_id = None
        else:
            partner_id = partner_profile.get("id")
        if not isinstance(partner_id, str) or not partner_id:
            return self.async_abort(reason="identity_unavailable")

        coordinator = getattr(self._config_entry, "runtime_data", None)
        primary_devices = getattr(coordinator, "devices", [])
        primary_id = getattr(coordinator, "user_id", "")
        shared_serials = util.shared_device_serials(primary_devices, partner_devices)
        if partner_id == primary_id:
            return self.async_abort(reason="partner_same_account")
        if (
            len(primary_devices) != 1
            or len(partner_devices) != 1
            or len(shared_serials) != 1
        ):
            return self.async_abort(reason="partner_device_ambiguous")

        partner_serial = next(iter(shared_serials))
        # CONF_PARTNER_ACCOUNT_ID and the device serial are written HERE,
        # in the SAME `_write_partner_change` as the credential they
        # describe. A separate write would open a window where a reload
        # sees the new credential next to the previous partner's recorded
        # id, which is the exact pairing that method exists to prevent.
        # `partner_id` is from the profile fetched with these exact
        # credentials, so it records who they actually belong to.
        self._write_partner_change(
            {
                **self._config_entry.data,
                **credential,
                CONF_PARTNER_DEVICE_SERIAL: partner_serial,
                CONF_PARTNER_ACCOUNT_ID: partner_id,
            }
        )
        # After `_write_partner_change`, never before: that call runs the
        # partner eviction and writes pruned options back to the entry, and
        # the save reads the entry's options as they are now.
        return self._async_save_options(self._pending_options)
