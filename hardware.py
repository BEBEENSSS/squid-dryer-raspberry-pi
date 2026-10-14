import threading
import time
from pathlib import Path

from gpiozero import OutputDevice

import config
from heater_guard import HeaterGuard


def read_temp(sensor_dir=None):
    """Return temperature in C, or None if the read is bad."""
    sensor_dir = sensor_dir or (config.W1_BASE / config.SENSOR_ID)
    try:
        lines = (Path(sensor_dir) / "w1_slave").read_text().strip().splitlines()
    except OSError:
        return None

    # line 1 must end with YES (CRC ok)
    if len(lines) < 2 or not lines[0].endswith("YES") or "t=" not in lines[1]:
        return None

    try:
        value = int(lines[1].split("t=")[1]) / 1000.0
    except ValueError:
        return None

    # -127 = disconnected, 85 = power-on default (not a real reading)
    if value == -127.0 or value == 85.0:
        return None

    return value


class Sensor:
    """Tracks the last valid reading and whether the sensor counts as healthy."""

    def __init__(self, read_fn=read_temp, clock=time.monotonic):
        self._read = read_fn
        self._clock = clock
        self._lock = threading.Lock()
        self.temp_c = None      # last valid temperature
        self.temp_time = 0.0    # monotonic time of last valid read
        self.bad_reads = 0      # consecutive bad reads
        self.reads = 0          # valid reads so far, lets callers spot new readings

    def poll(self):
        value = self._read()
        with self._lock:
            if value is None:
                self.bad_reads += 1
            else:
                self.bad_reads = 0
                self.temp_c = value
                self.temp_time = self._clock()
                self.reads += 1
        return value

    def ok(self):
        with self._lock:
            return (
                self.temp_c is not None
                and self.bad_reads < config.SENSOR_BAD_LIMIT
                and self._clock() - self.temp_time <= config.SENSOR_STALE_SECONDS
            )

    def temp(self):
        """Last valid temperature if the sensor is healthy, else None."""
        return self.temp_c if self.ok() else None


class SafeHeater:
    """The heater relay behind the last line of defence, used by tasks and manual mode alike.

    on() refuses without airflow, while the guard is latched, or when a sensor is attached and has
    no valid reading. check() runs every control tick and cuts the relay when the guard latches.
    """

    def __init__(self, device, fan, guard=None, mono=time.monotonic):
        self._dev = device
        self._fan = fan
        self.guard = guard or HeaterGuard()
        self._mono = mono
        self.temp_fn = None  # set by Outputs.attach_sensor

    @property
    def latched(self):
        return self.guard.latched

    @property
    def is_active(self):
        return self._dev.is_active

    @property
    def closed(self):
        return self._dev.closed

    def on(self):
        if self.guard.latched or not self._fan.is_active:
            self._dev.off()
            return False
        if self.temp_fn is not None and self.temp_fn() is None:
            self._dev.off()
            return False
        self._dev.on()
        return True

    def off(self):
        self._dev.off()

    def check(self):
        """Returns the reason if this call latched a fault. The relay is already off by then."""
        if not self._fan.is_active:
            self._dev.off()  # airflow lost: the heater must not stay on, whoever asked for it
        temp = self.temp_fn() if self.temp_fn is not None else None
        if self.temp_fn is not None and temp is None:
            self._dev.off()
        reason = self.guard.update(self._dev.is_active, temp, self._mono())
        if reason:
            self._dev.off()
        return reason

    def clear_latch(self):
        self.guard.clear()

    def close(self):
        self._dev.close()


class Outputs:
    """Fan and heater relays (active low, both off at init)."""

    def __init__(self, mono=time.monotonic):
        self.fan = OutputDevice(config.FAN_PIN, active_high=False, initial_value=False)
        self.heater = SafeHeater(
            OutputDevice(config.HEATER_PIN, active_high=False, initial_value=False), self.fan, mono=mono)

    def attach_sensor(self, sensor):
        self.heater.temp_fn = sensor.temp

    def all_off(self):
        self.heater.off()
        self.fan.off()

    def close(self):
        self.all_off()
        self.heater.close()
        self.fan.close()
