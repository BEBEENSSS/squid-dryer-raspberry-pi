import re
import threading
import time
from pathlib import Path

from gpiozero import OutputDevice

import calibration
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
    """Tracks the last valid reading and whether the sensor counts as healthy.

    `read_fn` returns a temperature in C, a `(temp_c, humidity_pct)` pair from a sensor that measures both (the
    DHT22), or None for a bad read. The humidity belongs to the same read as the temperature, so one bad read
    voids both and the sensor's health covers both.
    """

    def __init__(self, read_fn=read_temp, clock=time.monotonic,
                 bad_limit=config.SENSOR_BAD_LIMIT, stale_seconds=config.SENSOR_STALE_SECONDS):
        self._read = read_fn
        self._clock = clock
        self._bad_limit = bad_limit
        self._stale_seconds = stale_seconds
        self._lock = threading.Lock()
        self.temp_c = None      # last valid temperature
        self.humidity_pct = None  # humidity of that same read, None for a sensor that has none
        self.temp_time = 0.0    # monotonic time of last valid read
        self.bad_reads = 0      # consecutive bad reads
        self.reads = 0          # valid reads so far, lets callers spot new readings

    def poll(self):
        value = self._read()
        humidity = None
        if isinstance(value, tuple):
            value, humidity = value
        with self._lock:
            if value is None:
                self.bad_reads += 1
            else:
                self.bad_reads = 0
                self.temp_c = value
                self.humidity_pct = humidity
                self.temp_time = self._clock()
                self.reads += 1
        return value

    def ok(self):
        with self._lock:
            return (
                self.temp_c is not None
                and self.bad_reads < self._bad_limit
                and self._clock() - self.temp_time <= self._stale_seconds
            )

    def temp(self):
        """Last valid temperature if the sensor is healthy, else None."""
        return self.temp_c if self.ok() else None

    def humidity(self):
        """Last valid humidity in %RH if the sensor is healthy, else None (also for a sensor without one)."""
        return self.humidity_pct if self.ok() else None


def make_reader(sensor_id, offset_c=0.0, base=None):
    """Read function for one sensor by id: read_temp plus a calibration offset, None for a bad read."""
    path = Path(base or config.W1_BASE) / sensor_id

    def read():
        value = read_temp(path)
        return None if value is None else round(value + offset_c, 3)

    return read


def make_dht_reader(pin=None, offset_c=0.0, offset_rh=0.0, read_fn=None):
    """Read function for the DHT22: a `(temp_c, humidity_pct)` pair with the calibration offsets applied, None for a
    bad read. The driver (and its gpiod dependency) is imported on first use, so a PC without it can still run the
    simulation and the tests. `read_fn` replaces dht22.read in tests."""

    warned = []

    def read():
        reader = read_fn
        if reader is None:
            try:
                import dht22
            except ImportError as e:  # gpiod missing: the sensor reads as failed, and the log says why (once)
                if not warned:
                    warned.append(True)
                    print(f"DHT22 driver unavailable: {e} (pip install -r requirements.txt)")
                return None
            reader = dht22.read
        reading = reader(pin) if pin is not None else reader()
        if reading.error is not None or reading.temp_c is None or reading.humidity is None:
            return None
        return round(reading.temp_c + offset_c, 3), calibration.corrected_humidity(reading.humidity, offset_rh)

    return read


def make_zone_sensor(sensor_id, cfg=config):
    """A Sensor for one configured zone id, with its saved calibration: `(sensor, offset_c, offset_rh)`.

    A 1-Wire id is a DS18B20 read from /sys, the DHT22's id reads temperature and humidity from its GPIO pin. The
    offset is always 0 for sensor 1, the reference the others are matched to.
    """
    offset_c = calibration.zone_offset(sensor_id)
    if sensor_id == cfg.DHT22_ID:
        offset_rh = calibration.load_humidity_offset(sensor_id)
        sensor = Sensor(make_dht_reader(cfg.DHT22_PIN, offset_c, offset_rh),
                        bad_limit=cfg.DHT22_BAD_LIMIT, stale_seconds=cfg.DHT22_STALE_SECONDS)
        return sensor, offset_c, offset_rh
    return Sensor(make_reader(sensor_id, offset_c)), offset_c, 0.0


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

    def humidity(self):
        """Humidity in %RH from the zone that has a sensor for it, None while that sensor is unhealthy or absent."""
        sensor = self.sensors.get(self._cfg.HUMIDITY_ZONE)
        return sensor.humidity() if sensor is not None else None

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
