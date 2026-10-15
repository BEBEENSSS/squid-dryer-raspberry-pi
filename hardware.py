import re
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

    # a reading no real chamber can produce is a failing sensor, and a failed read turns the heater off
    if not config.TEMP_PLAUSIBLE_MIN_C <= value <= config.TEMP_PLAUSIBLE_MAX_C:
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


def make_reader(sensor_id, offset_c=0.0, base=None):
    """Read function for one sensor by id: read_temp plus a calibration offset, None for a bad read."""
    path = Path(base or config.W1_BASE) / sensor_id

    def read():
        value = read_temp(path)
        return None if value is None else round(value + offset_c, 3)

    return read


def unconfigured_zones(ids, cfg=config):
    """Zones whose sensor id is a placeholder, malformed or shared with another zone."""
    pattern = re.compile(cfg.ZONE_ID_PATTERN)
    bad = []
    for zone in cfg.ZONES:
        sensor_id = ids.get(zone)
        valid = isinstance(sensor_id, str) and pattern.match(sensor_id)
        shared = sum(1 for other in cfg.ZONES if ids.get(other) == sensor_id) > 1
        if not valid or shared:
            bad.append(zone)
    return bad


class SensorArray:
    """The chamber sensors by zone. The control sensor (bottom, sensor 1) is the one the thermostat, the
    heater interlock and restart recovery keep reading alone; the others only widen what counts as warm
    (warm-up needs every zone) and as hot (the hottest healthy reading decides over-temperature and cooldown).
    """

    def __init__(self, sensors, ids=None, cfg=config):
        self.sensors = dict(sensors)          # zone -> Sensor
        self.zones = tuple(z for z in cfg.ZONES if z in self.sensors)
        self.control = self.sensors[cfg.CONTROL_ZONE]
        self.control_zone = cfg.CONTROL_ZONE
        self._ids = None if ids is None else dict(ids)  # None: ids are not applicable (simulation, tests)
        self._cfg = cfg

    @property
    def others(self):
        """Zones other than the control one, bottom to top."""
        return tuple(z for z in self.zones if z != self.control_zone)

    def healthy(self, zone):
        return self.sensors[zone].ok()

    def temp(self, zone):
        return self.sensors[zone].temp()

    def temps(self):
        """Healthy reading per zone, None for a zone that is not healthy."""
        return {zone: self.sensors[zone].temp() for zone in self.zones}

    def unconfigured(self):
        return [] if self._ids is None else unconfigured_zones(self._ids, self._cfg)

    def configured(self):
        return not self.unconfigured()

    def hottest(self):
        """Hottest healthy reading. None while the control sensor is unhealthy: nothing may rely on a
        chamber reading the heater's own sensor cannot confirm."""
        if self.control.temp() is None:
            return None
        temps = [t for t in (s.temp() for s in self.sensors.values()) if t is not None]
        return max(temps) if temps else None


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
