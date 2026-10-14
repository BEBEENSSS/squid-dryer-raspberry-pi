import json
import subprocess
from types import SimpleNamespace

import config
import netwatch
from net_policy import NetPolicy
from netinfo import read_network_status

HOME = "netplan-wlan0-ANN-ANN 5G"  # the real profile name: it has spaces
HOTSPOT = "squid-hotspot"


def make_cfg(tmp_path):
    values = {k: getattr(config, k) for k in dir(config) if k.startswith("NET_")}
    values["NET_HOTSPOT_PSK_PATH"] = tmp_path / "hotspot_psk"
    values["NET_STATUS_PATH"] = tmp_path / "network.json"
    return SimpleNamespace(**values)


class FakeNmcli:
    """A tiny NetworkManager: profiles with properties, one radio, a home router that can be off."""

    def __init__(self, router_on=True):
        self.profiles = {HOME: {}}
        self.active = set()
        self.router_on = router_on
        self.clients = 0
        self.calls = []

    def __call__(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        assert isinstance(argv, list) and kwargs.get("timeout"), "always an argument list with a timeout"
        out, rc = self._handle(argv)
        return subprocess.CompletedProcess(argv, rc, out, "")

    def _handle(self, argv):
        tool, args = argv[0], argv[1:]
        if tool == "iw":
            if args[:2] == ["dev", "wlan0"] and self.clients is None:
                return "", 1
            return "".join(f"Station aa:bb:cc:dd:ee:{i:02x} (on wlan0)\n\tinactive time: 10 ms\n"
                           for i in range(self.clients or 0)), 0
        if args[0] == "-w":
            args = args[2:]
        if args[:3] == ["-t", "-f", "DEVICE,STATE"]:
            return "lo:unmanaged\nwlan0:connected", 0
        silent = args[0] == "-s"
        if silent:
            args = args[1:]
        prop = None
        if args[0] == "-g":
            prop, args = args[1], args[2:]
        verb = args[1] if args[0] == "connection" else None
        if args[0] == "connection" and args[1] == "show":
            name = args[3]
            if name not in self.profiles:
                return "", 10
            if prop == "GENERAL.STATE":
                return ("activated" if name in self.active else ""), 0
            if prop == "connection.id":
                return name, 0
            return self.profiles[name].get(prop, ""), 0
        if verb == "add":
            name = args[args.index("con-name") + 1]
            self.profiles[name] = {"802-11-wireless.ssid": args[args.index("ssid") + 1]}
            return "", 0
        if verb == "modify":
            self.profiles[args[3]].update(dict(zip(args[4::2], args[5::2])))
            return "", 0
        if verb == "up":
            name = args[3]
            if name == HOME and not self.router_on:
                return "", 4
            self.active = {name}  # one radio: joining one network leaves the other
            return "", 0
        if verb == "down":
            self.active.discard(args[3])
            return "", 0
        raise AssertionError(f"unexpected call {argv}")


def make_watch(tmp_path, router_on=True, uptime=0.0):
    cfg = make_cfg(tmp_path)
    cfg.NET_HOTSPOT_PSK_PATH.write_text("correct horse battery\n")
    fake = FakeNmcli(router_on)
    clock = SimpleNamespace(t=0.0)
    policy = NetPolicy(0.0, uptime, boot_grace=cfg.NET_BOOT_GRACE_SECONDS,
                       retry_interval=cfg.NET_RETRY_INTERVAL_SECONDS,
                       drop_grace=cfg.NET_WIFI_DROP_GRACE_SECONDS)
    written = []
    watch = netwatch.Netwatch(netwatch.Nm(cfg, run=fake), policy, cfg, mono=lambda: clock.t,
                              wall=lambda: 1_800_000_000 + clock.t,
                              write=lambda path, payload: written.append(payload),
                              address=lambda: "192.168.1.50", ping=lambda: None)
    assert watch.preflight() is None
    return watch, fake, clock, written


def run_until(watch, clock, end, step=5):
    while clock.t < end:
        watch.tick()
        clock.t += step


def test_profile_name_with_spaces_is_one_argument_and_no_shell_is_used():
    fake = FakeNmcli()
    nm = netwatch.Nm(config, run=fake)
    assert nm.exists(HOME)
    argv = fake.calls[-1][0]
    assert HOME in argv  # a single element, so no quoting is needed
    assert all("shell" not in kwargs for _, kwargs in fake.calls)


def test_a_hung_or_missing_tool_is_a_failure_not_an_exception():
    def hung(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, kwargs["timeout"])

    def missing(argv, **kwargs):
        raise FileNotFoundError(argv[0])

    assert netwatch.Nm(config, run=hung).is_active(HOME) is False
    assert netwatch.Nm(config, run=missing).clients() is None


def test_station_count_comes_from_iw():
    fake = FakeNmcli()
    nm = netwatch.Nm(config, run=fake)
    fake.clients = 2
    assert nm.clients() == 2
    fake.clients = None
    assert nm.clients() is None


def test_hotspot_profile_is_created_wpa2_on_2_4ghz_without_autoconnect(tmp_path):
    cfg = make_cfg(tmp_path)
    fake = FakeNmcli()
    assert netwatch.Nm(cfg, run=fake).ensure_hotspot_profile("correct horse battery")
    props = fake.profiles[HOTSPOT]
    assert props["802-11-wireless.mode"] == "ap"
    assert props["802-11-wireless.band"] == "bg"
    assert props["connection.autoconnect"] == "no"
    assert props["wifi-sec.key-mgmt"] == "wpa-psk"
    assert props["wifi-sec.proto"] == "rsn" and props["wifi-sec.pairwise"] == "ccmp"
    assert props["ipv4.addresses"] == cfg.NET_HOTSPOT_ADDRESS
    assert HOME in fake.profiles and fake.profiles[HOME] == {}  # the home profile is never touched


def test_hotspot_profile_is_left_alone_when_correct_and_repaired_when_it_drifted(tmp_path):
    cfg = make_cfg(tmp_path)
    fake = FakeNmcli()
    nm = netwatch.Nm(cfg, run=fake)
    nm.ensure_hotspot_profile("correct horse battery")
    fake.calls.clear()
    nm.ensure_hotspot_profile("correct horse battery")
    assert not any("modify" in argv for argv, _ in fake.calls)
    fake.profiles[HOTSPOT]["802-11-wireless.band"] = "a"
    nm.ensure_hotspot_profile("correct horse battery")
    assert fake.profiles[HOTSPOT]["802-11-wireless.band"] == "bg"


def test_preflight_names_the_missing_password(tmp_path):
    cfg = make_cfg(tmp_path)
    watch = netwatch.Netwatch(netwatch.Nm(cfg, run=FakeNmcli()), None, cfg)
    assert "--setup" in watch.preflight()
    cfg.NET_HOTSPOT_PSK_PATH.write_text("short")
    assert "--setup" in watch.preflight()  # shorter than 8 characters is not a valid WPA2 password


def test_preflight_reports_a_missing_home_profile(tmp_path):
    cfg = make_cfg(tmp_path)
    cfg.NET_HOTSPOT_PSK_PATH.write_text("correct horse battery")
    fake = FakeNmcli()
    del fake.profiles[HOME]
    problem = netwatch.Netwatch(netwatch.Nm(cfg, run=fake), None, cfg).preflight()
    assert HOME in problem


def test_scenario_router_boots_slower_than_the_device(tmp_path):
    watch, fake, clock, written = make_watch(tmp_path, router_on=False)
    seen_hotspot = False
    while clock.t < 120:
        watch.tick()
        seen_hotspot |= HOTSPOT in fake.active
        clock.t += 5
    fake.router_on = True  # the router finishes booting well inside the grace period
    while clock.t < 200:
        watch.tick()
        seen_hotspot |= HOTSPOT in fake.active
        clock.t += 5
    assert fake.active == {HOME}
    assert not seen_hotspot
    assert written[-1]["network"] == "wifi"
    assert written[-1]["address"] == "192.168.1.50"


def test_scenario_router_off_raises_the_hotspot_after_the_grace_period(tmp_path):
    watch, fake, clock, written = make_watch(tmp_path, router_on=False)
    run_until(watch, clock, 175)
    assert HOTSPOT not in fake.active
    assert written[-1]["network"] == "connecting"
    run_until(watch, clock, 200)
    assert fake.active == {HOTSPOT}
    assert written[-1]["network"] == "hotspot"
    assert written[-1]["address"] == "10.42.0.1"


def test_scenario_phone_on_the_hotspot_is_not_kicked_off_when_the_router_returns(tmp_path):
    watch, fake, clock, written = make_watch(tmp_path, router_on=False)
    run_until(watch, clock, 200)
    fake.clients = 1
    fake.router_on = True
    run_until(watch, clock, 3000)
    assert fake.active == {HOTSPOT}
    assert written[-1]["clients"] == 1


def test_scenario_device_returns_to_wifi_after_the_phone_leaves(tmp_path):
    watch, fake, clock, written = make_watch(tmp_path, router_on=False)
    run_until(watch, clock, 200)
    fake.clients = 1
    run_until(watch, clock, 600)
    fake.clients = 0
    fake.router_on = True
    left_at = clock.t
    run_until(watch, clock, left_at + 120 - 5)
    assert fake.active == {HOTSPOT}  # not before a full retry interval
    run_until(watch, clock, left_at + 120 + 40)
    assert fake.active == {HOME}
    assert written[-1]["network"] == "wifi"


def test_scenario_failed_retry_puts_the_hotspot_back(tmp_path):
    watch, fake, clock, written = make_watch(tmp_path, router_on=False)
    run_until(watch, clock, 200)
    run_until(watch, clock, 400)  # an empty hotspot probes the router, which is still off
    assert fake.active == {HOTSPOT}
    assert written[-1]["network"] == "hotspot"


def test_scenario_wifi_dropping_leads_to_the_hotspot_only_after_the_grace_and_a_retry(tmp_path):
    watch, fake, clock, written = make_watch(tmp_path, router_on=True)
    run_until(watch, clock, 30)
    assert fake.active == {HOME}
    fake.router_on = False
    fake.active.clear()  # the link drops
    drop_at = clock.t
    run_until(watch, clock, drop_at + 30)
    assert HOTSPOT not in fake.active
    run_until(watch, clock, drop_at + 120)
    assert fake.active == {HOTSPOT}


def test_status_file_is_read_back_by_the_controller_and_ignored_when_stale(tmp_path):
    path = tmp_path / "network.json"
    netwatch.write_status(path, {"network": "hotspot", "clients": 1, "address": "10.42.0.1", "updated_at": 1000.0})
    assert read_network_status(path, 30, wall=lambda: 1010.0) == {
        "network": "hotspot", "clients": 1, "address": "10.42.0.1"}
    assert read_network_status(path, 30, wall=lambda: 1100.0) is None  # netwatch stopped writing
    assert read_network_status(tmp_path / "missing.json", 30) is None
    path.write_text("{not json")
    assert read_network_status(path, 30) is None
    path.write_text(json.dumps({"network": "carrier-pigeon", "updated_at": 1000.0}))
    assert read_network_status(path, 30, wall=lambda: 1000.0) is None
