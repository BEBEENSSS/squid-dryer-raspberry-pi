import queue

from cloud_sync import CloudSync
from server_clock import ServerClock
from tests.conftest import FakeBase

SERVER_NOW = 1_900_000_000_000  # ms


def make(rig, base):
    q = queue.Queue()
    status = lambda: {"fan": rig.out.fan.is_active, "heater": rig.out.heater.is_active,
                      "status": rig.tm.machine_status(), "ready": True}
    sync = CloudSync(lambda: rig.store, rig.tm, status, q, rig.boot_id,
                     base_factory=lambda: base, mono=lambda: rig.clock.mono)
    return sync, q


def drain(q):
    items = []
    while not q.empty():
        items.append(q.get())
    return items


def command(base, command_id, issued_ms, fan=True, heater=False):
    base.data["commands"] = {"fan": fan, "heater": heater, "command_id": command_id, "issued_at": issued_ms}


def test_server_clock_carries_the_reading_forward_on_the_monotonic_clock():
    t = [100.0]
    clock = ServerClock(mono=lambda: t[0])
    assert clock.now_ms() is None
    clock.set(5000)
    t[0] += 2.5
    assert clock.now_ms() == 7500


def test_fresh_command_is_applied_once_with_its_id(rig):
    base = FakeBase()
    base.server_ms = SERVER_NOW
    sync, q = make(rig, base)
    command(base, "c1", SERVER_NOW - 2000)
    assert sync.sync_once()
    assert drain(q) == [("manual", (True, False, "c1", "online"))]
    assert sync.sync_once()
    assert drain(q) == []  # same command_id is never delivered twice


def test_command_left_during_an_outage_is_not_replayed(rig):
    base = FakeBase()
    base.server_ms = SERVER_NOW
    sync, q = make(rig, base)
    assert sync.sync_once()
    drain(q)  # the idle (False, False) request every fresh controller sees
    base.fail = True
    assert not sync.sync_once()
    base.fail = False
    command(base, "old", SERVER_NOW - 10 * 60 * 1000, fan=True, heater=True)  # written 10 minutes ago
    base.server_ms = SERVER_NOW + 5 * 60 * 1000  # server time has moved on
    rig.clock.advance(300)
    assert sync.sync_once()
    assert drain(q) == []
    types = [e["type"] for e in base.data["events"].values()]
    assert "stale_command_ignored" in types
    assert base.data["commands"]["heater"] is False  # the left-over heater request is withdrawn


def test_restart_with_old_firebase_values_does_not_move_a_relay(rig):
    base = FakeBase()
    base.server_ms = SERVER_NOW
    command(base, "left-over", SERVER_NOW - 3_600_000, fan=True, heater=True)
    sync, q = make(rig, base)  # a fresh controller: nothing remembered
    assert sync.sync_once()
    assert drain(q) == []


def test_command_without_issued_at_is_not_trusted(rig):
    base = FakeBase()
    base.server_ms = SERVER_NOW
    base.data["commands"] = {"fan": True, "heater": False, "command_id": "x"}
    sync, q = make(rig, base)
    assert sync.sync_once()
    assert drain(q) == []


def test_unknown_server_time_never_applies_a_new_style_command(rig):
    base = FakeBase()  # server timestamps stay literal, so the clock cannot be read
    sync, q = make(rig, base)
    command(base, "c1", SERVER_NOW)
    assert sync.sync_once()
    assert drain(q) == []


def test_legacy_command_without_id_still_works(rig):
    base = FakeBase()
    sync, q = make(rig, base)
    base.data["commands"] = {"fan": True, "heater": False}
    assert sync.sync_once()
    assert drain(q) == [("manual", (True, False, None, "online"))]


def test_task_command_age_uses_server_time_not_the_pi_clock(rig):
    rig.clock.synced = False  # a Pi that just booted: its own clock is not trusted
    rig.tm.server_now_ms = lambda: SERVER_NOW
    stale = {"command_id": "s1", "command": "start", "task_id": "T9", "duration_minutes": 5,
             "batch_count": 1, "row_levels": "1", "requested_at": SERVER_NOW - 11 * 60 * 1000}
    assert rig.tm.handle_command(stale)["reason"] == "stale_command"
    fresh = dict(stale, command_id="s2", requested_at=SERVER_NOW - 5000)
    assert rig.tm.handle_command(fresh)["result"] == "accepted"


def test_state_reports_the_last_command_id_and_source(rig):
    from dryer_cloud import ManualControl
    base = FakeBase()
    sync, _ = make(rig, base)
    manual = ManualControl(rig.out, rig.sensor, rig.store, sync, rig.tm, mono=lambda: rig.clock.mono)
    manual.apply(True, False, "c7", "online")
    assert (manual.last_command_id, manual.last_command_source) == ("c7", "online")
    manual.apply(False, False)  # a legacy command does not erase what was received
    assert manual.last_command_id == "c7"


def test_received_command_is_stamped_with_server_time(rig):
    from dryer_cloud import ManualControl
    base = FakeBase()
    base.server_ms = SERVER_NOW
    sync, _ = make(rig, base)
    assert sync.sync_once()
    manual = ManualControl(rig.out, rig.sensor, rig.store, sync, rig.tm, mono=lambda: rig.clock.mono)
    rig.clock.advance(2)
    manual.apply(True, False, "c9", "online")
    assert manual.last_command_at == SERVER_NOW + 2000
