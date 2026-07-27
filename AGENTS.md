# Orion Sleep - Home Assistant HACS Integration

## Project Overview

HACS-compatible Home Assistant custom integration for the **Orion Sleep** smart mattress topper. Per-person temperature control and scheduling, live vitals, sleep tracking (heart rate, breath rate, HRV, sleep stages), and rapid cooling.

**This repository is the canonical implementation.** It started as a fork and has since diverged past the point where upstream is a useful reference. The API contract was rebuilt from measured traffic rather than inherited: of the original 38 documented operations, 9 were fabrications traceable to feature flags, cache keys, and SDK method names, 4 more carried the wrong method or path, and 3 device routes pointed at the wrong identifier. The most-exercised endpoint in the integration was missing entirely.

Do not treat any upstream or sibling fork as authoritative on API behaviour. Treat the verification log at the bottom of this file as authoritative, and nothing else.

## Repository Structure

```
home-assistant-orion-integration/
├── hacs.json                          # HACS repo metadata
├── README.md                          # User-facing install/usage docs
├── openapi.yaml                       # OpenAPI 3.1 spec (reverse-engineered; WS section validated on-wire)
├── orion_info.py                      # Working CLI script (REST + WS capture tooling)
├── custom_components/
│   └── orion_sleep/
│       ├── __init__.py                # async_setup_entry / async_unload_entry
│       ├── manifest.json              # HA integration manifest (v2.0.0)
│       ├── const.py                   # DOMAIN, config keys, defaults, temp lookup table
│       ├── api.py                     # Async aiohttp API client
│       ├── coordinator.py             # DataUpdateCoordinator + data helpers
│       ├── config_flow.py             # Three-step auth flow + options flow
│       ├── entity.py                  # Base entity with DeviceInfo + temp conversion helpers
│       ├── climate.py                 # Independent live control for each device zone
│       ├── sensor.py                  # Sleep, schedule, live, and diagnostic sensors
│       ├── websocket.py                # Live device WebSocket client (per-device aiohttp)
│       ├── binary_sensor.py           # Session, occupancy, quiet mode, and safety
│       ├── button.py                  # Measured reboot and forget-WiFi actions
│       ├── switch.py                  # Runtime power and authenticated-user away mode
│       ├── diagnostics.py             # Diagnostics with PII redaction
│       ├── strings.json               # UI translations
│       ├── translations/
│       │   └── en.json                # English translations (mirrors strings.json)
│       └── brand/                     # Integration icon (96px + 180px)
```

## Source-of-Truth Policy

Both `openapi.yaml` and `orion_info.py` are kept in sync as new endpoints or behaviors are discovered. Live requests and captured mobile app traffic are authoritative. Android bytecode and UI capability lists are discovery aids only. `openapi.yaml` uses `x-verification-status` on important operations to distinguish measured behavior from app-derived behavior. When the files disagree, re-verify against the live server.

**A claim only becomes "measured" by appearing in the Verification Log at the bottom of this file.** A confident docstring is not evidence. A decompiled line number is not evidence. A request that was actually sent, and its result recorded, is evidence.

**Never ship a user-facing control against an unverified route.** A control that silently fails is worse than no control, and the codebase has removed several for exactly that reason. If a write cannot be demonstrated against a live bed, expose the read side only and say why in the docstring.

Writes that change the bed are tested with a backup, one single-field change, verification, and an unconditional restore. Where the field is schedule-shaped, probe a weekday that is **not today**, so nothing about that night can change.

Known gaps and unverified endpoints are called out in the tables below. When adding or changing behavior:

1. Prefer running `orion_info.py --ws-scenario` (or the individual flags) against a live account to confirm on-wire shapes.
2. Update **both** `openapi.yaml` and the relevant comments/flags in `orion_info.py`.
3. Reflect any new limitations or caveats in this file.

### API Base URL

```
https://api1.orionbed.com
```

### Working Endpoints

| Method | Path | Auth | Notes |
|--------|------|------|-------|
| POST | `/v1/auth/code` | No | Send verification code to email/phone |
| POST | `/v1/auth/verify` | No | Verify code, get tokens. Response nested: `response.session.{access_token, refresh_token, expires_at}` |
| POST | `/v1/auth/refresh` | No | Refresh tokens. Body: `{"refresh_token": "..."}`. Response may be nested or top-level. |
| GET | `/v1/auth/me` | Bearer | User profile. Wrapped in `{"response": {...}, "success": true}` |
| GET | `/v1/devices` | Bearer | Devices at `response.devices[]`. Fields: `id`, `serial_number`, `name`, `model`, `zones[]`, `temperature_range`, `temperature_scale` |
| GET | `/v1/sleep-schedules` | Bearer | Schedules at `response.schedules.{user_id}[]` (7 days). Also `today_sleep_schedule.{user_id}` |
| PUT | `/v1/sleep-schedules` | Bearer | Update schedule. Body: `{"schedules": [{"day": N, field: value}]}`. Partial updates work (only specified field changes). |
| POST | `/v1/sleep-configurations/user-away` | Bearer | Presence override. Body: `{"user_id": "...", "is_away": bool}`. Also powers the device down; prefer `/v1/devices/{id}/live` for pure power control. |
| PUT | `/v1/devices/{deviceId}` | Bearer | Update metadata (`name`, `orientation`, `timezone`). Partial updates accepted. |
| GET | `/v1/devices/{serial_number}/live` | Bearer | **Live runtime snapshot** (zones with `on`/`temp`, status, sensors, firmware). Path uses `serial_number`, NOT UUID. |
| PUT | `/v1/devices/{serial_number}/live` | Bearer | **Canonical power/temp/settings primitive.** Path uses `serial_number`, NOT UUID (UUID returns `403 "Device not found"`). Four known body keys, **one per request**: `zones` (array of `{id, on?, temp?}`, Celsius), `led_brightness` (int 0-100), `quiet_mode` (bool), `water_fill` (`"pour_water"` or `"unknown"`). The app never merges keys; `zones` is the only thing it batches. Returns the full live-device object, so a caller can update local state without a refetch. |
| PUT | `/v1/devices/{serial_number}/live/zones/{zoneId}` | Bearer | Single-zone power/temp. Path uses `serial_number`. Body: `{on?, temp?}` with `minProperties: 1`. |
| POST | `/v1/devices/{serial_number}/action` | Bearer | **Measured.** Accepts only `{"action_type": "reboot"}` or `{"action_type": "forget_wifi"}`. Uses serial number, not UUID. Note the wire values have no `device_` prefix, unlike the `allowed_actions` capability list. |
| POST | `/v1/devices/{deviceId}/activate` | Bearer | Pair device to account. Body: `{"model": "OSCT001-1"}`. |
| POST | `/v1/devices/{deviceId}/deactivate` | Bearer | Unpair device. |
| POST | `/v1/devices/{deviceId}/update` | Bearer | Trigger firmware update. |
| GET | `/v2/insights?from=&to=` | Bearer | NOT wrapped in `response`. Top-level: `{user_id, data: {date: {score, sessions[]}}, overview: {date: {score}}}` |

### Non-Working / Unverified Endpoints

| Path | Status | Notes |
|------|--------|-------|
| `/v1/sleep-configurations/devices` | **404** | A prior speculative contract listed it. The route does not exist. |
| `/v1/sleep-configurations/temperature` | Removed from measured contract | Use the verified live zone endpoints for runtime temperature writes. |
| `/v1/sleep-schedules?action=enable` | Unverified and not exposed | Only partial schedule field updates are verified. |
| `/v1/session-state` | Returns onboarding state | `{patch_step, is_survey_complete, ...}` — NOT sleep session state |
| `device_led_brightness` / `device_quiet_mode` at `/action` | **Rejected** | These are UI capability identifiers, not action names. They decide whether the app renders a control. Three separate efforts fired them at `/action` and were rejected. The real write route is `PUT .../live`, documented below. |

### Real API Response Shapes

**Devices** — each device has:
- `id` (UUID), `serial_number`, `name`, `model` ("OSCT001-1"), `type` ("control_tower")
- `zones`: `[{id: "zone_a", user: {...}}, {id: "zone_b", user: {...}}]`
- `temperature_range`: `{min: 10, max: 45}` (Celsius)
- `temperature_scale.fahrenheit[]`: `{in: 50..113, out: 10..45}` mapping
- `temperature_scale.relative[]`: `{in: -10..+10, out: 10..45}` non-linear offset-to-Celsius mapping
- `orientation`, `timezone`, `permissions`, `default_zone_id`

**Schedules** — keyed by user_id, 7 entries (day 0-6):
- `bedtime`, `wakeup` (HH:mm strings)
- `bedtime_is_active`, `wakeup_is_active` (booleans)
- `bedtime_temp`, `wakeup_temp`, `phase_1_temp`, `phase_2_temp` (Celsius floats)
- `auto_turn_off`, `is_smart_temperature_active`
- `override_date`, `is_override_available`, `is_override_applied`

**Insights sessions** — each session has:
- `session_id`, `zone_id`, `is_in_progress`, `start_time`, `end_time`, `confidence`
- `sleep_summary`: `{time_asleep, deep_sleep, rem_sleep, light_sleep, awake_time}` (minutes)
- `heart_rate`: `{average, min, max, values[]}` (BPM)
- `breath_rate`: `{average, min, max, values[]}` (breaths/min)
- `hrv`: `{average, min, max, values[]}` (ms, often null)
- `movement`: `{total_seconds, movement_rate, left_bed_seconds, values[]}`
- `temperature`: `{values[]}` (Celsius floats, ~3 per minute)

### Key Gotchas

- Token fields are **snake_case** (`access_token`, NOT `accessToken`)
- Refresh response may be nested (`response.session`) or flat — handle both
- Token expiry uses `expires_at` Unix timestamp, NOT JWT parsing
- Insights endpoint (`/v2/insights`) does NOT wrap in `response` — it's top-level
- All other endpoints wrap data in `{"response": {...}, "success": true}`
- Temperature values throughout the API are in **Celsius**
- Device zones are `zone_a`/`zone_b`, not `left`/`right`
- Sleep session detection uses `is_in_progress` from insights, not `/v1/session-state`
- Device power state is read from each zone's `on`/`is_on` field and written through `/v1/devices/{serial_number}/live`. `set_user_away` is a separate, user-specific presence override.
- Temperature offsets (app-style -10 to +10) map **non-linearly** to absolute Celsius via `temperature_scale.relative` table

## Architecture

- **Polling**: `DataUpdateCoordinator` polls `/v1/devices`, `/v1/sleep-schedules`, and `/v2/insights` on a configurable interval (default 600s)
- **One-time data**: User profile fetched once in `_async_setup()`
- **Per-poll data**: Device list re-fetched each poll to detect away/present (power) state changes
- **Token persistence**: Refresh callback updates `config_entry.data` so tokens survive HA restarts
- **Error handling**: Each polled endpoint has independent try/except — one failing doesn't break the others. Auth errors (`OrionAuthError`) always raise `ConfigEntryAuthFailed` to trigger re-auth flow.
- **Auth flow**: Three-step config flow (pick method -> enter email/phone -> enter verification code) + re-auth support
- **Options flow**: Configurable `scan_interval` (60-3600s) and `insights_days` (1-30 days)
- **Temperature conversion**: `OrionBaseEntity` provides `_celsius_to_offset()` and `_offset_to_celsius()` using per-device lookup table (falls back to `DEFAULT_RELATIVE_TEMP_TABLE` in `const.py`)

### Data Flow

```
Config Flow (auth) --> tokens stored in config_entry.data
       |
       v
__init__.py creates OrionApiClient + OrionDataUpdateCoordinator
       |
       v
coordinator._async_setup() -- fetches user profile + devices (once)
       |
       v
coordinator._async_update_data() -- polls every N seconds:
  1. ensure_valid_token() (auto-refresh, persists via callback)
  2. list_devices()        --> coordinator.devices (away/present detection)
  3. OrionWebSocketManager.sync_to_serials() (start/stop per-device WS)
  4. get_live_device(serial) per device (skipped when WS is fresh)
  5. get_sleep_schedules() --> data["schedules"]
  6. get_insights(days=N)  --> data["insights"]
       |
       v
Per-device live WebSocket (wss://live.api1.orionbed.com/device/<serial>):
  - Pushes live_device.snapshot on connect, live_device.update on every
    state change (+ idle heartbeat every ~2s)
  - Coordinator._handle_ws_message merges payload into live_devices
    and calls async_set_updated_data() so entities refresh immediately
  - Timeline field is stored at data["ws_timelines"][device_id]
       |
       v
Entities read from coordinator:
  - Climate: live per-zone setpoint, measured temp, power, and thermal state
  - Number: per-phase app-style temperature offsets (-10..+10)
  - Sensors: insights sessions + schedule + overview scores
             + per-topper-sensor live HR/BR/status (from WS)
  - Binary sensors: session.is_in_progress
                    + per-topper-sensor occupancy (from WS)
  - Switches: device zones (runtime power) + authenticated-user away mode
  - Diagnostic sensors: per-device WS connection state
                        + per-topper-sensor raw status_text
```

## Entities

| Platform | Entity | Key / unique_id suffix | Data Source |
|----------|--------|----------------------|-------------|
| Climate | Per-zone Climate | `_climate_{zone_id}` | Live zone setpoint, measured temperature, power state, and thermal state. Writes through `/v1/devices/{serial}/live/zones/{zoneId}`. |
| Sensor | Sleep Score | `_sleep_score` | `insights.overview.{latest_date}.score` with `quality_rating` extra attr |
| Sensor | Total Sleep Time | `_total_sleep_time` | `session.sleep_summary.time_asleep` (formatted as "Xh Ym") |
| Sensor | Deep Sleep Time | `_deep_sleep_time` | `session.sleep_summary.deep_sleep` |
| Sensor | REM Sleep Time | `_rem_sleep_time` | `session.sleep_summary.rem_sleep` |
| Sensor | Light Sleep Time | `_light_sleep_time` | `session.sleep_summary.light_sleep` |
| Sensor | Awake Time | `_awake_time` | `session.sleep_summary.awake_time` |
| Sensor | Heart Rate Average | `_heart_rate_avg` | `session.heart_rate.average` + min/max/range extra attrs |
| Sensor | Breath Rate | `_breath_rate` | `session.breath_rate.average` + min/max/range extra attrs |
| Sensor | HRV | `_hrv` | `session.hrv.average` + min/max extra attrs |
| Sensor | Body Movement Rate | `_body_movement_rate` | `session.movement.movement_rate` |
| Sensor | Restless Time | `_restless_time` | `session.movement.total_seconds` (formatted as "Xm Ys") |
| Sensor | Total Sleep Minutes | `_total_sleep_minutes` | `session.sleep_summary.time_asleep`, numeric. `state_class=measurement`, so it keeps long-term statistics. The string sensors above cannot. |
| Sensor | Deep Sleep Minutes | `_deep_sleep_minutes` | `session.sleep_summary.deep_sleep`, numeric |
| Sensor | REM Sleep Minutes | `_rem_sleep_minutes` | `session.sleep_summary.rem_sleep`, numeric |
| Sensor | Light Sleep Minutes | `_light_sleep_minutes` | `session.sleep_summary.light_sleep`, numeric |
| Sensor | Awake Minutes | `_awake_minutes` | `session.sleep_summary.awake_time`, numeric |
| Sensor | Last Session End | `_last_session_end` | `session.end_time` of the newest FINISHED session. Timestamp. Selected via `is_in_progress`, never via a missing `end_time`. |
| Binary Sensor | Sleep Session (partner) | `_partner_session_active` | Partner `session.is_in_progress`. Only created when a partner is linked. |
| Sensor | Bedtime | `_bedtime` | `today_sleep_schedule.bedtime` (HH:mm) |
| Sensor | Wake-up Time | `_wakeup_time` | `today_sleep_schedule.wakeup` |
| Sensor | Schedule Duration | `_schedule_duration` | Calculated from bedtime/wakeup (handles overnight) |
| Sensor | Bedtime Temperature | `_bedtime_temp` | `today_sleep_schedule.bedtime_temp` + phase/smart temp extra attrs |
| Sensor | Wake-up Temperature | `_wakeup_temp` | `today_sleep_schedule.wakeup_temp` |
| Sensor | Current Temp Offset | `_current_temp_offset` | Latest session `temperature.values[-1]` converted to app-style offset. |
| Sensor (diag) | Live Connection | `_websocket_state` | WS connection state (`connecting`/`connected`/`reconnecting`/`device_offline`/`auth_failed`/`stopped`) plus `seconds_since_last_message` extra attr |
| Sensor | Sensor 1/2 Heart Rate | `_sensorN_live_heart_rate` | WS `status.sensors.sensorN.heart_rate` (bpm). `0` (empty bed) and `255` (no reading yet) both mapped to `None`. |
| Sensor | Sensor 1/2 Breath Rate | `_sensorN_live_breath_rate` | WS `status.sensors.sensorN.breath_rate` (br/min). Same sentinel handling. |
| Sensor (diag) | Sensor 1/2 Status | `_sensorN_sensor_status` | Raw `status_text`: observed `left_bed` (empty) and `normal` (occupied). |
| Binary Sensor | Sleep Session Active | `_session_active` | `session.is_in_progress` (shows "Asleep" / "Not asleep") |
| Binary Sensor | Sensor 1/2 On Bed | `_sensorN_on_bed` | Occupancy device class. `status_text != "left_bed"`. The WS push itself is realtime, but the topper takes ~30s–1min to decide someone has sat down or left, so `status_text` transitions lag the real event. |
| Switch | Power | `_power` | On = all zones on, Off = all zones off. Uses `PUT /v1/devices/{serial_number}/live`. State is read from each zone's `on`/`is_on` field. |
| Switch | Away Mode | `_away_mode` | On = authenticated user marked away, Off = present. State checks that user's ID across `zones[*].user`. `POST /v1/sleep-configurations/user-away`. The known redundant-toggle 400 is swallowed. |
| Number | Bedtime Temperature Offset | `_bedtime_temp_offset` | App-style -10..+10 slider. Reads `today_sleep_schedule.bedtime_temp`, converts to offset via per-device relative table; writes back via `PUT /v1/sleep-schedules` on today's day-of-week, carrying an explicit `user_id`. |
| Number | Asleep Phase 1 Offset | `_phase_1_temp_offset` | As above, `phase_1_temp` field. |
| Number | Asleep Phase 2 Offset | `_phase_2_temp_offset` | As above, `phase_2_temp` field. |
| Number | Wake Up Temperature Offset | `_wakeup_temp_offset` | As above, `wakeup_temp` field. |
| Sensor | \<person\> Asleep Phase 1 Temperature | `_user_{userId}_phase_1_temp` | Promoted from an extra attribute so it graphs and generates statistics. New for both people. |
| Sensor | \<person\> Asleep Phase 2 Temperature | `_user_{userId}_phase_2_temp` | As above, `phase_2_temp`. |
| Binary Sensor | \<person\> Bedtime Enabled | `_user_{userId}_bedtime_is_active` | Read-only. The field name is accepted by the measured write route, but no individual flag has been executed, so the write stays app-derived. |
| Binary Sensor | \<person\> Wake Up Enabled | `_user_{userId}_wakeup_is_active` | As above. |
| Binary Sensor | \<person\> Automatic Turn Off | `_user_{userId}_auto_turn_off` | As above. |
| Binary Sensor | \<person\> Smart Temperature | `_user_{userId}_is_smart_temperature_active` | As above. |
| Binary Sensor (diag) | \<person\> Schedule Override | `_user_{userId}_is_override_applied` | Whether a single-day override is in force. `override_date` and `override_available` as attrs. Explains a surprising bedtime rather than being something to act on. |

| Sensor | \<zone\> Measured Temperature | `_{zoneId}_measured_temp` | `status.zones[].temp`. Duplicates the climate entity's `current_temperature` on purpose: climate attributes are not retained as long-term statistics, a `sensor` with a `state_class` is. |
| Sensor | \<zone\> Target Temperature | `_{zoneId}_target_temp` | `zones[].temp`, the LIVE setpoint. Distinct from `today_sleep_schedule.*_temp`, which is schedule intent and diverges the moment a zone is nudged by hand. |
| Sensor | \<person\> Next Scheduled Action | `_user_{userId}_next_scheduled_action` | Timestamp of the next temperature change the bed will make on its own, with the action name and per-zone targets as attrs. Built from the WS `timeline`, which is materialized server-side and so reflects overrides and smart temperature. **The server sends `timeline: []` even mid-schedule, measured 2026-07-27 at 01:20 local across five consecutive `update` frames, so this sensor reads unavailable.** Whether the array is ever non-empty is unconfirmed. |
| Binary Sensor (diag) | Device Online | `_device_online` | `status.online`, the server's view of the topper. Distinct from Live Connection, which is OUR socket to Orion. The two can legitimately disagree in both directions. |
| Update | Firmware | `_firmware_update` | `installed_version` from `status.firmware.cb`, `latest_version` from `status.pending_update.is_available`. Supports INSTALL and PROGRESS. Replaced the read-only Firmware Update Available binary sensor. See the caveats below. |
| Number | LED Brightness | `_led_brightness` | 0-100 write via `PUT /v1/devices/{serial_number}/live`. Debounced with an optimistic local write and a post-write lock, mirroring the vendor app. |
| Switch | Quiet Mode | `_quiet_mode` | Write via the same live route. Replaced the old read-only binary sensor once the write was measured. |
| Binary Sensor | \<zone\> Cooling | `_{zoneId}_thermal_relief` | Hot flash relief running on that side. Reads `zones[].thermal_relief`, and counts as active only when `end_time` is finite AND in the future, which is the app's own test. Exposes `ends_at`, `previous_temp`, and `previous_on`, so the temperature the bed will restore is visible while cooling runs. |
| Switch | \<person\> Rapid Cool | `_{zoneId}_rapid_cool` | Hot flash relief as a toggle. On starts a 30 minute window on that side, off cancels it and restores the previous setpoint. A switch rather than two buttons because the bed tracks the window server-side, so off is a genuine cancel. Carries the same attrs as the Cooling binary sensor. |
| Sensor | \<person\> Cooling Ends | `_{zoneId}_cooling_ends` | TIMESTAMP device class, so Home Assistant renders a live countdown that ticks on its own. `None` whenever cooling is not running. The same value exists as an `ends_at` attribute on the switch, but an attribute is static text. |
| Button | Swap Bed Sides | `_action_swap_sides` | `POST /v1/sleep-configurations/user-swap`. Enabled by default: pressing it again reverses it, so a misfire is cheap. |
| Button (disabled) | Split Zones | `_action_split_zones` | `POST /v1/sleep-configurations/user-split`. Disabled by default because **nothing in the live payload reports split state**, so a press has no observable result and no way to confirm what it did. |

**A two-zone device with no partner linked exposes 69 base entities. A linked partner adds 35 more, for 104.**

- 2 climate entities, one per zone.
- 33 sensors: 11 insights, 5 schedule temperatures and duration, current offset, live connection, 6 topper sensor readings, 2 measured and 2 target zone temperatures, 2 cooling countdowns, LED brightness, firmware, and Wi-Fi signal.
- 5 numbers: 4 schedule-phase temperature offsets plus LED brightness.
- 2 time entities: bedtime and wake up time.
- 9 switches: runtime power, Away Mode, quiet mode, 2 rapid cool, and 4 schedule flags. Away Mode is omitted for accounts with multiple devices because the API action is account-global.
- 7 binary sensors: sleep session, 2 occupancy sensors, 2 cooling sensors, safety problem, and the schedule override indicator.
- 1 update entity: Control Tower firmware.

### Firmware updates

Modelled on the Lucid Motors integration's `update.py`, which is the
closest well-built analogue: a device that reports its own installed
version and can be told to flash, with no way to roll back.

The read half is MEASURED. The install half is not, and cannot be made so
on demand:

- `latest_version` returns `installed_version` when nothing is waiting.
  That equality is how an update entity says "up to date".
- When an update **is** waiting, Orion says only
  `pending_update.is_available: true`. It has never named the version.
  So `latest_version` falls back to the literal string `Available`, which
  is a placeholder, not data. Every plausible version key in the block is
  checked first, so this stops being reachable the day Orion starts
  naming them.
- `PROGRESS` reports a bare boolean. Nothing carries a percentage, so the
  bar is indeterminate. `RELEASE_NOTES` is unsupported: no route exists.
- `POST /v1/devices/{serial_number}/update` takes the **serial**. The
  client had it wired to the UUID until 2026-07-27, which would have
  404'd exactly the way `/action` did. `activate_device` and
  `deactivate_device` carried the same bug and were corrected at the same
  time. All three still have zero verified executions.

This is the only write in the integration with no save-and-restore path,
and the only one that cannot be provoked for testing, because
`is_available` has been false continuously. That trade-off was accepted
on 2026-07-27: the first real update is the test, and warranty covers
the downside.
- 3 buttons: reboot, swap bed sides, and split zones. The last two are real routes from the app; reboot and split ship disabled by default. Forget Wi-Fi is intentionally not exposed.

A linked partner adds 11 insight sensors plus a second full schedule family of 16: 5 sensors, 4 offset numbers, 2 time entities, 4 switches, and the override indicator.

### Schedule entities are per person

`GET /v1/sleep-schedules` returns rows for everyone on the bed in a single
fetch with the primary token, so a partner's schedule costs no extra
request and stays readable even when their own token has expired. Writes
carry an explicit `user_id`, which the API honours, so one account sets
both people's temperatures. Neither half needs the partner's client.

`unique_id` is `{deviceId}_user_{userId}_{key}` for everyone, keyed on the
immutable Orion user id. Never on a role like "partner", which would
silently swap owners if the integration were re-authenticated as the
other account. Never on a display name, which is user-editable.

Deliberately uniform, with no exception for the authenticated account. An
earlier draft preserved nine un-namespaced ids so pre-existing history
would survive, but nothing had been built on those entities, so the only
thing that exception bought was a permanent special case in the code and
an asymmetry between the two people on the bed.

**Platform choice follows the write surface.** All ten writable fields
were measured on 2026-07-26, so anything settable is a control:

| Fields | Platform | Why |
|---|---|---|
| `bedtime`, `wakeup` | `time` | Wall clock with no date. NOT `SensorDeviceClass.TIMESTAMP`, which needs a tz-aware datetime, and the bed carries its own timezone. |
| 4 schedule booleans | `switch` | Writable, so a binary sensor would understate them. |
| 4 phase temperatures | `number` (offset) + `sensor` (absolute) | The number is the app-style -10..+10 control. The sensor carries absolute Celsius with a `state_class` so it generates long-term statistics, which a `number` does not. |
| `schedule_duration` | `sensor` | Computed, not stored. |
| `is_override_applied` | `binary_sensor` | Read-only. No route to CLEAR an override has been found. |

Both zone temperature sensors and the LED brightness sensor carry
`state_class=MEASUREMENT` so they generate long-term statistics. The
`number` entities do not and never will, which is why the LED keeps both
a Number (write) and a Sensor (history).

### Sensor Implementation Notes

- Duration sensors (total sleep, deep sleep, etc.) deliberately avoid `device_class=DURATION` because HA would override entity names
- Sleep score has special handling: reads from `insights.overview` (not sessions) and adds `quality_rating` extra attribute ("Excellent" >= 90, "Good" >= 80, "Fair" >= 60, "Poor" < 60)
- Temperature offset conversion uses per-device `temperature_scale.relative` lookup table, non-linear mapping
- Heart rate and breath rate sensors include min/max/range as extra state attributes

## API Client (`api.py`)

### Exception Hierarchy
- `OrionApiError` — base for all API errors
- `OrionAuthError(OrionApiError)` — 401 / invalid tokens
- `OrionConnectionError(OrionApiError)` — network failures (`aiohttp.ClientError`)

### Token Management
- `_token_expired(margin_seconds=60)` — checks `time.time() + 60` against `expires_at`
- `ensure_valid_token()` — auto-refreshes if expired
- `_refresh_tokens()` — handles both nested (`response.session`) and flat response shapes. Calls are serialized so rotating refresh tokens cannot race.
- `set_token_refresh_callback(callback)` — called after successful refresh to persist tokens

### Action Methods
| Method | Endpoint | Status |
|--------|----------|--------|
| `set_user_away(user_id, is_away)` | `POST /v1/sleep-configurations/user-away` | Working (used by away-mode switch; presence override) |
| `update_device(device_id, **fields)` | `PUT /v1/devices/{deviceId}` | Metadata updates (name/orientation/timezone) |
| `update_live_device_zones(serial_number, zones)` | `PUT /v1/devices/{serial_number}/live` | **Canonical power primitive** (used by power switch) |
| `update_live_device_zone(serial_number, zone_id, on=, temp=)` | `PUT /v1/devices/{serial_number}/live/zones/{zoneId}` | Per-zone power/temp |
| `device_action(serial_number, action)` | `POST /v1/devices/{serial_number}/action` | Measured values: `reboot`, `forget_wifi`. Uses body key `action_type`. |
| `activate_device(device_id, model)` | `POST /v1/devices/{deviceId}/activate` | Pair device |
| `deactivate_device(device_id)` | `POST /v1/devices/{deviceId}/deactivate` | Unpair device |
| `trigger_firmware_update(device_id)` | `POST /v1/devices/{deviceId}/update` | Firmware update |
| `update_schedule_temperature(day, field, celsius)` | `PUT /v1/sleep-schedules` | Partial updates verified |

## Testing

### Unit suite

```bash
mise exec pipx:pytest -- pytest -q          # 95 tests
mise exec pipx:ruff -- ruff check custom_components tests orion_info.py
mise exec -- python -m compileall -q custom_components tests
```

**Home Assistant and aiohttp are deliberately not installed**, and CI runs
without them. That constraint shapes the whole suite. Tests reach the
integration two ways, both via `tests/_orion.py`:

| Helper | Use |
|--------|-----|
| `_orion.load("util")` | Imports a dependency-free module off disk and exercises it normally. Covers `util.py` and `live_state.py`. |
| `_orion.tree("api")` | Parses a module as source with stdlib `ast`, never importing it. Covers `api.py` and `coordinator.py`. |

| File | Tests | Covers |
|------|-------|--------|
| `tests/test_util.py` | 58 | Pure helpers: unique ids, aliases, schedule validation, session selection, timeline parsing |
| `tests/test_api_errors.py` | 18 | Structural leak guards on `api.py`, malformed vendor payloads, auth response shapes |
| `tests/test_coordinator_safety.py` | 12 | Exception handler ordering, account isolation, poll carry-forward, hostile data |
| `tests/test_live_state.py` | 7 | Live payload extraction |

### The structural guards, and why they exist

Three bugs in this integration failed **silently**. A unit test that imports
and calls cannot catch them, because the code runs fine and simply does the
wrong thing. So they are enforced by parsing the source instead.

**1. Exception messages must not leak identifiers.** Messages once carried
the full request path, and `/live` and `/action` paths contain the device
serial. `test_exception_messages_interpolate_only_allowlisted_expressions`
holds an **allowlist** of four permitted interpolations (`method`,
`resp.status`, `type(err).__name__`, `keys`). A new substitution fails the
build until a human reviews it. An allowlist, not a blocklist, because a
blocklist passes anything nobody thought to ban.

**2. `OrionAuthError` subclasses `OrionApiError`.** Python matches handlers
top to bottom, so listing the parent first swallows every auth failure into
the generic branch. The integration then logs a warning and continues with a
dead token. The only symptom is that data quietly stops updating.
`test_auth_handler_always_precedes_the_general_handler` enforces the order.

**3. A partner auth failure must not reauth the primary account.**
`ConfigEntryAuthFailed` launches Home Assistant's reauth flow, which
re-verifies `CONF_AUTH_VALUE`, the **primary** account's email or phone.
Raising it because the partner token expired prompts the wrong person, and
completing the flow cannot fix the partner token anyway.
`test_partner_client_calls_never_raise_config_entry_auth_failed` proves no
`try` block touching `partner_api_client` raises it.

Also guarded: `_async_update_data` must carry `schedules`, `insights`, and
`partner_insights` forward across a failed poll rather than re-initialising
them empty. Blanking meant one transient 502 turned every dependent entity
`unknown` for the full poll interval.

**All of these were mutation tested.** Each bug was reintroduced, the
corresponding test was confirmed to fail, and the file was restored. A guard
that has never been seen to fail is not a guard.

### Live API verification

Run `orion_info.py` to verify API connectivity and response shapes:
```bash
python orion_info.py --email user@example.com
python orion_info.py --phone 15132015808
```
Tokens cache to `~/.orion_tokens.json`. Use `--relogin` to force fresh auth.

Additional `orion_info.py` flags:
- `--insights-days N` — number of days of insights to fetch
- `--set-away` / `--set-present` — toggle device power, then re-fetch devices/schedules to show changes
- `--power-on` / `--power-off` — write all zones through the verified serial-number live route
- `--websocket [--ws-duration N]` — open `/device/<serial>?token=<JWT>` and log every frame for N seconds (default 60)
- `--ws-scenario` — open the WebSocket and drive a scripted sequence of REST edits (zone on/off, temp low/high, bulk on/off, user-away) while logging frames; restores the original zone state at the end. Use this to re-verify the event taxonomy against the live server.

## WebSocket — Live Device Data

Validated against the live server with `orion_info.py --ws-scenario`.

### Connection

```
wss://live.api1.orionbed.com/device/<serial_number>?token=<JWT>
```

- Path uses the device's **`serial_number`**, NOT its UUID `id` (UUID returns 404 `{"error":"Not Found","message":"Device not found"}`).
- JWT is passed as a `token` query parameter.
- Cloudflare negotiates HTTP/2 by default which breaks the WS upgrade — the SSL context **must force ALPN to `http/1.1`**.
- Working User-Agent: `okhttp/4.12.0`.
- **No client-side handshake**. The server pushes `live_device.snapshot` immediately after the Upgrade completes, then `live_device.update` on state changes and approximately every 2s as an idle refresh.
- Close code `1001` on clean client shutdown.
- On 401 during upgrade, refresh via `POST /v1/auth/refresh` and reconnect with the new token.

### Event Taxonomy (exhaustive as of last capture)

| `type` | When | Notes |
|---|---|---|
| `live_device.snapshot` | Once, immediately after connect | Full state |
| `live_device.update` | On every REST mutation to `/v1/devices/{serial}/live[/zones/{zone}]` or `/v1/sleep-configurations/user-away`, plus ~every 2s as an idle refresh | Same payload shape as snapshot; may include a `timeline` array of today's schedule actions |

Both use the envelope `{"type": <event>, "payload": {...}}`. `set_user_away` does **not** emit a distinct event type — it produces another `live_device.update` with zones powered accordingly.

### Payload Shape (shared between snapshot and update)

```text
payload.serial_number         string
payload.model                 e.g. "OSCT001-1"
payload.zones[]               setpoints (user intent): {id, temp (°C), on}
payload.led_brightness        int 0-100
payload.water_fill            string (observed "unknown")
payload.is_in_water_fill_mode bool
payload.status.online         bool
payload.status.firmware       {cb, ib}
payload.status.firmware_update {workflow_id, started_at, updated_at, in_progress,
                                current_step, completed_at, result}
payload.status.pending_update {is_available}
payload.status.network        {last_seen, name, ip, rssi, uptime, mac}
payload.status.safety         {error, error_codes[], error_descriptions[]}
payload.status.zones[]        measured: {id, temp (°C), thermal_state}
payload.status.sensors.sensor1, sensor2
                              {heart_rate, breath_rate, status, status_text,
                               sign_of_asleep, sign_of_wake_up, timestamp,
                               uptime, is_working, firmware_version,
                               hardware_version}
payload.timeline[]            only on update; today's scheduled actions:
                              {id, user_id, label (bedtime|phase_1|phase_2|
                               wake_up|turn_off), scheduled_time, action:
                               {zones:[...]}, created_at}
```

Notable:
- `payload.zones[].temp` is the **setpoint**. The **measured** zone temperature lives at `payload.status.zones[].temp`.
- `status.zones[].thermal_state` was only observed as `"standby"`; heating/cooling values are plausible but unobserved.
- `sensors.sensor*.status_text` observed values: `"left_bed"` (empty bed, HR=BR=0) and `"normal"` (occupied, realistic HR/BR). The topper also reports HR=BR=255 as a "no reading yet" sentinel in the first ~2s after someone sits down. Other values hinted at by the app strings (e.g. sitting/asleep/error) are plausible but unobserved.
- `sensors.sensor*.sign_of_asleep` / `sign_of_wake_up` only ever observed as `1`; likely edge triggers that momentarily take another value during stage transitions (unconfirmed — a full sleep session hasn't been captured).

### Events NOT Observed (may exist, were not triggered)

- Distinct session-start / session-end events (likely still only available via `/v2/insights` polling)
- Device-offline event (device was online throughout the capture)
- quiet_mode / reboot action responses
- Firmware-update-in-progress transitions
- Water-fill-mode transitions

## Services

### `orion_sleep.override_schedule`

Changes one person's schedule for **today only**, leaving their seven
stored weekday rows untouched. Target the Bedtime or Wake Up Time entity
of whoever's schedule should change, which is how the caller identifies
the person without ever handling a raw Orion user id.

This is a genuinely different operation from setting a value on those
entities. Writing to the entity edits the stored weekday row permanently.
The service applies a one-day override and stamps `is_override_applied`
and `override_date`. "Warmer just for tonight" is the override; "we go to
bed later on Tuesdays now" is the entity.

Accepts `bedtime`, `wakeup`, the four temperature offsets on the app's
-10..+10 scale, and the four schedule flags. Any combination, all sent in
one request, because the override route takes a flat multi-field body.
That differs from the permanent route, which takes one field at a time.

Two behaviours worth knowing:

- The response body is **stale**, echoing pre-change values, so the
  handler refetches rather than trusting it.
- **No route to clear an override has been found.** It appears to reset
  when the date rolls over.

### `orion_sleep.start_cooling` and `orion_sleep.stop_cooling`

Hot flash relief. Target a person's climate entity, which is how the
caller picks a side without handling raw zone ids. `duration_minutes`
defaults to 30.

Genuinely per zone, so one person can cool their side while the other
holds their schedule. The app's own copy describes it as pausing the
schedule for temporary max cooling, and says an active session's
countdown stays visible even when the feature is switched off, which is
how we know the bed tracks the state server-side rather than the app
running a local timer.

The bed stashes `previous_temp` and `previous_on` when relief starts and
restores them when it ends. Both are surfaced as attributes on the
Cooling binary sensor.

`PUT .../live/thermal-relief` is the **only** route in this client that
legitimately sends multiple keys in one body. Every other live write is
strictly one key per request.

**Duration options are UNRESOLVED.** The app clamps its picker to a
`HOT_FLASH_DURATION_OPTIONS` array that lives in a separate bytecode
module and was not resolved to literals. 30 appears as the clamp seed.
The service accepts 1 to 240 and lets the server reject what it dislikes.

## Verification Log

Live-request confirmations, newest first. A claim only earns "measured" by appearing here.

### 2026-07-27 — Thermal relief (rapid cooling) works

`switch.sleepy_*_rapid_cool` was toggled on and back off from the dashboard.
The bed cooled, the switch reported on, and turning it off restored the previous
setpoint without any manual correction.

`PUT /v1/devices/{serial}/live/thermal-relief` and
`POST /v1/devices/{serial}/live/thermal-relief/cancel` are therefore **MEASURED**,
along with the read side at `zones[].thermal_relief` carrying `end_time`,
`previous_temp`, and `previous_on`.

The restore is server-side. The bed remembers what it was doing before the
cooling started and puts it back on cancel, which is why this write has a safe
undo path even though it changes the physical device.

Still unmeasured on this route: `duration_minutes` values other than 30, and the
`type` field beyond `"cool"`. The app only ever sends `"cool"`, so a heat variant
is assumed not to exist rather than known not to.

### 2026-07-27 — `timeline` arrives empty even mid-schedule

`sensor.sleepy_*_next_scheduled_action` read `unavailable` fifteen minutes after
a restart with the socket healthy, so the previous "it only comes on update
frames, wait for one" explanation was wrong.

Captured 75 seconds of live WebSocket traffic at **01:20 local**. One
`live_device.snapshot` followed by five `live_device.update` frames. **Every
update frame carried `"timeline": []`.**

So the field is present, the server sends it on every update, and it is empty.

Note on the clock, because the first read of this was wrong. **Home Assistant
container logs are UTC.** A capture that looked like an idle mid-morning window
was actually the middle of the night, roughly two hours after the configured
bedtime had fired and several hours before wake-up. That makes the empty array
harder to explain away, not easier: the bed was inside its own schedule, with a
known upcoming action, and still queued nothing.

Consequence: the sensor is not broken and needs no code change, but the "it only
populates near an action" theory has no support. **Open question:** whether the
array is ever non-empty at all. Worth one capture within a few minutes of a
scheduled transition. If that is empty too, the field carries no usable signal
and both sensors should be deleted rather than left permanently unavailable.

### 2026-07-27 — Occupancy is our invention, not the vendor's

Grepped the entire decompiled app bundle for `left_bed`. **One hit, and it
is `left_bed_seconds`, a movement statistic inside a completed session.**
`status_text` appears **zero** times anywhere in the app.

So the occupancy logic in this integration, `status_text != "left_bed"`,
was invented upstream. Orion's own client never treats that field as a
presence signal, and there is no vendor behaviour to copy.

That matters because the sensor is provably wrong. Observed over a
fifty minute window with a single occupant in the bed and nothing else
in the room, **both pads reported occupancy** and both reported plausible
heart rates. One side was empty. A "nobody in bed" automation would never
have fired.

Two observations narrow the replacement:

- The occupied pad showed a coherent settling curve: an elevated rate
  easing steadily down to a resting one, with breath rate holding within
  a two-per-minute band throughout. The empty pad sat in a flat rate band
  that never settled, and its breath rate swung by ten within seconds.
- After a later restart **both pads reported identical values to the
  digit** when an hour earlier they had differed by more than forty beats
  per minute. Two bodies do not produce identical readings. That suggests
  the topper duplicates a derived figure onto a side it cannot actually
  read, which is far easier to detect than a signal-quality judgment.

A plausible-heart-rate gate alone would NOT have caught this, since the
empty pad reported believable numbers. Instrumentation was added (see the
`status` integer, `is_working`, and raw unmapped rates on the pad status
sensors) so the fix can be designed from recorded history instead of one
night's impression. Deliberately not fixed yet.

### 2026-07-27 — SteadyTemp patch endpoints: real, and not buildable here

Five routes exist (`POST /v1/patches`, `GET /v1/patches`,
`GET|PUT /v1/patches/{id}`, `POST /v1/patches/cancel`). App strings show
what it is: an NFC wearable sleep test. Tap to activate, wear overnight,
tap again in the morning, and Orion uses the uploaded `raw_data` to
configure your temperature profile.

**Not worth building.** Activation and completion both require physically
tapping the patch to a phone. Home Assistant cannot do that, and the only
HA-reachable state is whether a test is in progress, which the person
wearing the patch already knows.

### 2026-07-26 — Insights session shape, read off a live in-progress session

`GET /v2/insights` was fetched with the partner token while that account
was genuinely mid-session, purely to learn key names. Read-only, no writes.

**The five sleep-stage field names are now MEASURED.** They had been
asserted upstream since April 2026 with no capture behind them, and the
schema types `sleep_summary` as `additionalProperties` naming zero keys.
Confirmed present: `time_asleep`, `deep_sleep`, `rem_sleep`,
`light_sleep`, `awake_time`. Also present and previously unknown:
`hypnogram`.

Also confirmed: `movement.{total_seconds, movement_rate,
left_bed_seconds, values}` and `{heart_rate, breath_rate,
hrv}.{average, min, max, values, axis}`. The `axis` sub-object is new.

**The trap this closed.** `end_time` is populated while
`is_in_progress` is still `true`. Any "last completed session" selector
that filters on a missing `end_time` will report a night currently
being slept as finished. `is_in_progress` is the only trustworthy
discriminator, and `util.latest_completed_session` uses it.

**Session fields present but not yet modelled** (all values were `null`
mid-session, so their shapes are still unknown): `apnea`, `hypnogram`,
`confidence`, `device`, `timezone`, `user`, `is_combined`,
`combined_zone_ids`, `has_been_edited`, `has_been_rated`,
`manual_confirmation`, `user_rating`, `last_updated_at`,
`in_bed_start_time`, `in_bed_end_time`, `user_fallasleep_timestamp`,
`user_wakeup_timestamp`, `temperature_control`, `temperature_setpoint`.
Note `in_bed_*` is distinct from `start_time` / `end_time`. Re-probe
once a session has completed and been processed to learn the shapes.

Day buckets also carry `color` and `quality` alongside `score`.

### 2026-07-26 — All ten writable schedule fields honoured

`PUT /v1/sleep-schedules` was probed field by field with an explicit
`user_id`. Every write targeted **day 3, which was not today (day 0)**, so
nothing about that night's behaviour could change. Verification read the
seven-day `schedules` array rather than the `today_sleep_schedule` view,
since the target row was not today.

| Field | Probe | Verdict |
|---|---|---|
| `bedtime_is_active` | `True` -> `False` | HONOURED |
| `wakeup_is_active` | `True` -> `False` | HONOURED |
| `auto_turn_off` | `True` -> `False` | HONOURED |
| `is_smart_temperature_active` | `True` -> `False` | HONOURED |
| `bedtime` | shifted back fifteen minutes | HONOURED |

Combined with the four temperatures and `wakeup` measured earlier the same
day, **all ten fields in `SCHEDULE_WRITABLE_FIELDS` are now measured.**
Nothing in that surface is app-derived.

Each write was restored immediately and all seven weekday rows were
deep-compared byte-identical against a pre-test backup. No write leaked
into a day other than the one targeted, which was checked explicitly after
every probe rather than assumed.

Method note worth reusing: writing to a non-today weekday row removes the
entire class of "this test changed how the bed behaves tonight" risk, at
the cost of verifying through the weekly array instead of the today view.
Prefer it for anything schedule-shaped. `schedule_flag_test.py`.

### 2026-07-26 — Schedule writes: two routes, two different operations

Tested `wakeup` on both schedule routes, backed up and restored each time.
`bedtime` was deliberately not used as the probe: the test ran close enough
to the configured bedtime that writing it risked firing or skipping the
schedule action outright.

**`PUT /v1/sleep-schedules` (no query param) — MEASURED.**
Body `{"schedules": [{"day": 0, "wakeup": "<HH:MM>"}], "user_id": "<uuid>"}`
returned 200 and the value changed. Confirms this route writes far more than
the four temperature fields the integration currently sends. It left
`is_override_applied` and `override_date` untouched, and its response body
carried the NEW value, so a caller can trust what comes back.

**`PUT /v1/sleep-schedules?action=override` — MEASURED, but it is not an edit.**
Body `{"user_id": "<uuid>", "day": 0, "wakeup": "<HH:MM>"}` returned 200, the
value changed, and the new value was confirmed in the vendor app. A deep compare
against the pre-test backup then showed the real cost:

```
today_sleep_schedule:
  is_override_applied:  false -> true
  override_date:        null  -> "2026-07-26"
```

This route applies a **single-day override**, not a schedule change. The
`schedules` array (the seven weekday rows) was byte-identical before and
after, so nothing permanent moved.

Its response body is also **stale**. The PUT echoed the pre-change value
while an immediate follow-up GET reported the new one. Unlike
`PUT /v1/devices/{serial}/live`, a caller cannot use this response to update
local state and must re-read.

**Design consequence.** These are not interchangeable. Permanent schedule
editing belongs on the plain route. The override route is the right
primitive for a "just for tonight" control and should be exposed separately
and labelled as such, never as the backing write for a bedtime entity.

**Residual state.** `is_override_applied` stayed true for 2026-07-26 after
restore. The plain route does not clear it and no clearing route has been
found. Harmless here because the overridden values were restored to their
originals, so the schedule behaves identically. Expected to reset with the
date.

### 2026-07-26 — Exhaustive APK route enumeration (app-derived, not measured)

Static analysis of the decompiled Orion Sleep v2.4.1 Hermes bundle (51 MB,
1,468,946 lines) resolved every one of the 20 endpoints previously carried
as `speculative`. All 85 `networkInstance` call sites were accounted for.
This is **app-derived**, not measured: nothing was executed.

Nine were fabricated and have been deleted: `verifyBeforeUpdateEmail`,
`registerDevice`, `isScheduleV2Enabled`, `cancelPatchInvite`,
`acceptDeviceInvite`, `isScheduleRecommendationsEnabled`, `getSurvey`,
`healthCheck`, `getSchedulesProModeTest`.

**The fabrication mechanism, worth understanding so it is not repeated.**
Every fabricated path exists verbatim in the extracted string table because
the Hermes string pool packs strings with shared substrings and no
delimiters. Grepping it manufactures paths that never existed. The
originals were LaunchDarkly flag keys, SWR cache-key prefixes, and Firebase
SDK method names. For example `/v1/onboarding/wake-update` is
`/v1/onboarding/wake-up` adjoining `dateCallCount`.

**Treat a raw string table as inadmissible for route discovery.** A path
claim needs a decompiled line where the literal is passed to an HTTP verb.

Four operations were real but wrong, and were corrected rather than
deleted: `removeUser` is `DELETE` not `POST`; `updateNotificationPreferences`
is `POST` not `PUT`; `submitSurvey` is `PUT` not `POST`;
`setOnboardingWakeTime` is `/v1/onboarding/wake-up` not `/wake-update`.

Three device routes were repointed from `{deviceId}` to `{serial_number}`
(`activate`, `deactivate`, `update`). Only `PUT /v1/devices/{deviceId}`
genuinely takes the UUID. This is the same identifier-confusion class that
already produced live 403s and 404s.

`GET /v1/devices/{serial_number}/live` was added. It is the most-exercised
endpoint in the integration, called on every poll, and it was **entirely
absent** from this spec.

Routes present in the app and still undocumented here, ranked by relevance:
`PUT /v1/devices/{serial}/live/thermal-relief` (hot-flash mode, the only
multi-key live payload the app ever sends), `PUT /v1/sleep-schedules?action=override`
(writes `bedtime`, `wakeup`, the activation flags, all four phase temps,
and `is_smart_temperature_active`), `PUT /v1/sleep-configurations/temperature`,
`PUT /v1/sleep-configurations/devices`, and a second HTTP host serving
`GET /v1/device/{serial}/online`.

### 2026-07-26 — `user_id` on `PUT /v1/sleep-schedules`

The vendor app sends `user_id` alongside `schedules` on this route (decompiled 673558, 673560). This integration never did, and assumed the write was scoped to the bearer token's owner. It is not.

Test at 21:43, using the PRIMARY account's token throughout:

1. Full schedule blob backed up to `working/orion-schedules-backup-20260726-214301.json` before any write.
2. `PUT /v1/sleep-schedules` with body `{"schedules": [{"day": 0, "phase_1_temp": 10}], "user_id": "<partner>"}` returned `200`.
3. Read back: the **partner's** `phase_1_temp` moved `16.7 -> 10`. The **primary's** stayed at `17.5`, untouched.
4. Restored the partner's original value through the same route. A final GET was deep-compared against the backup and came back byte-identical.

**Verdict: HONOURED.** One account can write another person's schedule by naming them in the body. A partner account is NOT required for schedule temperature writes.

Omitting `user_id` still writes the token owner's own schedule, so existing behaviour is unchanged.

**Incidental finding worth carrying forward:** the partner's `phase_1_temp` was `16.7`, which is not a value on the device's `temperature_scale.relative` ladder (`... 16, 17.5 ...`). Off-table Celsius values exist in production data. Round-tripping such a value through `_celsius_to_offset` and back would silently rewrite it to `16` or `17.5`. Any per-person slider must not write on read.

### 2026-07-26 — `quiet_mode` write key

`PUT /v1/devices/AA11BB22CC33/live` with body `{"quiet_mode": true}` returned `200` and echoed `"quiet_mode":true`. Confirmed four independent ways:

1. HTTP 200 with the new value in the response body.
2. The device pushed the change back over its own live WebSocket.
3. `binary_sensor.sleepy_quiet_mode` in Home Assistant flipped to `on` at 21:35:52.
4. The vendor's own Orion app showed quiet mode enabled.

Restored to `false` immediately after. Promotes the `quiet_mode` key from app-derived to measured.

`water_fill` is now the only key on this route that remains app-derived.

### 2026-07-26 — `led_brightness` write route

`PUT /v1/devices/AA11BB22CC33/live` with body `{"led_brightness": 30}` returned `200` and echoed `"led_brightness":30` in the response. Confirmed four independent ways:

1. HTTP 200 with the new value in the response body.
2. The device pushed 30 back over its own live WebSocket ~8 seconds later.
3. `sensor.sleepy_led_brightness` in Home Assistant changed 100 -> 30 at 21:27:37.
4. The vendor's own Orion app showed the change.

Restored to 100 immediately after. Promotes the route and the `led_brightness` key from app-derived to measured.

`water_fill` uses the identical mechanism on the same route but has NOT been observed executing. It remains app-derived.

## Known Limitations / Future Work

- `water_fill` writes are app-derived, not measured. The route and the other three body keys are proven; this one is not.
- Schedule enable and disable behavior is not verified and is not exposed.
- Runtime climate power does not modify the schedule. A later scheduled action can turn a zone back on.
- HRV values frequently null in real data
- No way to start/stop sleep sessions via API
- Zone splitting/merging not supported
- Guest user management not supported
- Power failures propagate to the Home Assistant UI. Away Mode specifically swallows the known `400 "User has no previous device to return to"` response for a redundant present action.
- Topper sensor1 ↔ sensor2 to zone_a ↔ zone_b mapping is unverified — entities are named per sensor rather than per side until a split-occupancy capture confirms the mapping
