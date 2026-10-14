"""Simulated chamber for running the controller with no hardware (SIMULATE=1).

Temperature follows the heater relay with realistic-ish inertia, so warm-up, thermostat cycling
and the heater guard can be watched end to end on a PC or on a Pi with the relays unplugged.
"""

import threading
import time


class SimOven:
    def __init__(self, ambient=25.0, heat_c_per_s=0.05, loss_per_s=0.001, mono=time.monotonic):
        self.temp = ambient
        self.ambient = ambient
        self.heat = heat_c_per_s
        self.loss = loss_per_s
        self.heater = None      # set to the SafeHeater once the outputs exist
        self.broken = False     # True behaves like an unplugged sensor
        self._mono = mono
        self._last = mono()
        self._lock = threading.Lock()

    def read(self):
        """Same contract as hardware.read_temp: a temperature in C, or None for a bad read."""
        with self._lock:
            now = self._mono()
            dt, self._last = now - self._last, now
            if self.heater is not None and self.heater.is_active:
                self.temp += self.heat * dt
            self.temp -= self.loss * (self.temp - self.ambient) * dt
            return None if self.broken else round(self.temp, 2)
