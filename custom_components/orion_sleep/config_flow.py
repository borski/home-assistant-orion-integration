"""Config flow for Orion Sleep integration."""

from __future__ import annotations

import logging
import re
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

from . import util
from .api import OrionApiClient, OrionApiError, OrionAuthError, OrionConnectionError
from .const import (
    CONF_ACCESS_TOKEN,
    CONF_AUTH_METHOD,
    CONF_AUTH_VALUE,
    CONF_DISPLAY_ALIASES,
    CONF_EXPIRES_AT,
    CONF_INSIGHTS_DAYS,
    CONF_PARTNER_ACCESS_TOKEN,
    CONF_PARTNER_AUTH_METHOD,
    CONF_PARTNER_AUTH_VALUE,
    CONF_PARTNER_CONFIGURED,
    CONF_PARTNER_DEVICE_SERIAL,
    CONF_PARTNER_EXPIRES_AT,
    CONF_PARTNER_REFRESH_TOKEN,
    CONF_PARTNER_REVISION,
    CONF_REFRESH_TOKEN,
    CONF_SCAN_INTERVAL,
    DEFAULT_INSIGHTS_DAYS,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

AUTH_METHOD_EMAIL = "email"
AUTH_METHOD_PHONE = "phone"

# Orion's auth endpoint requires a full US phone number including the leading
# country code ("1"), e.g. 15132015808. Anything shorter is rejected server-side.
_PHONE_RE = re.compile(r"^1\d{10}$")


def _normalize_phone(raw: str) -> str:
    """Strip spaces, dashes, parens and a leading + from a phone number."""
    return re.sub(r"[\s\-\(\)\+]", "", raw or "")


class OrionSleepConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Orion Sleep."""

    VERSION = 1

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
        """Step 1: User picks login method (email or phone)."""
        if user_input is not None:
            self._auth_method = user_input[CONF_AUTH_METHOD]
            if self._auth_method == AUTH_METHOD_EMAIL:
                return await self.async_step_email()
            return await self.async_step_phone()

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_AUTH_METHOD, default=AUTH_METHOD_EMAIL): vol.In(
                        {
                            AUTH_METHOD_EMAIL: "Email",
                            AUTH_METHOD_PHONE: "Phone",
                        }
                    ),
                }
            ),
        )

    async def _async_send_code(self, auth_value: str) -> ConfigFlowResult | None:
        """Send verification code. Returns None on success, or a step result with errors."""
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
        return None

    async def async_step_email(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Step 1a: User enters email address."""
        errors: dict[str, str] = {}

        if user_input is not None:
            try:
                result = await self._async_send_code(user_input["email"])
                if result is None:
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
            phone = _normalize_phone(raw)
            if not _PHONE_RE.match(phone):
                _LOGGER.debug(
                    "Rejected phone number %r (normalized: %r) — must be 11 digits starting with 1",
                    raw,
                    phone,
                )
                errors["base"] = "invalid_phone"
            else:
                try:
                    result = await self._async_send_code(phone)
                    if result is None:
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
                data = {
                    CONF_AUTH_METHOD: self._auth_method,
                    CONF_AUTH_VALUE: self._auth_value,
                    CONF_ACCESS_TOKEN: tokens["access_token"],
                    CONF_REFRESH_TOKEN: tokens["refresh_token"],
                    CONF_EXPIRES_AT: tokens["expires_at"],
                }

                if self._reauth_entry:
                    self.hass.config_entries.async_update_entry(
                        self._reauth_entry,
                        data={**self._reauth_entry.data, **data},
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
        """Confirm reauth and send a new verification code."""
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
        """
        users = self._known_users()
        labels = util.unique_alias_labels(users)
        label_to_id = {label: user_id for user_id, label in labels.items()}
        existing = dict(self._config_entry.options.get(CONF_DISPLAY_ALIASES) or {})

        if user_input is not None:
            aliases = {
                label_to_id[label]: value
                for label, value in user_input.items()
                if label in label_to_id
            }
            options = dict(self._pending_options)
            options[CONF_DISPLAY_ALIASES] = util.clean_alias_map(
                aliases, set(labels)
            )
            return await self._async_finish_init(options)

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
        )

    async def _async_finish_init(
        self, options: dict[str, Any]
    ) -> ConfigFlowResult:
        """Apply the partner action and write the options entry."""
        has_partner = bool(self._config_entry.data.get(CONF_PARTNER_ACCESS_TOKEN))
        partner_action = options.pop("partner_action", "keep")

        if partner_action == "remove" and has_partner:
            partner_keys = {
                CONF_PARTNER_AUTH_METHOD,
                CONF_PARTNER_AUTH_VALUE,
                CONF_PARTNER_ACCESS_TOKEN,
                CONF_PARTNER_REFRESH_TOKEN,
                CONF_PARTNER_EXPIRES_AT,
                CONF_PARTNER_DEVICE_SERIAL,
            }
            self.hass.config_entries.async_update_entry(
                self._config_entry,
                data={
                    key: value
                    for key, value in self._config_entry.data.items()
                    if key not in partner_keys
                },
            )
            options[CONF_PARTNER_CONFIGURED] = False
            return self.async_create_entry(title="", data=options)

        if partner_action == "add":
            options[CONF_PARTNER_CONFIGURED] = True
            options[CONF_PARTNER_REVISION] = uuid4().hex
            self._pending_options = options
            return await self.async_step_partner_method()

        options[CONF_PARTNER_CONFIGURED] = has_partner
        return self.async_create_entry(title="", data=options)

    async def async_step_partner_method(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Choose the partner login method."""
        if user_input is not None:
            self._partner_auth_method = user_input[CONF_AUTH_METHOD]
            if self._partner_auth_method == AUTH_METHOD_EMAIL:
                return await self.async_step_partner_email()
            return await self.async_step_partner_phone()

        return self.async_show_form(
            step_id="partner_method",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_AUTH_METHOD, default=AUTH_METHOD_EMAIL): vol.In(
                        {
                            AUTH_METHOD_EMAIL: "Email",
                            AUTH_METHOD_PHONE: "Phone",
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
            phone = _normalize_phone(phone_default)
            if not _PHONE_RE.match(phone):
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
                try:
                    partner_devices = util.dedupe_devices_by_id(
                        await authenticated_client.list_devices()
                    )
                except OrionAuthError:
                    errors["base"] = "invalid_code"
                except (OrionApiError, OrionConnectionError):
                    errors["base"] = "cannot_connect"
                else:
                    coordinator = getattr(self._config_entry, "runtime_data", None)
                    primary_devices = getattr(coordinator, "devices", [])
                    shared_serials = util.shared_device_serials(
                        primary_devices, partner_devices
                    )
                    if (
                        len(primary_devices) != 1
                        or len(partner_devices) != 1
                        or len(shared_serials) != 1
                    ):
                        errors["base"] = "partner_device_ambiguous"
                    else:
                        partner_serial = next(iter(shared_serials))
                        self.hass.config_entries.async_update_entry(
                            self._config_entry,
                            data={
                                **self._config_entry.data,
                                CONF_PARTNER_AUTH_METHOD: self._partner_auth_method,
                                CONF_PARTNER_AUTH_VALUE: self._partner_auth_value,
                                CONF_PARTNER_ACCESS_TOKEN: tokens["access_token"],
                                CONF_PARTNER_REFRESH_TOKEN: tokens["refresh_token"],
                                CONF_PARTNER_EXPIRES_AT: tokens["expires_at"],
                                CONF_PARTNER_DEVICE_SERIAL: partner_serial,
                            },
                        )
                        return self.async_create_entry(
                            title="", data=self._pending_options
                        )

        return self.async_show_form(
            step_id="partner_verify",
            data_schema=vol.Schema({vol.Required("code"): str}),
            errors=errors,
        )
