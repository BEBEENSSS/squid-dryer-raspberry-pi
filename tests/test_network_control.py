import queue

import config
from cloud_sync import CloudSync
from network_control import NetworkControl, network_ack_payload, parse_network_request
from snapshot import build_snapshot
from tests.conftest import FakeBase
from tests.test_cloud_command_age import make as make_sync
from tests.test_commands_age import SERVER_NOW, drain

NOW_MS = 1_900_000_000_000
WIFI = {"network": "wifi", "clients": None, "address": "192.168.1.50", "requested": False}
HOTSPOT = {"network": "hotspot", "clients": 0, "address": "10.42.0.1", "requested": True}


def control(status=WIFI, delivered=True):
    written = []

    def write(path, payload):
        written.append((path, payload))
        return delivered

    ctl = NetworkControl(now_ms=lambda: NOW_MS, wall=lambda: 5000.0, read_status=lambda: status,
                         write_request=write)
    return ctl, written


def test_parse_accepts_the_two_modes_and_invents_an_id_when_none_is_given():
    parsed, err = parse_network_request({"mode": "hotspot"})
    assert err is None and parsed["mode"] == "hotspot" and parsed["command_id"]
    assert parse_network_request({"mode": "wifi", "command_id": "c1"}) == ({"command_id": "c1", "mode": "wifi"}, None)


def test_parse_rejects_anything_else():
    assert parse_network_request({"mode": "reboot"})[1] == "invalid_mode"
    assert parse_network_request("hotspot")[1] == "invalid_mode"
    assert parse_network_request({"mode": "wifi", "command_id": ""})[1] == "invalid_command_id"
    assert parse_network_request({"mode": "wifi", "command_id": 7})[1] == "invalid_command_id"


def test_accepted_request_is_handed_to_netwatch_with_a_delay_so_the_ack_goes_out_first():
    ctl, written = control()
    ack = ctl.handle({"command_id": "c1", "mode": "hotspot"})
    assert ack == {"command_id": "c1", "mode": "hotspot", "result": "accepted", "reason": None, "at": NOW_MS}
    (path, payload), = written
    assert path == config.NET_REQUEST_PATH
    assert payload == {"command_id": "c1", "mode": "hotspot", "requested_at": 5000.0,
                       "not_before": 5000.0 + config.NET_SWITCH_DELAY_SECONDS}
    assert network_ack_payload(ack) == {"command_id": "c1", "mode": "hotspot", "result": "accepted", "at": NOW_MS}


def test_a_request_is_answered_once_even_if_it_is_read_again():
    ctl, written = control()
    assert ctl.handle({"command_id": "c1", "mode": "hotspot"}) is not None
    assert ctl.handle({"command_id": "c1", "mode": "hotspot"}) is None
    assert len(written) == 1


def test_requests_without_a_usable_id_are_ignored_because_nobody_could_match_the_answer():
    ctl, written = control()
    assert ctl.handle({"mode": "hotspot"}) is None
    assert ctl.handle({"command_id": 5, "mode": "hotspot"}) is None
    assert ctl.handle(None) is None
    assert written == []


def test_unknown_mode_is_rejected_and_nothing_is_written():
    ctl, written = control()
    ack = ctl.handle({"command_id": "c1", "mode": "reboot"})
    assert (ack["result"], ack["reason"]) == ("rejected", "invalid")
    assert written == []


def test_a_cloud_request_that_is_too_old_or_unprovable_never_changes_the_network():
    for verdict, reason in (("too_old", "stale_command"), ("superseded_by_local", "superseded_by_local"),
                            ("unverifiable", "unverifiable")):
        ctl, written = control()
        ack = ctl.handle({"command_id": "c1", "mode": "hotspot"}, verdict)
        assert (ack["result"], ack["reason"]) == ("rejected", reason)
        assert written == []


def test_rejected_when_netwatch_is_not_running():
    ctl, written = control(status=None)
    assert ctl.handle({"command_id": "c1", "mode": "hotspot"})["reason"] == "netwatch_unavailable"
    assert written == []


def test_rejected_when_the_machine_is_already_in_the_requested_mode():
    ctl, written = control(status=WIFI)
    assert ctl.handle({"command_id": "c1", "mode": "wifi"})["reason"] == "already_in_mode"
    ctl, written = control(status=HOTSPOT)
    assert ctl.handle({"command_id": "c2", "mode": "hotspot"})["reason"] == "already_in_mode"
    assert written == []


def test_a_fallback_hotspot_can_still_be_requested_to_get_the_short_timers():
    ctl, written = control(status=dict(HOTSPOT, requested=False))
    assert ctl.handle({"command_id": "c1", "mode": "hotspot"})["result"] == "accepted"


def test_wifi_can_be_requested_while_the_machine_is_still_connecting():
    ctl, written = control(status={"network": "connecting", "clients": None, "address": None})
    assert ctl.handle({"command_id": "c1", "mode": "wifi"})["result"] == "accepted"


def test_a_request_file_that_cannot_be_written_is_reported_not_swallowed():
    ctl, written = control(delivered=False)
    assert ctl.handle({"command_id": "c1", "mode": "hotspot"})["reason"] == "write_failed"


def test_the_local_snapshot_carries_the_latest_answer():
    ctl, _ = control()
    ctl.handle({"command_id": "c1", "mode": "hotspot"})

    class Settings(dict):
        updated_at = 0

    snap = build_snapshot(state={}, manual={}, settings=Settings(), request=None, report=None, ack=None,
                          pi_now_ms=1, cloud_online=False, loop_age_s=0.1,
                          network_ack=network_ack_payload(ctl.last_ack))
    assert snap["network"]["ack"]["command_id"] == "c1"


# ---------------- through Firebase ----------------

def network_request(base, command_id, issued_ms, mode="hotspot"):
    base.data["network"] = {"request": {"command_id": command_id, "mode": mode, "issued_at": issued_ms,
                                         "requested_at": issued_ms}}


def test_a_fresh_cloud_request_reaches_the_control_loop_once_with_no_verdict(rig):
    base = FakeBase()
    base.server_ms = SERVER_NOW
    sync, q = make_sync(rig, base)
    network_request(base, "n1", SERVER_NOW - 2_000)
    assert sync.sync_once()
    items = [i for i in drain(q) if i[0] == "network"]
    assert len(items) == 1 and items[0][1]["verdict"] is None and items[0][1]["request"]["mode"] == "hotspot"
    assert sync.sync_once()
    assert [i for i in drain(q) if i[0] == "network"] == []


def test_an_old_cloud_request_arrives_with_a_verdict_so_it_can_be_refused(rig):
    base = FakeBase()
    base.server_ms = SERVER_NOW
    sync, q = make_sync(rig, base)
    network_request(base, "n1", SERVER_NOW - 31_000)
    assert sync.sync_once()
    (_, payload), = [i for i in drain(q) if i[0] == "network"]
    assert payload["verdict"] == "too_old"


def test_the_answer_is_published_to_network_ack(rig):
    base = FakeBase()
    base.server_ms = SERVER_NOW
    ctl, _ = control()
    q = queue.Queue()
    sync = CloudSync(lambda: rig.store, rig.tm, lambda: {"fan": False, "heater": False, "status": "ready"}, q,
                     rig.boot_id, base_factory=lambda: base, mono=lambda: rig.clock.mono,
                     network_ack_fn=lambda: network_ack_payload(ctl.last_ack))
    assert sync.sync_once()
    assert "network" not in base.data or "ack" not in base.data["network"]
    ctl.handle({"command_id": "c1", "mode": "hotspot"})
    sync.notify()  # the control loop does this after every command, which is what publishes the answer
    assert sync.sync_once()
    assert base.data["network"]["ack"]["command_id"] == "c1"
    assert base.data["network"]["ack"]["result"] == "accepted"
