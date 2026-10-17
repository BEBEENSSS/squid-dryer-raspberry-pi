"""Humidity from the DHT22 at the top: from the sensor, through the zones and the state, into the history."""

import sqlite3
import sys
from types import ModuleType, SimpleNamespace

import pytest

import calibration
import config
from hardware import Sensor, SensorArray, make_dht_reader, make_reader, make_zone_sensor, unconfigured_zones
from snapshot import zone_state
from task_store import TaskStore
from temp_history import clean_readings, payload, worth_recording
from tests.test_temp_sync import Env, sample

LAST = {"bottom": 30.0, "middle": 29.0, "top": 28.0, "humidity": 60.0}


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


def reading(temp, humidity, error=None):
    return SimpleNamespace(temp_c=temp, humidity=humidity, error=error)


# ---------------- configuration ----------------
def test_the_zones_are_two_ds18b20_and_a_dht22_at_the_top():
    ids = config.ZONE_SENSOR_IDS
    assert ids["bottom"] == config.SENSOR_ID
    assert ids["top"] == config.DHT22_ID == "dht22-gpio22" and config.DHT22_PIN == 22
    assert ids["middle"].startswith("28-") and ids["middle"] != ids["bottom"]
    assert config.HUMIDITY_ZONE == "top"
    assert unconfigured_zones(ids) == []                      # the deployed ids are all valid
    assert unconfigured_zones({**ids, "middle": ids["top"]}) == ["middle", "top"]   # one sensor in two places
    assert unconfigured_zones({**ids, "top": "dht22-gpio"}) == ["top"]
    assert unconfigured_zones({**ids, "top": "28-0624C1511ABA"}) == ["top"]          # 1-Wire ids stay lower-case hex


# ---------------- Sensor ----------------
def test_a_pair_read_keeps_the_humidity_of_the_same_read_as_the_temperature():
    pairs = iter([(40.0, 55.0), (41.0, 54.0)])
    s = Sensor(lambda: next(pairs), clock=Clock())
    assert s.humidity() is None                     # nothing read yet
    s.poll()
    assert (s.temp(), s.humidity()) == (40.0, 55.0)
    s.poll()
    assert (s.temp(), s.humidity()) == (41.0, 54.0)


def test_a_plain_temperature_sensor_has_no_humidity():
    s = Sensor(lambda: 40.0, clock=Clock())
    s.poll()
    assert s.temp() == 40.0 and s.humidity() is None


def test_one_bad_read_keeps_the_last_good_pair_and_a_dead_sensor_loses_both():
    clock, results = Clock(), iter([(40.0, 55.0), None, None, None, None, None])
    s = Sensor(lambda: next(results), clock=clock, bad_limit=config.DHT22_BAD_LIMIT,
               stale_seconds=config.DHT22_STALE_SECONDS)
    s.poll()
    for _ in range(config.DHT22_BAD_LIMIT - 1):     # a DHT22 misses a read now and then: still healthy
        clock.now += 2
        s.poll()
        assert (s.temp(), s.humidity()) == (40.0, 55.0)
    clock.now += 2
    s.poll()                                        # the limit is reached: failed, and the humidity goes with it
    assert (s.ok(), s.temp(), s.humidity()) == (False, None, None)


def test_the_dht22_limits_are_longer_than_the_ds18b20_ones_and_outlast_the_read_interval():
    assert config.DHT22_BAD_LIMIT > config.SENSOR_BAD_LIMIT
    assert config.DHT22_STALE_SECONDS >= config.DHT22_BAD_LIMIT * config.DHT22_MIN_INTERVAL_S


def test_a_stale_dht22_loses_its_humidity():
    clock = Clock()
    s = Sensor(lambda: (40.0, 55.0), clock=clock, bad_limit=5, stale_seconds=15)
    s.poll()
    clock.now += 15
    assert s.humidity() == 55.0
    clock.now += 1
    assert s.humidity() is None


# ---------------- the DHT22 reader and the zone factory ----------------
def test_the_dht_reader_applies_both_offsets_and_keeps_humidity_in_range():
    read = make_dht_reader(22, offset_c=-0.4, offset_rh=-5.0, read_fn=lambda pin: reading(30.4, 55.0))
    assert read() == (30.0, 50.0)
    wet = make_dht_reader(22, offset_rh=8.0, read_fn=lambda pin: reading(30.0, 97.0))
    assert wet() == (30.0, 100.0)                   # a corrected reading cannot be wetter than saturated


def test_a_failed_dht_read_is_none_never_a_zero():
    for bad in (reading(None, None, "no response"), reading(30.0, None, None), reading(None, 50.0, None)):
        assert make_dht_reader(22, read_fn=lambda pin, r=bad: r)() is None


def test_a_missing_driver_reads_as_a_failed_sensor_and_says_so_once(monkeypatch, capsys):
    monkeypatch.setitem(sys.modules, "dht22", None)            # `import dht22` now raises ImportError
    read = make_dht_reader(22)
    assert read() is None and read() is None
    assert capsys.readouterr().out.count("DHT22 driver unavailable") == 1


def test_make_zone_sensor_picks_the_sensor_type_by_id(tmp_path, monkeypatch):
    monkeypatch.setattr(calibration, "CAL_PATH", tmp_path / "cal.json")
    calibration.save_offset(config.DHT22_ID, -0.4, "x", 30)
    calibration.save_humidity_offset(config.DHT22_ID, 2.5, "x", 30)
    calibration.save_offset("28-0624c1511aba", 0.2, "x", 30)
    calibration.save_offset(config.SENSOR_ID, 9.9, "x", 30)

    dht_module = ModuleType("dht22")
    dht_module.read = lambda pin=None: reading(30.4, 50.0)
    monkeypatch.setitem(sys.modules, "dht22", dht_module)      # no gpiod needed: the driver is imported on first read

    sensor, off_c, off_rh = make_zone_sensor(config.DHT22_ID)
    assert (off_c, off_rh) == (-0.4, 2.5)
    sensor.poll()
    assert (sensor.temp(), sensor.humidity()) == (30.0, 52.5)

    one_wire, off_c, off_rh = make_zone_sensor("28-0624c1511aba")
    assert (off_c, off_rh) == (0.2, 0.0)
    assert one_wire.humidity() is None

    _, off_c, _ = make_zone_sensor(config.SENSOR_ID)
    assert off_c == 0.0                                        # sensor 1 is the reference and is never offset


def test_a_1wire_reader_still_returns_a_plain_temperature(tmp_path):
    d = tmp_path / "28-000000000001"
    d.mkdir()
    (d / "w1_slave").write_text("aa bb YES\naa bb t=31250\n")
    assert make_reader("28-000000000001", 0.0, base=tmp_path)() == 31.25


# ---------------- SensorArray and the state ----------------
def make_zones(top):
    return SensorArray({"bottom": Sensor(lambda: 30.0, clock=Clock()), "middle": Sensor(lambda: 29.0, clock=Clock()),
                        "top": top})


def test_the_array_reports_the_top_sensors_humidity_only_while_it_is_healthy():
    clock = Clock()
    top = Sensor(lambda: (28.0, 61.26), clock=clock, bad_limit=5, stale_seconds=15)
    zones = make_zones(top)
    assert zones.humidity() is None
    top.poll()
    assert zones.humidity() == 61.26
    assert zone_state(zones)["humidity"] == 61.3
    clock.now += 60
    assert zones.humidity() is None
    assert zone_state(zones)["humidity"] is None                # None removes the state key


def test_an_array_without_a_humidity_sensor_has_none():
    zones = SensorArray({"bottom": Sensor(lambda: 30.0, clock=Clock())})
    assert zones.humidity() is None


# ---------------- what the history records ----------------
def test_humidity_is_left_out_when_missing_or_not_a_percentage_never_zeroed():
    temps = {"bottom": 30.0, "middle": 29.0, "top": 28.0}
    assert clean_readings(temps) == temps
    assert clean_readings(temps, None) == temps
    assert clean_readings(temps, 0.0) == {**temps, "humidity": 0.0}           # 0 %RH is a number, not a gap
    for bad in (-1.0, 100.5, float("nan"), float("inf"), True, "55"):
        assert clean_readings(temps, bad) == temps


def test_a_record_carries_the_humidity_only_when_it_has_one():
    row = {"t_ms": 7, "bottom": 30.0, "middle": None, "top": 28.0, "humidity": 55.46}
    assert payload(row) == {"t": 7, "bottom": 30.0, "top": 28.0, "humidity": 55.5}
    assert payload(dict(row, humidity=None)) == {"t": 7, "bottom": 30.0, "top": 28.0}
    assert payload({k: v for k, v in row.items() if k != "humidity"}) == {"t": 7, "bottom": 30.0, "top": 28.0}


@pytest.mark.parametrize("delta,expected", [(0.0, False), (1.9, False), (-1.9, False), (2.0, True), (-2.0, True),
                                            (7.0, True)])
def test_humidity_has_its_own_limit_of_2_percent(delta, expected):
    assert worth_recording(LAST, {**LAST, "humidity": 60.0 + delta}, 1.0, 2.0) is expected


def test_the_two_limits_do_not_borrow_from_each_other():
    assert worth_recording(LAST, {**LAST, "humidity": 61.5}, 1.0, 2.0) is False   # 1.5 %RH is under its limit
    assert worth_recording(LAST, {**LAST, "bottom": 30.5}, 1.0, 2.0) is False
    assert worth_recording(LAST, {**LAST, "bottom": 31.0}, 1.0, 2.0) is True
    assert worth_recording(LAST, {**LAST, "humidity": 61.5}, 1.0) is True         # no RH limit given: the same as °C


def test_humidity_starting_or_stopping_is_a_change():
    temps = {k: v for k, v in LAST.items() if k != "humidity"}
    assert worth_recording(LAST, temps, 1.0, 2.0) is True
    assert worth_recording(temps, LAST, 1.0, 2.0) is True
    assert worth_recording(temps, dict(temps), 1.0, 2.0) is False


# ---------------- the recorder and the store ----------------
@pytest.fixture
def steady(tmp_path):
    e = Env(tmp_path, TEMP_HISTORY_MIN_CHANGE_C=1.0, TEMP_HISTORY_MIN_CHANGE_RH=2.0)
    e.zones.humidity_value = 60.0
    yield e
    for s in e.stores:
        s.close()


def humidities(e):
    return [r["humidity"] for r in e.store.temp_history_since(0)]


def test_a_humidity_change_of_2_percent_makes_a_record_and_a_smaller_one_does_not(steady):
    assert sample(steady) is not None
    steady.zones.humidity_value = 61.9
    assert sample(steady) is None
    steady.zones.humidity_value = 62.0
    assert sample(steady) is not None
    assert humidities(steady) == [60.0, 62.0]


def test_a_record_holds_the_current_humidity_even_when_a_temperature_made_it(steady):
    sample(steady)
    steady.zones.humidity_value = 60.8
    assert sample(steady, bottom=32.0) is not None
    assert humidities(steady) == [60.0, 60.8]


def test_the_humidity_is_uploaded_with_the_record_and_absent_when_the_sensor_is_down(steady):
    sample(steady)
    steady.zones.humidity_value = None                      # the DHT22 failed, the temperatures still read
    assert sample(steady) is not None                       # a sensor stopping is a change that must show
    steady.settle()
    first, second = (steady.base.data["temp_history"][r["rid"]] for r in steady.store.temp_history_since(0))
    assert first["humidity"] == 60.0
    assert "humidity" not in second                         # no key at all, not null and not 0


def test_a_failed_top_sensor_with_no_temperature_anywhere_records_nothing_for_humidity_alone(steady):
    steady.zones.values = {"bottom": None, "middle": None, "top": None}
    assert sample(steady) is None


def test_databases_from_before_humidity_gain_the_column_and_keep_their_records(tmp_path):
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE temp_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT, rid TEXT UNIQUE, boot_id TEXT NOT NULL, mono REAL NOT NULL,
            wall REAL NOT NULL, clock_synced INTEGER NOT NULL, t_ms INTEGER, bottom REAL, middle REAL, top REAL,
            gap INTEGER NOT NULL DEFAULT 0, synced INTEGER NOT NULL DEFAULT 0);
        INSERT INTO temp_history(rid, boot_id, mono, wall, clock_synced, t_ms, bottom, middle, top, gap, synced)
        VALUES ('1800000000000', 'b', 1, 1.8e9, 1, 1800000000000, 30, 29, 28, 0, 1);
    """)
    conn.commit()
    conn.close()
    store = TaskStore(path, "b2", lambda: True, wall=lambda: 1_800_000_100.0)
    try:
        rows = store.temp_history_since(0)
        assert [(r["bottom"], r["humidity"]) for r in rows] == [(30.0, None)]
        store.add_temp_reading({"bottom": 31.0, "humidity": 58.0}, 5.0)
        assert [r["humidity"] for r in store.temp_history_since(0)] == [None, 58.0]
        assert payload(store.temp_history_since(0)[0]) == {"t": 1_800_000_000_000, "bottom": 30.0, "middle": 29.0,
                                                           "top": 28.0}
    finally:
        store.close()
