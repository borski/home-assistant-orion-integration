# Orion Sleep - Home Assistant Integration

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://github.com/hacs/integration)

Custom [Home Assistant](https://www.home-assistant.io/) integration for the **Orion Sleep** smart mattress topper. Per-person temperature control, per-person schedules, live vitals, sleep tracking, and rapid cooling, all from your Home Assistant dashboard.

## This is the maintained version

This repository started as a fork and is now the successor. Development
happens here, and issues and pull requests belong here.

What changed since the fork:

- The API contract was rebuilt from measured traffic and a decompile of the
  Orion Android client. Two thirds of the original OpenAPI operations turned
  out to be fabrications and were deleted.
- Every write is tested against a live bed before it ships as a control.
- Schedules, insights, vitals and controls are split per sleeper instead of
  collapsed into one account view.
- Three device routes were pointed at the wrong identifier and returned 403
  or 404 on every call. They take the serial number, not the UUID.

## What "verified" means here

Every endpoint carries a confidence label, and the rule is enforced rather than aspirational.

| Label | Meaning |
|---|---|
| **Measured** | A real request was sent to a real bed and the result was recorded in the verification log in `AGENTS.md`. |
| **App-derived** | Read out of the decompiled Orion Android client. Plausible, never executed. |
| **Speculative** | Guessed. Not implemented. |

No control ships against a speculative route. Writes that change the bed are tested with a backup-and-restore probe, and where possible against a day of the week that is not today, so a test cannot change how the bed behaves that night.

## Feature parity with the Orion app

Everything the Orion app does to control the bed or read your sleep data is
here, plus a few things the app doesn't show you at all.

These are the deliberate exceptions:

| Not implemented | Why |
|---|---|
| Sleep session rating | Feedback to Orion, changes nothing about the bed. |
| Orion Intelligence | The app's recommendation feed. Advice, not control. |
| SteadyTemp patch | Needs physical NFC taps against a phone. Not reachable from Home Assistant. |
| Notification preferences | Push settings for the phone app. |
| Profile editing | Name, email, date of birth, biological data. Account admin. |
| Onboarding and the setup survey | One-time flows that only run before a bed is paired. |
| Sleep advisor consultations | Booking a call with Orion. |
| Apple Health sync | Home Assistant is the destination here, so the bridge is redundant. |
| Subscription management | Billing. |
| Water fill mode | The only live-device key never observed doing anything. Left out rather than shipped unverified. |
| Hypnogram | 536 stage samples per night, with no sensible Home Assistant representation. The stage totals are exposed instead. |
| Night mode | Purely client side in the app. There is nothing on the wire to build. |

Two more work but have never run against a live bed, because no occasion has
come up. Both are marked in their docstrings:

- Firmware install. The plumbing is there and the entity is real, but Orion
  has not offered an update since this was written, and it is the only write
  with no undo.
- Sending and accepting invitations. The request contract is measured, but
  actually inviting somebody means texting a stranger to find out.

Things the app doesn't give you and this does: sleep efficiency, sleep stage
minutes you can graph, guest access expiry, apnea numbers as first class
sensors, and Orion's own occupancy answer sitting next to the topper's so you
can see them disagree.

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

### Home Assistant version

The declared minimum is **2025.1.0**, derived from the newest Home Assistant
APIs this integration actually calls: `ConfigEntry.runtime_data` and the
`update` entity's `update_percentage`.

That floor is inferred from API usage, not tested. The only version this has
ever run on is 2026.4. If it works on something older, or does not, open an
issue and the floor will be corrected to match reality.

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
| `orion_sleep.list_sleep_sessions` | any of that person's insight sensors | List recent sessions with their IDs, newest first. Read-only. Returns response data. |
| `orion_sleep.delete_sleep_session` | the same person's sensor | Permanently delete one session. **No undo.** Requires an explicit ID, a reason, and `confirm: true`. |
| `orion_sleep.confirm_sleep_session` | the same person's sensor | Tell the bed whose night it was. `claim: me` or `claim: both`. |
| `orion_sleep.edit_sleep_session` | the same person's sensor | Correct when sleep actually started and ended. The bed reanalyses the night. Reversible. |
| `orion_sleep.end_sleep_session` | any of that person's insight sensors | End the session that is running right now. Requires `confirm: true`. |
| `orion_sleep.list_invites` | the Bed Access sensor | Invitations sent but not yet accepted. Read-only. Returns response data. |
| `orion_sleep.invite_user` | the Bed Access sensor | Invite somebody by phone number as `member` or `guest`. |
| `orion_sleep.cancel_invite` | the Bed Access sensor | Withdraw an invitation before it is accepted. |
| `orion_sleep.accept_invite` | the Bed Access sensor | Redeem a code so this account is added to somebody else's bed. |
| `orion_sleep.remove_user_access` | the Bed Access sensor | Revoke access. Requires `confirm: true`. Not reversible without a fresh invite. |
| `orion_sleep.create_guest` | the Bed Access sensor | Add an unattached guest slot, then give it a number with the next service. |
| `orion_sleep.update_user_phone` | the Bed Access sensor | Attach or change a phone number for somebody on the bed. |
| `orion_sleep.assign_zones` | the Bed Access sensor | Put somebody on a zone. The app calls this Replace. Displaces whoever was there. |
| `orion_sleep.set_device_name` | the Bed Access sensor | Rename the bed in Orion. Home Assistant keeps its own name. |
| `orion_sleep.set_device_timezone` | the Bed Access sensor | Set the bed's timezone. Schedules are stored per weekday, so this moves bedtime rather than relabelling it. |

Point the override service at someone's Bedtime or Wake Up Time entity and it figures out whose schedule to write. You never handle a raw Orion user ID.

```yaml
action: orion_sleep.override_schedule
target:
  entity_id: time.sleepy_alex_bedtime
data:
  bedtime: "23:45:00"
  bedtime_temp_offset: -4
```

### Correcting a night the bed got wrong

Deleting is the blunt answer. If the sleep was real but the boundaries
are not, move them instead:

```yaml
action: orion_sleep.edit_sleep_session
data:
  entity_id: sensor.sleepy_alex_sleep_score
  session_id: 00000000-0000-0000-0000-000000000000
  fell_asleep: "2026-07-27 03:30:00"
  woke_up: "2026-07-27 07:23:00"
```

Times are local. Both are required, because the API rejects a partial
pair.

This recomputes rather than relabels. Sleep stages, heart rate,
breathing and apnea are all derived again from the new window, so the
numbers afterwards are genuinely different numbers. It is reversible:
run it again with the original times and every metric returns exactly.
Expect the call to take up to a minute while the server works.

### Removing a session the bed invented

The topper sometimes records a night that did not happen. See the occupancy
defect below. Those fabricated sessions carry a sleep score and stage
durations, so they drag down every average and sit permanently in long-term
statistics.

Find the culprit first. This is read-only and returns the data to you rather
than putting session IDs into entity state:

```yaml
action: orion_sleep.list_sleep_sessions
target:
  entity_id: sensor.sleepy_alex_sleep_score
```

Then delete it by ID, targeting the same person:

```yaml
action: orion_sleep.delete_sleep_session
target:
  entity_id: sensor.sleepy_alex_sleep_score
data:
  session_id: 3fa85f64-5717-4562-b3fc-2c963f66afa6
  reason: not_real_session
  confirm: true
```

**This cannot be undone.** There is no restore route and no way to list deleted
sessions. Three things have to line up before anything is sent: `confirm` must
be true, the reason must be one the vendor recognises, and the ID must belong
to the person whose entity you targeted. Sessions belong to the account that
recorded them, so a partner's session must be deleted through a partner sensor.

## Real-time updates

The integration opens one WebSocket per device to `wss://live.api1.orionbed.com/device/<serial_number>` and merges every `live_device.snapshot` and `live_device.update` frame into coordinator state.

- Power, climate, and vitals reflect app-side changes immediately.
- Reconnects with exponential backoff, and refreshes the JWT before reconnecting on a 401.
- Health is exposed by the **Live Connection** diagnostic sensor.

There is no option to disable it.


## Sharing a bed

An Orion bed can carry more than one person. One account owns it, a
member can control it, and a guest receives their own sleep insights
without the run of the device. The `Bed Access` sensor shows who is on
the bed and in what capacity, and every access service targets it.

```yaml
action: orion_sleep.invite_user
target:
  entity_id: sensor.orion_bed_access
data:
  phone_number: "+1 415 555 1234"
  role: guest
```

The role you pick here is the word the Orion app uses. A member is sent
to the API as `admin`; the integration handles that translation.

Guest access can carry an expiry, which shows up as `expires` in the
`people` attribute. The Orion app does not surface it.

To take somebody off the bed, read their `user_id` out of that same
attribute:

```yaml
action: orion_sleep.remove_user_access
target:
  entity_id: sensor.orion_bed_access
data:
  user_id: "the id from the people attribute"
  confirm: true
```

There is no undo. Getting somebody back means sending a fresh invite
that they have to accept.

## Entities

One Home Assistant device per paired topper. A two-zone bed with no partner
linked exposes **76 entities**. Linking a partner brings it to **131**.

The two sides aren't quite symmetrical. `In Bed (Orion)` and `Schedule Phase`
come from a route that reports for whoever holds the token, so they exist for
the authenticated account only.

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

Schedule writes carry an explicit user ID, so one account edits both people's schedules. A second login is only needed for the other person's sleep insights.

### Sleep insights, per person

Sleep Score, HRV, Heart Rate, Breath Rate, Body Movement Rate, Restless Time, Total Sleep Time, Deep / REM / Light Sleep, Awake Time, plus numeric minute equivalents for each stage so they graph, and Last Session End as a real timestamp.

**Breathing.** Apnea Index (AHI, events per hour of sleep), Obstructive Apnea
Time, Central Apnea Time, and Longest Apnea Event. Reported per completed
session, so they read unknown while someone is still asleep.

These carry the vendor's numbers and nothing else. No severity banding, no
interpretation. For context, published clinical thresholds put an AHI under 5
in the normal range, 5 to 15 mild, 15 to 30 moderate, and above 30 severe, but
**this is a mattress topper, not a diagnostic device.** Treat a number you do
not like as a reason to talk to a doctor and get a real study, not as a
result.

The minute sensors use `measurement`, deliberately. A cumulative state class would treat every shorter night as a meter reset and permanently corrupt the stored sum.

### Comfort and cooling

| Entity | Platform | Description |
|---|---|---|
| `<person>` Rapid Cool | Switch | Hot flash relief on that side. On starts a window, off cancels it and restores the previous setpoint. Reports the window it will use as a `duration_minutes` attribute. |
| `<person>` Rapid Cool Duration | Number | How long that side cools for, 5 to 120 minutes. A local preference, not device state, so it survives restarts. The `start_cooling` service still takes anything from 1 to 240. |
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
| `<person>` In Bed (Orion) | Orion's own occupancy answer, from the route the app reads. Deliberately a second entity rather than a replacement, because where it disagrees with the pads is where the bug is. |
| `<person>` Schedule Phase | Which part of tonight's schedule the bed thinks it is in. |

`0` and `255` are server-side sentinels for empty bed and no reading yet. Both report as `unknown` on the primary entities, and both are preserved unmapped in the attributes.

### Diagnostics and maintenance

| Entity | Description |
|---|---|
| Firmware | `update` entity. Install is wired up. |
| Live Connection | Our socket to Orion. |
| Device Online | The server's view of the topper. Genuinely different from Live Connection, and the two can disagree in both directions. |
| Wi-Fi Signal, Firmware Version, Safety Problem | Standard diagnostics. Safety exposes error codes and descriptions as attributes. |
| Bed Access | Who can use this bed and in what capacity, with access expiry. The target for every access service. |
| Zone Mode | Whether the two halves are driven together or independently. |
| Bed Orientation | Which side the bed faces. The app calls this "update your side to fix your insight". |
| App Temperature Scale | Whether the Orion app shows the offset ladder or Fahrenheit. Account level. |
| Reboot Control Tower | Disabled by default. |
| Swap Bed Sides | Swaps which side each person is assigned. Pressing again reverses it. |
| Split Zones | Disabled by default, because nothing in the payload reports split state, so a press has no observable result. |

## Dashboard example

A starting point: one column per sleeper, plus shared controls. Native cards
only, no custom components.

Replace `sleepy` with your device name and `alex` with your display alias.
The fastest way to get real entity IDs is **Developer Tools > States**,
filtered on your device name.

```yaml
views:
  - title: Sleep
    path: sleep
    cards:
      - type: vertical-stack
        cards:
          - type: thermostat
            entity: climate.sleepy_alex_climate
          - type: entities
            title: Alex
            state_color: true
            entities:
              - entity: time.sleepy_alex_bedtime
                name: Bedtime
              - entity: time.sleepy_alex_wake_up_time
                name: Wake up
              - entity: sensor.sleepy_alex_schedule_duration
                name: Time in bed
              - type: divider
              - entity: number.sleepy_alex_bedtime_offset
                name: Bedtime temp
              - entity: number.sleepy_alex_wake_up_offset
                name: Wake temp
              - type: divider
              - entity: switch.sleepy_alex_rapid_cool
                name: Rapid cool
              - entity: sensor.sleepy_alex_cooling_ends
                name: Cooling ends
              - type: divider
              - entity: sensor.sleepy_alex_sleep_score
                name: Last night
              - entity: sensor.sleepy_alex_sleep_efficiency
                name: Efficiency
              - entity: sensor.sleepy_alex_apnea_index
                name: Apnea index
              - entity: binary_sensor.sleepy_alex_in_bed_orion
                name: In bed

      - type: entities
        title: Shared
        state_color: true
        entities:
          - entity: switch.sleepy_power
            name: Power
          - entity: switch.sleepy_quiet_mode
            name: Quiet mode
          - entity: number.sleepy_led_brightness
            name: LED brightness
          - type: divider
          - entity: sensor.sleepy_bed_access
            name: Who has access
          - entity: sensor.sleepy_zone_mode
            name: Zone mode
          - entity: binary_sensor.sleepy_device_online
            name: Bed online
          - entity: update.sleepy_firmware
            name: Firmware
```

Duplicate the first column for the second sleeper, swapping `alex` for their
alias.

### Entities with no data yet

Sleep insights read `unknown` until a night finishes processing, which looks
broken on a fresh install. Hide the card until there is something to show:

```yaml
      - type: entities
        title: Last night
        entities:
          - sensor.sleepy_alex_sleep_score
          - sensor.sleepy_alex_total_sleep_time
        visibility:
          - condition: state
            entity: sensor.sleepy_alex_sleep_score
            state_not: unknown
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

- Which physical topper pad corresponds to which side is unconfirmed. Pad to person naming is provisional and may be backwards.
- The `phases` windows on the live session don't line up with the stored schedule, and nobody knows what anchors them. The sensor ships the numbers as given rather than claiming to interpret them.
- Firmware install has never been executed, because no update has been offered since this was written. The route is app-derived and it is the only write with no undo.
- Cooling above 120 minutes, and relief types other than cooling, are unverified.
- HRV is frequently `null` in real data and reports `unknown`.
- Split Zones ships disabled, because nothing in the payload reports split state and a press would have no observable result.
- Sleep latency is deliberately absent. The fall-asleep timestamps only populate on sessions somebody hand-edited in the app, which makes them overrides rather than measurements.

## Contributing

Read `AGENTS.md` first. It carries the measured API contract, the verification log, and the source-of-truth policy.

The one rule that matters: **do not ship a control against an unverified route.** If you cannot demonstrate a write against a live bed, leave it read-only and say so in the docstring.

## License

MIT. See [LICENSE](LICENSE).

Not affiliated with or endorsed by Orion Longevity Inc.

> `orion_info.py` prints raw API and WebSocket payloads, which include live heart rate, breathing rate and bed occupancy. Its output is unredacted by design. Read it before pasting it anywhere.

## Downgrading from 3.0

Version 3.0 re-keys every person's entities onto their Orion account id so
that replacing a linked partner can no longer hand one person's sleep
history to another. Entity ids, recorder history and dashboards are
unaffected by the upgrade.

Rolling back to 2.x is the direction that hurts. 2.x looks for the ids it
used to write, does not find them, and builds a second set of entities.
The originals stay in the registry holding all the history, permanently
unavailable.

Run the `orion_sleep.revert_unique_ids` action before downgrading. It
replays the recorded rename map backwards and puts everything back. Take a
Home Assistant backup first regardless.
