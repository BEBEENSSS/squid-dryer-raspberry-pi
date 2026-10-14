"""Heater runtime limits that hold no matter which UI or task asked for heat. Pure logic: no relays.

Two faults latch the heater off until an operator clears them:
- `heater_runtime_exceeded`: the relay stayed on, without a single off, for too long.
- `no_temperature_rise`: the relay was on for a while and the chamber did not warm up, which
  points at a detached sensor, a dead element or a stuck relay.
"""

import config

RUNTIME_EXCEEDED = "heater_runtime_exceeded"
NO_TEMPERATURE_RISE = "no_temperature_rise"


class HeaterGuard:
    def __init__(self, cfg=config):
        self.cfg = cfg
        self.latched = None     # reason string while the heater is blocked
        self._on_since = None   # monotonic start of the current uninterrupted ON period
        self._window_start = None
        self._window_temp = None

    def clear(self):
        self.latched = None
        self._reset()

    def _reset(self):
        self._on_since = self._window_start = self._window_temp = None

    def update(self, is_on, temp, now):
        """Call every control tick. Returns the reason when this call latches a new fault."""
        if self.latched is not None:
            return None
        if not is_on:
            self._reset()  # any off ends the continuous period, so thermostat cycling never trips it
            return None
        if self._on_since is None:
            self._on_since = self._window_start = now
            self._window_temp = temp
            return None

        if now - self._on_since >= self.cfg.HEATER_MAX_CONTINUOUS_SECONDS:
            self.latched = RUNTIME_EXCEEDED
            return self.latched

        if temp is None:
            return None  # a missing reading is the sensor fault's job, not a lack of warming
        if self._window_temp is None or temp - self._window_temp >= self.cfg.HEATER_MIN_RISE_C:
            self._window_start, self._window_temp = now, temp  # warming: restart the window
        elif now - self._window_start >= self.cfg.HEATER_RISE_CHECK_SECONDS:
            self.latched = NO_TEMPERATURE_RISE
            return self.latched
        return None
