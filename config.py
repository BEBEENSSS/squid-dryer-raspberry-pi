import os
from pathlib import Path

# ---------------- device / firebase ----------------
DEVICE_ID = "squid-dryer"
DATABASE_URL = "https://squid-dryer-default-rtdb.asia-southeast1.firebasedatabase.app/"
KEY_PATH = os.environ.get("SQUID_KEY_PATH", "/home/bensdev/squid-dryer/secrets/serviceAccountKey.json")
DB_PATH = Path(os.environ.get("SQUID_DB_PATH", "/home/bensdev/squid-dryer/data/dryer.db"))

# ---------------- hardware ----------------
FAN_PIN = 17
HEATER_PIN = 27

# temperature sensor (DS18B20 on GPIO4 via 1-Wire)
SENSOR_ID = "28-000000492d62"
W1_BASE = Path("/sys/bus/w1/devices")
SENSOR_INTERVAL = 2             # seconds between reads
SENSOR_BAD_LIMIT = 3            # this many bad reads in a row -> sensor failed
SENSOR_STALE_SECONDS = 10       # no good read for this long -> sensor failed
TEMP_MAX_C = 60.0               # heater hard cutoff, CHANGE TO YOUR LIMIT
TEMP_RESUME_DROP = 5.0          # heater can be re-enabled once this far below max

# ---------------- heater runtime guard (heater_guard.py) ----------------
# Tune against a measured cold warm-up: the continuous cap must be longer than the longest real
# uninterrupted heating period, or a slow cold start will trip it.
HEATER_MAX_CONTINUOUS_SECONDS = 45 * 60  # relay on without one off for this long -> latched off
HEATER_RISE_CHECK_SECONDS = 10 * 60      # on this long without HEATER_MIN_RISE_C of warming -> latched off
HEATER_MIN_RISE_C = 2.0

# ---------------- manual mode (commands/fan, commands/heater) ----------------
# commands/heater=true enables manual heating; the Pi then holds the target with hysteresis,
# with or without internet. The saved targets live in local storage (see machine_settings.py).
MANUAL_TARGET_TEMP_C = 47.0     # defaults, used until valid saved values exist
MANUAL_TARGET_TEMP_MIN_C = 30.0  # mirrors database.rules.json
MANUAL_TARGET_TEMP_MAX_C = 55.0
MANUAL_HYSTERESIS_C = 2.0       # manual heater back on at target - hysteresis
MANUAL_TARGET_HUMIDITY = 60.0   # stored and reported only, there is no humidity sensor
MANUAL_TARGET_HUMIDITY_MIN = 20.0
MANUAL_TARGET_HUMIDITY_MAX = 80.0

# heater cut-off (settings/heater_cutoff_c): heater off at or above it, on again `margin` below it.
# Applies to manual mode and tasks. TEMP_MAX_C stays the separate hard fault above it.
HEATER_CUTOFF_C = 58.0          # default, above the task setpoint so tasks behave as before
HEATER_CUTOFF_MIN_C = 30.0      # mirrors database.rules.json
HEATER_CUTOFF_MAX_C = 58.0
HEATER_CUTOFF_MARGIN_C = 3.0    # recovery margin, prevents rapid switching around the limit

# ---------------- drying tasks ----------------
TASK_SETPOINT_C = TEMP_MAX_C - TEMP_RESUME_DROP  # thermostat target during a task
TASK_HYSTERESIS_C = 2.0         # heater back on at setpoint - hysteresis
CHECKPOINT_SECONDS = 10         # how often running-task progress is saved to disk

# ---------------- warm-up before drying ----------------
# A task starts WARMING_UP and only becomes drying (status "running") once the smoothed chamber
# temperature has held the target band for the dwell. Every value here is copied into the task
# when it starts, so editing this file never changes a session that is already running.
# sample_interval and sensor_fail_limit are SENSOR_INTERVAL and SENSOR_BAD_LIMIT, set with the sensor.
MEDIAN_WINDOW = 5               # moving-median window, in samples
# Wider than TASK_HYSTERESIS_C: the thermostat lets the chamber sag by the hysteresis on every
# cycle, so a tighter band would drop out of "reached" during normal regulation.
TARGET_TOLERANCE_C = 2.5        # reached = smoothed temperature >= target - tolerance
DWELL_SECONDS = 60              # ...continuously for this long
DWELL_MAX_GAP_SECONDS = SENSOR_INTERVAL * 2.5  # a missing sample restarts the dwell
WARMUP_TIMEOUT_SECONDS = 3 * 3600  # target not confirmed by then -> FAULT, heater off
DIP_WARNING_LIMIT_SECONDS = 600  # a drying-phase dip below the band this long raises a warning
MAX_TEMP_CUTOFF_C = min(TASK_SETPOINT_C + 10.0, TEMP_MAX_C)  # software over-temperature fault
TEMP_PLAUSIBLE_MIN_C = -10.0    # outside this range a reading counts as a failed read
TEMP_PLAUSIBLE_MAX_C = 100.0
TEMP_LOG_UPLOAD_SECONDS = 60    # per-minute temperature summaries go to Firebase this often
TEMP_LOG_BATCH = 500            # summary minutes per write, so catching up after an outage stays small

# cooling after a task ends: heater off at once, fan runs until the measured temperature is safe
COOLDOWN_SAFE_TEMP_C = 40.0     # default; the web app can change it (settings/safe_temp_c)
COOLDOWN_SAFE_TEMP_MIN_C = 25.0  # mirrors database.rules.json
COOLDOWN_SAFE_TEMP_MAX_C = 50.0  # mirrors database.rules.json
COOLDOWN_MAX_SECONDS = 3600     # a hot room may never reach the threshold, so cooling is capped
COOLDOWN_NO_SENSOR_SECONDS = 600  # fan runtime if the sensor is dead while cooling

# start command validation (placeholder limits, tune for the machine)
MAX_WEIGHT_KG = 20.0            # only checked if the web app sends weight_kg
MAX_BATCH_COUNT = 100           # matches the database rules
ALLOWED_ROW_LEVELS = (1, 2, 3, 4)  # row_levels is a list like "1,3"
MIN_DURATION_MIN = 1
MAX_DURATION_MIN = 7 * 24 * 60  # matches the database rules (10080)
COMMAND_MAX_AGE_SECONDS = 600   # older commands are rejected as stale (needs synced clock)

# ---------------- reboot recovery ----------------
RECOVERY_OBSERVE_SECONDS = 20   # collect readings this long before deciding
RECOVERY_MIN_READS = 3          # valid readings needed for a decision
RECOVERY_CLOCK_WAIT_SECONDS = 120  # after a reboot, wait this long for NTP sync
TEMP_DROP_TOLERANCE_C = 3.0     # drop at or below this counts as normal fluctuation
TEMP_DROP_FAIL_C = 8.0          # drop at or above this -> failed (possible power loss)
RESUME_MAX_GAP_SECONDS = 300    # longer interruptions are never auto-resumed

# ---------------- cloud sync ----------------
SYNC_INTERVAL = 2               # seconds between command polls (backup when the stream is down)
SETTINGS_POLL_SECONDS = 10      # settings change rarely, so they are not read on every poll
COMMAND_LISTEN = True           # stream commands/task request changes to wake the sync at once
MANUAL_COMMAND_MAX_AGE_SECONDS = 30  # a Firebase fan/heater command older than this is never applied
CLOCK_REFRESH_SECONDS = 120     # how often the server time reference is re-read
LOCAL_API_HOST = os.environ.get("SQUID_API_HOST", "0.0.0.0")  # all interfaces: reachable from the LAN
LOCAL_API_PORT = int(os.environ.get("SQUID_API_PORT", "8080"))  # the local panel; also published as state/local_url
WEB_ROOT = Path(os.environ.get("SQUID_WEB_ROOT", "/home/bensdev/squid-dryer/web"))
PIN_PATH = Path(os.environ.get("SQUID_PIN_PATH", "/home/bensdev/squid-dryer/secrets/local_pin"))
HEARTBEAT_SECONDS = 30          # max time between state writes while online
TEMP_PUBLISH_SECONDS = 5        # how often temp is sent to Firebase
SYNC_BACKOFF_MAX = 30           # retry delay cap while offline
EVENT_BATCH = 50                # events uploaded per write

LOOP_INTERVAL = 0.5             # control loop tick
