import queue

import config
from cloud_sync import CloudSync
from dryer_cloud import ManualControl, coalesce_commands


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
    # a fresh task warms up first: the drying clock has not started
    assert rep["status"] == "warming_up" and rep["row_levels"] == "1,2" and rep["batch_count"] == 10
    assert rep["progress_percent"] == 0 and rep["duration_minutes"] == 5
    assert rep["started_at"] and rep.get("drying_started_at") is None  # (Firebase drops null fields)
    assert rep["target_temp_c"] == config.MANUAL_TARGET_TEMP_C  # the saved target (default 55) is the task's
    assert rep["reported_at"] == {".sv": "timestamp"}
    assert st["status"] == "warming_up" and st["ready"] is False
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
    # being offline does not touch task heating
    base.fail = True
    rig.clock.advance(300)
    sync.sync_once()
    manual.tick()
    rig.oven.fixed = 40
    rig.run(2)
    assert rig.out.heater.is_active and rig.task()["status"] == "running"


def _manual_rig(rig, online=True):
    from tests.conftest import FakeBase
    base = FakeBase()
    sync, q = make_sync(rig, base)
    manual = ManualControl(rig.out, rig.sensor, rig.store, sync, rig.tm, mono=lambda: rig.clock.mono)
    if online:
        sync.sync_once()
        manual.tick()  # clears the boot block once online
    return manual, sync, base


def _reading(rig, temp):
    rig.oven.fixed = temp
    rig.clock.advance(2)
    rig.sensor.poll()


def test_manual_heating_blocked_after_boot_until_reenabled_then_runs_offline(rig):
    manual, sync, base = _manual_rig(rig, online=False)
    rig.tm.apply_settings({"manual_target_temp_c": 47, "updated_at": 1})  # the saved target everything below holds
    _reading(rig, 30)
    manual.apply(True, True)
    assert not rig.out.heater.is_active  # blocked until Firebase confirms
    assert manual.block == "reboot_block"
    sync.sync_once()
    manual.tick()
    manual.apply(True, True)
    assert rig.out.heater.is_active and manual.enabled
    # the saved target is held locally, so losing the connection changes nothing
    base.fail = True
    rig.clock.advance(3600)
    _reading(rig, 30)
    sync.sync_once()
    manual.tick()
    assert rig.out.heater.is_active
    _reading(rig, 47.5)  # at or above the saved target: heater off, still enabled
    manual.tick()
    assert not rig.out.heater.is_active and manual.enabled
    _reading(rig, 46)  # inside the hysteresis band: stays off
    manual.tick()
    assert not rig.out.heater.is_active
    _reading(rig, 45)  # target - hysteresis: back on
    manual.tick()
    assert rig.out.heater.is_active


def _readings(rig, bottom, top):
    """The bottom sensor (the thermostat's) and the top one, read at the same moment."""
    rig.oven.fixed = bottom
    rig.oven.zone_fixed["top"] = top
    rig.clock.advance(2)
    for s in rig.sensors:
        s.poll()


def test_manual_pause_goes_by_the_hottest_sensor_and_recovers_with_margin(zrig):
    manual, _, _ = _manual_rig(zrig)
    zrig.tm.apply_settings({"manual_target_temp_c": 47, "heater_cutoff_c": 50})
    _readings(zrig, bottom=40, top=40)
    manual.apply(True, True)
    assert zrig.out.heater.is_active
    # a hot spot the thermostat's sensor cannot see: the bottom still asks for heat, the top is at the pause
    _readings(zrig, bottom=40, top=50)
    manual.tick()
    assert not zrig.out.heater.is_active and zrig.tm.cutoff.active and manual.enabled
    _readings(zrig, bottom=40, top=48)  # inside the margin: no rapid switching
    manual.tick()
    assert not zrig.out.heater.is_active
    _readings(zrig, bottom=40, top=47)  # pause - margin: released, heating still wanted
    manual.tick()
    assert zrig.out.heater.is_active and not zrig.tm.cutoff.active


def test_manual_faults_disable_heating_regardless_of_cutoff(rig):
    manual, _, _ = _manual_rig(rig)
    _reading(rig, 40)
    manual.apply(True, True)
    assert rig.out.heater.is_active
    _reading(rig, config.TEMP_MAX_C)
    manual.tick()
    assert not rig.out.heater.is_active and not manual.enabled and manual.block == "over_temperature"
    _reading(rig, 40)
    manual.apply(True, True)
    assert rig.out.heater.is_active
    rig.oven.broken = True
    rig.clock.advance(config.SENSOR_STALE_SECONDS + 1)
    for _ in range(config.SENSOR_BAD_LIMIT):
        rig.sensor.poll()
    manual.tick()
    assert not rig.out.heater.is_active and not manual.enabled and manual.block == "sensor_fault"


def test_manual_over_temperature_fault_is_the_saved_max_even_offline(rig):
    manual, sync, base = _manual_rig(rig)
    rig.tm.apply_settings({"manual_target_temp_c": 47, "heater_cutoff_c": 50, "temp_max_c": 52, "updated_at": 1})
    _reading(rig, 35)
    manual.apply(True, True)
    assert rig.out.heater.is_active
    base.fail = True  # the network is gone: the saved safe shutdown temperature is all the machine has, and it is enough
    sync.sync_once()
    _reading(rig, 51)  # past the pause, below the saved shutdown temperature, far below the old fixed 60
    manual.tick()
    assert manual.enabled
    _reading(rig, 52)
    manual.tick()
    assert not rig.out.heater.is_active and not manual.enabled and manual.block == "over_temperature"


def test_safe_temp_setting_is_polled_once(rig):
    from tests.conftest import FakeBase
    base = FakeBase()
    sync, q = make_sync(rig, base)

    def settings_items():
        items = []
        while not q.empty():
            kind, payload = q.get_nowait()
            if kind == "settings":
                items.append(payload)
        return items

    base.data = {"settings": {"safe_temp_c": 33}}
    assert sync.sync_once()
    assert settings_items() == [{"safe_temp_c": 33}]
    assert sync.sync_once()
    assert settings_items() == []  # unchanged value is not re-delivered
    base.data["settings"]["safe_temp_c"] = 36
    assert sync.sync_once()
    assert settings_items() == []  # settings are only re-read every SETTINGS_POLL_SECONDS
    rig.clock.advance(config.SETTINGS_POLL_SECONDS)
    assert sync.sync_once()
    assert settings_items() == [{"safe_temp_c": 36}]


def test_manual_commands_ignored_while_cooling(rig):
    from tests.conftest import FakeBase
    base = FakeBase()
    sync, q = make_sync(rig, base)
    manual = ManualControl(rig.out, rig.sensor, rig.store, sync, rig.tm, mono=lambda: rig.clock.mono)
    rig.start(row_levels="1", duration_min=60)
    rig.run(300)
    rig.cmd("complete_batch", "T1", row_levels="1")
    assert rig.tm.cooling_active()
    manual.apply(False, False)  # a stale "fan off" must not cut cooling short
    assert rig.out.fan.is_active
    manual.apply(True, True)
    assert not rig.out.heater.is_active and rig.out.fan.is_active
    assert sync._pending_writes == {"commands/heater": False, "commands/fan": False}


def test_state_change_publishes_without_waiting_for_the_timer(rig):
    from tests.conftest import FakeBase
    base = FakeBase()
    sync, q = make_sync(rig, base)
    assert sync.sync_once()
    n = base.updates
    rig.clock.advance(1)
    assert sync.sync_once()
    assert base.updates == n  # nothing changed, inside the publish interval
    rig.out.fan.on()
    assert sync.sync_once()
    assert base.updates == n + 1 and base.data["state"]["fan"] is True
    rig.clock.advance(1)
    assert sync.sync_once()
    assert base.updates == n + 1
    sync.notify()
    assert sync.sync_once()
    assert base.updates == n + 2


def test_temp_change_alone_does_not_force_a_publish(rig):
    from tests.conftest import FakeBase
    base = FakeBase()
    status = {"fan": False, "temp": 30.0}
    q = queue.Queue()
    sync = CloudSync(lambda: rig.store, rig.tm, lambda: dict(status), q, rig.boot_id,
                     base_factory=lambda: base, mono=lambda: rig.clock.mono)
    assert sync.sync_once()
    n = base.updates
    status["temp"] = 31.0
    rig.clock.advance(1)
    assert sync.sync_once()
    assert base.updates == n


def test_listener_wakes_sync_and_failure_falls_back_to_polling(rig):
    from tests.conftest import FakeBase
    base = FakeBase()
    sync, q = make_sync(rig, base)
    assert sync.sync_once()  # FakeBase cannot stream: polling still works
    assert sync._listeners == []

    callbacks, closed = [], []

    class Listener:
        def close(self):
            closed.append(True)

    class StreamBase(FakeBase):
        def child(self, path):
            ref = super().child(path)
            ref.listen = lambda cb: (callbacks.append(cb), Listener())[1]
            return ref

    sync, q = make_sync(rig, StreamBase())
    sync._wake.clear()
    assert sync.sync_once()
    assert len(callbacks) == 3 and len(sync._listeners) == 3
    sync._wake.clear()
    callbacks[0]({"event": "put"})
    assert sync._wake.is_set()
    sync._base.fail = True
    assert not sync.sync_once()
    assert len(closed) == 3 and sync._listeners == []  # reopened after reconnect
    sync._base.fail = False
    assert sync.sync_once()
    assert len(sync._listeners) == 3


def test_coalesce_keeps_task_order_and_only_the_latest_manual():
    items = [("manual", (True, False)), ("task", "a"), ("manual", (True, True)),
             ("settings", {"safe_temp_c": 33}), ("manual", (False, False))]
    assert coalesce_commands(items) == [("task", "a"), ("settings", {"safe_temp_c": 33}),
                                        ("manual", (False, False))]
    assert coalesce_commands([]) == []
