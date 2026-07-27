# Orion Sleep - Home Assistant HACS Integration

## Project Overview

HACS-compatible Home Assistant custom integration for the **Orion Sleep** smart mattress topper. Cloud-connected bed temperature control with per-zone support, sleep tracking (heart rate, breath rate, HRV, sleep stages), and sleep scheduling.

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
| Number | Bedtime Temperature Offset | `_bedtime_temp_offset` | App-style -10..+10 slider. Reads `today_sleep_schedule.bedtime_temp`, converts to offset via per-device relative table; writes back via `PUT /v1/sleep-schedules` on today's day-of-week. |
| Number | Asleep Phase 1 Offset | `_phase_1_temp_offset` | As above, `phase_1_temp` field. |
| Number | Asleep Phase 2 Offset | `_phase_2_temp_offset` | As above, `phase_2_temp` field. |
| Number | Wake Up Temperature Offset | `_wakeup_temp_offset` | As above, `wakeup_temp` field. |

| Sensor | \<zone\> Measured Temperature | `_{zoneId}_measured_temp` | `status.zones[].temp`. Duplicates the climate entity's `current_temperature` on purpose: climate attributes are not retained as long-term statistics, a `sensor` with a `state_class` is. |
| Sensor | \<zone\> Target Temperature | `_{zoneId}_target_temp` | `zones[].temp`, the LIVE setpoint. Distinct from `today_sleep_schedule.*_temp`, which is schedule intent and diverges the moment a zone is nudged by hand. |
| Binary Sensor (diag) | Firmware Update Available | `_firmware_update_available` | `status.pending_update.is_available`. Deliberately not an `update` entity: nothing in the payload carries the available version string. |
| Number | LED Brightness | `_led_brightness` | 0-100 write via `PUT /v1/devices/{serial_number}/live`. Debounced with an optimistic local write and a post-write lock, mirroring the vendor app. |
| Switch | Quiet Mode | `_quiet_mode` | Write via the same live route. Replaced the old read-only binary sensor once the write was measured. |

**A two-zone device exposes 47 base entities. A linked partner adds 11 partner insight sensors.**

- 2 climate entities, one per zone.
- 31 sensors: 11 insights, 5 schedule, current offset, live connection, 6 topper sensor readings, 2 measured and 2 target zone temperatures, LED brightness, firmware, and Wi-Fi signal.
- 5 numbers: 4 schedule-phase temperature offsets plus LED brightness.
- 5 binary sensors: sleep session, 2 occupancy sensors, firmware update available, and safety problem.
- 3 switches: runtime power, Away Mode, and quiet mode. Away Mode is omitted for accounts with multiple devices because the API action is account-global.
- 1 button: reboot. Forget Wi-Fi is intentionally not exposed.

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

## Verification Log

Live-request confirmations, newest first. A claim only earns "measured" by appearing here.

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
