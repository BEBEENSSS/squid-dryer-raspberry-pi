"""Warm-up -> drying hand-over, end to end through the task manager on a simulated oven."""

import pytest

import config
from tests.conftest import FakeBase, Rig
from tests.test_sync import make_sync


def drive_to_drying(rig, limit=400):
    for _ in range(limit):
        rig.run(1)
        if rig.task()["status"] == "running":
            return rig.task()
    raise AssertionError(f"still {rig.task()['status']} after {limit} s")


def test_drying_clock_waits_for_the_target(rig):
    assert rig.start(warmup=True, duration_min=10)["result"] == "accepted"
    rig.run(30)
    t = rig.task()
    assert t["status"] == "warming_up" and t["started_wall"] and t["drying_started_wall"] is None
    assert rig.tm.elapsed() == 0 and t["elapsed_s"] == 0
    assert rig.out.heater.is_active and rig.out.fan.is_active
    snap = rig.tm.snapshot()
    assert snap["status"] == "warming_up" and snap["drying_started_at"] is None
    assert snap["progress_percent"] == 0 and snap["target_temp_c"] == config.MANUAL_TARGET_TEMP_C
    assert 29 <= snap["warmup_seconds"] <= 31  # live warm-up time

    t = drive_to_drying(rig)
    assert t["started_wall"] < t["drying_started_wall"] < t["target_confirmed_wall"]
    held = t["target_confirmed_wall"] - t["drying_started_wall"]
    assert config.DWELL_SECONDS <= held < config.DWELL_SECONDS + 4
    # the drying clock includes the dwell: it is measured from the backdated start
    assert abs(rig.tm.elapsed() - (rig.clock.wall - t["drying_started_wall"])) < 1.5
    snap = rig.tm.snapshot()
    assert snap["warmup_seconds"] == round(t["drying_started_wall"] - t["started_wall"], 1)
    assert snap["drying_started_at"] == int(t["drying_started_wall"] * 1000)
    assert snap["target_confirmed_at"] == int(t["target_confirmed_wall"] * 1000)


def test_duration_is_measured_from_drying_start(rig):
    rig.start(warmup=True, duration_min=2)
    rig.run(125)  # longer than the whole duration, but most of it was warm-up
    assert rig.task()["status"] in ("warming_up", "running")
    for _ in range(400):
        rig.run(1)
        if rig.task()["status"] == "completed":
            break
    t = rig.task()
    assert t["status"] == "completed"
    assert abs((t["ended_wall"] - t["drying_started_wall"]) - 120) < 3
    assert t["ended_wall"] - t["started_wall"] > 120 + 30  # warm-up time is on top, never inside it


def test_warmup_timeout_faults_and_turns_the_heater_off(rig, monkeypatch):
    monkeypatch.setattr(config, "WARMUP_TIMEOUT_SECONDS", 90)
    rig.oven.fixed = 40.0  # never reaches the band
    rig.start(warmup=True)
    rig.run(85)
    assert rig.task()["status"] == "warming_up" and rig.out.heater.is_active
    rig.run(10)
    t = rig.task()
    assert (t["status"], t["failure_reason"]) == ("failed", "warmup_timeout")
    assert t["drying_started_wall"] is None and not rig.out.heater.is_active
    assert rig.tm.machine_status() == "failed"


def test_session_settings_are_frozen_at_start(rig, monkeypatch):
    rig.start(warmup=True)
    monkeypatch.setattr(config, "TARGET_TOLERANCE_C", 10.0)
    monkeypatch.setattr(config, "DWELL_SECONDS", 1)
    # the saved target is copied into the task at start: a new target waits for the next task
    assert rig.tm.apply_settings({"manual_target_temp_c": 46, "updated_at": 1}) == ["manual_target_temp_c"]
    t = drive_to_drying(rig)
    assert (t["tolerance_c"], t["dwell_s"], t["target_c"]) == (2.5, 60, 55.0)
    assert t["target_confirmed_wall"] - t["drying_started_wall"] >= 60
    assert rig.tm.snapshot()["dwell_seconds"] == 60


def test_sensor_dropout_during_warmup_faults(rig):
    rig.start(warmup=True)
    rig.run(20)
    rig.oven.broken = True
    rig.run(10)
    t = rig.task()
    assert (t["status"], t["failure_reason"]) == ("failed", "sensor_failure")
    assert t["drying_started_wall"] is None and not rig.out.heater.is_active


def test_a_short_dropout_restarts_the_dwell_instead_of_counting_as_reached(rig):
    rig.oven.fixed = 54.0  # inside the band from the first reading
    rig.start(warmup=True)
    rig.run(40)  # 30 s into the dwell
    rig.oven.broken = True
    rig.run(4)   # two failed reads: below the fault limit, but samples are missing
    rig.oven.broken = False
    resumed_at = rig.clock.wall
    rig.run(46)
    assert rig.task()["status"] == "warming_up"  # an uninterrupted dwell would have finished by now
    t = drive_to_drying(rig)
    assert t["drying_started_wall"] > resumed_at
    assert t["target_confirmed_wall"] - t["drying_started_wall"] >= config.DWELL_SECONDS


def test_over_temperature_during_warmup_faults(rig):
    rig.start(warmup=True)
    rig.run(5)
    rig.oven.fixed = config.TEMP_MAX_C + 0.5
    rig.run(3)
    t = rig.task()
    assert (t["status"], t["failure_reason"]) == ("failed", "over_temperature")
    assert not rig.out.heater.is_active


def test_reboot_mid_warmup_resumes_warming_with_a_fresh_dwell(tmp_path):
    r = Rig(tmp_path)
    r.oven.fixed = 54.0
    r.start(warmup=True, duration_min=30)
    r.run(50)  # most of a dwell, not all of it
    before = r.task()
    assert before["status"] == "warming_up"

    r2 = r.reboot(downtime=5)
    r2.run(config.RECOVERY_OBSERVE_SECONDS + 2)
    t = r2.task()
    assert t["status"] == "warming_up" and t["drying_started_wall"] is None
    assert t["started_wall"] == before["started_wall"]       # same session
    assert t["warmup_elapsed_s"] >= 40                        # the timeout keeps counting
    r2.run(40)
    assert r2.task()["status"] == "warming_up"                # the earlier dwell is not reused
    t = drive_to_drying(r2)
    assert t["drying_started_wall"] > before["started_wall"] + 50
    assert t["target_confirmed_wall"] - t["drying_started_wall"] >= config.DWELL_SECONDS


def test_reboot_mid_drying_resumes_with_the_original_drying_start(tmp_path):
    r = Rig(tmp_path)
    r.start(warmup=True, duration_min=30)
    drive_to_drying(r)
    r.run(60)
    before = r.task()

    r2 = r.reboot(downtime=30, temp=r.oven.temp)
    r2.run(config.RECOVERY_OBSERVE_SECONDS + 2)
    t = r2.task()
    assert t["status"] == "running"
    assert t["drying_started_wall"] == before["drying_started_wall"]
    assert t["target_confirmed_wall"] == before["target_confirmed_wall"]
    assert r2.tm.snapshot()["drying_started_at"] == int(before["drying_started_wall"] * 1000)


def test_dip_is_logged_and_warns_without_stopping_the_timer(rig, monkeypatch):
    monkeypatch.setattr(config, "DIP_WARNING_LIMIT_SECONDS", 30)
    rig.start(warmup=True, duration_min=30)
    drive_to_drying(rig)
    rig.run(20)
    rig.oven.fixed = 45.0
    rig.run(20)
    assert rig.store.open_excursion("T1") is not None and not rig.task()["dip_warning"]
    elapsed = rig.tm.elapsed()
    rig.run(40)
    t = rig.task()
    assert t["status"] == "running" and t["dip_warning"] == 1
    assert rig.tm.elapsed() > elapsed + 39               # the drying timer never paused
    rig.oven.fixed = 54.0
    rig.run(20)
    dips, total = rig.store.list_excursions("T1")
    assert total == 1 and dips[0]["end_wall"] is not None and dips[0]["min_c"] == 45.0
    snap = rig.tm.snapshot()
    assert snap["dip_warning"] is True and snap["dips"][0]["min_temp_c"] == 45.0
    assert rig.store.open_excursion("T1") is None


def test_a_dip_in_progress_survives_a_restart(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DIP_WARNING_LIMIT_SECONDS", 3600)
    r = Rig(tmp_path)
    r.start(warmup=True, duration_min=30)
    drive_to_drying(r)
    r.oven.fixed = 45.0
    r.run(30)
    opened = r.store.open_excursion("T1")
    assert opened is not None

    r2 = r.reboot(downtime=10)
    r2.run(config.RECOVERY_OBSERVE_SECONDS + 2)
    assert r2.task()["status"] == "running"
    r2.oven.fixed = 54.0
    r2.run(30)
    dips, total = r2.store.list_excursions("T1")
    assert total == 1 and dips[0]["start_wall"] == opened["start_wall"] and dips[0]["end_wall"] is not None


def test_warns_when_the_band_is_narrower_than_the_thermostat_swing(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "TARGET_TOLERANCE_C", 1.0)
    r = Rig(tmp_path)
    assert "config_warning" in [e["type"] for e in r.store.pending_events(100)]


def test_every_sample_is_logged_with_raw_and_smoothed_values(rig):
    rig.start(warmup=True)
    drive_to_drying(rig)
    rig.run(30)
    rig.tm.shutdown()  # flushes the buffered samples
    rows = rig.store.temp_log("T1")
    assert {r["phase"] for r in rows} == {"warming_up", "drying"}
    assert all(r["raw_c"] is not None and r["smoothed_c"] is not None for r in rows)
    gaps = [b["wall"] - a["wall"] for a, b in zip(rows, rows[1:])]
    assert all(abs(g - config.SENSOR_INTERVAL) < 1.1 for g in gaps)
    minutes = rig.store.pending_temp_minutes()
    assert sum(m["n"] for m in minutes) == len(rows)


def test_minute_summaries_sync_to_firebase_once(rig):
    base = FakeBase()
    sync, _q = make_sync(rig, base)
    rig.start(warmup=True)
    rig.run(70)
    assert sync.sync_once()
    logs = base.data["task_logs"]["T1"]
    assert logs and all(len(k) == 8 for k in logs)
    entry = next(iter(logs.values()))
    assert set(entry) == {"t", "avg", "min", "max", "raw", "n"} and entry["min"] <= entry["avg"] <= entry["max"]
    assert rig.store.pending_temp_minutes() == []


def test_invalid_session_settings_never_drive_the_heater(rig):
    rig.start(warmup=True)
    rig.run(10)
    assert rig.out.heater.is_active
    rig.tm.task = rig.store.update_task("T1", tolerance_c=None)
    rig.run(2)
    t = rig.task()
    assert (t["status"], t["failure_reason"]) == ("failed", "invalid_session")
    assert not rig.out.heater.is_active


def test_exception_in_the_control_loop_forces_the_heater_off(rig, monkeypatch):
    rig.start(warmup=True)
    rig.run(10)
    assert rig.out.heater.is_active

    def boom(now):
        raise RuntimeError("bug")

    monkeypatch.setattr(rig.tm, "_tick_warming", boom)
    with pytest.raises(RuntimeError):
        rig.tm.tick()
    assert not rig.out.heater.is_active
    assert "controller_error" in [e["type"] for e in rig.store.pending_events(100)]


def test_batches_cannot_be_added_while_warming_but_cancel_works(rig):
    rig.start(warmup=True)
    rig.run(5)
    assert rig.cmd("add_batch", "T1", batch_id="B1", duration_minutes=5, row_levels="3")["reason"] == "task_not_running"
    assert rig.cmd("complete_batch", "T1", row_levels="2")["reason"] == "not_allowed_while_warming_up"
    assert rig.cmd("cancel", "T1")["result"] == "accepted"
    assert rig.task()["status"] == "cancelled" and not rig.out.heater.is_active


def test_mode_is_recorded_and_validated(rig):
    assert rig.start(task_id="T2", mode="bad")["reason"] == "invalid_mode"
    assert rig.start(mode="experiment")["result"] == "accepted"
    assert rig.task()["mode"] == "experiment" and rig.tm.snapshot()["mode"] == "experiment"


def test_task_from_older_firmware_keeps_its_clock_and_gets_a_snapshot(tmp_path):
    r = Rig(tmp_path)
    r.start()
    r.run(10)
    r.store.update_task("T1", target_c=None, tolerance_c=None, drying_started_wall=None)  # as before warm-up existed
    r2 = r.reboot(downtime=10, temp=r.oven.temp)
    r2.run(config.RECOVERY_OBSERVE_SECONDS + 2)
    t = r2.task()
    assert t["target_c"] == config.MANUAL_TARGET_TEMP_C
    assert t["drying_started_wall"] == t["started_wall"]
    assert t["status"] == "running"
