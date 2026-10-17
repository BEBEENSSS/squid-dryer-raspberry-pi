"""DHT22 (AM2302) temperature and humidity sensor on one GPIO pin.

The sensor speaks a timed single-wire protocol. Reading it by polling the pin from Python misses edges, so
this does not poll: it sends the start signal, then lets the kernel record every edge with its own
timestamp (the GPIO character device) and decodes the pulse widths afterwards. How late Python is to
look cannot change a width.

    reading = read()                  # config.DHT22_PIN
    reading.temp_c, reading.humidity  # None when reading.error says why

The pin must be free: a `dtoverlay=w1-gpio` line for the same pin makes the kernel own it and the read fails
as busy. The sensor measures once per 2 s, so read no faster than that (config.DHT22_MIN_INTERVAL_S).
"""

import time
from typing import NamedTuple

import gpiod
from gpiod.edge_event import EdgeEvent
from gpiod.line import Bias, Direction, Edge, Value

import config

DATA_BITS = 40        # humidity (16), temperature (16), checksum (8)
EVENT_BUFFER = 256    # the reply is ~85 edges; the kernel holds them until they are read
RECORD_SECONDS = 0.02  # the whole reply takes ~5 ms
MIN_PULSE_US = 5      # a high pulse shorter or longer than any real bit is noise, not a reading
MAX_PULSE_US = 150


class Reading(NamedTuple):
    temp_c: float | None
    humidity: float | None
    error: str | None


def _bad(reason):
    return Reading(None, None, reason)


def decode(data):
    """Five reply bytes to (temp_c, humidity). Raises ValueError with the reason when they are unusable."""
    if len(data) != 5:
        raise ValueError(f"reply is {len(data)} bytes, expected 5")
    if sum(data[:4]) & 0xFF != data[4]:
        raise ValueError("checksum mismatch")
    humidity = ((data[0] << 8) | data[1]) / 10.0
    temp = (((data[2] & 0x7F) << 8) | data[3]) / 10.0
    if data[2] & 0x80:  # the top bit is the sign, not part of the magnitude
        temp = -temp
    if not config.HUMIDITY_PLAUSIBLE_MIN <= humidity <= config.HUMIDITY_PLAUSIBLE_MAX:
        raise ValueError(f"{humidity:.1f} %RH is outside the plausible range")
    if not config.TEMP_PLAUSIBLE_MIN_C <= temp <= config.TEMP_PLAUSIBLE_MAX_C:
        raise ValueError(f"{temp:.1f} C is outside the plausible range")
    return temp, humidity


def bytes_from_edges(edges):
    """Edge list [(timestamp_ns, rising)] to the five reply bytes. Raises ValueError with the reason.

    A bit is the width of one high pulse. The reply starts with a high pulse that is not a bit (the sensor's
    acknowledge) and the line's own release can add another, so only the last 40 high pulses count.
    """
    widths_us = []
    risen_at = None
    for timestamp_ns, rising in edges:
        if rising:
            risen_at = timestamp_ns
        elif risen_at is not None:
            widths_us.append((timestamp_ns - risen_at) / 1000.0)
            risen_at = None
    if not widths_us:
        raise ValueError("no response (check the wiring, the pull-up and that the data wire is on this pin)")
    if len(widths_us) < DATA_BITS:
        raise ValueError(f"reply cut short: {len(widths_us)} pulses, need {DATA_BITS}")

    bits = widths_us[-DATA_BITS:]
    if not all(MIN_PULSE_US <= w <= MAX_PULSE_US for w in bits):
        raise ValueError("pulse timing out of range (noisy line or a loaded system)")
    value = 0
    for width in bits:
        value = (value << 1) | (1 if width > config.DHT22_BIT_THRESHOLD_US else 0)
    return list(value.to_bytes(5, "big"))


def _record(pin, chip_path, request_lines, sleep):
    """Send the start signal and return the edges the sensor answers with, as [(timestamp_ns, rising)]."""
    request = request_lines(
        chip_path, consumer="squid-dht22", event_buffer_size=EVENT_BUFFER,
        config={pin: gpiod.LineSettings(direction=Direction.OUTPUT, output_value=Value.ACTIVE)})
    try:
        request.set_value(pin, Value.INACTIVE)
        sleep(config.DHT22_START_LOW_S)
        # one call turns the pin into an input with edge capture, so nothing of the reply is lost in between
        request.reconfigure_lines({pin: gpiod.LineSettings(
            direction=Direction.INPUT, edge_detection=Edge.BOTH, bias=Bias.PULL_UP)})
        sleep(RECORD_SECONDS)
        edges = []
        while request.wait_edge_events(0):
            edges += [(e.timestamp_ns, e.event_type == EdgeEvent.Type.RISING_EDGE)
                      for e in request.read_edge_events(EVENT_BUFFER)]
        return edges
    finally:
        request.release()


def read(pin=None, chip_path=None, request_lines=gpiod.request_lines, sleep=time.sleep):
    """One reading. Never raises for a bad read: temp_c and humidity are None and error says why."""
    pin = config.DHT22_PIN if pin is None else pin
    chip_path = chip_path or config.GPIO_CHIP_PATH
    try:
        edges = _record(pin, chip_path, request_lines, sleep)
    except PermissionError:
        return _bad(f"no permission on {chip_path} (the user needs to be in the gpio group)")
    except FileNotFoundError:
        return _bad(f"{chip_path} does not exist")
    except OSError as e:
        if e.errno == 16:  # EBUSY
            return _bad(f"GPIO{pin} is busy: another driver owns it. Remove dtoverlay=w1-gpio,gpiopin={pin} "
                        "from /boot/firmware/config.txt and reboot")
        return _bad(f"GPIO{pin} cannot be used ({e.strerror or e})")
    try:
        temp, humidity = decode(bytes_from_edges(edges))
    except ValueError as e:
        return _bad(str(e))
    return Reading(temp, humidity, None)
