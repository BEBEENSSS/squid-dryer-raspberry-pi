# Squid dryer controller (Raspberry Pi)

`dryer_cloud.py` runs the dryer: relays on GPIO17 (fan) and GPIO27 (heater, both active low), and three chamber sensors: two DS18B20 on 1-Wire (bottom GPIO4, middle GPIO23, one per bus) and a DHT22 on GPIO22 at the top, which also measures humidity (see "Temperature sensors" below).
The Pi is the authority on what the machine is actually doing. Firebase is used for commands and for showing state.
**Once a drying task is accepted, the Pi does not need the internet to finish it.**

| File | Role |
|---|---|
| `dryer_cloud.py` | Entry point: control loop, manual fan/heater mode, signals |
| `task_manager.py` | Drying task state machine, timer, thermostat, safety faults |
| `warmup.py` | Pure target-temperature detection (moving median + dwell, across all three zones) and dip tracking |
| `calibration.py` | Per-sensor offsets written by `sensor2_test.py --calibrate` and applied to the middle and top sensors (temperature, and humidity for the DHT22) |
| `dht22.py` | DHT22 temperature + humidity driver (GPIO character device, kernel-timestamped edges). The service reads it as the top sensor (`hardware.make_zone_sensor`), and `sensor2_test.py` tests it |
| `recovery.py` | Decision after a restart: resume / failed / recovery required |
| `task_store.py` | SQLite record (`data/dryer.db`): tasks, temperature log, event outbox, processed commands |
| `cloud_sync.py` | Background Firebase thread: command stream + polling, heartbeat, event upload |
| `temp_sync.py` | Temperature history: a recorder thread (SQLite) and an uploader thread (Firebase), both off the control loop |
| `temp_history.py` | Pure rules for that history: record ids, payloads, retry backoff, which old records may be trimmed |
| `link_monitor.py` | Tells `offline`, `firebase_unavailable` and `synced` apart from real request results (plus a small internet probe) |
| `hardware.py` | Relays, sensor reading (DS18B20 and DHT22), and `SensorArray` (the three chamber sensors by zone, plus the top one's humidity) |
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
- **Warm-up, then drying.** A task starts `warming_up`: the heater runs under the thermostat but the drying timer does not. Each valid sensor reading (every `SENSOR_INTERVAL` s) goes through a moving median over `MEDIAN_WINDOW` samples (`warmup.py`). **Every zone (bottom, middle, top) has its own median and its own band.** The target counts as reached only when all of them are in their band at the same time and the last one to get there has stayed for `DWELL_SECONDS`; a zone's band floor is `target − TARGET_TOLERANCE_C − ZONE_FLOOR_ALLOWANCE_C[zone]` (the allowance is 0 by default, and the bottom never has one). Falling out of the band, or a missing sample (more than `DWELL_MAX_GAP_SECONDS` between readings) in any zone, restarts the dwell, and a failed read is never treated as "reached". On confirmation the task becomes `running`, and `drying_started` is **backdated to the first sample at which all zones were in band**; `target_confirmed` is when the dwell finished. The default tolerance (2.5 C) is deliberately wider than `TASK_HYSTERESIS_C` (2 C): otherwise the thermostat's own heater cycling would keep dropping the chamber out of the band. The thermostat itself still reads sensor 1 (bottom) only, so the upper zones may lag it: see "Temperature sensors" for how to tune the floors.
- **Timestamps** (all from the Pi's clock): `started_wall` = session start, `drying_started_wall`, `target_confirmed_wall`, `ended_wall`. Warm-up time = `drying_started − session_started`; drying time = `(ended or now) − drying_started`. The configured `duration_minutes` is always the full drying duration: warm-up is recorded next to it and never added or subtracted.
- **Completion is unchanged** (duration reached, last `complete_batch`, cancel, faults), except that the duration is measured from `drying_started`. Experiments (`mode: "experiment"`, sent with the 10080 min ceiling) still end only through `complete_batch` or cancel. `mode` is recorded for audit and does not change behaviour.
- **Warm-up timeout:** if the target is not confirmed within `WARMUP_TIMEOUT_SECONDS` (powered-on time, kept across restarts) the task fails with `warmup_timeout` and the heater turns off.
- **Dips while drying:** if the smoothed temperature leaves the band after drying started, the timer keeps running. Each excursion `{start, end, min}` is stored (`task_excursions`) and reported in `dips`; one longer than `DIP_WARNING_LIMIT_SECONDS` sets `dip_warning` and logs `dip_warning`, without stopping anything.
- **Frozen settings:** `target_c`, `tolerance_c`, `hysteresis_c`, `dwell_s`, `median_window`, `warmup_timeout_s`, `dip_limit_s`, `max_temp_c` and the middle and top band floors (`floor_middle_c`, `floor_top_c`) are copied onto the task when it is accepted, and the thermostat, detection and faults all use that copy. `target_c` and `max_temp_c` are the saved target and safe shutdown temperature (`settings/manual_target_temp_c`, `settings/temp_max_c`) of that moment. **The saved target is the one target there is, so a running task follows it at once** (`target_c` and the two band floors move together, keeping each zone's allowance; a `task_target_changed` event is logged): in warm-up the dwell starts over against the new floors, while drying an open dip is closed and, after a *raise*, the chamber gets until `warmup_timeout_s` to reach the new band before being below it counts as a dip. A target that would sit less than `TEMP_MAX_GAP_C` under this task's own safe shutdown temperature (only possible if the shutdown was raised after the start) is not followed and logs `task_target_kept`. A raised shutdown temperature waits for the next task, but a **lowered shutdown temperature applies to a running task at once** (the lower of the two is its limit), so tightening the limit can never be ignored. The two floors are optional: a task saved before the zones existed has none and warms on the bottom sensor alone, as it was accepted. Editing `config.py` never changes a running session. A task whose frozen settings are missing or invalid fails as `invalid_session` and the heater stays off. Tasks saved by older firmware get their settings frozen at the first start after the upgrade and keep their original clock.
- **Safety nets:** the relays default to off at boot, in the `finally` of the main loop, and whenever the control tick raises (`controller_error` is logged and the heater forced off before the exception propagates). Reaching the safe shutdown temperature (`settings/temp_max_c`, saved on the Pi, default and ceiling `TEMP_MAX_C` = 60) **on any healthy sensor** fails the task and cuts the heater in software, independent of Firebase. A sensor 1 failure (`SENSOR_BAD_LIMIT` consecutive bad reads, or no valid reading for `SENSOR_STALE_SECONDS`) fails the task (`sensor_failure`). A middle or top sensor failing **during warm-up** fails it too (`zone_sensor_failure`, heater off), because warm-up cannot be confirmed without it; **while drying** it only logs `zone_sensor_fault` (and `zone_sensor_restored`), sets `state/sensor_middle_ok` or `sensor_top_ok` to false, and drying carries on under sensor 1's thermostat. A reading outside `TEMP_PLAUSIBLE_MIN_C`..`TEMP_PLAUSIBLE_MAX_C` counts as a failed read.
- **Temperature log:** every valid reading of every sensor (raw and smoothed, with phase `warming_up` or `drying` and its `zone`) is written to the `temp_log` table of the local database for the whole session, flushed with each checkpoint. Per-minute summaries (`temp_minutes`, one row per zone) are uploaded to `task_logs/{task_id}/{minute}` every `TEMP_LOG_UPLOAD_SECONDS`, in the same write as the events, so they queue up offline and sync when Wi-Fi returns. The bottom sensor's summary stays flat in the entry, exactly as with one sensor; the middle and top ones are `middle` / `top` sub-objects. A minute is always written whole, from every zone's row. `TEMP_LOG_BATCH` counts zone-minute rows.
- **Warm-up progression:** the first time each zone enters its band during warm-up a `warmup_zone_reached` event is logged (`zone`, `smoothed_c`, `floor_c`, `warmup_s`), so the order and timing of the heat climbing the box can be read straight from the events.
- **Timer:** only time actually spent running counts. Elapsed time comes from the monotonic clock and is saved every `CHECKPOINT_SECONDS`. After a restart the timer continues from the last saved value. It never restarts from zero, and downtime is never counted as drying time. At worst, one checkpoint interval (10 s) of drying gets repeated.
- **Thermostat during a task:** heater on at ≤ the task's `target_c − TASK_HYSTERESIS_C`, off at ≥ `target_c` (the saved **target**, `settings/manual_target_temp_c`), fan always on. The saved **safe shutdown temperature** (`settings/temp_max_c`) stays the hard stop; it is never the target.
- **Losing the internet never fails a task.** A sensor failure (3 bad reads or 10 s without a valid reading) or reaching the safe shutdown temperature turns the heater off and marks the task `failed`. Both limits are read from the Pi's own saved settings, so none of this depends on Firebase. The machine then cools (below).
- **Confirming a batch dry (`complete_batch`):** frees one row. The other rows keep drying and the schedule is unchanged. When the last row is freed the task ends `completed`.
- **Cooling:** whenever a task ends (completed, cancelled, failed) the heater turns off immediately and the fan keeps running while the measured temperature is above the safe shutdown temperature (the **hottest healthy sensor** decides, so the fan runs until the whole box is safe). Once it is at or below, the fan turns off and the status goes back to `ready`. The safe temperature comes from `settings/safe_temp_c` (25–50 °C), is saved in the local database and survives reboots and outages; the default is `COOLDOWN_SAFE_TEMP_C`. Cooling stops after `COOLDOWN_MAX_SECONDS`, or `COOLDOWN_NO_SENSOR_SECONDS` if the sensor is dead. A reboot while hot also starts cooling. A new task can start during cooling once it is below the `too_hot` limit, and manual fan/heater commands are ignored (and reset) while cooling. Everything runs locally, with no internet needed.
- A `failed` task blocks new starts until the web app sends `acknowledge`. Nothing ever restarts heating on its own after a failure.
- **Manual mode** (`commands/fan`, `commands/heater`): `commands/heater = true` means manual heating is enabled. The Pi then holds the saved target (`settings/manual_target_temp_c`) itself (heater off at the target, back on 2 C below it, `MANUAL_HYSTERESIS_C`) and keeps doing so with no internet. The old 600 s auto-off and 120 s offline cut are gone. After a boot the heater stays blocked until the operator re-enables it. Over temperature (the saved maximum), a failed sensor or a stopped fan still switch manual heating off and reset `commands/heater`. Ignored while a task is active or waiting for recovery.
- **Temperature limits: target < heater pause < safe shutdown, each with one job.** The **target** (`manual_target_temp_c`, 45-55 C) is what the thermostat holds, on the bottom sensor. The **heater pause** (`settings/heater_cutoff_c`, 46-59 C, the key keeps its old name): in manual mode and during tasks the heater is off while the **hottest healthy sensor** is at or above it and is allowed again at `HEATER_CUTOFF_MARGIN_C` (3 C) below it. It goes by the hottest sensor on purpose: the thermostat cannot see a hot spot on the middle or top sensor, and a pause on the bottom sensor alone would never differ from the thermostat. It only pauses heating: timers, fan, sensor monitoring and status keep running, and it raises no alert (`heater_block: cutoff`). The **safe shutdown temperature** (`temp_max_c`, 50-60 C) is the hard stop above it: it still fails a task, disables manual heating (`heater_block: over_temperature`) and cuts the heater, independent of the sensor-failure fault. `TEMP_MAX_C` (60) is the absolute limit, fixed in firmware, and bounds how high the shutdown temperature can be set. The order is enforced by `machine_settings.limits_error` (shutdown at least `TEMP_MAX_GAP_C` = 5 C above the target, pause at least `CUTOFF_TARGET_GAP_C` / `CUTOFF_SHUTDOWN_GAP_C` = 1 C inside both) and the same numbers are in the web app and `database.rules.json` (`tests/test_rules_sync.py` fails if the ranges drift). `state/shutdown_warning` is the machine's verdict that the hottest sensor is within `SHUTDOWN_WARN_MARGIN_C` (2 C) of the shutdown temperature but not at it; `state/temp_hottest` is that reading.
- **Local settings:** `heater_cutoff_c` (46-59, the **heater pause**), `manual_target_temp_c` (45-55, the **target**: manual heating and drying tasks), `temp_max_c` (50-60, the **safe shutdown temperature**: the over-temperature limit, at least `TEMP_MAX_GAP_C` = 5 C above the target), `manual_target_humidity` (20-80, stored and reported only: the DHT22 measures humidity at the top but nothing controls it) and `safe_temp_c` are validated on arrival, saved in the local database and loaded on every boot, so they apply offline. Missing or invalid saved values fall back to the `config.py` defaults (target 55, pause 58, shutdown 60). A value saved under the older, wider ranges (target from 30, pause from 30, maximum from 35) that is now below its minimum moves up to the minimum and is logged as `config_migrated`. A saved set that breaks the order keeps the shutdown temperature and lowers the pause and then the target under it (`config_warning`), so a fallback never loosens the limit. **Source of truth:** the Pi's saved copy is what runs, loaded on boot before the cloud thread starts and never cleared by an outage. Every setting is its own request with its own time. From Firebase, `settings/changed_at/{setting}` (server time) says which settings a write actually asked for; a setting without an entry is not a request, so a write for one setting can never undo a newer change to another made on the local panel. A request is applied only if it is newer than the last one seen for that setting; local-panel writes are stamped newer than anything already applied. Without `changed_at` (an older web app) `settings/updated_at` stamps the whole payload and one not newer than the last applied is ignored, as before. The target, pause and shutdown temperature are also checked together, each member that is not in the request counting with the value in use: a request that would break the order (see above) is refused whole, logged as `settings_rejected`, and its time is still recorded so it cannot come back after a restart.

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
| current temp ≥ the task's limit (`max_temp_c`, or the saved safe shutdown temperature if lower) | `recovery_required` | `over_temperature` |
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
| `MANUAL_TARGET_TEMP_C` | 55 | default target until one is saved (`TEMP_MAX_C − TEMP_RESUME_DROP`). A task starts from the saved target (`settings/manual_target_temp_c`, 45–55) and follows it when it changes |
| `TASK_HYSTERESIS_C` | 2 | heater turns back on this far below the target |
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
| `TEMP_MAX_C` | 60 | the absolute limit (firmware ceiling) and the default safe shutdown temperature. The shutdown temperature is `settings/temp_max_c` (`TEMP_MAX_MIN_C` 50 up to this), at least `TEMP_MAX_GAP_C` (5) above the target; it is the software over-temperature fault. Tasks copy it when they start |
| `MANUAL_TARGET_TEMP_MIN_C` / `_MAX_C` | 45 / 55 | the range of the target. `TEMP_MAX_MIN_C` = the minimum + `TEMP_MAX_GAP_C` |
| `HEATER_CUTOFF_C` | 58 | default heater pause. Range `HEATER_CUTOFF_MIN_C` 46 (minimum target + `CUTOFF_TARGET_GAP_C`) to `HEATER_CUTOFF_MAX_C` 59 (`TEMP_MAX_C` − `CUTOFF_SHUTDOWN_GAP_C`); it must sit between the target and the shutdown temperature |
| `CUTOFF_TARGET_GAP_C`, `CUTOFF_SHUTDOWN_GAP_C` | 1, 1 | the pause is at least this far above the target and below the shutdown temperature |
| `SHUTDOWN_WARN_MARGIN_C` | 2 | `state/shutdown_warning` is true from this far below the shutdown temperature until it is reached |
| `ZONE_SENSOR_IDS` | the three sensors' ids | 1-Wire id of the bottom and middle DS18B20, and `DHT22_ID` (`dht22-gpio22`) for the top. **A placeholder, malformed or duplicated id rejects every start (`zones_unconfigured`)**. The middle id still needs confirming by warming that sensor by hand |
| `DHT22_PIN`, `DHT22_BAD_LIMIT`, `DHT22_STALE_SECONDS` | 22, 5, 15 | the DHT22's GPIO, and how many bad reads in a row / how long without a good one before it counts as failed. Longer than a DS18B20's (`SENSOR_BAD_LIMIT` 3, `SENSOR_STALE_SECONDS` 10) because a DHT22 misses the odd read, and a failed top sensor fails a warm-up |
| `ZONE_FLOOR_ALLOWANCE_C` | middle 0, top 0 | extra degrees a zone may sit below the shared band floor and still count as warm. Frozen per task when it starts |
| `TEMP_PLAUSIBLE_MIN_C` / `MAX_C` | −10 / 100 | readings outside this count as failed reads |
| `TEMP_LOG_UPLOAD_SECONDS`, `TEMP_LOG_BATCH` | 60, 500 | per-minute temperature summaries to Firebase |
| `TEMP_HISTORY_INTERVAL_SECONDS` | 30 | the sensors are sampled this often. A sample becomes a record only if it changed (see "Temperature history and offline buffering") |
| `TEMP_HISTORY_MIN_CHANGE_C` | 1.0 | a sample is recorded only if some sensor moved at least this far from its own value in the previous record, the same for bottom, middle and top. `0` records every sample |
| `TEMP_HISTORY_MIN_CHANGE_RH` | 2.0 | the same rule for the humidity (%RH) in the record, with its own limit: about the DHT22's accuracy |
| `TEMP_HISTORY_RETENTION` | 5760 | newest records kept in Firebase. Older ones are deleted only when newer ones are uploaded. Records are written only on change, so this spans far more than 48 h unless the chamber is changing all the time |
| `TEMP_HISTORY_BATCH`, `TEMP_HISTORY_TRIM_CHUNK` | 100, 500 | records per upload request, and old records deleted per request |
| `TEMP_LOCAL_KEEP` | 5760 | delivered records kept in `data/dryer.db` so the local panel has a history |
| `TEMP_BUFFER_MAX` | 100000 | unsent records kept (35 days if every sample were recorded, much longer in practice). Past it the oldest are dropped and a `temp_buffer_overflow` event is logged |
| `TEMP_SYNC_BACKOFF_BASE` / `_MAX`, `TEMP_SYNC_IDLE_SECONDS` | 5 / 120, 10 | retry delay after a failed upload (doubling, with jitter), and how often an idle uploader looks again |
| `INTERNET_PROBE_TARGETS`, `INTERNET_PROBE_TIMEOUT` | three IP:port pairs, 3 | what counts as "the internet is reachable" (TCP connects, no DNS), and the limit per target |
| `INTERNET_PROBE_BACKOFF_BASE` / `_MAX` | 10 / 120 | spacing of probes while something keeps failing |
| `COOLDOWN_SAFE_TEMP_C` | 40 | default cooldown temperature; the web app can change it (`settings/safe_temp_c`, 25–50) |
| `COOLDOWN_MAX_SECONDS` | 3600 | cooling is cut off after this long |
| `COOLDOWN_NO_SENSOR_SECONDS` | 600 | fan run time if the sensor is dead while cooling |
| `CLOUD_COMMAND_MAX_AGE_SECONDS` | 30 | a command that came through Firebase (fan/heater or task) made more than this long ago is ignored; so is one made before the latest local command. Task requests are aged from `issued_at`, falling back to `requested_at`. Rejections are acked as `stale_command` |
| `COMMAND_MAX_AGE_SECONDS` | 600 | the same rule for requests that do not come through Firebase (only checked when the clock is synced) |
| `TEMP_PLAUSIBLE_MIN_C`, `_MAX_C` | -10, 100 | a reading outside this range counts as a failed read, which turns the heater off |
| `MAX_BATCH_COUNT`, `MIN/MAX_DURATION_MIN` | 100, 1–10080 | start validation, same limits as `database.rules.json` |
| `ALLOWED_ROW_LEVELS`, `MAX_WEIGHT_KG` | 1–4, 20 | **placeholders, set them for the machine**. Weight is only checked when sent |

## Temperature sensors (three zones)

| Zone | Sensor | GPIO | Where | Used for |
|---|---|---|---|---|
| `bottom` | 1 | 4 | bottom of the box, near the intake fan and the heater | the **reference** for calibration, and the only sensor the thermostat, the heater interlock, the runtime guard and restart recovery read |
| `middle` | 2 (DS18B20) | 23 | middle of the box | warm-up, over-temperature, cooldown, display |
| `top` | 3 (**DHT22**) | 22 | top of the box | warm-up, over-temperature, cooldown, display, and the only **humidity** reading |

Which sensor decides what:

- **Warm-up:** all three zones (see "Warm-up, then drying"). The heater is still controlled from sensor 1, so the upper zones follow it up the box.
- **Over-temperature** (task fault, manual mode, `heater_block`), the `too_hot` start check, `state/ready` and **cooldown:** the hottest healthy sensor.
- **Dip tracking while drying:** sensor 1 only.
- A sensor that is not healthy (`SENSOR_BAD_LIMIT` bad reads in a row, or nothing valid for `SENSOR_STALE_SECONDS`; `DHT22_BAD_LIMIT` and `DHT22_STALE_SECONDS` for the DHT22) is left out of the "hottest" reading, but a failing **sensor 1** still stops everything, as before.
- **Humidity** (`state/humidity`, and the `humidity` of a `temp_history` record) comes from the DHT22 only and is monitored, not controlled: nothing in the thermostat, the warm-up or the faults reads it. It is read in the same call as the DHT22's temperature, so one bad read voids both and the humidity disappears whenever the top sensor is unhealthy.

**Wiring and set-up.** The two DS18B20 are each on their own 1-Wire bus with their own 4.7 k pull-up to 3V3: add one `dtoverlay=w1-gpio,gpiopin=<pin>` line per pin (4 and 23) to `/boot/firmware/config.txt` and reboot. The bus numbers (`w1_bus_master1`, ...) can swap between boots, so the service goes by **sensor id**, never by bus. Put the real middle id in `ZONE_SENSOR_IDS` in `config.py`:

1. `python sensor2_test.py --expect 3` lists the ids (it also fails loudly if one is missing): sensor 1, the middle DS18B20 and `dht22-gpio22`.
2. Warm the GPIO23 sensor with your hand while it runs: the column that rises is the middle one. Do this once.
3. Until the ids are real, `state/zones_configured` is false and every start is rejected as `zones_unconfigured`; the service logs a `config_warning` event at start-up.

**Calibration.** `python sensor2_test.py --calibrate` (sensors side by side) matches each extra sensor to sensor 1 and saves one offset per sensor id in `data/sensor2_calibration.json`. The service applies those offsets to the middle and top sensors when it starts (listed in the `device_started` event); **restart `squid-dryer.service` after recalibrating**. Sensor 1 is never offset, whatever the file says.

**Top sensor: a DHT22.** The top sensor is a DHT22 on GPIO22 that reads temperature **and** humidity. The service reads it through `hardware.make_zone_sensor` (`ZONE_SENSOR_IDS["top"]` is `DHT22_ID`): its temperature goes through the same `Sensor` and `SensorArray` as the DS18B20s, so warm-up, the hottest-sensor checks, `bypass_zones` and the zone alerts treat it as the top zone, and its humidity is published as `state/humidity` and recorded in `temp_history`. The DHT22 is read at most once per `DHT22_MIN_INTERVAL_S` (2 s). Only one reader can use GPIO22 at a time, so stop `squid-dryer.service` while running `sensor2_test.py` on it (or pass `--no-dht`). `sensor2_test.py` lists the DHT22 as `dht22-gpio22` next to the 1-Wire sensors and counts it in `--expect`. Calibration covers both readings, saved under that id in the same file: temperature is matched to sensor 1 like the other extra sensors, humidity (`offset_rh`) only when `--true-humidity PCT` gives a trusted hygrometer reading, because the Pi has no humidity reference of its own. Calibrating one leaves the other offset as it was, and `--raw` shows both uncalibrated. `--no-dht` leaves the DHT22 out and `--dht-pin` moves it to another GPIO.

**GPIO22 must have no 1-Wire overlay.** A `dtoverlay=w1-gpio,gpiopin=22` line in `/boot/firmware/config.txt` makes the kernel own the pin, and the DHT22 read fails as "busy". Delete that line and reboot, or free the pin until the next boot with `echo onewire@16 | sudo tee /sys/bus/platform/drivers/w1-gpio/unbind`. Wire the DHT22 data pin to GPIO22 with a 10 k pull-up to 3V3 (a 3-pin module already has one), VCC to 3V3 and GND to GND.

**Starting without a sensor.** If the middle or top sensor is not reading, a start is rejected (`zone_sensor_unavailable`) unless the operator sends `bypass_zones` for it, which the web app offers after a warning. The bypassed zone is ignored when judging warm-up. Sensor 1 cannot be bypassed.

**Tuning the zone floors.** The thermostat holds sensor 1 at about the saved target (53 to 55 C at the default 55), and the upper zones lag it, so with `ZONE_FLOOR_ALLOWANCE_C` at 0 a top that never gets within `TARGET_TOLERANCE_C` (2.5 C) of that would sit in warm-up until `warmup_timeout`. Do a cold run and read the `warmup_zone_reached` events and the `zones` in `task/report` (or `temp_log` per zone in `data/dryer.db`). If a zone settles below its floor, raise its allowance by that shortfall plus a little margin, restart the service, and keep in mind that this starts drying while that zone is cooler. A task already running keeps the floors it started with.

## Firebase schema (`devices/squid-dryer/...`)

The schema follows the web app. Security rules are in `database.rules.json`. The Pi uses the Admin SDK, which ignores rules, so the rules only limit what the web app can do: it may write `commands`, `settings` and `task/request`, and nothing else. `settings/safe_temp_c` is the post-task cooldown temperature and `settings/heater_cutoff_c` (the heater pause), `manual_target_temp_c` (the target), `temp_max_c` (the safe shutdown temperature), `manual_target_humidity`, `updated_at` and `changed_at/{setting}` are the temperature limits and the humidity target, with the time each setting was last requested. The machine reports what it applies in `state/safe_temp`, `heater_cutoff`, `heater_cutoff_margin`, `target_temp` (the target), `temp_max` (the safe shutdown temperature), `temp_hottest`, `shutdown_warning`, `shutdown_warn_margin`, `target_humidity` and `settings_updated_at`, plus `manual_heating` (manual heating enabled, not the relay) and `heater_block` (`cutoff`, `over_temperature`, `sensor_fault`, `no_fan`, `reboot_block`, or absent). `state/heater` is always the actual relay. `state/cooling` / `state.status = "cooling"` while cooling.

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
  "bypass_zones": "top",
  "requested_by": "<uid>",
  "requested_at": {".sv": "timestamp"}
}
```

`bypass_zones` (optional, `start` only; `"middle"`, `"top"` or `"middle,top"`) is the operator starting **without** a middle or top sensor that is not reading, after being warned. A start is rejected `zone_sensor_unavailable` when one of those sensors is unhealthy and not listed here. The bypassed zones are ignored when judging warm-up (they are still read and logged if they work) and are recorded on the task. Sensor 1 (bottom) can never be bypassed (`invalid_bypass_zones`), and `zones_unconfigured` cannot be bypassed either.

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
- `network/request`: `{command_id, mode: "hotspot"|"wifi", requested_at, issued_at, requested_by}` asks the Pi to raise its own hotspot (`hotspot`) or to go back to the home Wi-Fi (`wifi`). `requested_at` must be the server timestamp and `issued_at` is the client's server-corrected time. Aged like a manual command (30 s, and not before a local command). Answered once per `command_id` through `network/ack`. See "Hotspot mode on request" below.

**The Pi writes** (the web app only reads these):

- `task/ack`: `{command_id, command, task_id, result: accepted|rejected|duplicate, reason, at}`. The request has been processed once `task/ack.command_id` equals your `command_id`, or `"<task_id>:<command>:<requested_at>"` when you don't send one. Read `requested_at` back from `task/request` to get the resolved server time. Until then it has **not been processed yet**. `reason` explains a rejection, e.g. `too_hot`, `invalid_row_levels`, `unacknowledged_failure`.
- `task/report`: `{task_id, status, seq, boot_id, started_at, drying_started_at, target_confirmed_at, ended_at, warmup_seconds, mode, target_temp_c, tolerance_c, dwell_seconds, warmup_timeout_seconds, smoothed_temp_c, zones, bypass_zones, dips, dip_count, dip_warning, duration_minutes, elapsed_seconds, remaining_seconds, progress_percent, batch_count, row_levels, weight_kg, completed_rows, reason, detail, acknowledged, batches, reported_at}`
  - `smoothed_temp_c` is still the bottom sensor's. `zones` (live session only) is `{bottom|middle|top: {smoothed_temp_c, floor_c, in_band, bypassed}}`: each zone's moving-median reading, its band floor (`target_temp_c − tolerance`, so it moves with the saved target), whether it is in band right now, and `true` when the operator started without it. `bypass_zones` is the comma list that was bypassed (absent when none).
  - `started_at` is the session start (warm-up began). `drying_started_at` is absent while warming up. `elapsed_seconds`, `remaining_seconds` and `progress_percent` count **drying** time only and stay 0 / full / 0 while warming up. Batch `offset_seconds` is measured from the drying start.
  - `dips` lists up to the last 20 excursions below the band, `{start, end, min_temp_c}` (epoch ms, `end` absent while one is open).
- `task_logs/{task_id}/{minute}`: `{t, avg, min, max, raw, n, middle, top}`, one per minute of the session (smoothed average/min/max, last raw reading, sample count). The flat keys are the bottom sensor's; `middle` and `top` are `{avg, min, max, raw, n}` sub-objects, absent for a minute in which that sensor gave no reading. The full-resolution log stays in the Pi's `temp_log` table.
  - `batches` lists the batches added with `add_batch`: `{batch_id, row_level, offset_seconds, duration_minutes, elapsed_seconds, remaining_seconds, progress_percent}`. Each is measured from when it was added. `[]` when none were added.
  - `status` is `starting`, `warming_up`, `running`, `completed`, `cancelled`, `failed`, `recovering` or `recovery_required`.
  - `reason` is set for failures and recovery holds, e.g. `possible_power_loss`, `warmup_timeout`, `invalid_session`, `zone_sensor_failure` (a middle or top sensor stopped reading during warm-up). Start rejections added with the zones: `zones_unconfigured`, `zone_sensor_unavailable`, `invalid_bypass_zones`.
  - `detail` is a short readable summary. The full diagnostics are in `events`.
  - `seq` increases with every change.
- `temp_history/{key}`: `{t, bottom, middle, top, humidity?, gap?}`, the rolling temperature and humidity record behind the dashboard charts. `humidity` is the top (DHT22) sensor's, in %RH, and is absent when that sensor had no valid reading, never `0`. `key` is the collection time in ms, zero-padded to 13 digits, so keys sort by time and are also the record id. `t` is the same time as a number. A sensor with no valid reading is absent from the record. A record exists only when a temperature changed (`TEMP_HISTORY_MIN_CHANGE_C`), so the time between two records says nothing about an outage; `gap: true` is what marks one, see below. Unlike `task_logs` it is written whether or not a task runs, and it is bounded: see "Temperature history and offline buffering".
- `state/temp_pending`: how many recorded readings Firebase has not confirmed yet (0 when caught up).
- `state/status`: `ready`, `starting`, `warming_up`, `running`, `cooling`, `recovering`, `recovery_required` or `failed` (a failure not yet acknowledged). `state/ready` is true when a new task can be started.
- `state/fan`, `heater`, `temp`, `sensor_ok`, `temp_max`, `last_event`, `boot_id`, `pending_events` (events still waiting to upload). `temp` and `sensor_ok` are the **bottom** sensor (sensor 1), as they always were.
- `state/humidity`: relative humidity in %RH from the top (DHT22) sensor, one decimal. Removed while that sensor is unhealthy and on a machine without one; it is never `0` for "unknown". Monitored only: nothing on the machine acts on it.
- `state/temp_middle`, `temp_top` (removed while that sensor is unhealthy), `sensor_middle_ok`, `sensor_top_ok`, and `zones_configured` (false while a sensor id in `config.py` is still a placeholder; every start is then rejected and `ready` is false).
- `network/ack`: `{command_id, mode, result: accepted|rejected, reason, at}`. `accepted` means the request was handed to netwatch, **not** that the radio has switched: the switch starts `NET_SWITCH_DELAY_SECONDS` later, and when the Pi leaves the home Wi-Fi it can no longer report anything. `reason` for a rejection: `stale_command`, `superseded_by_local`, `unverifiable` (the Pi could not prove the request was recent), `already_in_mode`, `netwatch_unavailable`, `write_failed` or `invalid`.
- `state/network`: `wifi`, `hotspot` or `connecting`, as last reported by netwatch (removed when netwatch is not running). While the Pi is on its hotspot it has no route to Firebase, so `hotspot` is only ever seen locally; the cloud sees `last_seen` stop and, later, `wifi` again.
- `state/network_since` (epoch ms, Pi clock), `state/network_reason` (why the machine is in this mode: `boot`, `requested`, `fallback`, `no_client_timeout`, `clients_left`, `switched_back` or `wifi_back`), `state/hotspot_connect_timeout_s`, `state/hotspot_empty_grace_s`.
- `state/last_seen`: server timestamp of the last successful write.
- `events/{0000000001…}`: the local event log in order. Events recorded offline are uploaded after reconnection, and their `at` is the Pi's local time.

**Manual control latency:** a Firebase stream on `commands` and `task/request` wakes the sync thread the moment the web app writes, and the 2 s poll (`SYNC_INTERVAL`) is the backup if the stream is down (`COMMAND_LISTEN`). The control loop applies the newest manual request at once and publishes `state` as soon as fan, heater or status change, instead of waiting for the 5 s timer. The temperature sensor is read on its own thread so a slow read never delays a command. The Pi only ever resets `commands/*` to `false`, so racing an operator write can't switch equipment on. The web app still treats `state` as the only proof of what the hardware did.

**Online/offline:** while connected the Pi writes about every 5 s. There's no "online" flag, because the Pi can't report that it has lost its connection. Treat the machine as offline when `serverNow − last_seen > 90 s`, with `serverNow = Date.now() + .info/serverTimeOffset`. After it comes back:
- If `boot_id` changed and there's a `device_started` event, the machine restarted.
- If only `connection_lost` / `connection_restored` events appear, it was just an internet outage.

To notify the user about a technical failure, watch `task/report`. `status == "failed"` (`reason` says why, e.g. `possible_power_loss` or `sensor_failure`) needs an `acknowledge`; `recovery_required` needs a `resume` or `abort`.

## Temperature history and offline buffering

The dashboard's Temperature Monitoring chart reads `temp_history`. It is separate from `task_logs`, which stays the per-task audit trail (per-minute summaries, written only while a task runs).

**Recording** (`TempRecorder`, its own thread). Every `TEMP_HISTORY_INTERVAL_SECONDS` it takes the latest healthy reading of each of the three sensors (sensor 1 bottom / GPIO4, sensor 2 middle / GPIO23, sensor 3 top, the DHT22 / GPIO22) and the top sensor's humidity, and inserts one row into the `temp_history` table of `data/dryer.db` (WAL, `synchronous=FULL`, so rows survive a crash, a service restart and a reboot) **only if it changed**. A stale or failed sensor is left out of the row, never stored as `0`. Recording never touches the network and runs whether or not a task is active.

**What counts as a change.** Each sensor is compared with its own value in the previous *record* (not the previous sample), and the same limit, `TEMP_HISTORY_MIN_CHANGE_C` (1 C), applies to all three: a move of 1 C or more, up or down, by any one of them records a row holding all three current readings. A drift of 0.9 C does not, but a later reading 1 C from the record does, so slow drift is caught when it adds up. The humidity is judged the same way against its own value in the previous record, with its own limit, `TEMP_HISTORY_MIN_CHANGE_RH` (2 %RH, about the sensor's accuracy), and a record holds the current humidity whichever value made it. A sensor that starts or stops reading is a change however small (a fault has to show), the DHT22's humidity included. The first sample after the service starts is always recorded, and so is the first one after every sensor had failed; both carry `gap: true`, "nothing recorded before this continues here". Nothing else carries it: a sensor dropping out is just a record without that sensor. The web app breaks the line before a `gap` record, holds the last value flat across steady stretches, and applies the same 1 C rule to its live readings, so the chart and the stored history agree. If every sensor fails nothing is written until one reads again (the chart shows a gap, then the `gap` record).

The per-task `task_logs` summaries are not thinned: they are the audit trail of a run (per-minute min, max and average) and keep every minute.

**Uploading** (`TempUploader`, its own thread, separate from the command thread so a backlog cannot delay a command). It sends pending rows oldest first, up to `TEMP_HISTORY_BATCH` per request, as one atomic multi-path `update()`. A row is marked delivered **only after that call returns**. On any error the rows stay pending, and the retry waits `5, 10, 20 ... 120 s` (with jitter). New readings do not shorten that wait, but the main heartbeat getting through again does. Each request is bounded by the Admin SDK's `httpTimeout` (10 s).

**No duplicates.** The record id is the Firebase key, derived once from the collection time and stored with the row. A retry (including after a reply that was lost although Firebase had stored the data) writes the same path with the same value, so it cannot create a second record. Delivered rows are deleted locally only past `TEMP_LOCAL_KEEP` (they stay so the local panel has a history); unsent rows are never pruned, except by the `TEMP_BUFFER_MAX` safety cap, which logs `temp_buffer_overflow`.

**Retention.** Firebase keeps at most `TEMP_HISTORY_RETENTION` records. The uploader remembers which keys exist (one shallow read after start-up, and again after any request whose outcome it could not see), and the request that stores new records also deletes exactly the oldest ones needed to get back to the limit. Two rules: nothing is deleted unless a new record is being stored, so an idle machine never loses history because time passed; and nothing at or after the oldest record still waiting to be sent is ever deleted. If the shallow read fails, uploads carry on and trimming waits.

**The clock.** The Pi has no RTC. A reading taken while `clock_synced` is false has no time yet; it is dated afterwards from its monotonic timestamp, once the clock syncs in the same boot, and only then offered for upload. Readings of a boot whose clock never synced are kept locally but never sent with a guessed time.

**Connectivity.** `link_monitor.py` classifies the link from the outcome of real requests (the heartbeat and the uploads), not from the Wi-Fi interface:

| status | meaning |
|---|---|
| `synced` | Firebase answered the latest request on every channel |
| `firebase_unavailable` | a request failed or timed out, but a TCP connection to a public IP (`INTERNET_PROBE_TARGETS`) opens. `link.dns` says whether the Firebase host name resolves |
| `offline` | a request failed and no probe target answers |
| `unknown` | nothing tried yet, or a failure not probed yet |

Probing only happens while something is failing, with backoff, and never on the control loop. The status is in `/api/snapshot` as `link` (local panel only: a machine that cannot reach Firebase cannot tell the cloud so). The cloud sees `state/temp_pending` instead, and the dashboard infers an outage from `last_seen` going stale.

Look at the buffer on the Pi with `sqlite3 data/dryer.db "SELECT COUNT(*) FROM temp_history WHERE synced=0"`.

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
| `NET_HOTSPOT_CONNECT_TIMEOUT_SECONDS` | 300 | a hotspot an operator asked for gives up and returns to Wi-Fi if no phone has joined within this time |
| `NET_HOTSPOT_EMPTY_GRACE_SECONDS` | 60 | a requested hotspot that a phone has joined returns to Wi-Fi this long after the last phone left |
| `NET_WIFI_MAX_PROFILES` | 5 | saved Wi-Fi networks tried per return to Wi-Fi, the home profile first |
| `NET_SWITCH_DELAY_SECONDS` | 5 | a requested switch starts this long after it was accepted, so the answer can be published first |
| `NET_REQUEST_MAX_AGE_SECONDS` | 60 | netwatch never acts on a request file older than this (guards against replay after a restart) |
| `NET_REQUEST_PATH` | `data/network-request.json` | where the controller hands a request to netwatch (written by `bensdev`, read by root) |

### How it decides

- **Wi-Fi is the default.** The hotspot is a temporary fallback.
- **After power-on**, the Pi waits up to the boot grace period for the router, which may boot slower than the Pi. Meanwhile it asks for the Wi-Fi every 30 s, because NetworkManager gives up on automatic retries after a few failures.
- **Wi-Fi without internet stays Wi-Fi.** Internet is not part of the decision. Use the LAN address.
- **In hotspot mode with nobody connected**, every retry interval the hotspot is switched off for up to 40 s while the home Wi-Fi is tried, then restored if that fails. The Pi does not scan while the hotspot is up (one radio cannot do both).
- **A phone connected to the hotspot is never kicked off.** The count of connected phones is read from `iw`; if it cannot be read, the Pi assumes someone is connected.
- **If the Wi-Fi drops while running**, NetworkManager gets 60 s, then the Pi tries once explicitly, then raises the hotspot and follows the rules above.
- Every transition is logged with a timestamp: `journalctl -u squid-netwatch -f`.

netwatch checks on start that NetworkManager manages `wlan0`, that the home profile exists and that the hotspot password file is valid. If not, it logs why and touches nothing.

### Hotspot mode on request

When the internet is slow or unreliable, an operator can put the machine on its own hotspot on purpose, from the Profile page of the online app (`network/request`, `mode: "hotspot"`) or, to go back, from the local panel (`POST /api/network/request`, PIN session needed). The controller validates and answers the request, then writes it to `NET_REQUEST_PATH`; netwatch, which owns the radio, acts on it. **Nothing in this path touches the relays, the sensor or the drying task**, so a run continues through a switch and through the time spent without internet.

A hotspot that was requested has its own, shorter rules (the automatic fallback hotspot above keeps its own):

- **Nobody joins** within `NET_HOTSPOT_CONNECT_TIMEOUT_SECONDS` → the hotspot is taken down and the Pi returns to Wi-Fi (`network_reason: no_client_timeout`).
- **A phone joins** → the hotspot stays up for as long as any phone is connected (an unreadable count counts as connected).
- **The last phone leaves** → after `NET_HOTSPOT_EMPTY_GRACE_SECONDS` the Pi returns to Wi-Fi (`clients_left`).
- **Returning to Wi-Fi** takes the hotspot down and tries each saved Wi-Fi profile in turn (home profile first, at most `NET_WIFI_MAX_PROFILES`; only networks already saved in NetworkManager, never unknown or open ones). The first that connects wins and cloud control resumes.
- **No saved network in range** → the Pi is not left without a network: the ordinary fallback hotspot comes back and the usual retry (`NET_RETRY_INTERVAL_SECONDS`) keeps looking for a known network.
- **Asking for `wifi`** from the hotspot returns to Wi-Fi straight away, even with a phone connected (`switched_back`).

A request is acted on once: netwatch remembers the last `command_id` it handled (also across a restart) and ignores request files older than `NET_REQUEST_MAX_AGE_SECONDS`. A cloud request that is too old or cannot be aged (no server time yet) is refused with `network/ack` `rejected`, so a request left in Firebase from before an outage can never take the machine off its network.

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
9. **Heater pause and safe shutdown.** Set a target of 45 C, a pause of 46 C and a safe shutdown temperature of 50 C and warm the top sensor by hand: the heater pauses when the top sensor reaches 46 C even though the bottom sensor still asks for heat, and at 50 C the heater goes off with `heater_block: over_temperature`. Unplug the temperature sensor: the heater turns off with `heater_block: sensor_fault`, whatever command is sent from the cloud or the panel.
10. **Reboot persistence.** After `sudo reboot`, `nmcli connection show squid-hotspot` still exists with `connection.autoconnect: no` and band `bg`, and the home profile still has its original name. (On a netplan system the profile is stored under `/etc/netplan/`; netwatch repairs the hotspot profile on every start if it is missing.)
11. **Cloud mode unchanged.** Log in to the online app: manual toggles, starting and cancelling a task, and the dashboard all behave as before.
12. **Hotspot on request, nobody joins.** Start a drying task, then request Hotspot Mode from the online app. The task keeps running, `SquidDryer` appears within about 10 s, and after `NET_HOTSPOT_CONNECT_TIMEOUT_SECONDS` the Pi is back on Wi-Fi with `state/network_reason: no_client_timeout`.
13. **Hotspot on request, phone joins.** Request it again, join `SquidDryer` and open the panel. The hotspot stays up well past the connect timeout. Disconnect the phone: after `NET_HOTSPOT_EMPTY_GRACE_SECONDS` plus the Wi-Fi try the Pi is back on Wi-Fi (`clients_left`).
14. **Switch back from the panel.** Request the hotspot, join it, then use the panel's "Switch back to Wi-Fi". The phone loses the hotspot, the Pi joins Wi-Fi (`switched_back`).
15. **Router off during a requested hotspot.** Switch the router off, request the hotspot and let it time out. The hotspot comes back (`fallback`) instead of the Pi being left without a network, and joins the router when it returns.
