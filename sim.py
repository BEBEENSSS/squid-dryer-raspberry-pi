"""Simulated chamber for running the controller with no hardware (SIMULATE=1).

Temperature follows the heater relay with realistic-ish inertia, so warm-up, thermostat cycling
and the heater guard can be watched end to end on a PC or on a Pi with the relays unplugged.
The middle and top sensors follow the bottom one with a lag and a slightly smaller rise over
ambient, so heat visibly climbs the box and warm-up has to wait for the top.
"""

import threading
import time


class SimOven:
    def __init__(self, ambient=25.0, heat_c_per_s=0.05, loss_per_s=0.001, mono=time.monotonic,
                 zone_lag_s=None, zone_gain=None):
        self.temp = ambient     # the bottom sensor, next to the heater
        self.ambient = ambient
        self.heat = heat_c_per_s
        self.loss = loss_per_s
        self.heater = None      # set to the SafeHeater once the outputs exist
        self.broken = False     # True behaves like an unplugged bottom sensor
        self.broken_zones = set()  # zones ("middle", "top") that behave like unplugged sensors
        self.lag = dict(zone_lag_s) if zone_lag_s is not None else {"middle": 60.0, "top": 150.0}
        # how much of the bottom's rise over ambient a zone ends up with: the upper box runs a little cooler
        self.gain = dict(zone_gain) if zone_gain is not None else {"middle": 0.98, "top": 0.95}
        self.zone_temp = {zone: ambient for zone in self.lag}
        self._mono = mono
        self._last = mono()
        self._lock = threading.Lock()

    def _advance(self):
        """Move the chamber on to now. Every sensor read calls this, so time is integrated once."""
        now = self._mono()
        dt, self._last = now - self._last, now
        if self.heater is not None and self.heater.is_active:
            self.temp += self.heat * dt
        self.temp -= self.loss * (self.temp - self.ambient) * dt
        for zone, tau in self.lag.items():
            goal = self.ambient + self.gain.get(zone, 1.0) * (self.temp - self.ambient)
            self.zone_temp[zone] += (goal - self.zone_temp[zone]) * min(1.0, dt / tau)

    def read(self):
        """Same contract as hardware.read_temp: a temperature in C, or None for a bad read."""
        with self._lock:
            self._advance()
            return None if self.broken else round(self.temp, 2)

    def read_zone(self, zone):
        """The same contract for the middle or top sensor."""
        with self._lock:
            self._advance()
            if zone in self.broken_zones:
                return None
            return round(self.zone_temp[zone], 2)
