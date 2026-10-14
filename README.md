# Squid dryer controller (Raspberry Pi)

`dryer_cloud.py` runs the dryer: relays on GPIO17 (fan) and GPIO27 (heater, both active low), and a DS18B20 sensor on 1-Wire.
The Pi is the authority on what the machine is actually doing. Firebase is used for commands and for showing state.
**Once a drying task is accepted, the Pi does not need the internet to finish it.**

| File | Role |
|---|---|
| `dryer_cloud.py` | Entry point: control loop, manual fan/heater mode, signals |
| `task_manager.py` | Drying task state machine, timer, thermostat, safety faults |
| `warmup.py` | Pure target-temperature detection (moving median + dwell) and dip tracking |
| `recovery.py` | Decision after a restart: resume / failed / recovery required |
| `task_store.py` | SQLite record (`data/dryer.db`): tasks, temperature log, event outbox, processed commands |
| `cloud_sync.py` | Background Firebase thread: command stream + polling, heartbeat, event upload |
| `hardware.py` | Relays and sensor reading (unchanged logic) |
| `local_api.py` | Local HTTP API and the web panel, so the machine works from a phone with no internet |
| `netwatch.py`, `net_policy.py` | Own service: home Wi-Fi first, hotspot as fallback (`net_policy.py` is the pure decision logic) |
| `command_age.py` | Pure rules for rejecting old cloud commands |
| `config.py` | All tunable values |

Run the tests with `.venv/bin/python -m pytest` (the relays are mocked, so no hardware is touched).

## Task lifecycle

```
ready ─start─▶ starting ─▶ warming_up ─(target held)─▶ running ─▶ completed ─▶ cooling ─(temp ≤ safe)─▶ ready
                              │   │                      │  │
                              │   └─ timeout / sensor /  │  └─ sensor fault / over-temp ─▶ failed ──acknowledge──▶ ready
                              │      over-temp ─▶ failed │
                              └─ cancel ─▶ cancelled     └─ cancel ─▶ cancelled

restart while active ─▶ recovering ─┬─▶ warming_up         (was warming: dwell starts over)
                                    ├─▶ running            (auto-resume, checks passed)
                                    ├─▶ failed              (large temperature drop)
                                    └─▶ recovery_required ──resume──▶ running
                                                          └─abort───▶ cancelled
```

- The task row is committed to SQLite as `starting` and then `warming_up` **before** the heater can switch on.
- **Warm-up, then drying.** A task starts `warming_up`: the heater runs under the thermostat but the drying timer does not. Each valid sensor reading (every `SENSOR_INTERVAL` s) goes through a moving median over `MEDIAN_WINDOW` samples (`warmup.py`). The target counts as reached only when that smoothed value stays at or above `target − TARGET_TOLERANCE_C` continuously for `DWELL_SECONDS`. Falling out of the band, or a missing sample (more than `DWELL_MAX_GAP_SECONDS` between readings), restarts the dwell, and a failed read is never treated as "reached". On confirmation the task becomes `running`, and `drying_started` is **backdated to the first sample of the qualifying dwell**; `target_confirmed` is when the dwell finished. The default tolerance (2.5 C) is deliberately wider than `TASK_HYSTERESIS_C` (2 C): otherwise the thermostat's own heater cycling would keep dropping the chamber out of the band.
- **Timestamps** (all from the Pi's clock): `started_wall` = session start, `drying_started_wall`, `target_confirmed_wall`, `ended_wall`. Warm-up time = `drying_started − session_started`; drying time = `(ended or now) − drying_started`. The configured `duration_minutes` is always the full drying duration: warm-up is recorded next to it and never added or subtracted.
- **Completion is unchanged** (duration reached, last `complete_batch`, cancel, faults), except that the duration is measured from `drying_started`. Experiments (`mode: "experiment"`, sent with the 10080 min ceiling) still end only through `complete_batch` or cancel. `mode` is recorded for audit and does not change behaviour.
- **Warm-up timeout:** if the target is not confirmed within `WARMUP_TIMEOUT_SECONDS` (powered-on time, kept across restarts) the task fails with `warmup_timeout` and the heater turns off.
- **Dips while drying:** if the smoothed temperature leaves the band after drying started, the timer keeps running. Each excursion `{start, end, min}` is stored (`task_excursions`) and reported in `dips`; one longer than `DIP_WARNING_LIMIT_SECONDS` sets `dip_warning` and logs `dip_warning`, without stopping anything.
- **Frozen settings:** `target_c`, `tolerance_c`, `hysteresis_c`, `dwell_s`, `median_window`, `warmup_timeout_s`, `dip_limit_s` and `max_temp_c` are copied onto the task when it is accepted, and the thermostat, detection and faults all use that copy. Editing `config.py` never changes a running session. A task whose frozen settings are missing or invalid fails as `invalid_session` and the heater stays off. Tasks saved by older firmware get their settings frozen at the first start after the upgrade and keep their original clock.
- **Safety nets:** the relays default to off at boot, in the `finally` of the main loop, and whenever the control tick raises (`controller_error` is logged and the heater forced off before the exception propagates). Reaching `MAX_TEMP_CUTOFF_C` (`min(target + 10, TEMP_MAX_C)`) fails the task and cuts the heater in software, independent of Firebase. A sensor failure (`SENSOR_BAD_LIMIT` consecutive bad reads, or no valid reading for `SENSOR_STALE_SECONDS`) fails the task. A reading outside `TEMP_PLAUSIBLE_MIN_C`..`TEMP_PLAUSIBLE_MAX_C` counts as a failed read.
- **Temperature log:** every valid reading (raw and smoothed, with phase `warming_up` or `drying`) is written to the `temp_log` table of the local database for the whole session, flushed with each checkpoint. Per-minute summaries (`temp_minutes`) are uploaded to `task_logs/{task_id}/{minute}` every `TEMP_LOG_UPLOAD_SECONDS`, in the same write as the events, so they queue up offline and sync when Wi-Fi returns.
- **Timer:** only time actually spent running counts. Elapsed time comes from the monotonic clock and is saved every `CHECKPOINT_SECONDS`. After a restart the timer continues from the last saved value. It never restarts from zero, and downtime is never counted as drying time. At worst, one checkpoint interval (10 s) of drying gets repeated.
- **Thermostat during a task:** heater on at ≤ `TASK_SETPOINT_C − TASK_HYSTERESIS_C`, off at ≥ `TASK_SETPOINT_C`, fan always on. `TEMP_MAX_C` stays the hard cutoff.
- **Losing the internet never fails a task.** A sensor failure (3 bad reads or 10 s without a valid reading) or reaching `TEMP_MAX_C` turns the heater off and marks the task `failed`. The machine then cools (below).
- **Confirming a batch dry (`complete_batch`):** frees one row. The other rows keep drying and the schedule is unchanged. When the last row is freed the task ends `completed`.
- **Cooling:** whenever a task ends (completed, cancelled, failed) the heater turns off immediately and the fan keeps running while the measured temperature is above the safe shutdown temperature. Once it is at or below, the fan turns off and the status goes back to `ready`. The safe temperature comes from `settings/safe_temp_c` (25–50 °C), is saved in the local database and survives reboots and outages; the default is `COOLDOWN_SAFE_TEMP_C`. Cooling stops after `COOLDOWN_MAX_SECONDS`, or `COOLDOWN_NO_SENSOR_SECONDS` if the sensor is dead. A reboot while hot also starts cooling. A new task can start during cooling once it is below the `too_hot` limit, and manual fan/heater commands are ignored (and reset) while cooling. Everything runs locally, with no internet needed.
- A `failed` task blocks new starts until the web app sends `acknowledge`. Nothing ever restarts heating on its own after a failure.
- **Manual mode** (`commands/fan`, `commands/heater`): `commands/heater = true` means manual heating is enabled. The Pi then holds `settings/manual_target_temp_c` itself (heater off at the target, back on 2 C below it, `MANUAL_HYSTERESIS_C`) and keeps doing so with no internet. The old 600 s auto-off and 120 s offline cut are gone. After a boot the heater stays blocked until the operator re-enables it. Over temperature (`TEMP_MAX_C`), a failed sensor or a stopped fan still switch manual heating off and reset `commands/heater`. Ignored while a task is active or waiting for recovery.
- **Heater cut-off** (`settings/heater_cutoff_c`, 30-58 C): in manual mode and during tasks the heater turns off at or above this temperature and is allowed again at `HEATER_CUTOFF_MARGIN_C` (3 C) below it. This only pauses heating: timers, fan, sensor monitoring and status keep running. It is separate from the `TEMP_MAX_C` fault, which still fails a task and cuts the heater, and from the sensor-failure fault.
- **Local settings:** `heater_cutoff_c`, `manual_target_temp_c` (30-55), `manual_target_humidity` (20-80, stored and reported only, there is no humidity sensor) and `safe_temp_c` are validated on arrival, saved in the local database and loaded on every boot, so they apply offline. Missing or invalid saved values fall back to the `config.py` defaults. `settings/updated_at` (server timestamp) is required; a payload not newer than the last applied one is ignored, so a stale dashboard write cannot undo newer settings.

## Restart and power-interruption detection

On every start the controller compares the saved `boot_id` with the current one (`/proc/sys/kernel/random/boot_id`). That tells a process restart apart from a full reboot. It also reads a `clean_shutdown` flag, which is only set on SIGTERM/SIGINT and never after a crash.
**If no task was active, startup is normal.** A fresh boot is never treated as a failure.

If a task was active, the heater and fan stay off for `RECOVERY_OBSERVE_SECONDS`. During that time the controller collects temperature readings and compares the median of the earliest readings with the last temperature saved before the interruption.

**A task that was still `warming_up` does not use the table below**, because no drying time was at stake. It resumes as `warming_up` if the sensor is healthy and the chamber is below `max_temp_c`, with the dwell starting over (the target is never taken as reached from before the restart) and the warm-up timeout continuing from its last checkpoint. Otherwise it fails (`sensor_unavailable`, `over_temperature`). A task that was `running` keeps its original `drying_started`.

The **gap** (how long the machine was interrupted) is measured like this:
- After a process restart (same boot): from the monotonic clock, which is exact.
- After a reboot: from the wall clock. The Pi has **no RTC**, so the wall clock is only trusted once systemd-timesyncd has synced (`/run/systemd/timesync/synchronized`). The controller waits up to `RECOVERY_CLOCK_WAIT_SECONDS` for that.

The rules are checked in this order:

| Condition | Result | Reason |
|---|---|---|
| saved elapsed ≥ duration | `completed` | duration was already proven before the interruption |
| sensor unhealthy or < `RECOVERY_MIN_READS` valid readings | `recovery_required` | `sensor_unavailable` |
| current temp ≥ `TEMP_MAX_C` | `recovery_required` | `over_temperature` |
| no valid saved temperature | `recovery_required` | `no_reference_temp` |
| drop ≥ `TEMP_DROP_FAIL_C`, reboot, not clean | **`failed`** | **`possible_power_loss`** |
| drop ≥ `TEMP_DROP_FAIL_C`, otherwise | `failed` | `interrupted_heat_lost` |
| gap unknown (clock never synced) | `recovery_required` | `elapsed_time_unverifiable` |
| gap > `RESUME_MAX_GAP_SECONDS` | `recovery_required` | `interruption_too_long` |
| `TEMP_DROP_TOLERANCE_C` < drop < `TEMP_DROP_FAIL_C` | `recovery_required` | `temperature_drop_uncertain` |
| otherwise | auto-resume | `checks_passed` |

> **A temperature drop alone does not prove a power cut.** The same drop can come from normal cooling, a moved sensor, or another fault. This is why `possible_power_loss` is only reported when there's also an unclean reboot, and why uncertain cases stop and ask a person instead of guessing. Every decision records its evidence (reference and current temperature, drop, gap, boot change, clean flag, clock sync) in `diagnostics`.

### Tunable values (`config.py`)

| Setting | Default | Meaning |
|---|---|---|
| `TASK_SETPOINT_C` | 55 | thermostat target (`TEMP_MAX_C − TEMP_RESUME_DROP`) |
| `TASK_HYSTERESIS_C` | 2 | heater turns back on this far below the setpoint |
| `TEMP_DROP_TOLERANCE_C` | 3 | drop at or below this counts as normal startup fluctuation. Keep it above `TASK_HYSTERESIS_C`, because the saved reference can be at the top of the thermostat band |
| `TEMP_DROP_FAIL_C` | 8 | drop at or above this counts as heat loss → failed |
| `RECOVERY_OBSERVE_SECONDS` | 20 | observation window before deciding |
| `RECOVERY_MIN_READS` | 3 | valid readings needed for a decision |
| `RECOVERY_CLOCK_WAIT_SECONDS` | 120 | maximum wait for NTP after a reboot |
| `RESUME_MAX_GAP_SECONDS` | 300 | longer interruptions are never resumed automatically |
| `CHECKPOINT_SECONDS` | 10 | how often progress is saved |
| `SENSOR_INTERVAL` | 2 | seconds between chamber readings (`sample_interval`) |
| `SENSOR_BAD_LIMIT` | 3 | consecutive bad reads before the sensor counts as failed (`sensor_fail_limit`) |
| `MEDIAN_WINDOW` | 5 | samples in the moving median |
| `TARGET_TOLERANCE_C` | 2.5 | target counts as reached at `target − tolerance`. Keep it above `TASK_HYSTERESIS_C` |
| `DWELL_SECONDS` | 60 | how long the smoothed value must stay in the band |
| `DWELL_MAX_GAP_SECONDS` | 5 | a longer gap between readings restarts the dwell |
| `WARMUP_TIMEOUT_SECONDS` | 10800 | target not confirmed by then → `warmup_timeout`, heater off |
| `DIP_WARNING_LIMIT_SECONDS` | 600 | a dip below the band this long during drying sets `dip_warning` |
| `MAX_TEMP_CUTOFF_C` | 60 | software over-temperature fault: `min(target + 10, TEMP_MAX_C)` |
| `TEMP_PLAUSIBLE_MIN_C` / `MAX_C` | −10 / 100 | readings outside this count as failed reads |
| `TEMP_LOG_UPLOAD_SECONDS`, `TEMP_LOG_BATCH` | 60, 500 | per-minute temperature summaries to Firebase |
| `COOLDOWN_SAFE_TEMP_C` | 40 | default cooldown temperature; the web app can change it (`settings/safe_temp_c`, 25–50) |
| `COOLDOWN_MAX_SECONDS` | 3600 | cooling is cut off after this long |
| `COOLDOWN_NO_SENSOR_SECONDS` | 600 | fan run time if the sensor is dead while cooling |
| `CLOUD_COMMAND_MAX_AGE_SECONDS` | 30 | a command that came through Firebase (fan/heater or task) made more than this long ago is ignored; so is one made before the latest local command. Task requests are aged from `issued_at`, falling back to `requested_at`. Rejections are acked as `stale_command` |
| `COMMAND_MAX_AGE_SECONDS` | 600 | the same rule for requests that do not come through Firebase (only checked when the clock is synced) |
| `TEMP_PLAUSIBLE_MIN_C`, `_MAX_C` | -10, 100 | a reading outside this range counts as a failed read, which turns the heater off |
| `MAX_BATCH_COUNT`, `MIN/MAX_DURATION_MIN` | 100, 1–10080 | start validation, same limits as `database.rules.json` |
| `ALLOWED_ROW_LEVELS`, `MAX_WEIGHT_KG` | 1–4, 20 | **placeholders, set them for the machine**. Weight is only checked when sent |

## Firebase schema (`devices/squid-dryer/...`)

The schema follows the web app. Security rules are in `database.rules.json`. The Pi uses the Admin SDK, which ignores rules, so the rules only limit what the web app can do: it may write `commands`, `settings` and `task/request`, and nothing else. `settings/safe_temp_c` is the post-task cooldown temperature and `settings/heater_cutoff_c`, `manual_target_temp_c`, `manual_target_humidity` and `updated_at` are the heater cut-off and manual targets. The machine reports what it applies in `state/safe_temp`, `heater_cutoff`, `heater_cutoff_margin`, `target_temp`, `target_humidity` and `settings_updated_at`, plus `manual_heating` (manual heating enabled, not the relay) and `heater_block` (`cutoff`, `over_temperature`, `sensor_fault`, `no_fan`, `reboot_block`, or absent). `state/heater` is always the actual relay. `state/cooling` / `state.status = "cooling"` while cooling.

**The web app writes** `task/request`, replacing it with each new request:

```json
{
  "command": "start",
  "task_id": "batch-2026-10-13-01",
  "duration_minutes": 240,
  "batch_count": 40,
  "row_levels": "1,3",
  "weight_kg": 2.5,
  "mode": "standard",
  "requested_by": "<uid>",
  "requested_at": {".sv": "timestamp"}
}
```

- `command` is one of these:
  - `start`
  - `add_batch` (adds one batch to the running task, see below)
  - `complete_batch` (`row_levels` = one row: the operator confirmed that batch dry. Frees the row; the last row ends the task and starts cooling. The report lists freed rows in `completed_rows`)
  - `cancel` (a running task, or one waiting for recovery)
  - `resume` (only from `recovery_required`)
  - `abort` (gives up a task in `recovery_required`)
  - `acknowledge` (clears a `failed` task so a new one can start)
- `task_id` must match `[A-Za-z0-9_-]{1,64}` and can never be reused. For commands other than `start` it's optional; if sent, it must match the current task.
- `add_batch` needs the running `task_id`, a new `batch_id` (`[A-Za-z0-9_-]{1,64}`), `duration_minutes` for that batch alone, and exactly one free `row_levels` entry (`weight_kg` optional). The batch is timed from the moment it is added. The task is extended if the batch would finish after the task's current end, and is never shortened. The relays, the running clock and the batches already drying are untouched. Only a task that is drying accepts it: while `warming_up` it is rejected with `task_not_running` (and `complete_batch` with `not_allowed_while_warming_up`; `cancel` works). Rejected with `task_not_running`, `task_mismatch`, `row_occupied`, `duplicate_batch`, `invalid_batch_id`, `invalid_row_levels`, `invalid_duration`, `invalid_weight`, `invalid_batch_count`, `not_ready` or `sensor_unavailable`.
- `row_levels` is a comma-separated list of rows. Every row must be in `ALLOWED_ROW_LEVELS`, with no duplicates.
- `weight_kg` is optional.
- `requested_at` must be the server timestamp. Each request is processed **exactly once**, identified by `command_id` if one is sent, otherwise by `task_id + command + requested_at`. Reading the same request again after a reconnect or reboot does nothing.
- `commands/fan` and `commands/heater` work as before (manual mode).

**The Pi writes** (the web app only reads these):

- `task/ack`: `{command_id, command, task_id, result: accepted|rejected|duplicate, reason, at}`. The request has been processed once `task/ack.command_id` equals your `command_id`, or `"<task_id>:<command>:<requested_at>"` when you don't send one. Read `requested_at` back from `task/request` to get the resolved server time. Until then it has **not been processed yet**. `reason` explains a rejection, e.g. `too_hot`, `invalid_row_levels`, `unacknowledged_failure`.
- `task/report`: `{task_id, status, seq, boot_id, started_at, drying_started_at, target_confirmed_at, ended_at, warmup_seconds, mode, target_temp_c, tolerance_c, dwell_seconds, warmup_timeout_seconds, smoothed_temp_c, dips, dip_count, dip_warning, duration_minutes, elapsed_seconds, remaining_seconds, progress_percent, batch_count, row_levels, weight_kg, completed_rows, reason, detail, acknowledged, batches, reported_at}`
  - `started_at` is the session start (warm-up began). `drying_started_at` is absent while warming up. `elapsed_seconds`, `remaining_seconds` and `progress_percent` count **drying** time only and stay 0 / full / 0 while warming up. Batch `offset_seconds` is measured from the drying start.
  - `dips` lists up to the last 20 excursions below the band, `{start, end, min_temp_c}` (epoch ms, `end` absent while one is open).
- `task_logs/{task_id}/{minute}`: `{t, avg, min, max, raw, n}`, one per minute of the session (smoothed average/min/max, last raw reading, sample count). The full-resolution log stays in the Pi's `temp_log` table.
  - `batches` lists the batches added with `add_batch`: `{batch_id, row_level, offset_seconds, duration_minutes, elapsed_seconds, remaining_seconds, progress_percent}`. Each is measured from when it was added. `[]` when none were added.
  - `status` is `starting`, `warming_up`, `running`, `completed`, `cancelled`, `failed`, `recovering` or `recovery_required`.
  - `reason` is set for failures and recovery holds, e.g. `possible_power_loss`, `warmup_timeout`, `invalid_session`.
  - `detail` is a short readable summary. The full diagnostics are in `events`.
  - `seq` increases with every change.
- `state/status`: `ready`, `starting`, `warming_up`, `running`, `cooling`, `recovering`, `recovery_required` or `failed` (a failure not yet acknowledged). `state/ready` is true when a new task can be started.
- `state/fan`, `heater`, `temp`, `sensor_ok`, `temp_max`, `last_event`, `boot_id`, `pending_events` (events still waiting to upload)
- `state/last_seen`: server timestamp of the last successful write.
- `events/{0000000001…}`: the local event log in order. Events recorded offline are uploaded after reconnection, and their `at` is the Pi's local time.

**Manual control latency:** a Firebase stream on `commands` and `task/request` wakes the sync thread the moment the web app writes, and the 2 s poll (`SYNC_INTERVAL`) is the backup if the stream is down (`COMMAND_LISTEN`). The control loop applies the newest manual request at once and publishes `state` as soon as fan, heater or status change, instead of waiting for the 5 s timer. The temperature sensor is read on its own thread so a slow read never delays a command. The Pi only ever resets `commands/*` to `false`, so racing an operator write can't switch equipment on. The web app still treats `state` as the only proof of what the hardware did.

**Online/offline:** while connected the Pi writes about every 5 s. There's no "online" flag, because the Pi can't report that it has lost its connection. Treat the machine as offline when `serverNow − last_seen > 90 s`, with `serverNow = Date.now() + .info/serverTimeOffset`. After it comes back:
- If `boot_id` changed and there's a `device_started` event, the machine restarted.
- If only `connection_lost` / `connection_restored` events appear, it was just an internet outage.

To notify the user about a technical failure, watch `task/report`. `status == "failed"` (`reason` says why, e.g. `possible_power_loss` or `sensor_failure`) needs an `acknowledge`; `recovery_required` needs a `resume` or `abort`.

## Local mode and networking

The machine can be controlled from a phone with no internet at all. The Pi serves the web panel and its API itself (`local_api.py`, port 8080), and a second service, `netwatch.py`, keeps the Pi reachable: it joins the home Wi-Fi when it can and raises its own hotspot when it cannot. The relay control loop never waits for either of them.

The panel and the API are the same ones the cloud commands use: every command, from Firebase or from the phone, goes through the same queue into the same `ManualControl` and `TaskManager`. The heater limits, the sensor-failure cut-off and the runtime guard sit below that queue and override any command from any source.

### Install

On the Pi, from the repo folder (the venv is created if it is missing):

```
sudo bash deploy/install.sh
sudo -u bensdev .venv/bin/python local_api.py --set-pin     # the panel always asks for this PIN
```

`install.sh` is safe to run again. It installs both services, asks once for the hotspot password (8 to 63 characters, stored in `secrets/hotspot_psk`, which git ignores), creates the hotspot profile and starts everything. Both services start on power-up and restart if they crash.

All network settings are in the `NET_*` block of `config.py`. Each can also be set with an environment variable of the same name prefixed with `SQUID_` (for example `SQUID_NET_BOOT_GRACE_SECONDS=300`).

| Value | Default | Meaning |
|---|---|---|
| `NET_WIFI_CONNECTION` | `netplan-wlan0-ANN-ANN 5G` | NetworkManager profile of the home Wi-Fi, exactly as `nmcli connection show` prints it. Spaces are fine. **netwatch never renames or edits this profile** |
| `NET_HOTSPOT_CONNECTION`, `NET_HOTSPOT_SSID` | `squid-hotspot`, `SquidDryer` | the hotspot profile name and the network name phones see |
| `NET_HOTSPOT_PSK_PATH` | `secrets/hotspot_psk` | the WPA2 password |
| `NET_HOTSPOT_BAND`, `NET_HOTSPOT_CHANNEL` | `bg`, 6 | 2.4 GHz, for range and so that every phone can join, even though the home network is 5 GHz |
| `NET_HOTSPOT_ADDRESS` | `10.42.0.1/24` | fixed address in hotspot mode |
| `NET_BOOT_GRACE_SECONDS` | 180 | after power-on, wait this long for the router before considering the hotspot |
| `NET_RETRY_INTERVAL_SECONDS` | 120 | an empty hotspot tries the home Wi-Fi this often |
| `NET_WIFI_TRY_TIMEOUT_SECONDS` | 40 | how long one try at the home Wi-Fi may take |
| `NET_WIFI_DROP_GRACE_SECONDS` | 60 | a lost Wi-Fi link gets this long to come back before the Pi tries explicitly and then raises the hotspot |

### How it decides

- **Wi-Fi is the default.** The hotspot is a temporary fallback.
- **After power-on**, the Pi waits up to the boot grace period for the router, which may boot slower than the Pi. Meanwhile it asks for the Wi-Fi every 30 s, because NetworkManager gives up on automatic retries after a few failures.
- **Wi-Fi without internet stays Wi-Fi.** Internet is not part of the decision. Use the LAN address.
- **In hotspot mode with nobody connected**, every retry interval the hotspot is switched off for up to 40 s while the home Wi-Fi is tried, then restored if that fails. The Pi does not scan while the hotspot is up (one radio cannot do both).
- **A phone connected to the hotspot is never kicked off.** The count of connected phones is read from `iw`; if it cannot be read, the Pi assumes someone is connected.
- **If the Wi-Fi drops while running**, NetworkManager gets 60 s, then the Pi tries once explicitly, then raises the hotspot and follows the rules above.
- Every transition is logged with a timestamp: `journalctl -u squid-netwatch -f`.

netwatch checks on start that NetworkManager manages `wlan0`, that the home profile exists and that the hotspot password file is valid. If not, it logs why and touches nothing.

### Finding the Pi

| Situation | Address |
|---|---|
| Home Wi-Fi (LAN) | `http://<lan address>:8080`. The Pi also publishes it as `state/local_url`; look it up in the router's device list or with `hostname -I` on the Pi |
| Hotspot | join `SquidDryer`, then open `http://10.42.0.1:8080` |

**Give the Pi a fixed LAN address.** The simplest way is a DHCP reservation for the Pi's MAC address in the router (`ip link show wlan0`). That needs no change on the Pi. Setting a static address on the home profile (`nmcli connection modify "<profile>" ipv4.method manual ipv4.addresses ... ipv4.gateway ... ipv4.dns ...`) also works, but on this Pi the profile is owned by netplan, so check afterwards that the setting survived a reboot, and test it with the router in reach before relying on it.

On the hotspot the address is fixed by `NET_HOTSPOT_ADDRESS`. Use the IP address rather than a hostname, since phones often do not resolve `.local` names.

### Phone tips

- On the hotspot, the phone will say "no internet" and may offer to switch back to mobile data. Tap **Stay connected** (Android) or leave "Auto-Join" on (iPhone), and turn off mobile data while you use the panel. Otherwise the browser may send the request out through mobile data instead of to the Pi.
- Open the panel by IP address, not by a hostname.
- If the hotspot does not show up on a phone, check `iw reg get`: a country must be set (`raspi-config`, Localisation Options, WLAN Country).

### Deploying the web panel

The Pi never builds or installs anything for the web side. On the computer with the web app: `npm run deploy:pi` (see the web repo README). That builds the local-mode bundle and copies only `dist-pi/` to the Pi's web folder (`SQUID_WEB_ROOT`).

### Stale cloud commands

A browser can queue a write while offline and deliver it later. The Pi therefore ignores a cloud command when it was made more than `CLOUD_COMMAND_MAX_AGE_SECONDS` (30 s) before it was read, and also when it was made before the most recent local command. Ignored fan/heater commands are logged as `stale_command_ignored` (with `reason` `too_old`, `superseded_by_local` or `unverifiable`) and a left-over heater request is withdrawn. Ignored task requests are acked as `rejected` with reason `stale_command`. Ages are compared as durations on the server clock, so a wrong Pi clock cannot cause or hide a stale command.

When the internet comes back, the Pi publishes its current actual state at once (`state/*`), so the online dashboard shows the truth rather than what was last requested.

### Manual test checklist

1. **Router slower than the Pi.** Power off both, power on the Pi, power on the router about 60 s later. Within a minute or two of the router being ready, the Pi is on Wi-Fi and `nmcli connection show --active` does not list `squid-hotspot`. The log never says "starting the hotspot".
2. **Router off.** After about 3 minutes `SquidDryer` appears. Join it from a phone, open `http://10.42.0.1:8080`, enter the PIN and switch the fan and the heater.
3. **Router back while a phone is on the hotspot.** Switch the router on. The phone stays connected and the log shows no "trying the home Wi-Fi".
4. **Phone leaves the hotspot.** Disconnect the phone. Within about 120 s plus 40 s the Pi is on the home Wi-Fi and the hotspot is gone.
5. **Wi-Fi up, internet down.** Unplug the router's WAN cable. The Pi stays on Wi-Fi, no hotspot appears, and `http://<lan address>:8080` works. The panel banner shows "cloud offline".
6. **Internet returns.** Plug the WAN cable back in. Within a few seconds the online dashboard shows the current fan, heater and temperature.
7. **Late cloud command.** Disconnect the Pi from the internet, send a fan command from the online app, wait over 30 s, reconnect. The fan does not change, and the `events` list shows `stale_command_ignored`.
8. **Local beats an older cloud command.** Send a cloud fan command and, within 30 s and before the Pi has read it, use the panel to set a different state. The panel's state wins and `stale_command_ignored` shows `superseded_by_local`.
9. **Heater cut-off.** With a low `heater_cutoff_c` the heater turns off at that temperature. Unplug the temperature sensor: the heater turns off with `heater_block: sensor_fault`, whatever command is sent from the cloud or the panel.
10. **Reboot persistence.** After `sudo reboot`, `nmcli connection show squid-hotspot` still exists with `connection.autoconnect: no` and band `bg`, and the home profile still has its original name. (On a netplan system the profile is stored under `/etc/netplan/`; netwatch repairs the hotspot profile on every start if it is missing.)
11. **Cloud mode unchanged.** Log in to the online app: manual toggles, starting and cancelling a task, and the dashboard all behave as before.
