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
    assert rig.out.fan.is_active  # cooling: still hot
    assert rig.tm.machine_status() == "cooling"
    assert rig.oven.temp > rig.tm.safe_temp_c
    rig.run(300)
    assert rig.oven.temp <= rig.tm.safe_temp_c
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
    assert types == ["task_accepted", "task_warming_up", "task_running", "task_cancelled"]


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


# ---------------- add_batch ----------------
def add(rig, batch_id="B1", rows="3", duration_min=5, **kw):
    kw.setdefault("row_levels", rows)
    return rig.cmd("add_batch", "T1", batch_id=batch_id, duration_minutes=duration_min, **kw)


def test_add_batch_is_timed_separately_and_does_not_restart(rig):
    rig.start(duration_min=10)
    rig.run(120)
    heater_before = rig.out.heater.is_active
    assert add(rig, duration_min=5)["result"] == "accepted"
    t = rig.task()
    assert t["status"] == "running" and t["row_levels"] == "2,3" and t["batch_count"] == 21
    assert t["duration_s"] == 600  # 120 + 300 < 600, so the task is not extended
    assert rig.out.fan.is_active and rig.out.heater.is_active == heater_before
    rig.run(60)
    snap = rig.tm.snapshot()
    batch = snap["batches"][0]
    assert batch["batch_id"] == "B1" and batch["row_level"] == 3
    assert batch["offset_seconds"] == pytest.approx(120, abs=2)
    assert batch["elapsed_seconds"] == pytest.approx(60, abs=2)
    assert snap["elapsed_seconds"] == pytest.approx(180, abs=3)  # the task clock never reset


def test_add_batch_extends_task_to_cover_a_longer_batch(rig):
    rig.start(duration_min=10)
    rig.run(300)
    assert add(rig, duration_min=20)["result"] == "accepted"
    assert rig.task()["duration_s"] == pytest.approx(300 + 1200, abs=2)
    assert add(rig, "B2", rows="4", duration_min=1)["result"] == "accepted"
    assert rig.task()["duration_s"] == pytest.approx(300 + 1200, abs=2)  # never shortened


@pytest.mark.parametrize("kw,reason", [
    ({"batch_id": "bad id"}, "invalid_batch_id"),
    ({"rows": "9"}, "invalid_row_levels"),
    ({"rows": "1,3"}, "invalid_row_levels"),
    ({"rows": "2"}, "row_occupied"),
    ({"duration_min": 0}, "invalid_duration"),
    ({"weight_kg": 0}, "invalid_weight"),
])
def test_add_batch_rejections(rig, kw, reason):
    rig.start()
    rig.run(5)
    ack = add(rig, **kw)
    assert (ack["result"], ack["reason"]) == ("rejected", reason)
    assert rig.tm.snapshot()["batches"] == []


def test_add_batch_needs_a_running_task(rig):
    assert add(rig)["reason"] == "task_not_running"
    rig.start()
    rig.run(5)
    assert rig.cmd("add_batch", "OTHER", batch_id="B1", duration_minutes=5, row_levels="3")["reason"] == "task_mismatch"
    rig.cmd("cancel", "T1")
    assert add(rig)["reason"] == "task_not_running"


def test_add_batch_duplicate_id_is_not_added_twice(rig):
    rig.start()
    rig.run(5)
    assert add(rig)["result"] == "accepted"
    ack = add(rig, rows="4")  # same batch_id again
    assert (ack["result"], ack["reason"]) == ("duplicate", "duplicate_batch")
    assert len(rig.tm.snapshot()["batches"]) == 1


def test_add_batch_survives_a_restart(rig):
    rig.start(duration_min=30)
    rig.run(60)
    add(rig, duration_min=40)
    rig2 = rig.reboot(downtime=5)
    snap = rig2.tm.snapshot()
    assert [b["batch_id"] for b in snap["batches"]] == ["B1"]
    assert snap["row_levels"] == "2,3"


# ---------------- confirming batches / cooling ----------------
def start_two_rows(rig):
    assert rig.start(row_levels="1,2", batch_count=2, duration_min=120)["result"] == "accepted"
    rig.run(60)


def complete(rig, row, task_id="T1"):
    return rig.cmd("complete_batch", task_id, row_levels=str(row))


def test_completing_one_of_two_rows_keeps_drying(rig):
    start_two_rows(rig)
    assert complete(rig, 1)["result"] == "accepted"
    t = rig.task()
    assert t["status"] == "running"
    assert t["row_levels"] == "2" and t["completed_rows"] == "1"
    rig.run(120)
    assert rig.out.fan.is_active
    assert rig.tm.machine_status() == "running"
    assert rig.tm.snapshot()["completed_rows"] == "1"


def test_completing_last_row_stops_heater_and_cools_by_temperature(rig):
    start_two_rows(rig)
    complete(rig, 1)
    rig.run(300)  # heat up well past the safe temperature
    assert rig.oven.temp > rig.tm.safe_temp_c
    assert complete(rig, 2)["result"] == "accepted"
    assert rig.task()["status"] == "completed"
    assert not rig.out.heater.is_active
    assert rig.out.fan.is_active
    assert rig.tm.machine_status() == "cooling"
    rig.run(20)
    assert rig.out.fan.is_active and not rig.out.heater.is_active
    rig.run(600)
    assert rig.oven.temp <= rig.tm.safe_temp_c
    assert not rig.out.fan.is_active
    assert rig.tm.machine_status() == "ready"


def test_complete_batch_rejections(rig):
    assert complete(rig, 1)["reason"] == "no_active_task"
    start_two_rows(rig)
    assert complete(rig, 3)["reason"] == "row_not_in_task"
    assert rig.cmd("complete_batch", "T1", row_levels="1,2")["reason"] == "invalid_row_levels"
    assert complete(rig, 1, task_id="OTHER")["reason"] == "task_mismatch"
    assert complete(rig, 1)["result"] == "accepted"
    again = complete(rig, 1)
    assert again["reason"] == "row_already_completed" and again["result"] == "duplicate"


def test_completed_row_can_be_reused_by_add_batch(rig):
    start_two_rows(rig)
    complete(rig, 1)
    ack = rig.cmd("add_batch", "T1", batch_id="B1", row_levels="1", duration_minutes=30)
    assert ack["result"] == "accepted"
    t = rig.task()
    assert t["row_levels"] == "1,2" and t["completed_rows"] == ""


def test_completing_an_added_batch_row(rig):
    start_two_rows(rig)
    rig.cmd("add_batch", "T1", batch_id="B1", row_levels="3", duration_minutes=30)
    assert complete(rig, 3)["result"] == "accepted"
    assert rig.task()["row_levels"] == "1,2"


def test_completion_survives_restart(rig):
    start_two_rows(rig)
    complete(rig, 1)
    r2 = rig.reboot(downtime=5, new_boot=False)
    t = r2.task()
    assert t["completed_rows"] == "1" and t["row_levels"] == "2"


def test_new_start_during_cooling_takes_over(rig):
    start_two_rows(rig)
    rig.run(300)
    complete(rig, 1)
    complete(rig, 2)
    assert rig.tm.cooling_active()
    rig.oven.fixed = 45.0  # cool enough to accept a task, still above the safe temperature
    rig.run(4)
    assert rig.start(task_id="T2")["result"] == "accepted"
    assert not rig.tm.cooling_active()
    assert rig.tm.machine_status() == "running"


def test_cooling_stops_at_time_cap(rig):
    start_two_rows(rig)
    complete(rig, 1)
    complete(rig, 2)
    rig.oven.fixed = 45.0  # never reaches the safe temperature
    rig.run(config.COOLDOWN_MAX_SECONDS - 10)
    assert rig.out.fan.is_active
    rig.run(20)
    assert not rig.out.fan.is_active and not rig.tm.cooling_active()


def test_cooling_without_sensor_stops_after_limit(rig):
    start_two_rows(rig)
    complete(rig, 1)
    complete(rig, 2)
    rig.oven.broken = True
    rig.run(config.COOLDOWN_NO_SENSOR_SECONDS - 10)
    assert rig.out.fan.is_active
    rig.run(20)
    assert not rig.out.fan.is_active


def test_reboot_while_hot_starts_cooling(rig):
    r2 = rig.reboot(downtime=5, temp=50.0)
    assert r2.tm.machine_status() == "cooling"
    assert r2.out.fan.is_active and not r2.out.heater.is_active
    r2.run(120)
    assert not r2.out.fan.is_active


def test_safe_temp_setting(rig):
    assert rig.tm.set_safe_temp(35)
    assert rig.tm.safe_temp_c == 35.0
    assert not rig.tm.set_safe_temp(90)
    assert not rig.tm.set_safe_temp("hot")
    assert rig.tm.safe_temp_c == 35.0
    assert rig.reboot(downtime=1, new_boot=False).tm.safe_temp_c == 35.0
