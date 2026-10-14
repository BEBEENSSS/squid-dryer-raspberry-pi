# Squid dryer controller (Raspberry Pi)

`dryer_cloud.py` runs the dryer: relays on GPIO17 (fan) and GPIO27 (heater, both active low), and a DS18B20 sensor on 1-Wire.
The Pi is the authority on what the machine is actually doing. Firebase is used for commands and for showing state.
**Once a drying task is accepted, the Pi does not need the internet to finish it.**

| File | Role |
|---|---|
| `dryer_cloud.py` | Entry point: control loop, manual fan/heater mode, signals |
| `task_manager.py` | Drying task state machine, timer, thermostat, safety faults |
| `recovery.py` | Decision after a restart: resume / failed / recovery required |
| `task_store.py` | SQLite record (`data/dryer.db`): tasks, event outbox, processed commands |
| `cloud_sync.py` | Background Firebase thread: command polling, heartbeat, event upload |
| `hardware.py` | Relays and sensor reading (unchanged logic) |
| `config.py` | All tunable values |

Run the tests with `.venv/bin/python -m pytest` (the relays are mocked, so no hardware is touched).

## Task lifecycle

```
ready ──start──▶ starting ──▶ running ──▶ completed ──▶ cooling ──(temp ≤ safe)──▶ ready
                               │  │
                               │  └─ sensor fault / over-temp ─▶ failed ──acknowledge──▶ ready
                               └─ stop ─▶ cancelled

restart while active ─▶ recovering ─┬─▶ running            (auto-resume, checks passed)
                                    ├─▶ failed              (large temperature drop)
                                    └─▶ recovery_required ──resume──▶ running
                                                          └─abort───▶ cancelled
```

- The task row is committed to SQLite as `starting` and then `running` **before** the heater can switch on.
- **Timer:** only time actually spent running counts. Elapsed time comes from the monotonic clock and is saved every `CHECKPOINT_SECONDS`. After a restart the timer continues from the last saved value. It never restarts from zero, and downtime is never counted as drying time. At worst, one checkpoint interval (10 s) of drying gets repeated.
- **Thermostat during a task:** heater on at ≤ `TASK_SETPOINT_C − TASK_HYSTERESIS_C`, off at ≥ `TASK_SETPOINT_C`, fan always on. `TEMP_MAX_C` stays the hard cutoff.
- **Losing the internet never fails a task.** A sensor failure (3 bad reads or 10 s without a valid reading) or reaching `TEMP_MAX_C` turns the heater off and marks the task `failed`. The machine then cools (below).
- **Confirming a batch dry (`complete_batch`):** frees one row. The other rows keep drying and the schedule is unchanged. When the last row is freed the task ends `completed`.
- **Cooling:** whenever a task ends (completed, cancelled, failed) the heater turns off immediately and the fan keeps running while the measured temperature is above the safe shutdown temperature. Once it is at or below, the fan turns off and the status goes back to `ready`. The safe temperature comes from `settings/safe_temp_c` (25–50 °C), is saved in the local database and survives reboots and outages; the default is `COOLDOWN_SAFE_TEMP_C`. Cooling stops after `COOLDOWN_MAX_SECONDS`, or `COOLDOWN_NO_SENSOR_SECONDS` if the sensor is dead. A reboot while hot also starts cooling. A new task can start during cooling once it is below the `too_hot` limit, and manual fan/heater commands are ignored (and reset) while cooling. Everything runs locally, with no internet needed.
- A `failed` task blocks new starts until the web app sends `acknowledge`. Nothing ever restarts heating on its own after a failure.
- **Manual mode** (`commands/fan`, `commands/heater`) works as before, including the 600 s auto-off and the 120 s offline failsafe. It is ignored while a task is active or waiting for recovery.

## Restart and power-interruption detection

On every start the controller compares the saved `boot_id` with the current one (`/proc/sys/kernel/random/boot_id`). That tells a process restart apart from a full reboot. It also reads a `clean_shutdown` flag, which is only set on SIGTERM/SIGINT and never after a crash.
**If no task was active, startup is normal.** A fresh boot is never treated as a failure.

If a task was active, the heater and fan stay off for `RECOVERY_OBSERVE_SECONDS`. During that time the controller collects temperature readings and compares the median of the earliest readings with the last temperature saved before the interruption.

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
| `COOLDOWN_SAFE_TEMP_C` | 40 | default safe shutdown temperature; the web app can change it (`settings/safe_temp_c`, 25–50) |
| `COOLDOWN_MAX_SECONDS` | 3600 | cooling is cut off after this long |
| `COOLDOWN_NO_SENSOR_SECONDS` | 600 | fan run time if the sensor is dead while cooling |
| `COMMAND_MAX_AGE_SECONDS` | 600 | older commands are rejected as `stale_command` (only checked when the clock is synced) |
| `MAX_BATCH_COUNT`, `MIN/MAX_DURATION_MIN` | 100, 1–10080 | start validation, same limits as `database.rules.json` |
| `ALLOWED_ROW_LEVELS`, `MAX_WEIGHT_KG` | 1–4, 20 | **placeholders, set them for the machine**. Weight is only checked when sent |

## Firebase schema (`devices/squid-dryer/...`)

The schema follows the web app. Security rules are in `database.rules.json`. The Pi uses the Admin SDK, which ignores rules, so the rules only limit what the web app can do: it may write `commands`, `settings` and `task/request`, and nothing else. `settings/safe_temp_c` is the safe shutdown temperature; the machine reports the value it applies in `state/safe_temp`, and `state/cooling` / `state.status = "cooling"` while cooling.

**The web app writes** `task/request`, replacing it with each new request:

```json
{
  "command": "start",
  "task_id": "batch-2026-10-13-01",
  "duration_minutes": 240,
  "batch_count": 40,
  "row_levels": "1,3",
  "weight_kg": 2.5,
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
- `add_batch` needs the running `task_id`, a new `batch_id` (`[A-Za-z0-9_-]{1,64}`), `duration_minutes` for that batch alone, and exactly one free `row_levels` entry (`weight_kg` optional). The batch is timed from the moment it is added. The task is extended if the batch would finish after the task's current end, and is never shortened. The relays, the running clock and the batches already drying are untouched. Rejected with `task_not_running`, `task_mismatch`, `row_occupied`, `duplicate_batch`, `invalid_batch_id`, `invalid_row_levels`, `invalid_duration`, `invalid_weight`, `invalid_batch_count`, `not_ready` or `sensor_unavailable`.
- `row_levels` is a comma-separated list of rows. Every row must be in `ALLOWED_ROW_LEVELS`, with no duplicates.
- `weight_kg` is optional.
- `requested_at` must be the server timestamp. Each request is processed **exactly once**, identified by `command_id` if one is sent, otherwise by `task_id + command + requested_at`. Reading the same request again after a reconnect or reboot does nothing.
- `commands/fan` and `commands/heater` work as before (manual mode).

**The Pi writes** (the web app only reads these):

- `task/ack`: `{command_id, command, task_id, result: accepted|rejected|duplicate, reason, at}`. The request has been processed once `task/ack.command_id` equals your `command_id`, or `"<task_id>:<command>:<requested_at>"` when you don't send one. Read `requested_at` back from `task/request` to get the resolved server time. Until then it has **not been processed yet**. `reason` explains a rejection, e.g. `too_hot`, `invalid_row_levels`, `unacknowledged_failure`.
- `task/report`: `{task_id, status, seq, boot_id, started_at, ended_at, duration_minutes, elapsed_seconds, remaining_seconds, progress_percent, batch_count, row_levels, weight_kg, reason, detail, acknowledged, batches, reported_at}`
  - `batches` lists the batches added with `add_batch`: `{batch_id, row_level, offset_seconds, duration_minutes, elapsed_seconds, remaining_seconds, progress_percent}`. Each is measured from when it was added. `[]` when none were added.
  - `status` is `starting`, `running`, `completed`, `cancelled`, `failed`, `recovering` or `recovery_required`.
  - `reason` is set for failures and recovery holds, e.g. `possible_power_loss`.
  - `detail` is a short readable summary. The full diagnostics are in `events`.
  - `seq` increases with every change.
- `state/status`: `ready`, `starting`, `running`, `recovering`, `recovery_required` or `failed` (a failure not yet acknowledged). `state/ready` is true when a new task can be started.
- `state/fan`, `heater`, `temp`, `sensor_ok`, `temp_max`, `last_event`, `boot_id`, `pending_events` (events still waiting to upload)
- `state/last_seen`: server timestamp of the last successful write.
- `events/{0000000001…}`: the local event log in order. Events recorded offline are uploaded after reconnection, and their `at` is the Pi's local time.

**Online/offline:** while connected the Pi writes about every 5 s. There's no "online" flag, because the Pi can't report that it has lost its connection. Treat the machine as offline when `serverNow − last_seen > 90 s`, with `serverNow = Date.now() + .info/serverTimeOffset`. After it comes back:
- If `boot_id` changed and there's a `device_started` event, the machine restarted.
- If only `connection_lost` / `connection_restored` events appear, it was just an internet outage.

To notify the user about a technical failure, watch `task/report`. `status == "failed"` (`reason` says why, e.g. `possible_power_loss` or `sensor_failure`) needs an `acknowledge`; `recovery_required` needs a `resume` or `abort`.
