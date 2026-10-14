import queue

from cloud_sync import CloudSync
from command_age import LocalCommandMark, cloud_verdict
from hardware import read_temp
from tests.conftest import FakeBase
from tests.test_commands_age import SERVER_NOW, command, drain

MAX_AGE = 30


def make(rig, base, mark=None):
    q = queue.Queue()
    status = lambda: {"fan": rig.out.fan.is_active, "heater": rig.out.heater.is_active,
                      "status": rig.tm.machine_status(), "ready": True}
    sync = CloudSync(lambda: rig.store, rig.tm, status, q, rig.boot_id, base_factory=lambda: base,
                     mono=lambda: rig.clock.mono, local_mark=mark)
    return sync, q


def start_request(**kw):
    return dict({"command_id": "s1", "command": "start", "task_id": "T9", "duration_minutes": 5,
                 "batch_count": 1, "row_levels": "1", "requested_at": SERVER_NOW}, **kw)


def test_verdict_rules():
    assert cloud_verdict(MAX_AGE, None, MAX_AGE) is None
    assert cloud_verdict(MAX_AGE + 0.1, None, MAX_AGE) == "too_old"
    assert cloud_verdict(10, 20, MAX_AGE) is None            # made after the local command
    assert cloud_verdict(10, 5, MAX_AGE) == "superseded_by_local"  # made before it
    assert cloud_verdict(40, 5, MAX_AGE) == "too_old"


def test_local_mark_measures_time_since_the_last_local_command():
    t = [100.0]
    mark = LocalCommandMark(mono=lambda: t[0])
    assert mark.since() is None
    mark.touch()
    t[0] += 7
    assert mark.since() == 7


def test_manual_command_just_inside_the_window_runs_and_just_outside_does_not(rig):
    base = FakeBase()
    base.server_ms = SERVER_NOW
    sync, q = make(rig, base)
    command(base, "ok", SERVER_NOW - 29_000)
    assert sync.sync_once()
    assert len(drain(q)) == 1
    command(base, "late", SERVER_NOW - 31_000)
    assert sync.sync_once()
    assert drain(q) == []
    events = [e for e in base.data["events"].values() if e["type"] == "stale_command_ignored"]
    assert events[-1]["data"]["reason"] == "too_old"


def test_manual_cloud_command_made_before_a_local_one_is_ignored_and_logged(rig):
    base = FakeBase()
    base.server_ms = SERVER_NOW
    mark = LocalCommandMark(mono=lambda: rig.clock.mono)
    sync, q = make(rig, base, mark)
    mark.touch()
    rig.clock.advance(5)
    command(base, "before", SERVER_NOW - 10_000)  # made 10 s ago, the local command was 5 s ago
    assert sync.sync_once()
    assert drain(q) == []
    events = [e for e in base.data["events"].values() if e["type"] == "stale_command_ignored"]
    assert events[-1]["data"] == {"command_id": "before", "reason": "superseded_by_local"}
    assert base.data["commands"]["heater"] is False
    command(base, "after", SERVER_NOW - 2_000)  # made after the local command
    assert sync.sync_once()
    assert len(drain(q)) == 1


def test_cloud_task_request_is_aged_from_when_it_was_made_not_when_it_was_delivered(rig):
    rig.tm.server_now_ms = lambda: SERVER_NOW
    # queued offline for two minutes, delivered just now: requested_at is fresh, issued_at is not
    queued = start_request(issued_at=SERVER_NOW - 120_000)
    assert rig.tm.handle_command(queued, cloud=True)["reason"] == "stale_command"
    fresh = start_request(command_id="s2", issued_at=SERVER_NOW - 5_000)
    assert rig.tm.handle_command(fresh, cloud=True)["result"] == "accepted"


def test_cloud_task_request_expires_after_thirty_seconds_but_local_ones_are_not_aged(rig):
    rig.tm.server_now_ms = lambda: SERVER_NOW
    old = start_request(issued_at=SERVER_NOW - 31_000)
    assert rig.tm.handle_command(old, cloud=True)["reason"] == "stale_command"
    assert rig.tm.handle_command(dict(old, command_id="local1"))["result"] == "accepted"


def test_cloud_task_request_made_before_a_local_command_is_rejected(rig):
    rig.tm.server_now_ms = lambda: SERVER_NOW
    mark = LocalCommandMark(mono=lambda: rig.clock.mono)
    rig.tm.local_mark = mark
    mark.touch()
    rig.clock.advance(5)
    assert rig.tm.handle_command(start_request(issued_at=SERVER_NOW - 10_000), cloud=True)["reason"] == "stale_command"
    ok = start_request(command_id="s2", issued_at=SERVER_NOW - 2_000)
    assert rig.tm.handle_command(ok, cloud=True)["result"] == "accepted"


def test_cloud_task_request_without_issued_at_falls_back_to_requested_at_and_the_short_window(rig):
    rig.tm.server_now_ms = lambda: SERVER_NOW
    assert rig.tm.handle_command(start_request(requested_at=SERVER_NOW - 60_000), cloud=True)["reason"] == "stale_command"
    ok = start_request(command_id="s2", requested_at=SERVER_NOW - 5_000)
    assert rig.tm.handle_command(ok, cloud=True)["result"] == "accepted"


def sensor_dir(tmp_path, milli_c):
    (tmp_path / "w1_slave").write_text(f"72 01 4b 46 7f ff 0e 10 57 : crc=57 YES\n72 01 4b 46 7f ff 0e 10 57 t={milli_c}\n")
    return tmp_path


def test_implausible_temperature_counts_as_a_failed_read(tmp_path):
    assert read_temp(sensor_dir(tmp_path, 45_000)) == 45.0
    assert read_temp(sensor_dir(tmp_path, 150_000)) is None   # no real chamber is this hot
    assert read_temp(sensor_dir(tmp_path, -30_000)) is None
