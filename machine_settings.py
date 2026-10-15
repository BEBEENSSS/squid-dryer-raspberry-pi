"""Operator settings kept on the Pi. Pure logic: no relays, no network.

The Pi is the authority: values are validated and saved to local storage when they arrive, and
loaded from there on boot, so they apply with or without an internet connection.

Each setting is its own request with its own time (see `MachineSettings.apply`): the newest request
for a setting wins, whichever panel made it, and a setting a writer did not touch is never a request.
"""

import json
import threading

import config


def _number(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and v == v and abs(v) != float("inf")


# setting key -> (meta key, config default name, config min name, config max name)
FIELDS = {
    "safe_temp_c": ("safe_temp_c", "COOLDOWN_SAFE_TEMP_C", "COOLDOWN_SAFE_TEMP_MIN_C", "COOLDOWN_SAFE_TEMP_MAX_C"),
    "heater_cutoff_c": ("heater_cutoff_c", "HEATER_CUTOFF_C", "HEATER_CUTOFF_MIN_C", "HEATER_CUTOFF_MAX_C"),
    "manual_target_temp_c": ("manual_target_temp_c", "MANUAL_TARGET_TEMP_C",
                             "MANUAL_TARGET_TEMP_MIN_C", "MANUAL_TARGET_TEMP_MAX_C"),
    "temp_max_c": ("temp_max_c", "TEMP_MAX_C", "TEMP_MAX_MIN_C", "TEMP_MAX_MAX_C"),
    "manual_target_humidity": ("manual_target_humidity", "MANUAL_TARGET_HUMIDITY",
                               "MANUAL_TARGET_HUMIDITY_MIN", "MANUAL_TARGET_HUMIDITY_MAX"),
}

# The target is one setting for manual heating and for drying tasks (the key keeps its old name so saved
# values and the database rules carry over). The maximum is the over-temperature limit, never a target.
TARGET_KEY = "manual_target_temp_c"
MAX_KEY = "temp_max_c"


def gap_ok(target, maximum, cfg=config):
    """The maximum has to sit at least TEMP_MAX_GAP_C above the target, or the thermostat's normal
    overshoot would trip the over-temperature fault."""
    return maximum - target >= float(cfg.TEMP_MAX_GAP_C) - 1e-9


class MachineSettings:
    def __init__(self, store, cfg=config):
        self.store = store
        self.cfg = cfg
        self.lock = threading.RLock()
        self.values = {key: self._load(key) for key in FIELDS}
        self._settle_pair()
        self.updated_at = self._load_updated_at()
        self.stamps = self._load_stamps()  # per setting: the time of the newest request seen for it

    def _default(self, key):
        return float(getattr(self.cfg, FIELDS[key][1]))

    def valid(self, key, value):
        _, _, lo, hi = FIELDS[key]
        return _number(value) and getattr(self.cfg, lo) <= value <= getattr(self.cfg, hi)

    def _load(self, key):
        raw = self.store.get_meta(FIELDS[key][0])
        try:
            value = float(raw)
        except (TypeError, ValueError):
            value = None
        # a corrupt or out-of-range saved value must never reach the control logic
        if value is not None and self.valid(key, value):
            return value
        if raw is not None:
            self.store.log_event("config_warning", f"saved {key} {raw!r} is invalid, using the default")
        return self._default(key)

    def _settle_pair(self):
        """A saved pair that breaks the gap (corrupt or edited by hand) keeps the maximum, which is the
        protective limit, and lowers the target. The fallback never loosens the limit."""
        target, maximum = self.values[TARGET_KEY], self.values[MAX_KEY]
        if gap_ok(target, maximum, self.cfg):
            return
        lowered = maximum - float(self.cfg.TEMP_MAX_GAP_C)
        if self.valid(TARGET_KEY, lowered):
            self.values[TARGET_KEY] = lowered
        else:
            self.values[TARGET_KEY], self.values[MAX_KEY] = self._default(TARGET_KEY), self._default(MAX_KEY)
        self.store.log_event(
            "config_warning",
            f"saved target {target:.1f} C and maximum {maximum:.1f} C break the gap, "
            f"using {self.values[TARGET_KEY]:.1f} / {self.values[MAX_KEY]:.1f} C")

    def _load_updated_at(self):
        try:
            return int(self.store.get_meta("settings_updated_at"))
        except (TypeError, ValueError):
            return 0

    def _load_stamps(self):
        """Settings saved before per-setting times existed take the one old time they all shared."""
        try:
            saved = json.loads(self.store.get_meta("settings_stamps") or "{}")
        except ValueError:
            saved = {}
        if not isinstance(saved, dict):
            saved = {}
        return {key: int(saved[key]) if _number(saved.get(key)) else self.updated_at for key in FIELDS}

    def _reject(self, key, value, why):
        self.store.log_event("settings_rejected", f"{key} {value!r} {why}",
                             data={key: value if _number(value) else None})

    def get(self, key):
        with self.lock:
            return self.values[key]

    def apply(self, payload):
        """Apply a settings dict from the web app or the local panel. Returns the keys that changed.

        Every setting is its own request with its own time, so a stale write can never undo a newer
        change to the same setting, and a setting the writer did not touch is never a request:
          * From Firebase, `changed_at` holds the server time each field was last written. A field with
            no entry is skipped, unless this Pi has never had a time for it (a fresh database taking its
            first settings from the cloud).
          * Without `changed_at` (the local panel, or an older web app) `updated_at` is the time of every
            field in the payload, and a payload not newer than the last applied one is ignored.
        Each field is validated on its own, then the target and maximum together (the gap). A refused
        request is logged, its time is still recorded, and the old value stays, so a restart cannot
        revive it later.
        """
        if isinstance(payload, (int, float)) and not isinstance(payload, bool):
            payload = {"safe_temp_c": payload}  # legacy: settings used to be a bare safe temperature
        if not isinstance(payload, dict):
            return []
        with self.lock:
            stamp = payload.get("updated_at")
            stamp = int(stamp) if _number(stamp) else None
            marks = payload.get("changed_at")

            due = {}  # setting -> time of its request (None: unstamped, applied as given)
            if isinstance(marks, dict):
                for key in FIELDS:
                    if key not in payload:
                        continue
                    mark = marks.get(key)
                    if not _number(mark):
                        mark = stamp if not self.stamps[key] else None
                    if mark is not None and int(mark) > self.stamps[key]:
                        due[key] = int(mark)
            else:
                if stamp is not None and stamp <= self.updated_at:
                    return []
                due = {key: stamp for key in FIELDS if key in payload}

            new = {}
            for key in due:
                value = payload[key]
                if self.valid(key, value):
                    new[key] = float(value)
                else:
                    self._reject(key, value, "out of range")
            changed = {key: value for key, value in new.items() if value != self.values[key]}

            pair = {key: value for key, value in changed.items() if key in (TARGET_KEY, MAX_KEY)}
            if pair and not gap_ok(pair.get(TARGET_KEY, self.values[TARGET_KEY]),
                                   pair.get(MAX_KEY, self.values[MAX_KEY]), self.cfg):
                for key, value in pair.items():
                    self._reject(key, value, f"breaks the gap: maximum must be at least "
                                             f"{self.cfg.TEMP_MAX_GAP_C:g} C above the target")
                    del changed[key]

            meta = {}
            for key, value in changed.items():
                self.values[key] = value
                meta[FIELDS[key][0]] = value
            for key, mark in due.items():
                if mark is not None:
                    self.stamps[key] = max(self.stamps[key], mark)
                    meta["settings_stamps"] = json.dumps(self.stamps)
            if stamp is not None:
                self.updated_at = max(self.updated_at, stamp)
                meta["settings_updated_at"] = self.updated_at
            if meta:
                self.store.set_meta(**meta)  # one transaction: a value never lands without its time
            for key, value in changed.items():
                self.store.log_event("settings_applied", f"{key} {value:.1f}", data={key: value})
            return list(changed)


class HeaterCutoff:
    """Heater cut-off with hysteresis: latches at the limit, releases `margin` degrees below it.

    This only pauses heating. It never ends a task or touches the fan; over-temperature and sensor
    faults are separate protections that stay in the callers.
    """

    def __init__(self, store, margin):
        self.store = store
        self.margin = margin
        self.active = False

    def update(self, temp, limit):
        """Feed a valid reading. Returns True while the heater must stay off."""
        if temp is None:
            return self.active  # no reading: keep the last decision, callers handle sensor faults
        if not self.active and temp >= limit:
            self.active = True
            self.store.log_event("heater_cutoff", f"heater cut off at {temp:.1f} C (limit {limit:.1f} C)",
                                 data={"temp_c": temp, "limit_c": limit})
        elif self.active and temp <= limit - self.margin:
            self.active = False
            self.store.log_event("heater_cutoff_cleared", f"heater allowed again at {temp:.1f} C",
                                 data={"temp_c": temp, "limit_c": limit, "margin_c": self.margin})
        return self.active
