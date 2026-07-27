# Orion Sleep - Home Assistant Integration

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://github.com/hacs/integration)

Custom [Home Assistant](https://www.home-assistant.io/) integration for the **Orion Sleep** smart mattress topper. Per-person temperature control, per-person schedules, live vitals, sleep tracking, and rapid cooling, all from your Home Assistant dashboard.

**This repository is the actively maintained implementation.** It began as a fork and has since diverged substantially: the API contract was rebuilt from measured traffic, every write path was verified against a live device before shipping, and roughly two thirds of the original OpenAPI operations turned out to be fabrications and were deleted. Issues and pull requests belong here.

## What "verified" means here

Every endpoint carries a confidence label, and the rule is enforced rather than aspirational.

| Label | Meaning |
|---|---|
| **Measured** | A real request was sent to a real bed and the result was recorded in the verification log in `AGENTS.md`. |
| **App-derived** | Read out of the decompiled Orion Android client. Plausible, never executed. |
| **Speculative** | Guessed. Not implemented. |

No control ships against a speculative route. Writes that change the bed are tested with a backup-and-restore probe, and where possible against a day of the week that is not today, so a test cannot change how the bed behaves that night.

## Features

- **Per-person everything.** Two people share one bed and one Orion account. Schedules, temperatures, sleep insights, and vitals are split per sleeper rather than collapsed into a single account view.
- **Display aliases.** Map an Orion user ID to whatever you actually call that person. Friendly names change, entity IDs do not, so history and dashboards survive.
- **Live WebSocket stream.** Temperature, power, and vitals update the moment the bed or the Orion app changes anything.
- **Per-zone climate.** Independent target temperature, measured temperature, power, and thermal action for each side.
- **Per-person schedules.** Bedtime, wake time, four phase temperatures, and four schedule flags, all writable, all per person. One account can write both people's schedules.
- **Rapid cooling.** Hot flash relief per side, as a toggle with a live countdown. The bed remembers what it interrupted and restores it on cancel.
- **Sleep insights.** Score, HRV, heart and breath rate, sleep stages as both formatted durations and graphable minutes, plus a real timestamp for when the last session ended.
- **Partner insights.** Link a second Orion login to pull the other sleeper's sleep data, which is scoped to their token and invisible otherwise.
- **Firmware updates.** Exposed as a proper `update` entity with install support.
- **Passwordless auth.** Same email or phone plus verification code flow as the app. Tokens refresh automatically.
- **Redacted diagnostics.** The debug bundle strips tokens, identifiers, contact details, network PII, biometrics, and schedules.

## Installation

### HACS (Recommended)

1. Make sure [HACS](https://hacs.xyz/) is installed.

2. Click the button below to add this repository:

   [![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=borski&repository=home-assistant-orion-integration&category=integration)

   Or add it manually: **HACS > Integrations > three-dot menu > Custom repositories**, paste `https://github.com/borski/home-assistant-orion-integration`, category **Integration**.

3. Search for "Orion Sleep" in HACS and download it.

4. Restart Home Assistant.

### Manual

1. Copy `custom_components/orion_sleep` into your Home Assistant `config/custom_components/` directory.
2. Restart Home Assistant.

## Configuration

[![Open your Home Assistant instance and start setting up a new integration.](https://my.home-assistant.io/badges/config_flow_start.svg)](https://my.home-assistant.io/redirect/config_flow_start/?domain=orion_sleep)

Or go to **Settings > Devices & Services > Add Integration** and search for "Orion Sleep".

### Setup steps

1. Choose **email** or **phone**.
2. Enter your Orion account email or phone number. A verification code is sent the same way the app sends it.
3. Enter the code.

### Options

**Settings > Devices & Services > Orion Sleep > Configure**

| Option | Default | Description |
|---|---|---|
| Polling interval | 600 s | REST poll interval, 60 to 3600 s. The WebSocket runs continuously and ignores this. |
| Insights days | 7 | Days of sleep history to retrieve, 1 to 30. |
| Partner account | off | Link a second Orion login for the other sleeper's insights. Both accounts must resolve to the same physical bed, and the mapping is rechecked every poll. |
| Display names | off | Rename each sleeper. Affects friendly names only. |

### Display names

Orion stores whatever name was on the account at signup, which is often not what anyone calls that person. Tick **Edit display names** in options and you get one text field per known sleeper, labelled with the vendor's own name so you can tell who is who.

This changes friendly names only. Entity IDs, unique IDs, history, statistics, and dashboards are untouched.

## Services

| Service | Target | Description |
|---|---|---|
| `orion_sleep.override_schedule` | a `time` entity | Change tonight only. Accepts any mix of bedtime, wake time, four temperature offsets, and four flags. Leaves the stored weekday schedule alone. |
| `orion_sleep.start_cooling` | a `climate` entity | Start rapid cooling on that side. `duration_minutes` defaults to 30. |
| `orion_sleep.stop_cooling` | a `climate` entity | Cancel cooling early. The bed restores the setpoint it interrupted. |

Point the override service at someone's Bedtime or Wake Up Time entity and it figures out whose schedule to write. You never handle a raw Orion user ID.

```yaml
action: orion_sleep.override_schedule
target:
  entity_id: time.sleepy_alex_bedtime
data:
  bedtime: "23:45:00"
  bedtime_temp_offset: -4
```

## Real-time updates

The integration opens one WebSocket per device to `wss://live.api1.orionbed.com/device/<serial_number>` and merges every `live_device.snapshot` and `live_device.update` frame into coordinator state.

- Power, climate, and vitals reflect app-side changes immediately.
- Reconnects with exponential backoff, and refreshes the JWT before reconnecting on a 401.
- Health is exposed by the **Live Connection** diagnostic sensor.

There is no option to disable it.

## Entities

One Home Assistant device per paired topper. A two-zone bed with no partner linked exposes **69 entities**. Linking a partner adds 35 more.

Names below use `<person>` where the display alias is substituted, and `<zone>` for a bed side.

### Climate

| Entity | Description |
|---|---|
| `<person>` Climate | One per zone. Target and measured temperature from the live snapshot. HVAC mode controls that zone's runtime power. |

### Schedule, per person

| Entity | Platform | Description |
|---|---|---|
| `<person>` Bedtime | Time | Writable. |
| `<person>` Wake Up Time | Time | Writable. |
| `<person>` Schedule Duration | Sensor | Calculated, handles overnight. |
| `<person>` Bedtime / Phase 1 / Phase 2 / Wake Up Temperature | Sensor | Absolute Celsius, with statistics. |
| `<person>` Bedtime / Phase 1 / Phase 2 / Wake Up Offset | Number | App-style -10 to +10 slider, mapped non-linearly through the device's own lookup table. |
| `<person>` Bedtime Enabled / Wake Up Enabled / Automatic Turn Off / Smart Temperature | Switch | The four schedule flags. |
| `<person>` Schedule Override | Binary sensor | Whether a single-day override is in force, with the date as an attribute. Explains a surprising bedtime. |
| `<person>` Next Scheduled Action | Sensor | Timestamp of the next change the bed will make on its own. See limitations. |

Schedule writes carry an explicit user ID, so one account edits both people's schedules. A second login is only needed for the other person's sleep insights.

### Sleep insights, per person

Sleep Score, HRV, Heart Rate, Breath Rate, Body Movement Rate, Restless Time, Total Sleep Time, Deep / REM / Light Sleep, Awake Time, plus numeric minute equivalents for each stage so they graph, and Last Session End as a real timestamp.

The minute sensors use `measurement`, deliberately. A cumulative state class would treat every shorter night as a meter reset and permanently corrupt the stored sum.

### Comfort and cooling

| Entity | Platform | Description |
|---|---|---|
| `<person>` Rapid Cool | Switch | Hot flash relief on that side. On starts 30 minutes, off cancels and restores the previous setpoint. |
| `<person>` Cooling Ends | Sensor | Timestamp, so Home Assistant renders a countdown that ticks on its own. |
| `<zone>` Cooling | Binary sensor | Active only when the end time is in the future, which is the app's own test. |
| Power | Switch | All zones on or off. |
| Away Mode | Switch | Marks the authenticated user away. Omitted on multi-device accounts because the vendor action is account-global. |
| Quiet Mode | Switch | Measured write. |
| LED Brightness | Number | 0 to 100, debounced with an optimistic write and rollback on failure. |

### Live vitals

| Entity | Description |
|---|---|
| `<person>` Live Heart Rate | bpm, realtime. |
| `<person>` Live Breath Rate | br/min, realtime. |
| `<person>` On Bed | Occupancy. **See the occupancy caveat below.** |
| `<person>` Bed Sensor Status | Raw classification, plus the undocumented raw fields as attributes. |

`0` and `255` are server-side sentinels for empty bed and no reading yet. Both report as `unknown` on the primary entities, and both are preserved unmapped in the attributes.

### Diagnostics and maintenance

| Entity | Description |
|---|---|
| Firmware | `update` entity. Install is wired up. |
| Live Connection | Our socket to Orion. |
| Device Online | The server's view of the topper. Genuinely different from Live Connection, and the two can disagree in both directions. |
| Wi-Fi Signal, Firmware Version, Safety Problem | Standard diagnostics. Safety exposes error codes and descriptions as attributes. |
| Reboot Control Tower | Disabled by default. |
| Swap Bed Sides | Swaps which side each person is assigned. Pressing again reverses it. |
| Split Zones | Disabled by default, because nothing in the payload reports split state, so a press has no observable result. |

## Dashboard example

A two-column Lovelace view, one per sleeper, plus shared controls. Native cards only, no custom components required.

Replace `sleepy` with your device name, and `alex` / `sam` with your own display aliases. The quickest way to get the real IDs is **Developer Tools > States**, filtered on your device name.

```yaml
views:
  - title: Sleep
    path: sleep
    icon: mdi:bed-king-outline
    type: sections
    max_columns: 3

    badges:
      - type: entity
        entity: sensor.sleepy_live_connection
        name: Live
      - type: entity
        entity: binary_sensor.sleepy_safety_problem
        name: Safety
        color: red
        visibility:
          - condition: state
            entity: binary_sensor.sleepy_safety_problem
            state: "on"

    sections:
      # ── One sleeper ────────────────────────────────────────────────
      - type: grid
        cards:
          - type: heading
            heading: Alex
            icon: mdi:bed

          - type: thermostat
            entity: climate.sleepy_alex_climate
            features:
              - type: climate-hvac-modes
                hvac_modes: [heat_cool, "off"]

          - type: entities
            title: Tonight
            state_color: true
            entities:
              - entity: time.sleepy_alex_bedtime
                name: Bedtime
              - entity: time.sleepy_alex_wake_up_time
                name: Wake up
              - entity: sensor.sleepy_alex_schedule_duration
                name: In bed for
              - type: divider
              - entity: switch.sleepy_alex_rapid_cool
                name: Rapid Cool
              # Countdown only while cooling is actually running.
              - type: conditional
                conditions:
                  - entity: switch.sleepy_alex_rapid_cool
                    state: "on"
                row:
                  entity: sensor.sleepy_alex_cooling_ends
                  name: Cooling ends

          - type: entities
            title: Temperature offsets
            entities:
              - entity: number.sleepy_alex_bedtime_offset
                name: Bedtime
              - entity: number.sleepy_alex_asleep_phase_1_offset
                name: Asleep phase 1
              - entity: number.sleepy_alex_asleep_phase_2_offset
                name: Asleep phase 2
              - entity: number.sleepy_alex_wake_up_offset
                name: Wake up

          # Nothing to show until a night has been processed.
          - type: conditional
            conditions:
              - condition: state
                entity: sensor.sleepy_alex_sleep_score
                state_not: unknown
              - condition: state
                entity: sensor.sleepy_alex_sleep_score
                state_not: unavailable
            card:
              type: gauge
              entity: sensor.sleepy_alex_sleep_score
              name: Last night
              min: 0
              max: 100
              needle: true
              severity: { red: 0, yellow: 60, green: 80 }

      # ── The other sleeper: same block, different prefix ────────────
      - type: grid
        cards:
          - type: heading
            heading: Sam
            icon: mdi:bed

          - type: thermostat
            entity: climate.sleepy_sam_climate
            features:
              - type: climate-hvac-modes
                hvac_modes: [heat_cool, "off"]

          - type: entities
            title: Tonight
            state_color: true
            entities:
              - entity: time.sleepy_sam_bedtime
                name: Bedtime
              - entity: time.sleepy_sam_wake_up_time
                name: Wake up
              - entity: sensor.sleepy_sam_schedule_duration
                name: In bed for
              - type: divider
              - entity: switch.sleepy_sam_rapid_cool
                name: Rapid Cool
              - type: conditional
                conditions:
                  - entity: switch.sleepy_sam_rapid_cool
                    state: "on"
                row:
                  entity: sensor.sleepy_sam_cooling_ends
                  name: Cooling ends

      # ── Shared ─────────────────────────────────────────────────────
      - type: grid
        cards:
          - type: heading
            heading: Bed
            icon: mdi:tune

          - type: entities
            title: Controls
            state_color: true
            entities:
              - entity: switch.sleepy_power
                name: Power
              - entity: switch.sleepy_quiet_mode
                name: Quiet mode
              - entity: switch.sleepy_away_mode
                name: Away mode
              - entity: number.sleepy_led_brightness
                name: LED brightness

          - type: entities
            title: Diagnostics
            entities:
              - entity: binary_sensor.sleepy_device_online
                name: Server sees bed
              - entity: sensor.sleepy_wi_fi_signal
                name: Wi-Fi
              - entity: update.sleepy_firmware
                name: Firmware
```

### Handling entities with no data yet

Sleep insights read `unknown` until Orion finishes processing a night, and `unavailable` when a partner is not linked. Home Assistant resolves a nonexistent entity to `unavailable`, so a single `visibility` block distinguishes all three states without any templating:

```yaml
visibility:
  # Hide until there is a real value. `unknown` means the entity exists
  # and is healthy but has no data. `unavailable` means it was never
  # created, which is what an unlinked partner looks like.
  - condition: state
    entity: sensor.sleepy_sam_sleep_score
    state_not: unknown
  - condition: state
    entity: sensor.sleepy_sam_sleep_score
    state_not: unavailable
```

## Troubleshooting

- **Re-authentication.** If both tokens expire or are revoked, Home Assistant raises a re-auth flow. Follow the prompts for a new code.
- **Away Mode looks stuck.** The server returns a `400 "User has no previous device to return to"` on a redundant toggle. That specific error is swallowed and logged at debug.
- **Live Connection stuck on `reconnecting`.** Usually a network problem reaching `live.api1.orionbed.com`. REST polling continues, so the rest of the integration keeps working.
- **Logs.**

  ```yaml
  logger:
    default: warning
    logs:
      custom_components.orion_sleep: debug
  ```

- **Diagnostics.** **Settings > Devices & Services > Orion Sleep > three-dot menu > Download diagnostics.** Tokens, identifiers, names, contact details, network details, activation codes, biometrics, and schedules are stripped.

## Notes and limitations

### Occupancy is unreliable

**This is a real, open defect, not a caveat.** The on-bed sensors derive from the topper's `status_text`. With one person in the bed, both pads have been observed reporting someone present, with believable heart rates on the empty side. An automation gated on "nobody in bed" would never fire.

Grepping the vendor's own app shows it never reads `status_text` at all, so there is no correct behaviour to copy. The raw fields are exposed as attributes on the bed sensor status entities so the failure can be characterised properly before a fix is guessed at. Treat heart rate as the better occupancy signal for now.

### Everything else

- Which physical side each topper pad corresponds to is unconfirmed. Pad-to-person naming is currently provisional.
- `Next Scheduled Action` reads `unavailable` most of the time. The server sends an empty timeline array even mid-schedule. The sensor is correct, its source is empty.
- Firmware install has never been executed, because no update has been offered since the integration was written. The route is app-derived and is the only write with no undo.
- Cooling durations other than 30 minutes are unverified.
- HRV is frequently `null` in real data and will report `unknown`.
- Starting and stopping sleep sessions is not supported by the API.
- Split Zones is shipped disabled because nothing reports split state.

## Contributing

Read `AGENTS.md` first. It carries the measured API contract, the verification log, and the source-of-truth policy.

The one rule that matters: **do not ship a control against an unverified route.** If you cannot demonstrate a write against a live bed, leave it read-only and say so in the docstring.

## License

Not affiliated with or endorsed by Orion Longevity Inc.
