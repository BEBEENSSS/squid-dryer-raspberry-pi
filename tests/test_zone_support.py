"""Calibration, sensor ids, the database migration, the simulator and the per-zone logs/state."""

import sqlite3

import calibration
import config
from hardware import SensorArray, make_reader, unconfigured_zones
from sim import SimOven
from snapshot import minute_entries, zone_state
from task_store import TaskStore
from tests.conftest import FakeBase
from tests.test_sync import make_sync

MIDDLE, TOP = "28-0000000000a1", "28-0000000000a2"
GOOD_ID = {"bottom": config.SENSOR_ID, "middle": MIDDLE, "top": TOP}
PLACEHOLDER_IDS = {"bottom": config.SENSOR_ID, "middle": "28-REPLACE-MIDDLE", "top": "28-REPLACE-TOP"}


class FakeHeater:
    is_active = False


# ---------------- calibration ----------------
def test_offsets_apply_to_the_upper_sensors_and_never_to_sensor_1(tmp_path, monkeypatch):
    monkeypatch.setattr(calibration, "CAL_PATH", tmp_path / "cal.json")
    calibration.save_offset(MIDDLE, -0.4, "sensor 1 (main)", 60)
    calibration.save_offset(config.SENSOR_ID, 3.0, "by mistake", 60)   # even a stray entry is ignored
    assert calibration.zone_offset(MIDDLE) == -0.4
    assert calibration.zone_offset(config.SENSOR_ID) == 0.0
    assert calibration.zone_offset(TOP) == 0.0                          # never calibrated


def test_an_unusable_calibration_file_means_no_correction(tmp_path, monkeypatch):
    cal = tmp_path / "cal.json"
    monkeypatch.setattr(calibration, "CAL_PATH", cal)
    assert calibration.zone_offset(MIDDLE) == 0.0                       # no file
    for text in ("{not json", "[1, 2]", '{"%s": {"offset_c": "warm"}}' % MIDDLE,
                 '{"%s": {"offset_c": NaN}}' % MIDDLE, '{"%s": {}}' % MIDDLE):
        cal.write_text(text)
        assert calibration.zone_offset(MIDDLE) == 0.0, text


def w1(tmp_path, sensor_id, reply):
    d = tmp_path / sensor_id
    d.mkdir()
    (d / "w1_slave").write_text(reply)


def reply(milli, crc="YES"):
    return f"72 01 4b 46 7f ff 0e 10 57 : crc=57 {crc}\n72 01 4b 46 7f ff 0e 10 57 t={milli}\n"


def test_the_reader_applies_the_offset_and_still_refuses_bad_reads(tmp_path):
    w1(tmp_path, MIDDLE, reply(23125))
    assert make_reader(MIDDLE, -0.4, base=tmp_path)() == 22.725
    assert make_reader(MIDDLE, base=tmp_path)() == 23.125
    w1(tmp_path, TOP, reply(23125, crc="NO"))
    assert make_reader(TOP, 1.0, base=tmp_path)() is None               # CRC failure
    assert make_reader("28-0000000000ff", 1.0, base=tmp_path)() is None  # not on the bus
    w1(tmp_path, "28-0000000000a3", reply(85000))
    assert make_reader("28-0000000000a3", -40.0, base=tmp_path)() is None  # power-on default is never a reading


# ---------------- sensor ids ----------------
def test_placeholder_malformed_and_shared_ids_are_unconfigured():
    assert unconfigured_zones(PLACEHOLDER_IDS) == ["middle", "top"]
    assert unconfigured_zones(GOOD_ID) == []
    assert unconfigured_zones({**GOOD_ID, "top": MIDDLE}) == ["middle", "top"]   # one sensor in two places
    assert unconfigured_zones({**GOOD_ID, "top": "28-0000000000A2"}) == ["top"]  # ids are lower-case hex
    assert unconfigured_zones({**GOOD_ID, "middle": None}) == ["middle"]


def test_the_array_reports_the_hottest_healthy_sensor_only_with_a_healthy_control(rig):
    from hardware import Sensor
    clock = lambda: rig.clock.mono  # noqa: E731
    reads = {"bottom": 40.0, "middle": 44.0, "top": 52.0}
    sensors = {z: Sensor(lambda z=z: reads[z], clock=clock) for z in reads}
    array = SensorArray(sensors, ids=GOOD_ID)
    for s in sensors.values():
        s.poll()
    assert array.hottest() == 52.0 and array.configured()
    assert array.temps() == {"bottom": 40.0, "middle": 44.0, "top": 52.0}
    reads["top"] = None                                                  # the hot one goes dark
    for _ in range(config.SENSOR_BAD_LIMIT):
        sensors["top"].poll()
    assert array.hottest() == 44.0 and not array.healthy("top")
    reads["bottom"] = None                                               # the control sensor goes dark
    for _ in range(config.SENSOR_BAD_LIMIT):
        sensors["bottom"].poll()
    assert array.hottest() is None


# ---------------- database migration ----------------
def test_an_existing_database_keeps_its_samples_and_gains_the_zone_columns(tmp_path):
    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.executescript("""
        CREATE TABLE temp_log (task_id TEXT NOT NULL, wall REAL NOT NULL, mono REAL NOT NULL,
            raw_c REAL NOT NULL, smoothed_c REAL NOT NULL, phase TEXT NOT NULL);
        CREATE TABLE temp_minutes (task_id TEXT NOT NULL, minute INTEGER NOT NULL, n INTEGER NOT NULL,
            sum_c REAL NOT NULL, min_c REAL NOT NULL, max_c REAL NOT NULL, raw_last REAL NOT NULL,
            uploaded INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (task_id, minute));
        INSERT INTO temp_log VALUES ('T1', 100.0, 5.0, 50.0, 50.0, 'drying');
        INSERT INTO temp_minutes VALUES ('T1', 7, 3, 150.0, 49.0, 51.0, 50.0, 1);
    """)
    old.commit()
    old.close()

    store = TaskStore(path, "boot", lambda: True)
    assert [r["zone"] for r in store.temp_log("T1")] == ["bottom"]
    row = store.temp_minutes("T1")[0]
    assert (row["zone"], row["minute"], row["n"], row["sum_c"]) == ("bottom", 7, 3, 150.0)
    assert store.pending_temp_minutes() == []                            # uploaded before, still uploaded

    # a row without a zone is the bottom sensor's, as every caller wrote it before there were three
    store.append_temp_samples([("T1", 421.0, 2.0, 55.0, 55.0, "drying"),
                               ("T1", 420.0, 1.0, 60.0, 60.0, "drying", "top")])
    pending = {m["zone"]: m for m in store.pending_temp_minutes()}
    assert set(pending) == {"bottom", "top"} and pending["bottom"]["n"] == 4 and pending["top"]["n"] == 1
    store.mark_temp_minutes_uploaded([("T1", 7, 4), ("T1", 7, 1, "top")])
    assert store.pending_temp_minutes() == []

    store.close()
    again = TaskStore(path, "boot", lambda: True)                        # opening it again changes nothing
    assert {(r["zone"], r["n"]) for r in again.temp_minutes("T1")} == {("bottom", 4), ("top", 1)}


# ---------------- minute entries and sync ----------------
def test_the_bottom_summary_stays_flat_and_the_others_nest():
    rows = [
        {"minute": 5, "zone": "bottom", "n": 30, "sum_c": 1500.0, "min_c": 49.0, "max_c": 51.0, "raw_last": 50.5},
        {"minute": 5, "zone": "top", "n": 30, "sum_c": 1350.0, "min_c": 44.0, "max_c": 46.0, "raw_last": 45.5},
        {"minute": 6, "n": 2, "sum_c": 100.0, "min_c": 49.0, "max_c": 51.0, "raw_last": 50.0},   # no zone: bottom
    ]
    out = minute_entries(rows)
    assert out["00000005"] == {"t": 300000, "avg": 50.0, "min": 49.0, "max": 51.0, "raw": 50.5, "n": 30,
                               "top": {"avg": 45.0, "min": 44.0, "max": 46.0, "raw": 45.5, "n": 30}}
    assert set(out["00000006"]) == {"t", "avg", "min", "max", "raw", "n"}


def test_minute_summaries_reach_firebase_with_every_zone_and_are_never_cut_short(zrig):
    zrig.oven.zone_drop["top"] = 3.0
    base = FakeBase()
    sync, _q = make_sync(zrig, base)
    zrig.start(warmup=True)
    zrig.run(70)
    assert sync.sync_once()
    logs = base.data["task_logs"]["T1"]
    assert len(logs) >= 2
    for entry in logs.values():
        assert set(entry) == {"t", "avg", "min", "max", "raw", "n", "middle", "top"}
        for zone in ("middle", "top"):
            assert set(entry[zone]) == {"avg", "min", "max", "raw", "n"}
    assert all(e["top"]["avg"] < e["avg"] for e in logs.values())        # the top ran cooler here
    assert zrig.store.pending_temp_minutes() == []

    # one zone gains a sample in a minute that was already sent: only its row is pending, yet the whole
    # minute is written again, so the other zones' data is not replaced by a partial entry
    key, entry = next(iter(logs.items()))
    minute = int(key)
    zrig.store.append_temp_samples([("T1", minute * 60.0 + 1, 1.0, 50.0, 50.0, "warming_up", "top")])
    assert [m["zone"] for m in zrig.store.pending_temp_minutes()] == ["top"]
    zrig.clock.advance(config.TEMP_LOG_UPLOAD_SECONDS + 1)
    assert sync.sync_once()
    again = base.data["task_logs"]["T1"][key]
    assert again["top"]["n"] == entry["top"]["n"] + 1
    assert again["avg"] == entry["avg"] and again["middle"] == entry["middle"]
    assert zrig.store.pending_temp_minutes() == []


def test_state_carries_the_upper_temperatures_and_their_health(zrig):
    zrig.run(4)
    state = zone_state(zrig.zones)
    assert state == {"temp_middle": zrig.oven.read_zone("middle"), "temp_top": zrig.oven.read_zone("top"),
                     "sensor_middle_ok": True, "sensor_top_ok": True, "zones_configured": True}
    zrig.oven.zone_broken.add("top")
    zrig.run(10)
    state = zone_state(zrig.zones)
    assert state["temp_top"] is None and state["sensor_top_ok"] is False   # None removes the key in Firebase
    assert state["temp_middle"] is not None and state["sensor_middle_ok"] is True


def test_state_says_when_the_ids_are_still_placeholders(tmp_path):
    from tests.conftest import Rig
    r = Rig(tmp_path, zoned=True, zone_ids=PLACEHOLDER_IDS)
    assert zone_state(r.zones)["zones_configured"] is False


def test_the_ids_in_config_py_are_real_or_no_task_can_ever_start():
    """Guards the deployed file: a placeholder or a duplicated id there rejects every start (zones_unconfigured)."""
    assert unconfigured_zones(config.ZONE_SENSOR_IDS) == []
    assert config.ZONE_SENSOR_IDS["bottom"] == config.SENSOR_ID   # sensor 1 is the control sensor


# ---------------- simulator ----------------
def test_the_simulated_upper_zones_trail_the_bottom_and_can_break():
    t = [0.0]
    oven = SimOven(mono=lambda: t[0])
    oven.heater = FakeHeater()
    oven.heater.is_active = True
    for _ in range(120):
        t[0] += 1
        bottom, middle, top = oven.read(), oven.read_zone("middle"), oven.read_zone("top")
    assert bottom > middle > top > oven.ambient                          # heat climbs the box
    for _ in range(1500):
        t[0] += 1
        bottom, middle, top = oven.read(), oven.read_zone("middle"), oven.read_zone("top")
    assert 0 < bottom - top < 0.1 * (bottom - oven.ambient) + 1          # settles a little cooler at the top
    oven.broken_zones.add("top")
    assert oven.read_zone("top") is None and oven.read_zone("middle") is not None
