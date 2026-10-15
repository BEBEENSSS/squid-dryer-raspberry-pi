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

# Three temperatures, lowest to highest (the keys keep their old names so saved values and the database rules carry
# over). The target is one setting for manual heating and for drying tasks. The heater pause only pauses the heater.
# The maximum is the safe shutdown temperature, the hard stop, and is never a target.
TARGET_KEY = "manual_target_temp_c"
CUTOFF_KEY = "heater_cutoff_c"
MAX_KEY = "temp_max_c"
LIMIT_KEYS = (TARGET_KEY, CUTOFF_KEY, MAX_KEY)


def limits_error(target, cutoff, maximum, cfg=config):
    """Why target < heater pause < safe shutdown does not hold, or None when it does.

    Any argument may be None (a value the caller does not have): the checks that need it are skipped.
    The safe shutdown temperature must sit TEMP_MAX_GAP_C above the target or the thermostat's normal overshoot
    would trip it; the pause has to sit between the two, or it could never act before the shutdown (or, below
    the target, it would quietly act as a second, lower target)."""
    def too_close(low, high, gap):
        return low is not None and high is not None and high - low < float(gap) - 1e-9

    if too_close(target, maximum, cfg.TEMP_MAX_GAP_C):
        return f"the safe shutdown temperature must be at least {cfg.TEMP_MAX_GAP_C:g} C above the target"
    if too_close(target, cutoff, cfg.CUTOFF_TARGET_GAP_C):
        return f"the heater pause temperature must be at least {cfg.CUTOFF_TARGET_GAP_C:g} C above the target"
    if too_close(cutoff, maximum, cfg.CUTOFF_SHUTDOWN_GAP_C):
        return (f"the heater pause temperature must be at least {cfg.CUTOFF_SHUTDOWN_GAP_C:g} C below "
                f"the safe shutdown temperature")
    return None


def shutdown_warning(hottest, maximum, margin):
    """True while the hottest healthy sensor is within `margin` of the safe shutdown temperature but not at it.

    At or above it the shutdown has happened (heater_block says over_temperature), so that is not a warning.
    This is the machine's verdict, published in state/shutdown_warning, so every panel shows the same one."""
    return hottest is not None and maximum - margin <= hottest < maximum


class MachineSettings:
    def __init__(self, store, cfg=config):
        self.store = store
        self.cfg = cfg
        self.lock = threading.RLock()
        self.values = {key: self._load(key) for key in FIELDS}
        self._settle_limits()
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
        # A temperature limit saved when the ranges were wider (the target used to go down to 30) and now sits
        # below the lowest allowed value moves up to it; the default could be far hotter than what was saved.
        if value is not None and key in LIMIT_KEYS and _number(value) and value < getattr(self.cfg, FIELDS[key][2]):
            moved = float(getattr(self.cfg, FIELDS[key][2]))
            self.store.log_event("config_migrated", f"saved {key} {raw!r} is below the allowed range, using {moved:.1f}",
                                 data={key: moved})
            return moved
        if raw is not None:
            self.store.log_event("config_warning", f"saved {key} {raw!r} is invalid, using the default")
        return self._default(key)

    def _settle_limits(self):
        """Saved temperatures that break the order (corrupt, edited by hand, or from before the pause had to sit
        between the target and the shutdown) keep the safe shutdown temperature, which is the protective limit,
        and lower the pause and then the target under it. The fallback never loosens the limit."""
        target, cutoff, maximum = (self.values[key] for key in LIMIT_KEYS)
        if limits_error(target, cutoff, maximum, self.cfg) is None:
            return
        cfg = self.cfg
        new_cutoff = min(cutoff, maximum - float(cfg.CUTOFF_SHUTDOWN_GAP_C))
        new_target = min(target, maximum - float(cfg.TEMP_MAX_GAP_C), new_cutoff - float(cfg.CUTOFF_TARGET_GAP_C))
        settled = {TARGET_KEY: new_target, CUTOFF_KEY: new_cutoff, MAX_KEY: maximum}
        if all(self.valid(key, value) for key, value in settled.items()) \
                and limits_error(new_target, new_cutoff, maximum, cfg) is None:
            self.values.update(settled)
        else:
            self.values.update({key: self._default(key) for key in LIMIT_KEYS})
        self.store.log_event(
            "config_warning",
            f"saved target {target:.1f} C, heater pause {cutoff:.1f} C and safe shutdown {maximum:.1f} C break "
            f"the order, using {self.values[TARGET_KEY]:.1f} / {self.values[CUTOFF_KEY]:.1f} / "
            f"{self.values[MAX_KEY]:.1f} C")

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
        Each field is validated on its own, then the target, heater pause and safe shutdown temperature together
        (`limits_error`; a member that is not in the payload counts with the value in use). A refused
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

            limits = {key: value for key, value in changed.items() if key in LIMIT_KEYS}
            if limits:
                trial = {key: limits.get(key, self.values[key]) for key in LIMIT_KEYS}
                why = limits_error(trial[TARGET_KEY], trial[CUTOFF_KEY], trial[MAX_KEY], self.cfg)
                if why:
                    for key, value in limits.items():
                        self._reject(key, value, f"breaks the temperature order: {why}")
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
