"""The DHT22 driver and the humidity half of calibration. No hardware: the GPIO line is a fake that plays back
the edges a real sensor produces."""
import errno
import fcntl
import math
import os
import random
import threading
import time
from types import SimpleNamespace

import pytest
from gpiod.edge_event import EdgeEvent
from gpiod.line import Direction, Edge, Value

import calibration
import config
import dht22

RISING, FALLING = EdgeEvent.Type.RISING_EDGE, EdgeEvent.Type.FALLING_EDGE


def frame(humidity, temp):
    """The five bytes a DHT22 sends for these values."""
    h = round(abs(humidity) * 10)
    t = round(abs(temp) * 10)
    data = [h >> 8, h & 0xFF, (t >> 8) | (0x80 if temp < 0 else 0), t & 0xFF]
    return data + [sum(data) & 0xFF]


def edges_for(data, jitter_us=0.0, seed=1, release=True):
    """What the kernel records after the host lets go of the line: [(timestamp_ns, rising)].
    The host's own release, the sensor's acknowledge, then 40 bits (low 50 us, high 27 us for 0 / 70 us for 1)."""
    rng = random.Random(seed)
    j = lambda: rng.uniform(-jitter_us, jitter_us)  # noqa: E731
    t = 1_000_000.0  # ns
    edges = []

    def edge(rising, after_us):
        nonlocal t
        t += (after_us + j()) * 1000
        edges.append((int(t), rising))

    if release:
        edge(True, 0)      # the pull-up lifts the line as the host lets go
    edge(False, 30)        # the sensor answers: low 80 us...
    edge(True, 80)         # ...high 80 us
    edge(False, 80)
    for byte in data:
        for i in range(7, -1, -1):
            edge(True, 50)
            edge(False, 70 if byte >> i & 1 else 27)
    edge(True, 50)         # the sensor lets go again
    return edges


@pytest.fixture(autouse=True)
def lock_in_tmp(tmp_path, monkeypatch):
    monkeypatch.setattr(dht22, "LOCK_PATH", tmp_path / "dht22.lock")   # never the real data/ directory


# ---------------- decode ----------------
def test_decode_reads_humidity_and_temperature():
    assert dht22.decode(frame(65.2, 35.1)) == (35.1, 65.2)
    assert dht22.decode([0x02, 0x8C, 0x01, 0x5F, 0xEE]) == (35.1, 65.2)  # the datasheet's example


def test_decode_negative_temperature_uses_the_sign_bit():
    assert dht22.decode(frame(40.0, -5.3)) == (-5.3, 40.0)


def test_decode_rejects_a_bad_checksum():
    data = frame(65.2, 35.1)
    data[4] ^= 0x01
    with pytest.raises(ValueError, match="checksum"):
        dht22.decode(data)


def test_decode_rejects_values_no_chamber_can_produce():
    with pytest.raises(ValueError, match="%RH"):
        dht22.decode(frame(100.1, 30.0))
    with pytest.raises(ValueError, match="plausible"):
        dht22.decode(frame(50.0, 100.1))
    assert dht22.decode(frame(100.0, 99.9)) == (99.9, 100.0)


def test_decode_rejects_a_short_reply():
    with pytest.raises(ValueError, match="4 bytes"):
        dht22.decode([1, 2, 3, 6])


# ---------------- edges to bytes ----------------
def test_edges_round_trip_to_the_bytes():
    data = frame(48.3, 41.7)
    assert dht22.bytes_from_edges(edges_for(data)) == data


@pytest.mark.parametrize("lost_bits", [1, 2, 6])
def test_bits_lost_at_the_front_are_the_zero_bits_of_humidity(lost_bits):
    # the kernel arms edge detection a little late, so the first edges of the reply are not recorded
    data = frame(65.2, 35.1)                                   # humidity 652 = 0x028C: top six bits are 0
    edges = edges_for(data)[4 + 2 * lost_bits:]                # release, acknowledge, then the lost bits
    assert dht22.bytes_from_edges(edges) == data


def test_a_bit_whose_rising_edge_was_lost_but_not_its_falling_edge_counts_as_lost():
    data = frame(65.2, 35.1)
    edges = edges_for(data)[4 + 2 * 2 + 1:]                    # the third bit starts with its falling edge
    assert dht22.bytes_from_edges(edges) == data


def test_losing_more_than_the_known_zero_bits_is_a_failed_read():
    edges = edges_for(frame(65.2, 35.1))[4 + 2 * 7:]
    with pytest.raises(ValueError, match="cut short"):
        dht22.bytes_from_edges(edges)


def test_the_hosts_release_edge_is_not_needed():
    data = frame(48.3, 41.7)
    assert dht22.bytes_from_edges(edges_for(data, release=False)) == data


def test_timing_jitter_inside_the_margin_still_decodes():
    data = frame(23.4, 56.7)
    for seed in range(20):
        assert dht22.bytes_from_edges(edges_for(data, jitter_us=10, seed=seed)) == data


def test_silence_means_no_sensor_answered():
    with pytest.raises(ValueError, match="no response"):
        dht22.bytes_from_edges([])
    with pytest.raises(ValueError, match="no response"):
        dht22.bytes_from_edges([(1000, True)])  # only the line lifting back up


def test_a_cut_short_reply_is_rejected():
    with pytest.raises(ValueError, match="cut short"):
        dht22.bytes_from_edges(edges_for(frame(48.3, 41.7))[:30])


def test_an_impossible_pulse_is_rejected():
    edges = edges_for(frame(48.3, 41.7))
    # stretch the falling edge of the last bit to 400 us
    ts, rising = edges[-2]
    edges[-2] = (ts + 400_000, rising)
    with pytest.raises(ValueError, match="timing"):
        dht22.bytes_from_edges(edges)


def test_a_dropped_edge_is_caught_by_the_checksum():
    edges = edges_for(frame(48.3, 41.7))
    del edges[10:12]  # one whole bit lost: everything shifts
    with pytest.raises(ValueError):
        dht22.decode(dht22.bytes_from_edges(edges))


# ---------------- read() against a fake line ----------------
class FakeLine:
    """A GPIO line request. The sensor only talks after the host switched the pin to an input."""

    def __init__(self, edges):
        self.edges = edges
        self.pending = []
        self.calls = []
        self.released = False

    def set_value(self, pin, value):
        self.calls.append(("set_value", pin, value))

    def reconfigure_lines(self, cfg):
        self.calls.append(("reconfigure", cfg))
        self.pending = [SimpleNamespace(timestamp_ns=ts, event_type=RISING if rising else FALLING)
                        for ts, rising in self.edges]

    def wait_edge_events(self, timeout=None):
        return bool(self.pending)

    def read_edge_events(self, max_events=None):
        out, self.pending = self.pending[:max_events], self.pending[max_events:]
        return out

    def release(self):
        self.released = True


def fake_driver(line):
    seen = {}

    def request_lines(path, **kwargs):
        seen.update(path=path, **kwargs)
        return line

    return request_lines, seen


def test_read_sends_the_start_signal_then_listens():
    line = FakeLine(edges_for(frame(55.5, 38.2)))
    request_lines, seen = fake_driver(line)
    sleeps = []
    reading = dht22.read(request_lines=request_lines, sleep=sleeps.append)

    assert reading == dht22.Reading(38.2, 55.5, None)
    assert seen["path"] == config.GPIO_CHIP_PATH
    assert seen["config"][config.DHT22_PIN].direction == Direction.OUTPUT
    assert seen["config"][config.DHT22_PIN].output_value == Value.ACTIVE      # idle high
    assert line.calls[0] == ("set_value", config.DHT22_PIN, Value.INACTIVE)    # start signal: pulled low...
    assert sleeps[0] == config.DHT22_START_LOW_S                               # ...for at least 1 ms...
    listen = line.calls[1][1][config.DHT22_PIN]                                # ...then straight to listening
    assert listen.direction == Direction.INPUT and listen.edge_detection == Edge.BOTH
    assert line.released


def test_read_uses_the_pin_it_is_given():
    line = FakeLine(edges_for(frame(55.5, 38.2)))
    request_lines, seen = fake_driver(line)
    assert dht22.read(pin=5, request_lines=request_lines, sleep=lambda s: None).error is None
    assert list(seen["config"]) == [5]


def test_read_with_no_sensor_reports_no_response_and_still_lets_go_of_the_pin():
    line = FakeLine([])
    request_lines, _ = fake_driver(line)
    reading = dht22.read(request_lines=request_lines, sleep=lambda s: None)
    assert reading.temp_c is None and reading.humidity is None
    assert "no response" in reading.error
    assert line.released


def test_read_reports_a_checksum_error_as_a_bad_read():
    data = frame(55.5, 38.2)
    data[4] ^= 0x10
    request_lines, _ = fake_driver(FakeLine(edges_for(data)))
    reading = dht22.read(request_lines=request_lines, sleep=lambda s: None)
    assert reading.temp_c is None and "checksum" in reading.error


@pytest.mark.parametrize("exc, text", [
    (OSError(errno.EBUSY, "Device or resource busy"), "w1-gpio"),
    (PermissionError(errno.EACCES, "denied"), "gpio group"),
    (FileNotFoundError(errno.ENOENT, "missing"), "does not exist"),
    (OSError(errno.EINVAL, "Invalid argument"), "cannot be used"),
])
def test_read_explains_why_the_pin_could_not_be_used(exc, text):
    def request_lines(path, **kwargs):
        raise exc

    reading = dht22.read(request_lines=request_lines, sleep=lambda s: None)
    assert reading.temp_c is None and text in reading.error


# ---------------- humidity calibration ----------------
@pytest.fixture
def cal(tmp_path, monkeypatch):
    monkeypatch.setattr(calibration, "CAL_PATH", tmp_path / "cal.json")
    return calibration.CAL_PATH


def test_the_dht22_id_is_not_a_1wire_id_and_sensor_1_is_untouched():
    assert config.DHT22_ID == "dht22-gpio22"
    assert config.ZONE_SENSOR_IDS["top"] == config.DHT22_ID  # the service reads it as the top sensor
    assert config.ZONE_SENSOR_IDS["bottom"] == config.SENSOR_ID


def test_humidity_offset_is_saved_and_loaded(cal):
    calibration.save_humidity_offset(config.DHT22_ID, -4.5, "true humidity 55.00 %RH", 30)
    assert calibration.load_humidity_offset(config.DHT22_ID) == -4.5
    assert calibration.load_offset(config.DHT22_ID) == 0.0   # temperature was not calibrated


def test_temperature_and_humidity_offsets_keep_each_other(cal):
    calibration.save_offset(config.DHT22_ID, -0.4, "sensor 1 (main) x", 30)
    calibration.save_humidity_offset(config.DHT22_ID, 3.0, "true humidity 50.00 %RH", 30)
    assert calibration.zone_offset(config.DHT22_ID) == -0.4
    assert calibration.load_humidity_offset(config.DHT22_ID) == 3.0

    calibration.save_offset(config.DHT22_ID, -0.5, "sensor 1 (main) x", 40)   # recalibrating temperature...
    assert calibration.zone_offset(config.DHT22_ID) == -0.5
    assert calibration.load_humidity_offset(config.DHT22_ID) == 3.0           # ...leaves humidity alone


def test_the_existing_file_format_still_loads(cal):
    cal.write_text('{"28-0624c12820aa": {"offset_c": -0.31, "against": "x", "samples": 14, "at": "t"}}')
    assert calibration.zone_offset("28-0624c12820aa") == -0.31
    assert calibration.load_humidity_offset("28-0624c12820aa") == 0.0
    calibration.save_humidity_offset(config.DHT22_ID, 1.0, "x", 10)
    assert calibration.zone_offset("28-0624c12820aa") == -0.31                # other sensors survive a save


@pytest.mark.parametrize("bad", ['"nan"', "null", '"abc"', "[]", "1e999"])
def test_an_unusable_humidity_offset_means_no_correction(cal, bad):
    cal.write_text('{"%s": {"offset_rh": %s}}' % (config.DHT22_ID, bad))
    assert calibration.load_humidity_offset(config.DHT22_ID) == 0.0


def test_corrected_humidity_stays_between_dry_and_wet():
    assert calibration.corrected_humidity(50.0, 3.5) == 53.5
    assert calibration.corrected_humidity(99.0, 5.0) == 100.0
    assert calibration.corrected_humidity(2.0, -5.0) == 0.0
    assert not math.isnan(calibration.corrected_humidity(50.0, 0.0))


# ---------------- retries ----------------
class Sequence:
    """request_lines that hands out one FakeLine per call, from a list of edge lists (or an exception to raise)."""

    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.calls = 0

    def __call__(self, path, **kwargs):
        self.calls += 1
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return FakeLine(outcome)


def test_a_bad_read_is_asked_again_and_the_good_one_is_returned():
    good = edges_for(frame(55.5, 38.2))
    lost_edge = good[:30] + good[31:]            # one edge missing mid-reply: the checksum rejects it
    driver, sleeps = Sequence(lost_edge, [], good), []
    reading = dht22.read(request_lines=driver, sleep=sleeps.append)
    assert reading == dht22.Reading(38.2, 55.5, None)
    assert driver.calls == 3
    assert sleeps.count(config.DHT22_RETRY_GAP_S) == 2     # a short pause before each repeat


def test_all_attempts_failing_gives_the_last_reason():
    driver = Sequence([], [], [])
    reading = dht22.read(request_lines=driver, sleep=lambda s: None)
    assert reading.temp_c is None and reading.humidity is None
    assert "no response" in reading.error and "3 tries" in reading.error
    assert driver.calls == config.DHT22_ATTEMPTS


def test_one_attempt_means_no_retry_and_no_tries_note():
    driver = Sequence([])
    reading = dht22.read(request_lines=driver, sleep=lambda s: None, attempts=1)
    assert driver.calls == 1 and "tries" not in reading.error


def test_a_pin_that_cannot_be_used_is_not_retried():
    driver = Sequence(OSError(errno.EBUSY, "Device or resource busy"))
    reading = dht22.read(request_lines=driver, sleep=lambda s: None)
    assert driver.calls == 1 and "busy" in reading.error


# ---------------- one reader at a time ----------------
def test_readers_take_turns_on_the_pin():
    """The service and sensor2_test.py read the same pin: the kernel would refuse the second one as busy."""
    holders = []
    overlaps = []

    class Line(FakeLine):
        def __init__(self):
            super().__init__(edges_for(frame(55.5, 38.2)))
            if holders:
                overlaps.append(True)   # someone else still has the pin
            holders.append(self)

        def release(self):
            holders.remove(self)
            super().release()

    def request_lines(path, **kwargs):
        return Line()

    results = []

    def reader():
        for _ in range(10):
            results.append(dht22.read(request_lines=request_lines, sleep=lambda s: time.sleep(0.001)))

    threads = [threading.Thread(target=reader) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not overlaps
    assert len(results) == 40 and all(r.error is None for r in results)


def test_a_reader_waits_while_another_program_holds_the_lock(tmp_path):
    other = os.open(dht22.LOCK_PATH, os.O_CREAT | os.O_RDWR)
    fcntl.flock(other, fcntl.LOCK_EX)                  # e.g. the service in the middle of its read
    started = []

    def request_lines(path, **kwargs):
        started.append(time.monotonic())
        return FakeLine(edges_for(frame(55.5, 38.2)))

    result = []
    t = threading.Thread(target=lambda: result.append(dht22.read(request_lines=request_lines, sleep=lambda s: None)))
    t.start()
    t.join(0.2)
    assert t.is_alive() and not started                # still waiting: it did not touch the pin
    os.close(other)                                    # the other reader is done
    t.join(5)
    assert result and result[0].error is None and started


def test_the_lock_is_free_again_after_a_failed_read():
    def request_lines(path, **kwargs):
        raise OSError(errno.EBUSY, "Device or resource busy")

    assert "busy" in dht22.read(request_lines=request_lines, sleep=lambda s: None).error
    fd = os.open(dht22.LOCK_PATH, os.O_RDWR)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)     # raises if the failed read kept the lock
    os.close(fd)
