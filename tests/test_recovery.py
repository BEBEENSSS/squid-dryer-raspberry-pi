import pytest

import config
import recovery


def run_recovery(r):
    r.run(config.RECOVERY_OBSERVE_SECONDS + 2)
    return r.task()


@pytest.fixture
def running(rig):
    """A task 5 minutes into a 30 minute run, hot and steady."""
    rig.start(duration_min=30)
    rig.run(300)
    assert rig.task()["status"] == "running"
    return rig


# ---------------- end to end ----------------
def test_process_crash_resumes_with_saved_elapsed(running):
    before = running.task()["elapsed_s"]
    r = running.reboot(downtime=10, new_boot=False)
    assert r.task()["status"] == "recovering"
    assert not r.out.heater.is_active
    t = run_recovery(r)
    assert t["status"] == "running"
    # resumed from the checkpoint, not from zero, and downtime not counted
    assert t["elapsed_s"] == pytest.approx(before, abs=0.01)
    r.run(30)
    snap = r.tm.snapshot()
    assert snap["remaining_seconds"] < 1800 - before
    assert r.out.heater.is_active or r.out.fan.is_active


def test_reboot_small_fluctuation_resumes(running):
    ref = running.task()["last_temp_c"]
    r = running.reboot(downtime=60, temp=ref - 0.5)
    r.oven.fixed = ref - 0.5
    t = run_recovery(r)
    assert t["status"] == "running"
    types = [e["type"] for e in r.store.pending_events(500)]
    assert "task_resumed" in types and "task_completed" not in types


def test_power_loss_detected(running):
    ref = running.task()["last_temp_c"]
    r = running.reboot(downtime=900, temp=ref - 12)
    r.oven.fixed = ref - 12
    t = run_recovery(r)
    assert (t["status"], t["failure_reason"]) == ("failed", "possible_power_loss")
    assert t["diagnostics"]["temp_drop_c"] == pytest.approx(12, abs=0.1)
    assert t["diagnostics"]["boot_changed"] is True
    assert not r.out.heater.is_active
    r.run(120)
    assert not r.out.heater.is_active          # never restarts heating by itself
    assert r.task()["status"] == "failed"      # never reported as completed
    assert r.start(task_id="T2")["reason"] == "unacknowledged_failure"


def test_clean_stop_with_large_drop_is_not_called_power_loss(running):
    ref = running.task()["last_temp_c"]
    r = running.reboot(downtime=900, temp=ref - 12, clean=True)
    r.oven.fixed = ref - 12
    t = run_recovery(r)
    assert (t["status"], t["failure_reason"]) == ("failed", "interrupted_heat_lost")


def test_unsynced_clock_after_reboot_requires_recovery(running):
    ref = running.task()["last_temp_c"]
    r = running.reboot(downtime=60, temp=ref - 1, synced=False)
    r.oven.fixed = ref - 1
    r.run(config.RECOVERY_OBSERVE_SECONDS + 2)
    assert r.task()["status"] == "recovering"  # still waiting for NTP
    r.run(config.RECOVERY_CLOCK_WAIT_SECONDS)
    t = r.task()
    assert (t["status"], t["failure_reason"]) == ("recovery_required", "elapsed_time_unverifiable")
    assert not r.out.heater.is_active


def test_clock_sync_arriving_during_wait_allows_resume(running):
    ref = running.task()["last_temp_c"]
    r = running.reboot(downtime=30, temp=ref - 1, synced=False)
    r.oven.fixed = ref - 1
    r.run(30)
    r.clock.synced = True
    r.run(2)
    assert r.task()["status"] == "running"


def test_sensor_missing_at_boot_requires_recovery(running):
    r = running.reboot(downtime=30)
    r.oven.broken = True
    t = run_recovery(r)
    assert (t["status"], t["failure_reason"]) == ("recovery_required", "sensor_unavailable")


def test_uncertain_drop_requires_recovery_then_resume(running):
    ref = running.task()["last_temp_c"]
    before = running.task()["elapsed_s"]
    r = running.reboot(downtime=60, temp=ref - 5)
    r.oven.fixed = ref - 5
    t = run_recovery(r)
    assert (t["status"], t["failure_reason"]) == ("recovery_required", "temperature_drop_uncertain")
    r.run(60)
    assert not r.out.heater.is_active and not r.out.fan.is_active
    # explicit confirmation continues with the saved elapsed time
    r.oven.fixed = None
    r.oven.temp = 40
    r.run(4)
    assert r.cmd("resume", "T1")["result"] == "accepted"
    r.run(3)
    t = r.task()
    assert t["status"] == "running" and t["elapsed_s"] == pytest.approx(before, abs=3.5)


def test_recovery_required_survives_another_reboot(running):
    ref = running.task()["last_temp_c"]
    r = running.reboot(downtime=60, temp=ref - 5)
    r.oven.fixed = ref - 5
    run_recovery(r)
    r2 = r.reboot(downtime=30, temp=ref - 5)
    assert r2.task()["status"] == "recovery_required"
    r2.run(60)
    assert r2.task()["status"] == "recovery_required"
    assert r2.cmd("abort", "T1")["result"] == "accepted"
    assert r2.task()["status"] == "cancelled"


def test_long_gap_requires_recovery(running):
    ref = running.task()["last_temp_c"]
    r = running.reboot(downtime=config.RESUME_MAX_GAP_SECONDS + 60, temp=ref - 1)
    r.oven.fixed = ref - 1
    assert run_recovery(r)["failure_reason"] == "interruption_too_long"


def test_crash_during_recovery_keeps_original_checkpoint(running):
    ref = running.task()["last_temp_c"]
    r = running.reboot(downtime=200, temp=ref - 1)
    r.oven.fixed = ref - 1
    r.run(5)
    r2 = r.reboot(downtime=200, temp=ref - 1)  # gap from the original checkpoint is now > 400 s
    r2.oven.fixed = ref - 1
    assert run_recovery(r2)["failure_reason"] == "interruption_too_long"


def test_task_finished_just_before_crash_completes(tmp_path):
    from tests.conftest import Rig
    r = Rig(tmp_path)
    r.start(duration_min=1)
    r.run(55)
    # pretend the last checkpoint already proved the full duration
    r.store.update_task("T1", elapsed_s=60.0)
    r2 = r.reboot(downtime=600, temp=25)
    assert run_recovery(r2)["status"] == "completed"


def test_interrupted_task_never_completes_by_downtime(running):
    """Even a reboot longer than the remaining duration must not count as drying time."""
    ref = running.task()["last_temp_c"]
    r = running.reboot(downtime=4 * 3600, temp=ref - 0.5)
    r.oven.fixed = ref - 0.5
    t = run_recovery(r)
    assert t["status"] == "recovery_required"
    assert t["status"] != "completed"


# ---------------- decision table ----------------
TASK = {"last_temp_c": 50.0, "elapsed_s": 100.0, "duration_s": 600.0}


@pytest.mark.parametrize("current,gap,boot,clean,expected", [
    (50.0, 30, True, False, ("resume", "checks_passed")),
    (50.0 - config.TEMP_DROP_TOLERANCE_C, 30, True, False, ("resume", "checks_passed")),
    (50.0 - config.TEMP_DROP_TOLERANCE_C - 0.1, 30, True, False,
     ("recovery_required", "temperature_drop_uncertain")),
    (50.0 - config.TEMP_DROP_FAIL_C, 30, True, False, ("failed", "possible_power_loss")),
    (50.0 - config.TEMP_DROP_FAIL_C, None, True, False, ("failed", "possible_power_loss")),
    (50.0 - config.TEMP_DROP_FAIL_C, 30, False, False, ("failed", "interrupted_heat_lost")),
    (50.0, None, True, False, ("recovery_required", "elapsed_time_unverifiable")),
    (50.0, config.RESUME_MAX_GAP_SECONDS + 1, True, False, ("recovery_required", "interruption_too_long")),
    (55.0, 30, True, False, ("resume", "checks_passed")),
    (config.TEMP_MAX_C, 30, True, False, ("recovery_required", "over_temperature")),
])
def test_decision_table(current, gap, boot, clean, expected):
    d = recovery.decide_recovery(TASK, [current] * 3, True, gap, boot, clean)
    assert (d.action, d.reason) == expected


def test_decision_uses_the_tasks_own_over_temperature_limit():
    assert recovery.decide_recovery(TASK, [50.0] * 3, True, 30, True, False).reason == "checks_passed"
    d = recovery.decide_recovery(TASK, [50.0] * 3, True, 30, True, False, max_temp_c=50.0)
    assert (d.action, d.reason) == ("recovery_required", "over_temperature")


def test_decision_needs_enough_readings_and_reference():
    assert recovery.decide_recovery(TASK, [50.0], True, 30, True, False).reason == "sensor_unavailable"
    assert recovery.decide_recovery(TASK, [50.0] * 3, False, 30, True, False).reason == "sensor_unavailable"
    no_ref = dict(TASK, last_temp_c=None)
    assert recovery.decide_recovery(no_ref, [50.0] * 3, True, 30, True, False).reason == "no_reference_temp"


def test_gap_measurement():
    t = {"checkpoint_boot_id": "a", "checkpoint_mono": 100.0, "checkpoint_wall": 1000.0,
         "checkpoint_clock_synced": 1}
    assert recovery.interruption_gap(t, "a", 130.0, 9999.0, False) == 30.0
    assert recovery.interruption_gap(t, "b", 5.0, 1060.0, True) == 60.0
    assert recovery.interruption_gap(t, "b", 5.0, 1060.0, False) is None
    assert recovery.interruption_gap(dict(t, checkpoint_clock_synced=0), "b", 5.0, 1060.0, True) is None
    assert recovery.interruption_gap(t, "b", 5.0, 900.0, True) is None  # clock went backwards
