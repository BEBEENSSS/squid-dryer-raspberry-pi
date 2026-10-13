import queue

import config
from cloud_sync import CloudSync
from dryer_cloud import ManualControl


def make_sync(rig, base):
    status = lambda: {"fan": rig.out.fan.is_active, "heater": rig.out.heater.is_active,
                      "status": rig.tm.machine_status(),
                      "ready": rig.tm.machine_status() == "ready"}
    q = queue.Queue()
    sync = CloudSync(lambda: rig.store, rig.tm, status, q, rig.boot_id,
                     base_factory=lambda: base, mono=lambda: rig.clock.mono)
    return sync, q


def deliver(rig, q):
    acks = []
    while not q.empty():
        kind, payload = q.get()
        if kind == "task":
            acks.append(rig.tm.handle_command(payload))
    return acks


def test_command_ack_and_state_published(rig):
    from tests.conftest import FakeBase
    base = FakeBase()
    sync, q = make_sync(rig, base)
    base.data = {"task": {"request": {"command": "start", "task_id": "T1", "batch_count": 10,
                                      "row_levels": "1,2", "duration_minutes": 5,
                                      "requested_at": rig.clock.wall * 1000,
                                      "requested_by": "uid-1"}}}
    assert sync.sync_once()
    assert deliver(rig, q)[0]["result"] == "accepted"
    rig.run(3)
    assert sync.sync_once()
    st = base.data["state"]
    ack = base.data["task"]["ack"]
    assert ack["command"] == "start" and ack["task_id"] == "T1" and ack["result"] == "accepted"
    rep = base.data["task"]["report"]
    assert rep["status"] == "running" and rep["row_levels"] == "1,2" and rep["batch_count"] == 10
    assert 0 < rep["progress_percent"] < 100 and rep["duration_minutes"] == 5
    assert rep["reported_at"] == {".sv": "timestamp"}
    assert st["status"] == "running" and st["ready"] is False
    assert "online" not in st and st["last_seen"] == {".sv": "timestamp"}
    assert set(base.data["events"]) == {"0000000001", "0000000002"}
    # same command still sitting in Firebase is not re-delivered
    assert sync.sync_once()
    assert q.empty()


def test_offline_task_continues_and_events_sync_later(rig):
    from tests.conftest import FakeBase
    base = FakeBase()
    sync, q = make_sync(rig, base)
    assert sync.sync_once()
    rig.start(duration_min=5)
    base.fail = True
    for _ in range(320):           # the whole task runs with no internet
        rig.run(1)
        sync.sync_once()
    assert not sync.online
    t = rig.task()
    assert t["status"] == "completed" and t["elapsed_s"] >= 300
    assert "task_completed" not in str(base.data.get("events"))

    base.fail = False
    for _ in range(20):            # drain the outbox
        sync.sync_once()
    events = [base.data["events"][k] for k in sorted(base.data["events"])]
    types = [e["type"] for e in events]
    assert types.index("task_accepted") < types.index("connection_lost") < types.index("task_completed")
    assert "connection_restored" in types
    assert base.data["task"]["report"]["status"] == "completed"
    assert base.data["task"]["report"]["progress_percent"] == 100
    assert base.data["state"]["pending_events"] == 0
    assert rig.store.pending_count() == 0


def test_offline_failure_preserved_and_synced(rig):
    from tests.conftest import FakeBase
    base = FakeBase()
    sync, q = make_sync(rig, base)
    rig.start()
    base.fail = True
    rig.run(20)
    rig.oven.broken = True
    rig.run(10)
    sync.sync_once()
    base.fail = False
    for _ in range(5):
        sync.sync_once()
    rep = base.data["task"]["report"]
    assert (rep["status"], rep["reason"]) == ("failed", "sensor_failure")
    assert "last valid temp" in rep["detail"]
    assert base.data["state"]["status"] == "failed"


def test_online_only_after_successful_write(rig):
    from tests.conftest import FakeBase
    base = FakeBase()
    base.fail = True
    sync, q = make_sync(rig, base)
    assert not sync.sync_once()
    assert not sync.online and not sync.ever_ok


def test_manual_mode_blocked_during_task(rig):
    from tests.conftest import FakeBase
    base = FakeBase()
    sync, q = make_sync(rig, base)
    manual = ManualControl(rig.out, rig.sensor, rig.store, sync, rig.tm, mono=lambda: rig.clock.mono)
    sync.sync_once()
    manual.tick()  # clears the boot failsafe once online
    manual.apply(True, True)
    assert rig.out.heater.is_active
    manual.apply(False, False)
    rig.start()
    rig.run(3)
    manual.apply(False, False)  # would normally switch everything off
    assert rig.out.fan.is_active
    # offline failsafe does not touch task heating
    base.fail = True
    rig.clock.advance(config.OFFLINE_FAILSAFE_SECONDS + 10)
    sync.sync_once()
    manual.tick()
    rig.oven.fixed = 40
    rig.run(2)
    assert rig.out.heater.is_active and rig.task()["status"] == "running"


def test_manual_failsafe_still_works_without_task(rig):
    from tests.conftest import FakeBase
    base = FakeBase()
    sync, q = make_sync(rig, base)
    manual = ManualControl(rig.out, rig.sensor, rig.store, sync, rig.tm, mono=lambda: rig.clock.mono)
    manual.apply(True, True)
    assert not rig.out.heater.is_active  # blocked until Firebase confirms
    sync.sync_once()
    manual.tick()
    manual.apply(True, True)
    assert rig.out.heater.is_active
    base.fail = True
    rig.clock.advance(config.OFFLINE_FAILSAFE_SECONDS + 1)
    rig.sensor.poll()
    sync.sync_once()
    manual.tick()
    assert not rig.out.heater.is_active
