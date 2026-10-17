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
TEMP_MAX_C = 60.0               # the absolute limit: firmware ceiling for the safe shutdown, and its default
TEMP_RESUME_DROP = 5.0          # heater can be re-enabled once this far below the safe shutdown temperature
# Temperature limits, lowest to highest. Each has one job and the order is enforced everywhere
# (machine_settings.limits_error, the web app and database.rules.json use the same numbers):
#   target (manual_target_temp_c)  the temperature the thermostat holds
#   heater pause (heater_cutoff_c) heater paused at/above it on the HOTTEST sensor, resumes by itself, task goes on
#   safe shutdown (temp_max_c)     hottest sensor at/above it: heater off, task failed, manual heating off, alert
#   TEMP_MAX_C                     absolute limit, never an operator setting
# The safe shutdown temperature is saved on the Pi (settings/temp_max_c). It can go lower than TEMP_MAX_C,
# never higher, and must stay at least TEMP_MAX_GAP_C above the target so normal overshoot cannot trip it.
MANUAL_TARGET_TEMP_MIN_C = 45.0  # mirrors database.rules.json
MANUAL_TARGET_TEMP_MAX_C = 55.0  # the highest target there is; the safe shutdown temperature is always above it
TEMP_MAX_GAP_C = TEMP_RESUME_DROP
TEMP_MAX_MAX_C = TEMP_MAX_C
TEMP_MAX_MIN_C = MANUAL_TARGET_TEMP_MIN_C + TEMP_MAX_GAP_C   # 50; mirrors database.rules.json
# The pause sits strictly between the target and the safe shutdown temperature, so it can act before the shutdown.
CUTOFF_TARGET_GAP_C = 1.0       # pause at least this far above the target
CUTOFF_SHUTDOWN_GAP_C = 1.0     # ...and at least this far below the safe shutdown temperature
# "Approaching safe shutdown" is reported when the hottest sensor is within this of the safe shutdown temperature.
SHUTDOWN_WARN_MARGIN_C = 2.0

# ---------------- chamber zones: one DS18B20 per 1-Wire bus ----------------
# bottom = sensor 1 (GPIO4, near the heater and fan), middle = sensor 2 (GPIO22), top = sensor 3 (GPIO23).
# Sensor 1 alone holds the heater thermostat and the heater interlock, so it can never be bypassed and
# never gets a calibration offset (it is the reference the others are matched to). The other two widen what
# counts as "warm" (every zone has to be) and as "hot" (the hottest healthy one decides).
ZONES = ("bottom", "middle", "top")  # bottom to top
CONTROL_ZONE = "bottom"
ZONE_SENSOR_IDS = {
    "bottom": SENSOR_ID,
    # Ids as listed by `python sensor2_test.py --expect 3`. The middle/top assignment follows the 1-Wire bus
    # order (master1 = GPIO22, master2 = GPIO23) and has NOT been confirmed by warming one sensor by hand: if
    # the dashboard's Middle row reacts when you warm the TOP sensor, swap these two lines and restart.
    # (A placeholder or malformed id makes every task start be rejected, zones_unconfigured.)
    "middle": "28-0624c12820aa",
    "top": "28-0624c1511aba",
}
ZONE_ID_PATTERN = r"^28-[0-9a-f]{12}$"

# ---------------- top sensor: DHT22 on GPIO22 (temperature + humidity) ----------------
# Being tried out in sensor2_test.py only (dht22.py, calibration.py). The service does not read it yet: it still
# goes by ZONE_SENSOR_IDS above, so nothing here changes what the dryer does. The DHT22 has no 1-Wire id; its
# calibration entry is keyed by DHT22_ID instead.
# GPIO22 must not also carry a 1-Wire bus: remove dtoverlay=w1-gpio,gpiopin=22 from /boot/firmware/config.txt
# (the kernel driver owns the pin and a DHT22 read gets "device or resource busy").
DHT22_PIN = 22
DHT22_ID = f"dht22-gpio{DHT22_PIN}"
GPIO_CHIP_PATH = "/dev/gpiochip0"  # the header pins on a Pi 4
DHT22_MIN_INTERVAL_S = 2.0         # the sensor measures once per 2 s; asking sooner returns the previous reading
DHT22_START_LOW_S = 0.002          # host start signal: data line held low (spec: at least 1 ms, at most 20 ms)
DHT22_BIT_THRESHOLD_US = 50        # a data bit's high pulse is ~27 us for 0 and ~70 us for 1
HUMIDITY_PLAUSIBLE_MIN = 0.0
HUMIDITY_PLAUSIBLE_MAX = 100.0
# Degrees a zone may sit below the shared band floor (target - TARGET_TOLERANCE_C) and still count as warm. The
# thermostat sits on the bottom sensor, so the upper zones may lag it; raise these from measured runs
# (the warmup_zone_reached events and the per-zone temperature log show the real gradient).
ZONE_FLOOR_ALLOWANCE_C = {"middle": 0.0, "top": 0.0}

# ---------------- heater runtime guard (heater_guard.py) ----------------
# Tune against a measured cold warm-up: the continuous cap must be longer than the longest real
# uninterrupted heating period, or a slow cold start will trip it.
HEATER_MAX_CONTINUOUS_SECONDS = 45 * 60  # relay on without one off for this long -> latched off
HEATER_RISE_CHECK_SECONDS = 10 * 60      # on this long without HEATER_MIN_RISE_C of warming -> latched off
HEATER_MIN_RISE_C = 2.0

# ---------------- manual mode (commands/fan, commands/heater) ----------------
# commands/heater=true enables manual heating; the Pi then holds the target with hysteresis,
# with or without internet. The saved targets live in local storage (see machine_settings.py).
# The target is one setting for manual heating and for drying tasks. Until a valid saved value exists it is
# 5 C below the default maximum, which is where tasks always ran before the target was configurable.
MANUAL_TARGET_TEMP_C = TEMP_MAX_C - TEMP_RESUME_DROP  # defaults, used until valid saved values exist
MANUAL_HYSTERESIS_C = 2.0       # manual heater back on at target - hysteresis
MANUAL_TARGET_HUMIDITY = 60.0   # stored and reported only, there is no humidity sensor
MANUAL_TARGET_HUMIDITY_MIN = 20.0
MANUAL_TARGET_HUMIDITY_MAX = 80.0

# heater pause (settings/heater_cutoff_c, the key keeps its old name): heater off while the HOTTEST healthy sensor is at
# or above it, on again `margin` below it. Applies to manual mode and tasks. Unlike the thermostat, which reads the
# bottom sensor only, it sees a hot spot elsewhere in the chamber. The safe shutdown temperature stays the separate
# hard stop above it.
HEATER_CUTOFF_C = 58.0          # default: above the default target, below the default safe shutdown temperature
HEATER_CUTOFF_MIN_C = MANUAL_TARGET_TEMP_MIN_C + CUTOFF_TARGET_GAP_C    # 46; mirrors database.rules.json
HEATER_CUTOFF_MAX_C = TEMP_MAX_MAX_C - CUTOFF_SHUTDOWN_GAP_C            # 59
HEATER_CUTOFF_MARGIN_C = 3.0    # recovery margin, prevents rapid switching around the limit

# ---------------- drying tasks ----------------
# A task's target and maximum are the saved settings (machine_settings.py), copied into the task when it starts.
TASK_HYSTERESIS_C = 2.0         # heater back on at target - hysteresis
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
CLOUD_COMMAND_MAX_AGE_SECONDS = 30  # a Firebase command (manual or task) older than this is never applied
CLOCK_REFRESH_SECONDS = 120     # how often the server time reference is re-read
LOCAL_API_HOST = os.environ.get("SQUID_API_HOST", "0.0.0.0")  # all interfaces: reachable from the LAN
LOCAL_API_PORT = int(os.environ.get("SQUID_API_PORT", "8080"))  # the local panel; also published as state/local_url
WEB_ROOT = Path(os.environ.get("SQUID_WEB_ROOT", "/home/bensdev/squid-dryer/web"))
PIN_PATH = Path(os.environ.get("SQUID_PIN_PATH", "/home/bensdev/squid-dryer/secrets/local_pin"))
HEARTBEAT_SECONDS = 30          # max time between state writes while online
TEMP_PUBLISH_SECONDS = 5        # how often temp is sent to Firebase
SYNC_BACKOFF_MAX = 30           # retry delay cap while offline
EVENT_BATCH = 50                # events uploaded per write

# ---------------- temperature history (temp_sync.py) ----------------
# A rolling chart history, separate from the per-task audit log: the Pi samples every sensor on an interval
# whether or not a task runs, but only records a sample when it differs from the previous record (below). It
# keeps unsent records in SQLite and uploads them under temp_history/{collection time in ms}. The key is the
# record id, so a retry overwrites instead of duplicating.
TEMP_HISTORY_INTERVAL_SECONDS = 30  # how often the sensors are sampled, not how often a record is written
# A sample is recorded only if some sensor moved at least this far from its own value in the previous record
# (or a sensor started or stopped reading). The same limit applies to bottom, middle and top. 0 records every sample.
TEMP_HISTORY_MIN_CHANGE_C = 1.0
# Newest records kept in Firebase, trimmed only when new ones arrive. Records are written only on change, so this
# spans far more than 48 h unless the chamber is changing all the time.
TEMP_HISTORY_RETENTION = 5760
TEMP_HISTORY_BATCH = 100        # records per upload request
TEMP_HISTORY_TRIM_CHUNK = 500   # most old records deleted in one request
TEMP_LOCAL_KEEP = 5760          # uploaded records kept locally so the local panel has a history
TEMP_BUFFER_MAX = 100_000       # unsent records kept (35 days if every sample were recorded, much longer in practice); beyond it the oldest go, with an event
TEMP_SYNC_IDLE_SECONDS = 10     # how often an idle uploader looks for work it was not told about
TEMP_SYNC_BACKOFF_BASE = 5      # seconds before the first retry after a failed upload, doubling ...
TEMP_SYNC_BACKOFF_MAX = 120     # ... up to this

# ---------------- connectivity classification (link_monitor.py) ----------------
# Reaching one of these over TCP proves the Pi has internet, whatever Firebase is doing. They are IP literals
# on purpose: a DNS outage must not look like "no internet".
INTERNET_PROBE_TARGETS = (("1.1.1.1", 443), ("8.8.8.8", 53), ("9.9.9.9", 443))
INTERNET_PROBE_TIMEOUT = 3      # seconds per target
INTERNET_PROBE_BACKOFF_BASE = 10  # seconds between probes while Firebase keeps failing, doubling ...
INTERNET_PROBE_BACKOFF_MAX = 120  # ... up to this

LOOP_INTERVAL = 0.5             # control loop tick

# ---------------- network fallback (netwatch.py) ----------------
# Wi-Fi first, own hotspot only when the home router cannot be reached. Every value can be
# overridden by an environment variable of the same name with a SQUID_ prefix.
def _env(name, default, cast=str):
    value = os.environ.get(f"SQUID_{name}")
    return default if value is None else cast(value)


NET_IFACE = _env("NET_IFACE", "wlan0")
# The NetworkManager profile of the home Wi-Fi, exactly as `nmcli con show` prints it (spaces are fine).
NET_WIFI_CONNECTION = _env("NET_WIFI_CONNECTION", "netplan-wlan0-ANN-ANN 5G")
NET_HOTSPOT_CONNECTION = _env("NET_HOTSPOT_CONNECTION", "squid-hotspot")
NET_HOTSPOT_SSID = _env("NET_HOTSPOT_SSID", "SquidDryer")
# WPA2 password, kept out of git: written by `python netwatch.py --setup`, 8 to 63 characters.
NET_HOTSPOT_PSK_PATH = Path(_env("NET_HOTSPOT_PSK_PATH", "/home/bensdev/squid-dryer/secrets/hotspot_psk"))
NET_HOTSPOT_BAND = _env("NET_HOTSPOT_BAND", "bg")             # 2.4 GHz: best range, every phone can join
NET_HOTSPOT_CHANNEL = _env("NET_HOTSPOT_CHANNEL", 6, int)
NET_HOTSPOT_ADDRESS = _env("NET_HOTSPOT_ADDRESS", "10.42.0.1/24")  # fixed address in hotspot mode
NET_BOOT_GRACE_SECONDS = _env("NET_BOOT_GRACE_SECONDS", 180, float)  # wait this long after power-on for the router
NET_RETRY_INTERVAL_SECONDS = _env("NET_RETRY_INTERVAL_SECONDS", 120, float)  # empty hotspot: retry Wi-Fi this often
NET_WIFI_TRY_TIMEOUT_SECONDS = _env("NET_WIFI_TRY_TIMEOUT_SECONDS", 40, float)
NET_WIFI_DROP_GRACE_SECONDS = _env("NET_WIFI_DROP_GRACE_SECONDS", 60, float)  # a lost link may come back by itself
NET_POLL_SECONDS = _env("NET_POLL_SECONDS", 5, float)
NET_ASSIST_SECONDS = _env("NET_ASSIST_SECONDS", 30, float)  # while waiting for the router, ask for Wi-Fi this often
NET_ASSIST_TIMEOUT_SECONDS = _env("NET_ASSIST_TIMEOUT_SECONDS", 20, float)
# A hotspot an operator asked for from the web app or the local panel (see net_policy.request_hotspot).
NET_HOTSPOT_CONNECT_TIMEOUT_SECONDS = _env("NET_HOTSPOT_CONNECT_TIMEOUT_SECONDS", 300, float)  # nobody joined: back to Wi-Fi
NET_HOTSPOT_EMPTY_GRACE_SECONDS = _env("NET_HOTSPOT_EMPTY_GRACE_SECONDS", 60, float)  # last phone left: back to Wi-Fi
NET_WIFI_MAX_PROFILES = _env("NET_WIFI_MAX_PROFILES", 5, int)  # saved networks tried per probe, home profile first
NET_SWITCH_DELAY_SECONDS = _env("NET_SWITCH_DELAY_SECONDS", 5, float)  # lets the ack reach Firebase before the radio switches
NET_REQUEST_MAX_AGE_SECONDS = _env("NET_REQUEST_MAX_AGE_SECONDS", 60, float)  # an older request file is never acted on
# Written by the controller (bensdev), read by netwatch (root); not under /run/squid-dryer, which root owns.
NET_REQUEST_PATH = Path(_env("NET_REQUEST_PATH", "/home/bensdev/squid-dryer/data/network-request.json"))
NET_STATUS_PATH = Path(_env("NET_STATUS_PATH", "/run/squid-dryer/network.json"))
NET_STATUS_MAX_AGE_SECONDS = 30  # an older status file means netwatch is not running
