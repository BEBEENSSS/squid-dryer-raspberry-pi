"""Hotspot mode on request (web app or local panel) and trying every saved Wi-Fi network."""

import json

import netwatch
from netinfo import read_network_status, write_network_request
from .test_netwatch import HOME, HOTSPOT, FakeNmcli, make_cfg, make_watch, run_until


# ---------------- saved networks ----------------

def test_saved_networks_are_listed_home_first_without_the_hotspot_or_other_access_points(tmp_path):
    cfg = make_cfg(tmp_path)
    fake = FakeNmcli()
    nm = netwatch.Nm(cfg, run=fake)
    nm.ensure_hotspot_profile("correct horse battery")
    fake.profiles = {"Cafe": {}, "Ann: 5G": {}, "Phone AP": {"802-11-wireless.mode": "ap"}, **fake.profiles}
    assert nm.saved_wifi_profiles() == [HOME, "Cafe", "Ann: 5G"]  # a colon in a name survives nmcli -t escaping


def test_at_most_the_configured_number_of_networks_are_tried(tmp_path):
    cfg = make_cfg(tmp_path)
    cfg.NET_WIFI_MAX_PROFILES = 2
    fake = FakeNmcli()
    fake.profiles.update({"A": {}, "B": {}, "C": {}})
    assert len(netwatch.Nm(cfg, run=fake).saved_wifi_profiles()) == 2


def test_any_saved_network_counts_as_wifi_being_up(tmp_path):
    fake = FakeNmcli()
    fake.profiles["Cafe"] = {}
    nm = netwatch.Nm(make_cfg(tmp_path), run=fake)
    assert not nm.wifi_up()
    fake.active = {"Cafe"}
    assert nm.wifi_up()


def test_probe_falls_back_to_another_saved_network_when_the_home_router_is_off(tmp_path):
    watch, fake, clock, written = make_watch(tmp_path, router_on=False)
    fake.profiles["Cafe"] = {}
    fake.reachable = {"Cafe"}
    run_until(watch, clock, 400)
    assert fake.active == {"Cafe"}
    assert written[-1]["network"] == "wifi"


# ---------------- requests ----------------

def ask(watch, clock, mode, command_id="c1", delay=0.0, age=0.0):
    wall = 1_800_000_000 + clock.t
    write_network_request(watch.cfg.NET_REQUEST_PATH, {
        "command_id": command_id, "mode": mode, "requested_at": wall - age, "not_before": wall + delay})


def on_wifi(tmp_path, **kwargs):
    watch, fake, clock, written = make_watch(tmp_path, **kwargs)
    run_until(watch, clock, 30)
    assert fake.active == {HOME}
    return watch, fake, clock, written


def test_request_for_the_hotspot_drops_wifi_and_raises_the_hotspot(tmp_path):
    watch, fake, clock, written = on_wifi(tmp_path)
    ask(watch, clock, "hotspot")
    run_until(watch, clock, clock.t + 10)
    assert fake.active == {HOTSPOT}
    assert written[-1]["network"] == "hotspot"
    assert written[-1]["reason"] == "requested" and written[-1]["requested"] is True
    assert written[-1]["handled_request_id"] == "c1"
    assert written[-1]["connect_timeout_s"] == 300 and written[-1]["empty_grace_s"] == 60


def test_request_waits_for_its_not_before_time_so_the_ack_can_be_published_first(tmp_path):
    watch, fake, clock, written = on_wifi(tmp_path)
    ask(watch, clock, "hotspot", delay=30)
    run_until(watch, clock, clock.t + 25)
    assert fake.active == {HOME}
    run_until(watch, clock, clock.t + 15)
    assert fake.active == {HOTSPOT}


def test_nobody_joining_returns_to_wifi_after_the_connect_timeout(tmp_path):
    watch, fake, clock, written = on_wifi(tmp_path)
    ask(watch, clock, "hotspot")
    asked_at = clock.t
    run_until(watch, clock, asked_at + 290)
    assert fake.active == {HOTSPOT}
    run_until(watch, clock, asked_at + 300 + 60)
    assert fake.active == {HOME}
    assert written[-1]["network"] == "wifi" and written[-1]["reason"] == "no_client_timeout"


def test_a_connected_phone_keeps_the_requested_hotspot_and_leaving_ends_it_after_the_grace(tmp_path):
    watch, fake, clock, written = on_wifi(tmp_path)
    ask(watch, clock, "hotspot")
    run_until(watch, clock, clock.t + 20)
    fake.clients = 1
    run_until(watch, clock, clock.t + 3000)
    assert fake.active == {HOTSPOT}
    fake.clients = 0
    left_at = clock.t
    run_until(watch, clock, left_at + 50)
    assert fake.active == {HOTSPOT}  # not before the empty grace period
    run_until(watch, clock, left_at + 60 + 60)
    assert fake.active == {HOME}
    assert written[-1]["reason"] == "clients_left"


def test_no_network_in_range_after_a_requested_hotspot_leaves_the_fallback_hotspot_retrying(tmp_path):
    watch, fake, clock, written = on_wifi(tmp_path)
    ask(watch, clock, "hotspot")
    fake.router_on = False
    asked_at = clock.t
    run_until(watch, clock, asked_at + 300 + 60)
    assert fake.active == {HOTSPOT}
    assert written[-1]["network"] == "hotspot"
    assert written[-1]["requested"] is False and written[-1]["reason"] == "fallback"
    fake.router_on = True  # the router is back: the usual retry finds it
    run_until(watch, clock, clock.t + 120 + 60)
    assert fake.active == {HOME}


def test_request_for_wifi_leaves_the_hotspot_even_with_a_phone_connected(tmp_path):
    watch, fake, clock, written = on_wifi(tmp_path)
    ask(watch, clock, "hotspot")
    run_until(watch, clock, clock.t + 10)
    fake.clients = 1
    run_until(watch, clock, clock.t + 30)
    ask(watch, clock, "wifi", command_id="c2")
    run_until(watch, clock, clock.t + 10)
    assert fake.active == {HOME}
    assert written[-1]["reason"] == "switched_back"


def test_a_request_is_acted_on_once(tmp_path):
    watch, fake, clock, written = on_wifi(tmp_path)
    ask(watch, clock, "hotspot", command_id="c1")
    asked_at = clock.t
    run_until(watch, clock, asked_at + 300 + 60)
    assert fake.active == {HOME}  # nobody joined, so it went back
    # the file still says "hotspot" and is read on every tick, but it was already handled
    run_until(watch, clock, clock.t + 600)
    assert fake.active == {HOME}


def test_a_stale_request_is_ignored(tmp_path):
    watch, fake, clock, written = on_wifi(tmp_path)
    ask(watch, clock, "hotspot", age=120)
    run_until(watch, clock, clock.t + 30)
    assert fake.active == {HOME}


def test_a_malformed_request_file_is_ignored(tmp_path):
    watch, fake, clock, written = on_wifi(tmp_path)
    watch.cfg.NET_REQUEST_PATH.write_text("{not json")
    run_until(watch, clock, clock.t + 10)
    watch.cfg.NET_REQUEST_PATH.write_text(json.dumps({"command_id": "x", "mode": "reboot", "requested_at": 1}))
    run_until(watch, clock, clock.t + 10)
    assert fake.active == {HOME}


def test_a_restart_does_not_replay_the_request_it_already_handled(tmp_path):
    watch, fake, clock, written = on_wifi(tmp_path)
    netwatch.write_status(watch.cfg.NET_STATUS_PATH, {"network": "wifi", "handled_request_id": "c1"})
    restarted = netwatch.Netwatch(watch.nm, watch.policy, watch.cfg, mono=lambda: clock.t,
                                  wall=lambda: 1_800_000_000 + clock.t, write=lambda p, d: None,
                                  address=lambda: None, ping=lambda: None)
    ask(watch, clock, "hotspot", command_id="c1")
    restarted.tick()
    assert fake.active == {HOME}


def test_the_status_says_connecting_while_each_saved_network_is_tried(tmp_path):
    watch, fake, clock, written = on_wifi(tmp_path)
    ask(watch, clock, "hotspot")
    run_until(watch, clock, clock.t + 10)
    seen = len(written)
    fake.clients = 0
    run_until(watch, clock, clock.t + 300 + 60)
    assert fake.active == {HOME}
    assert "connecting" in [w["network"] for w in written[seen:]]  # not a stale file that reads as "netwatch is down"


def test_status_file_carries_the_reason_and_the_timeouts_back_to_the_controller(tmp_path):
    path = tmp_path / "network.json"
    netwatch.write_status(path, {"network": "wifi", "clients": None, "address": "192.168.1.50", "updated_at": 1000.0,
                                 "reason": "clients_left", "requested": False, "handled_request_id": "c1",
                                 "connect_timeout_s": 300.0, "empty_grace_s": 60.0})
    status = read_network_status(path, 30, wall=lambda: 1010.0)
    assert status["reason"] == "clients_left" and status["handled_request_id"] == "c1"
    assert status["connect_timeout_s"] == 300.0 and status["empty_grace_s"] == 60.0
