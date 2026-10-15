"""Three-zone warm-up, faults and cooldown, end to end through the task manager on a simulated oven."""

import config
from tests.conftest import Rig
from tests.test_warmup_session import drive_to_drying

IDS = {"bottom": config.SENSOR_ID, "middle": "28-0000000000a1", "top": "28-0000000000a2"}
PLACEHOLDER_IDS = {"bottom": config.SENSOR_ID, "middle": "28-REPLACE-MIDDLE", "top": "28-REPLACE-TOP"}


def pin(rig, bottom=54.0, middle=54.0, top=54.0):
    """Hold every sensor at a fixed reading, so the thermostat plays no part."""
    rig.oven.fixed = bottom
    rig.oven.zone_fixed = {"middle": middle, "top": top}


def event_types(rig):
    return [e["type"] for e in rig.store.pending_events(1000)]


# ---------------- warm-up ----------------
def test_warmup_waits_for_a_top_that_lags_the_bottom(zrig):
    zrig.oven.zone_lag["top"] = 60.0
    assert zrig.start(warmup=True)["result"] == "accepted"
    for _ in range(300):
        zrig.run(1)
        if zrig.tm.detector.in_band("bottom"):
            break
    assert zrig.tm.detector.in_band("bottom")
    zrig.run(int(config.DWELL_SECONDS) + 10)           # the bottom alone would have confirmed by now
    t = zrig.task()
    assert t["status"] == "warming_up" and not zrig.tm.detector.in_band("top")
    zones = zrig.tm.snapshot()["zones"]
    assert zones["bottom"]["in_band"] is True and zones["top"]["in_band"] is False
    assert zones["top"]["smoothed_temp_c"] < zones["top"]["floor_c"]

    t = drive_to_drying(zrig, limit=1200)
    assert t["target_confirmed_wall"] - t["drying_started_wall"] >= config.DWELL_SECONDS
    order = [e["data"]["zone"] for e in zrig.store.pending_events(1000) if e["type"] == "warmup_zone_reached"]
    assert order == ["bottom", "middle", "top"]        # the heat climbs the box


def test_drying_clock_is_backdated_to_the_last_zone_to_qualify(zrig):
    pin(zrig, top=30.0)
    zrig.start(warmup=True)
    zrig.run(60)
    assert zrig.task()["status"] == "warming_up"        # bottom and middle have been warm the whole time
    zrig.oven.zone_fixed["top"] = 54.0
    resumed = zrig.clock.wall
    t = drive_to_drying(zrig)
    assert resumed <= t["drying_started_wall"] < resumed + 10   # the top's first full window, not the bottom's
    assert t["target_confirmed_wall"] - t["drying_started_wall"] >= config.DWELL_SECONDS


def test_a_top_that_never_warms_times_out_with_the_heater_off(zrig, monkeypatch):
    monkeypatch.setattr(config, "WARMUP_TIMEOUT_SECONDS", 120)
    pin(zrig, top=40.0)
    zrig.start(warmup=True)
    zrig.run(110)
    assert zrig.task()["status"] == "warming_up"
    zrig.run(20)
    t = zrig.task()
    assert (t["status"], t["failure_reason"]) == ("failed", "warmup_timeout")
    assert not zrig.out.heater.is_active and t["drying_started_wall"] is None


def test_report_lists_each_zone_with_its_frozen_floor(zrig):
    zrig.start(warmup=True)
    zrig.run(10)
    snap = zrig.tm.snapshot()
    assert set(snap["zones"]) == {"bottom", "middle", "top"}
    assert {z["floor_c"] for z in snap["zones"].values()} == {config.MANUAL_TARGET_TEMP_C - config.TARGET_TOLERANCE_C}
    assert snap["bypass_zones"] is None
    assert snap["smoothed_temp_c"] == snap["zones"]["bottom"]["smoothed_temp_c"]  # unchanged meaning


def test_a_zone_allowance_lowers_only_that_zones_floor_and_is_frozen(zrig, monkeypatch):
    monkeypatch.setattr(config, "ZONE_FLOOR_ALLOWANCE_C", {"middle": 0.0, "top": 4.0})
    pin(zrig, top=49.0)                                 # 3.5 below the shared floor
    zrig.start(warmup=True)
    t = zrig.task()
    assert (t["floor_middle_c"], t["floor_top_c"]) == (52.5, 48.5)
    monkeypatch.setattr(config, "ZONE_FLOOR_ALLOWANCE_C", {"middle": 0.0, "top": 20.0})  # later edits change nothing
    drive_to_drying(zrig)
    assert zrig.task()["floor_top_c"] == 48.5


def test_a_task_from_older_firmware_without_floors_warms_on_the_bottom_alone(tmp_path):
    r = Rig(tmp_path, zoned=True)
    pin(r, top=30.0)
    r.start(warmup=True)
    r.run(10)
    r.store.update_task("T1", floor_middle_c=None, floor_top_c=None)   # as saved before the zones existed
    r2 = r.reboot(downtime=5)
    r2.run(config.RECOVERY_OBSERVE_SECONDS + 2)
    assert r2.task()["status"] == "warming_up"
    assert drive_to_drying(r2)["status"] == "running"   # the cold top is not waited for


def test_a_restart_mid_warmup_restarts_every_dwell(tmp_path):
    r = Rig(tmp_path, zoned=True)
    pin(r)
    r.start(warmup=True, duration_min=30)
    r.run(50)
    before = r.task()
    assert before["status"] == "warming_up"
    r2 = r.reboot(downtime=5)
    r2.run(config.RECOVERY_OBSERVE_SECONDS + 2)
    assert r2.task()["status"] == "warming_up"
    r2.run(40)
    assert r2.task()["status"] == "warming_up"           # the earlier dwell is not reused
    t = drive_to_drying(r2)
    assert t["drying_started_wall"] > before["started_wall"] + 50
    assert t["floor_top_c"] == before["floor_top_c"]


# ---------------- start gate ----------------
def test_start_needs_a_bypass_when_a_zone_sensor_is_down(zrig):
    zrig.oven.zone_broken.add("top")
    zrig.run(10)
    assert zrig.start(warmup=True)["reason"] == "zone_sensor_unavailable"
    assert zrig.task("T1") is None

    ack = zrig.start(task_id="T2", warmup=True, bypass_zones="top")
    assert ack["result"] == "accepted"
    t = zrig.task("T2")
    assert t["bypass_zones"] == "top"
    for _ in range(400):
        zrig.run(1)
        if zrig.task("T2")["status"] == "running":
            break
    assert zrig.task("T2")["status"] == "running"        # warmed on the bottom and middle alone
    snap = zrig.tm.snapshot()
    assert snap["bypass_zones"] == "top" and snap["zones"]["top"]["bypassed"] is True
    assert "zone_sensor_fault" not in event_types(zrig)  # the operator was warned at start


def test_a_bypassed_zone_is_still_reported_but_not_waited_for(zrig):
    pin(zrig, top=30.0)                                  # healthy, but cold
    assert zrig.start(warmup=True, bypass_zones="top")["result"] == "accepted"
    assert drive_to_drying(zrig)["status"] == "running"
    zrig.run(12)                                         # the detector starts fresh when drying begins
    zones = zrig.tm.snapshot()["zones"]
    assert zones["top"]["bypassed"] is True and zones["top"]["in_band"] is False
    assert zones["top"]["smoothed_temp_c"] == 30.0       # still read and logged


def test_bypass_zones_are_validated_and_the_bottom_can_never_be_bypassed(zrig):
    assert zrig.start(task_id="A", warmup=True, bypass_zones="bottom")["reason"] == "invalid_bypass_zones"
    assert zrig.start(task_id="B", warmup=True, bypass_zones="top,top")["reason"] == "invalid_bypass_zones"
    assert zrig.start(task_id="C", warmup=True, bypass_zones="middle,bottom")["reason"] == "invalid_bypass_zones"
    assert zrig.start(task_id="D", warmup=True, bypass_zones=5)["reason"] == "invalid_bypass_zones"
    assert zrig.start(task_id="E", warmup=True, bypass_zones="top, middle")["result"] == "accepted"
    assert zrig.task("E")["bypass_zones"] == "middle,top"   # normalised, bottom to top


def test_the_bottom_sensor_down_blocks_the_start_even_with_every_zone_bypassed(zrig):
    zrig.oven.broken = True
    zrig.run(10)
    assert zrig.start(warmup=True, bypass_zones="middle,top")["reason"] == "sensor_unavailable"


def test_placeholder_sensor_ids_block_every_start_and_cannot_be_bypassed(tmp_path):
    r = Rig(tmp_path, zoned=True, zone_ids=PLACEHOLDER_IDS)   # the middle and top are placeholders
    assert "config_warning" in event_types(r)
    assert r.start(warmup=True)["reason"] == "zones_unconfigured"
    assert r.start(task_id="T2", warmup=True, bypass_zones="middle,top")["reason"] == "zones_unconfigured"
    assert r.task("T1") is None and r.task("T2") is None


def test_real_ids_let_a_start_through(tmp_path):
    r = Rig(tmp_path, zoned=True, zone_ids=IDS)
    assert event_types(r).count("config_warning") == 0
    assert r.start(warmup=True)["result"] == "accepted"


def test_a_hot_upper_zone_blocks_the_start_as_too_hot(zrig):
    zrig.oven.zone_fixed["top"] = config.TEMP_MAX_C - config.TEMP_RESUME_DROP + 1
    zrig.run(4)
    assert zrig.start(warmup=True)["reason"] == "too_hot"


# ---------------- faults ----------------
def test_a_zone_sensor_dropping_out_during_warmup_fails_the_task_safely(zrig):
    pin(zrig)
    zrig.start(warmup=True)
    zrig.run(10)
    zrig.oven.zone_broken.add("middle")
    zrig.run(15)
    t = zrig.task()
    assert (t["status"], t["failure_reason"]) == ("failed", "zone_sensor_failure")
    assert t["diagnostics"]["zone"] == "middle"
    assert not zrig.out.heater.is_active and t["drying_started_wall"] is None


def test_a_zone_sensor_dropping_out_while_drying_only_warns(zrig):
    zrig.start(warmup=True)
    drive_to_drying(zrig)
    zrig.oven.zone_broken.add("top")
    zrig.run(20)
    assert zrig.task()["status"] == "running"
    assert event_types(zrig).count("zone_sensor_fault") == 1     # logged once, not every tick
    assert zrig.tm.snapshot()["status"] == "running"
    zrig.oven.zone_broken.discard("top")
    zrig.run(10)
    assert zrig.task()["status"] == "running" and "zone_sensor_restored" in event_types(zrig)


def test_the_bottom_sensor_failing_is_still_a_sensor_failure(zrig):
    zrig.start(warmup=True)
    zrig.run(10)
    zrig.oven.broken = True
    zrig.run(10)
    t = zrig.task()
    assert (t["status"], t["failure_reason"]) == ("failed", "sensor_failure")
    assert not zrig.out.heater.is_active


def test_a_hot_middle_sensor_trips_over_temperature_even_if_the_bottom_is_fine(zrig):
    pin(zrig, bottom=50.0, middle=40.0)
    zrig.start(warmup=True)
    zrig.run(6)
    zrig.oven.zone_fixed["middle"] = config.TEMP_MAX_C + 0.5
    zrig.run(4)
    t = zrig.task()
    assert (t["status"], t["failure_reason"]) == ("failed", "over_temperature")
    assert t["diagnostics"]["zone_temps_c"]["middle"] == config.TEMP_MAX_C + 0.5
    assert not zrig.out.heater.is_active


# ---------------- cooldown ----------------
def test_cooldown_waits_for_the_hottest_zone(zrig):
    zrig.start(warmup=True)
    drive_to_drying(zrig)
    pin(zrig, bottom=30.0, middle=30.0, top=config.COOLDOWN_SAFE_TEMP_C + 5)   # the bottom is cool, the top is not
    assert zrig.cmd("cancel", "T1")["result"] == "accepted"
    zrig.run(30)
    assert zrig.tm.cooling_active() and zrig.out.fan.is_active and not zrig.out.heater.is_active
    zrig.oven.zone_fixed["top"] = config.COOLDOWN_SAFE_TEMP_C - 5
    zrig.run(6)
    assert not zrig.tm.cooling_active() and not zrig.out.fan.is_active


# ---------------- logs ----------------
def test_every_zone_is_logged_and_summarised_separately(zrig):
    zrig.oven.zone_drop["top"] = 2.0
    zrig.start(warmup=True)
    zrig.run(70)
    zrig.tm.shutdown()                                   # flushes the buffered samples
    for zone in ("bottom", "middle", "top"):
        rows = zrig.store.temp_log("T1", zone=zone)
        assert rows and all(r["zone"] == zone for r in rows)
    top, bottom = zrig.store.temp_log("T1", zone="top")[-1], zrig.store.temp_log("T1", zone="bottom")[-1]
    assert top["raw_c"] < bottom["raw_c"]
    zones = {m["zone"] for m in zrig.store.pending_temp_minutes()}
    assert zones == {"bottom", "middle", "top"}
