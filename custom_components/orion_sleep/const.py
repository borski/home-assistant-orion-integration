"""Constants for the Orion Sleep integration."""

DOMAIN = "orion_sleep"

# Config entry data keys (stored in config_entry.data)
CONF_AUTH_METHOD = "auth_method"  # "email", "phone", or "api_key"
CONF_AUTH_VALUE = "auth_value"  # the email address or phone number
CONF_ACCESS_TOKEN = "access_token"
CONF_REFRESH_TOKEN = "refresh_token"
CONF_EXPIRES_AT = "expires_at"  # Unix timestamp
# The raw Orion API key (os_live_...) when the account is key-authed. It is
# stored as the access token for the client and never expires on a schedule
# this integration can predict, so no refresh token or expiry is recorded
# alongside it. MUST be redacted from diagnostics.
CONF_API_KEY = "api_key"
CONF_PARTNER_API_KEY = "partner_api_key"
CONF_ACCOUNT_ID = "_account_id_v3"
CONF_DEVICE_IDS = "_device_ids_v3"
CONF_UID_MIGRATION = "_uid_migration_v3"
CONF_UID_RECOVERY_ACTIVE = "_uid_recovery_active_v3"

# Optional partner account. Tokens live in config entry data. The configured
# flag lives in options so adding or removing a partner triggers one reload.
CONF_PARTNER_AUTH_METHOD = "partner_auth_method"
CONF_PARTNER_AUTH_VALUE = "partner_auth_value"
CONF_PARTNER_ACCESS_TOKEN = "partner_access_token"
CONF_PARTNER_REFRESH_TOKEN = "partner_refresh_token"
CONF_PARTNER_EXPIRES_AT = "partner_expires_at"
CONF_PARTNER_CONFIGURED = "partner_configured"
CONF_PARTNER_REVISION = "partner_revision"
CONF_PARTNER_DEVICE_SERIAL = "partner_device_serial"
# The Orion account behind the partner tokens, recorded when the partner is
# linked so a later profile response can be checked against it.
#
# Named like CONF_ACCOUNT_ID rather than like its CONF_PARTNER_AUTH_VALUE
# neighbours, because it is the same KIND of thing as CONF_ACCOUNT_ID. The
# plainly named partner keys are what the user typed. This one is derived
# from a server response and is only ever written by the integration.
#
# The primary account has had a recorded-versus-returned identity check
# since 3.0 and the partner had nothing at all. `partner_mapping_valid`
# compared device serials and counts, which says the two accounts still
# share one bed and says nothing about WHICH partner account this is. That
# id then names every record `migrations._partner_recovery_renames` writes
# into the downgrade journal, and reverting a journal that names the wrong
# partner hands the previous partner's entities to the current one, merging
# two people's heart rate, HRV and apnea history under one identity.
CONF_PARTNER_ACCOUNT_ID = "_partner_account_id_v3"

# Whether the partner linked to this entry has ever been changed.
#
# Written by `ConfigFlow._write_partner_change` whenever a partner is
# already linked at the moment the linkage changes, so it covers both a
# direct replacement and a removal followed later by a new partner.
#
# It exists for exactly one decision, in `migrations.async_revert_unique_ids`.
# A normal 2.x upgrade leaves the pre-3.0 `{device}_partner_{key}` rows in
# the registry on purpose, and their presence is used to SUPPRESS the
# "partner is unmapped" refusal, because 2.x will find those rows exactly
# where it left them. That reasoning holds only while the partner who
# filled them is still the partner. After a replacement those rows hold
# the PREVIOUS partner's history, 2.x will write the CURRENT partner's
# heart rate straight into them, and the suppression turns a two-person
# history merge into a downgrade that reports itself ready.
#
# `_has_legacy_partner_rows` cannot tell those apart on its own. A
# `_partner_` substring says the row exists. It cannot say who filled it.
CONF_PARTNER_REPLACED = "_partner_replaced_v3"

# Options flow keys
CONF_SCAN_INTERVAL = "scan_interval"  # polling interval in seconds
DEFAULT_SCAN_INTERVAL = 600  # 10 minutes

# Escape hatch for an entry that cannot prove which account its tokens
# belong to. Read by `OrionDataUpdateCoordinator._async_setup`, which is
# where the assertion it relaxes lives, and documented in full there.
#
# In options rather than in data on purpose. Data is where the integration
# records what it learned. Options are where the household states what it
# wants, and this is a decision only the household can make.
CONF_ALLOW_UNVERIFIED_ACCOUNT = "allow_unverified_account"
DEFAULT_ALLOW_UNVERIFIED_ACCOUNT = False

# Insights
CONF_INSIGHTS_DAYS = "insights_days"
DEFAULT_INSIGHTS_DAYS = 7

# Rapid cooling (the app calls it Hot Flash Relief).
#
# The app clamps its picker to a HOT_FLASH_DURATION_OPTIONS array that lives
# in a separate bytecode module and was not resolved, so the exact menu
# values are UNRESOLVED. 30 shows up as the clamp seed and the app's default
# is that array's index 1. Whether the server enforces a range at all is
# unknown, so these are deliberately generous bounds around the one value we
# actually saw.
DEFAULT_COOLING_MINUTES = 30
MIN_COOLING_MINUTES = 1
MAX_COOLING_MINUTES = 240

# Bounds for the per-side Rapid Cool Duration slider. Deliberately narrower
# than the service, which still accepts 1 to 240. The app's own picker reads
# from a HOT_FLASH_DURATION_OPTIONS list that lives in a separate bytecode
# module and was never resolved, so these are our choice, not the vendor's:
# wide enough to be useful for hot flash relief, tight enough that a stray
# drag cannot leave a side cold for four hours.
RAPID_COOL_SLIDER_MIN = 5
RAPID_COOL_SLIDER_MAX = 120
RAPID_COOL_SLIDER_STEP = 5

# Display aliases. Maps an immutable Orion user id to the name the household
# actually uses, since Orion stores whatever was on the account at signup.
#
# Changing an alias is always safe. It is what the two identifiers below do
# and do not derive from that decides why, and an earlier version of this
# comment got the second one wrong in a way that read as a guarantee.
#
#   unique_id   Built from device and zone ids, never from a person's name.
#               True. Renaming cannot orphan recorder history, long term
#               statistics, dashboards, or voice assistant mappings.
#
#   entity_id   DERIVED FROM THE DISPLAY NAME. Home Assistant slugifies an
#               entity's `_attr_name` into its entity_id at FIRST
#               registration and never revisits it on a later rename, so
#               the first name an entity ever carries is the one its
#               entity_id keeps forever. Setting an alias later changes the
#               friendly name shown in the UI and leaves the entity_id, and
#               everything keyed on it, exactly where it was. That is why
#               renaming is non-breaking, and it is the opposite of the
#               reason this comment used to give.
#
# That permanence is also why a vendor-supplied name is not trusted on the
# way to an entity name. `orion_sleep_api.util.orion_user_label` falls back
# through first_name, firstName, name, email, and finally phone, so an
# account that never had a name set reaches a login credential on the
# ordinary path rather than as an edge case. `helpers.is_safe_display_name`
# is the predicate that refuses an email or a phone number before it can be
# slugified into a permanent identifier, and every naming path in this
# integration converges on it through `coordinator.display_name_for_user`.
# An attribute carrying a credential can be purged later. An entity_id
# cannot.
CONF_DISPLAY_ALIASES = "display_aliases"

# The Orion app displays temperature as a relative offset (-10 to +10).
# The mapping between offset and absolute Celsius is NON-LINEAR and comes
# from the device's temperature_scale.relative[] lookup table.
# Fallback table used when the device data isn't available yet:
DEFAULT_RELATIVE_TEMP_TABLE: list[dict[str, float]] = [
    {"in": -10, "out": 10},
    {"in": -9, "out": 12},
    {"in": -8, "out": 14},
    {"in": -7, "out": 16},
    {"in": -6, "out": 17.5},
    {"in": -5, "out": 19},
    {"in": -4, "out": 20.5},
    {"in": -3, "out": 23},
    {"in": -2, "out": 24.5},
    {"in": -1, "out": 26},
    {"in": 0, "out": 27.5},
    {"in": 1, "out": 29},
    {"in": 2, "out": 30.5},
    {"in": 3, "out": 32},
    {"in": 4, "out": 33.5},
    {"in": 5, "out": 35},
    {"in": 6, "out": 37},
    {"in": 7, "out": 39},
    {"in": 8, "out": 41},
    {"in": 9, "out": 43},
    {"in": 10, "out": 45},
]

# How long an occupancy reading has to hold before it is believed.
#
# Measured over one night (2026-07-27, 20 hours of recorder history):
# the two pads produced 16 spurious on-episodes between them, every one
# under 3.7 minutes, alongside exactly one real episode of 527 minutes.
# Nothing landed in between. Five minutes sits in that gap with a wide
# margin on both sides.
OCCUPANCY_HOLD_SECONDS = 300
