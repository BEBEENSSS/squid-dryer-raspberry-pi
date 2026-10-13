import pytest

import config
from task_manager import TaskManager


# ---------------- start validation ----------------
def test_accept_and_persist_before_heating(rig, monkeypatch):
    seen = []
    real_on = rig.out.heater.on

    def on():
        seen.append(rig.store.get_task("T1")["status"])
        real_on()

    monkeypatch.setattr(rig.out.heater, "on", on)
    ack = rig.start()
    assert ack["result"] == "accepted"
    assert not rig.out.heater.is_active  # heater only switches in the control tick
    rig.run(3)
    assert rig.out.heater.is_active and rig.out.fan.is_active
    assert seen and all(s == "running" for s in seen)
    assert rig.task()["duration_s"] == 600


@pytest.mark.parametrize("field,value,reason", [
    ("task_id", "bad id!", "invalid_task_id"),
    ("task_id", None, "invalid_task_id"),
    ("weight_kg", 0, "invalid_weight"),
    ("weight_kg", config.MAX_WEIGHT_KG + 1, "invalid_weight"),
    ("weight_kg", True, "invalid_weight"),
    ("batch_count", 2.5, "invalid_batch_count"),
    ("batch_count", 0, "invalid_batch_count"),
    ("batch_count", config.MAX_BATCH_COUNT + 1, "invalid_batch_count"),
    ("row_levels", "9", "invalid_row_levels"),
    ("row_levels", "1,1", "invalid_row_levels"),
    ("row_levels", "1,x", "invalid_row_levels"),
    ("row_levels", 2, "invalid_row_levels"),
    ("duration_minutes", 0, "invalid_duration"),
    ("duration_minutes", "60", "invalid_duration"),
    ("command", "explode", "unknown_action"),
])
def test_rejects_invalid_fields(rig, field, value, reason):
    ack = rig.start(**{field: value})
    assert (ack["result"], ack["reason"]) == ("rejected", reason)
    assert not rig.out.heater.is_active
    assert rig.store.latest_task() is None


def test_optional_weight_and_multiple_rows(rig):
    ack = rig.start(weight_kg=None, row_levels="3, 1")
    assert ack["result"] == "accepted"
    t = rig.task()
    assert t["weight_kg"] is None and t["row_levels"] == "1,3"


def test_request_without_command_id_uses_timestamp_key(rig):
    req = {"command": "start", "task_id": "T9", "batch_count": 5, "row_levels": "1",
           "duration_minutes": 10, "requested_at": rig.clock.wall * 1000}
    assert rig.tm.handle_command(req)["result"] == "accepted"
    assert rig.tm.handle_command(dict(req)) is None  # same request read again
    assert rig.tm.handle_command({"command": "start", "task_id": "T9"}) is None  # no identity


def test_rejects_when_sensor_bad_or_hot(rig):
    rig.oven.broken = True
    rig.run(10)
    assert rig.start()["reason"] == "sensor_unavailable"
    rig.oven.broken = False
    rig.oven.fixed = config.TEMP_MAX_C - config.TEMP_RESUME_DROP
    rig.run(2)
    assert rig.start(task_id="T2")["reason"] == "too_hot"


def test_rejects_when_manual_heater_on(rig):
    rig.out.fan.on()
    rig.out.heater.on()
    assert rig.start()["reason"] == "heater_in_use"


def test_duplicates(rig):
    assert rig.start(command_id="A")["result"] == "accepted"
    # same command delivered again: silently ignored, no second ack
    assert rig.start(command_id="A") is None
    # new command, same task id
    ack = rig.start(command_id="B")
    assert (ack["result"], ack["reason"]) == ("duplicate", "duplicate_start")
    # different task while one is running
    assert rig.start(task_id="T2")["reason"] == "task_active"
    rig.cmd("cancel", "T1")
    # reusing a finished task id
    assert rig.start(task_id="T1")["reason"] == "task_id_already_used"


def test_stale_command_rejected(rig):
    ack = rig.start(requested_at=(rig.clock.wall - config.COMMAND_MAX_AGE_SECONDS - 5) * 1000)
    assert ack["reason"] == "stale_command"
    assert rig.start(task_id="T2", requested_at=rig.clock.wall * 1000)["result"] == "accepted"


# ---------------- running ----------------
def test_runs_thermostat_and_completes(rig):
    rig.start(duration_min=10)
    peak = 0
    states = set()
    for _ in range(590):
        rig.run(1)
        peak = max(peak, rig.oven.temp)
        states.add(rig.out.heater.is_active)
    assert states == {True, False}  # cycled
    assert peak < config.TEMP_MAX_C
    assert rig.task()["status"] == "running"
    rig.run(15)
    t = rig.task()
    assert t["status"] == "completed"
    assert 600 <= t["elapsed_s"] < 602
    assert not rig.out.heater.is_active
    assert rig.out.fan.is_active  # cooldown
    rig.run(config.FAN_COOLDOWN_SECONDS + 1)
    assert not rig.out.fan.is_active
    assert rig.tm.machine_status() == "ready"


def test_sensor_failure_fails_task(rig):
    rig.start()
    rig.run(30)
    rig.oven.broken = True
    rig.run(10)
    t = rig.task()
    assert (t["status"], t["failure_reason"]) == ("failed", "sensor_failure")
    assert not rig.out.heater.is_active
    # a failed task must be acknowledged before a new one
    rig.oven.broken = False
    rig.run(4)
    assert rig.start(task_id="T2")["reason"] == "unacknowledged_failure"
    assert rig.cmd("acknowledge", "T1")["result"] == "accepted"
    assert rig.start(task_id="T2")["result"] == "accepted"


def test_over_temperature_fails_task(rig):
    rig.start()
    rig.run(4)
    rig.oven.fixed = config.TEMP_MAX_C + 1
    rig.run(2)
    assert rig.task()["failure_reason"] == "over_temperature"
    assert not rig.out.heater.is_active


def test_cancel(rig):
    rig.start()
    rig.run(20)
    assert rig.cmd("cancel", "other")["reason"] == "task_mismatch"
    assert rig.cmd("cancel", "T1")["result"] == "accepted"
    assert rig.task()["status"] == "cancelled"
    assert not rig.out.heater.is_active


def test_checkpoints_saved(rig):
    rig.start()
    rig.run(35)
    t = rig.task()
    assert 25 <= t["elapsed_s"] <= 35
    assert t["checkpoint_boot_id"] == "boot-1"
    assert t["last_temp_c"] is not None


def test_events_logged_in_order(rig):
    rig.start()
    rig.run(4)
    rig.cmd("cancel", "T1")
    types = [e["type"] for e in rig.store.pending_events(100)]
    assert types == ["task_accepted", "task_running", "task_cancelled"]


def test_fresh_boot_without_task_is_ready(tmp_path):
    from tests.conftest import Rig
    r = Rig(tmp_path)
    r = r.reboot(downtime=3600, temp=25)
    assert r.tm.machine_status() == "ready"
    assert r.store.latest_task() is None
    assert not r.tm.busy()


def test_manager_never_heats_without_task(rig):
    rig.run(60)
    assert not rig.out.heater.is_active
    assert isinstance(rig.tm, TaskManager)
