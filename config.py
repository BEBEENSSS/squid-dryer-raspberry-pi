from pathlib import Path

# ---------------- device / firebase ----------------
DEVICE_ID = "squid-dryer"
DATABASE_URL = "https://squid-dryer-default-rtdb.asia-southeast1.firebasedatabase.app/"
KEY_PATH = "/home/bensdev/squid-dryer/secrets/serviceAccountKey.json"
DB_PATH = Path("/home/bensdev/squid-dryer/data/dryer.db")

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

# ---------------- manual mode (commands/fan, commands/heater) ----------------
HEATER_MAX_SECONDS = 600        # manual heater auto-off
OFFLINE_FAILSAFE_SECONDS = 120  # manual heater only: no successful sync this long -> off

# ---------------- drying tasks ----------------
TASK_SETPOINT_C = TEMP_MAX_C - TEMP_RESUME_DROP  # thermostat target during a task
TASK_HYSTERESIS_C = 2.0         # heater back on at setpoint - hysteresis
CHECKPOINT_SECONDS = 10         # how often running-task progress is saved to disk

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
HEARTBEAT_SECONDS = 30          # max time between state writes while online
TEMP_PUBLISH_SECONDS = 5        # how often temp is sent to Firebase
SYNC_BACKOFF_MAX = 30           # retry delay cap while offline
EVENT_BATCH = 50                # events uploaded per write

LOOP_INTERVAL = 0.5             # control loop tick
