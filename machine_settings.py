"""Operator settings kept on the Pi. Pure logic: no relays, no network.

The Pi is the authority: values are validated and saved to local storage when they arrive, and
loaded from there on boot, so they apply with or without an internet connection.
"""

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
    "manual_target_humidity": ("manual_target_humidity", "MANUAL_TARGET_HUMIDITY",
                               "MANUAL_TARGET_HUMIDITY_MIN", "MANUAL_TARGET_HUMIDITY_MAX"),
}


class MachineSettings:
    def __init__(self, store, cfg=config):
        self.store = store
        self.cfg = cfg
        self.lock = threading.RLock()
        self.values = {key: self._load(key) for key in FIELDS}
        self.updated_at = self._load_updated_at()

    def _default(self, key):
        return float(getattr(self.cfg, FIELDS[key][1]))

    def valid(self, key, value):
        _, _, lo, hi = FIELDS[key]
        return _number(value) and getattr(self.cfg, lo) <= value <= getattr(self.cfg, hi)

    def _load(self, key):
        try:
            value = float(self.store.get_meta(FIELDS[key][0]))
        except (TypeError, ValueError):
            return self._default(key)
        # a corrupt or out-of-range saved value must never reach the control logic
        return value if self.valid(key, value) else self._default(key)

    def _load_updated_at(self):
        try:
            return int(self.store.get_meta("settings_updated_at"))
        except (TypeError, ValueError):
            return 0

    def get(self, key):
        with self.lock:
            return self.values[key]

    def apply(self, payload):
        """Apply a settings dict from the web app. Returns the keys that changed.

        Each field is validated on its own. A payload carrying an `updated_at` that is not newer
        than the last applied one is ignored, so a stale dashboard write cannot undo newer settings.
        """
        if isinstance(payload, (int, float)) and not isinstance(payload, bool):
            payload = {"safe_temp_c": payload}  # legacy: settings used to be a bare safe temperature
        if not isinstance(payload, dict):
            return []
        with self.lock:
            stamp = payload.get("updated_at")
            if _number(stamp):
                stamp = int(stamp)
                if stamp <= self.updated_at:
                    return []
            else:
                stamp = None

            changed = []
            for key in FIELDS:
                if key not in payload:
                    continue
                value = payload[key]
                if not self.valid(key, value):
                    self.store.log_event("settings_rejected", f"{key} {value!r} out of range",
                                         data={key: value if _number(value) else None})
                    continue
                value = float(value)
                if value == self.values[key]:
                    continue
                self.values[key] = value
                self.store.set_meta(**{FIELDS[key][0]: value})
                self.store.log_event("settings_applied", f"{key} {value:.1f}", data={key: value})
                changed.append(key)
            if stamp is not None:
                self.updated_at = stamp
                self.store.set_meta(settings_updated_at=stamp)
            return changed


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
