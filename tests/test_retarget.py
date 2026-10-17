"""The saved target is the one target there is: a running drying task follows it, tolerance and dwell stay frozen."""

import pytest

import config
from task_manager import retarget_fields
from tests.conftest import Rig
from tests.test_warmup_session import drive_to_drying


def event_types(rig):
    return [e["type"] for e in rig.store.pending_events(1000)]


def set_target(rig, value, stamp):
    return rig.tm.apply_settings({"manual_target_temp_c": value, "updated_at": stamp})


# ---------------- the pure part ----------------
def test_retarget_moves_the_target_and_every_floor_by_the_same_distance():
    task = {"target_c": 55.0, "tolerance_c": 2.5, "floor_middle_c": 52.5, "floor_top_c": 48.5}
    assert retarget_fields(task, 47.0) == {"target_c": 47.0, "floor_middle_c": 44.5, "floor_top_c": 40.5}


def test_retarget_leaves_a_zone_without_a_floor_without_one():
    task = {"target_c": 55.0, "tolerance_c": 2.5, "floor_middle_c": None, "floor_top_c": None}
    assert retarget_fields(task, 50.0) == {"target_c": 50.0}


# ---------------- warm-up ----------------
@pytest.mark.parametrize("target", [45, 47.5, 50, 55])
def test_warmup_uses_the_saved_target_and_confirms_only_inside_its_band(zrig, target):
    set_target(zrig, target, 1)
    assert zrig.start(warmup=True)["result"] == "accepted"
    snap = zrig.tm.snapshot()
    floor = target - config.TARGET_TOLERANCE_C
    assert snap["target_temp_c"] == target
    assert {z["floor_c"] for z in snap["zones"].values()} == {floor}

    peak = 0.0
    for _ in range(400):
        zrig.run(1)
        peak = max(peak, zrig.oven.temp)
        if zrig.task()["status"] == "running":
            break
    t = zrig.task()
    assert t["status"] == "running" and t["target_c"] == target
    assert min(s.temp_c for s in zrig.zones.sensors.values()) >= floor   # every zone was in its band
    assert peak < target + 2   # the thermostat held the saved target, not some other temperature


def test_a_new_target_mid_warmup_moves_the_floors_and_restarts_the_hold(zrig):
    set_target(zrig, 45, 1)
    zrig.start(warmup=True)
    for _ in range(400):
        zrig.run(1)
        if zrig.tm.detector.in_band("bottom"):
            break
    assert zrig.tm.detector.in_band("bottom") and zrig.task()["status"] == "warming_up"

    changed_at = zrig.clock.wall
    assert set_target(zrig, 50, 2) == ["manual_target_temp_c"]
    t = zrig.task()
    assert (t["target_c"], t["floor_middle_c"], t["floor_top_c"]) == (50, 47.5, 47.5)
    snap = zrig.tm.snapshot()
    assert snap["target_temp_c"] == 50
    assert {z["floor_c"] for z in snap["zones"].values()} == {47.5}
    assert not any(z["in_band"] for z in snap["zones"].values())   # the earlier hold is not reused
    assert "task_target_changed" in event_types(zrig)

    zrig.run(2)
    assert zrig.out.heater.is_active   # 45 would already have been enough to switch it off
    t = drive_to_drying(zrig)
    assert t["drying_started_wall"] >= changed_at
    assert t["target_confirmed_wall"] - t["drying_started_wall"] >= config.DWELL_SECONDS
    assert min(s.temp_c for s in zrig.zones.sensors.values()) >= 47.5


def test_a_zones_own_allowance_survives_a_new_target(zrig, monkeypatch):
    monkeypatch.setattr(config, "ZONE_FLOOR_ALLOWANCE_C", {"middle": 0.0, "top": 4.0})
    zrig.start(warmup=True)   # target 55
    assert (zrig.task()["floor_middle_c"], zrig.task()["floor_top_c"]) == (52.5, 48.5)
    set_target(zrig, 47, 1)
    assert (zrig.task()["floor_middle_c"], zrig.task()["floor_top_c"]) == (44.5, 40.5)


# ---------------- drying ----------------
def test_lowering_the_target_mid_drying_switches_the_heater_off_until_the_new_band(rig):
    rig.start(warmup=True)
    drive_to_drying(rig)
    set_target(rig, 47, 1)
    rig.run(2)
    assert rig.task()["target_c"] == 47 and rig.task()["status"] == "running"
    assert not rig.out.heater.is_active   # the chamber is at ~55, far above 47

    seen_heating = False
    for _ in range(150):
        rig.run(1)
        seen_heating = seen_heating or rig.out.heater.is_active
    assert seen_heating                    # regulating again, now around 47
    assert 43 <= rig.oven.temp <= 48
    assert rig.task()["status"] == "running"


def test_an_open_dip_is_closed_when_the_target_changes(rig):
    rig.start(warmup=True)
    drive_to_drying(rig)
    rig.oven.fixed = 50.0   # below the 52.5 band
    rig.run(10)
    assert rig.tm.dips.open and rig.store.open_excursion("T1")
    set_target(rig, 47, 1)
    assert not rig.tm.dips.open and rig.store.open_excursion("T1") is None
    assert "dip_closed" in event_types(rig)
    rig.run(10)
    assert not rig.tm.dips.open   # 50 is inside the new band (44.5)


def test_raising_the_target_mid_drying_is_not_a_dip_until_the_grace_runs_out(rig):
    set_target(rig, 45, 1)
    rig.start(warmup=True)
    drive_to_drying(rig)
    rig.tm.task = rig.store.update_task("T1", warmup_timeout_s=100)   # a short deadline for the test
    rig.oven.fixed = 44.0   # held below the new floor, so only the grace decides
    set_target(rig, 55, 2)
    rig.run(60)
    assert rig.store.list_excursions("T1")[1] == 0 and not rig.tm.dips.open
    rig.run(60)
    assert rig.store.list_excursions("T1")[1] == 1   # it never got there: reported as a real dip
    assert rig.task()["status"] == "running"


# ---------------- limits and restarts ----------------
def test_a_target_too_close_to_the_tasks_own_shutdown_is_not_followed(rig):
    rig.tm.apply_settings({"manual_target_temp_c": 47, "heater_cutoff_c": 50, "temp_max_c": 52, "updated_at": 1})
    rig.start(warmup=True)
    rig.run(5)
    assert rig.task()["max_temp_c"] == 52
    # the shutdown is raised after the start: a running task keeps the limit it started with
    assert rig.tm.apply_settings({"temp_max_c": 60, "updated_at": 2}) == ["temp_max_c"]
    assert rig.tm.apply_settings({"heater_cutoff_c": 58, "updated_at": 3}) == ["heater_cutoff_c"]
    assert set_target(rig, 55, 4) == ["manual_target_temp_c"]
    assert rig.tm.settings.get("manual_target_temp_c") == 55   # the setting itself is saved...
    assert rig.task()["target_c"] == 47                        # ...the task stays inside its own margin
    types = event_types(rig)
    assert "task_target_kept" in types and "task_target_changed" not in types


def test_a_restart_after_a_new_target_keeps_it(rig):
    rig.start(warmup=True)
    rig.run(20)
    set_target(rig, 50, 1)
    r2 = rig.reboot(downtime=5)
    r2.run(config.RECOVERY_OBSERVE_SECONDS + 2)
    t = r2.task()
    assert t["status"] == "warming_up" and t["target_c"] == 50
    assert r2.tm.snapshot()["target_temp_c"] == 50


def test_a_target_saved_while_the_task_is_recovering_applies_when_it_resumes(rig):
    rig.start(warmup=True)
    rig.run(20)
    r2 = rig.reboot(downtime=5)
    assert r2.task()["status"] == "recovering"
    set_target(r2, 52, 1)
    assert r2.task()["target_c"] == 52
    r2.run(config.RECOVERY_OBSERVE_SECONDS + 2)
    t = r2.task()
    assert t["status"] == "warming_up" and t["target_c"] == 52
    assert r2.tm.detector.floor("bottom") == 52 - config.TARGET_TOLERANCE_C
