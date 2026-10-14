"""Keeps the machine reachable from a phone: home Wi-Fi first, its own hotspot as the fallback.

Runs as its own service so a stuck nmcli can never delay the relay control loop. It talks to
NetworkManager through nmcli (argument lists, never a shell, so profile names with spaces are safe)
and tells the controller what it did through a small status file.

    python netwatch.py            run (what the systemd unit does)
    python netwatch.py --setup    create or repair the hotspot profile and store its password
"""

import argparse
import datetime
import getpass
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import config
import sd_notify
from net_policy import BOOT_WAIT, PROBE_WIFI, START_HOTSPOT, WIFI_LOST, NetPolicy
from netinfo import lan_ip


def log(message):
    stamp = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
    print(f"{stamp} netwatch: {message}", flush=True)


class Nm:
    """The few NetworkManager and iw calls netwatch needs. `run` is injectable for tests."""

    def __init__(self, cfg=config, run=subprocess.run):
        self.cfg = cfg
        self._run = run

    def call(self, command, args, timeout=20):
        """(ok, stdout). A missing tool or a hung call is a failure, never an exception."""
        try:
            done = self._run([command, *args], capture_output=True, text=True, timeout=timeout)
        except (OSError, subprocess.TimeoutExpired) as e:
            log(f"{command} {' '.join(args[:3])} failed: {e}")
            return False, ""
        return done.returncode == 0, (done.stdout or "").strip()

    def nmcli(self, args, timeout=20):
        return self.call("nmcli", args, timeout)

    def connection_state(self, name):
        """'activated', 'activating', or '' when the profile is not active."""
        ok, out = self.nmcli(["-g", "GENERAL.STATE", "connection", "show", "id", name], timeout=10)
        return out if ok else ""

    def is_active(self, name):
        return self.connection_state(name) == "activated"

    def exists(self, name):
        ok, _ = self.nmcli(["-g", "connection.id", "connection", "show", "id", name], timeout=10)
        return ok

    def up(self, name, wait_seconds):
        ok, _ = self.nmcli(["-w", str(int(wait_seconds)), "connection", "up", "id", name],
                           timeout=wait_seconds + 10)
        return ok

    def down(self, name):
        ok, _ = self.nmcli(["connection", "down", "id", name], timeout=15)
        return ok

    def clients(self):
        """Phones on the hotspot, or None if that could not be read."""
        ok, out = self.call("iw", ["dev", self.cfg.NET_IFACE, "station", "dump"], timeout=10)
        if not ok:
            return None
        return sum(1 for line in out.splitlines() if line.startswith("Station "))

    def interface_managed(self):
        ok, out = self.nmcli(["-t", "-f", "DEVICE,STATE", "device"], timeout=10)
        if not ok:
            return False
        for line in out.splitlines():
            device, _, state = line.partition(":")
            if device == self.cfg.NET_IFACE:
                return state != "unmanaged"
        return False

    def regulatory_country_set(self):
        ok, out = self.call("iw", ["reg", "get"], timeout=10)
        return ok and "country 00" not in out and "country " in out

    def _get(self, name, prop):
        ok, out = self.nmcli(["-s", "-g", prop, "connection", "show", "id", name], timeout=10)
        return out if ok else None

    def ensure_hotspot_profile(self, psk):
        """Create the AP profile, or repair any setting that drifted. Safe to run on every start.

        autoconnect stays off on purpose: the hotspot is raised by netwatch's rules, never by
        NetworkManager on its own, otherwise it would steal the radio from the home Wi-Fi.
        """
        cfg = self.cfg
        name = cfg.NET_HOTSPOT_CONNECTION
        if not self.exists(name):
            ok, _ = self.nmcli(["connection", "add", "type", "wifi", "ifname", cfg.NET_IFACE,
                                "con-name", name, "autoconnect", "no", "ssid", cfg.NET_HOTSPOT_SSID])
            if not ok:
                return False
            log(f"created hotspot profile '{name}'")
        wanted = [
            ("connection.autoconnect", "no"),
            ("802-11-wireless.ssid", cfg.NET_HOTSPOT_SSID),
            ("802-11-wireless.mode", "ap"),
            ("802-11-wireless.band", cfg.NET_HOTSPOT_BAND),
            ("802-11-wireless.channel", str(cfg.NET_HOTSPOT_CHANNEL)),
            ("ipv4.method", "shared"),
            ("ipv4.addresses", cfg.NET_HOTSPOT_ADDRESS),
            ("ipv6.method", "disabled"),
            ("wifi-sec.key-mgmt", "wpa-psk"),
            ("wifi-sec.proto", "rsn"),
            ("wifi-sec.pairwise", "ccmp"),
            ("wifi-sec.group", "ccmp"),
            ("wifi-sec.psk", psk),
        ]
        drifted = [(prop, value) for prop, value in wanted if self._get(name, prop) != value]
        if drifted:
            args = ["connection", "modify", "id", name]
            for prop, value in drifted:
                args += [prop, value]
            ok, _ = self.nmcli(args)
            if not ok:
                return False
            log("repaired hotspot profile: " + ", ".join(p for p, _ in drifted if p != "wifi-sec.psk"))
        return True


def read_psk(path):
    """The hotspot password, or None if it is missing or not a valid WPA2 passphrase."""
    try:
        psk = Path(path).read_text().strip()
    except OSError:
        return None
    return psk if 8 <= len(psk) <= 63 else None


def write_status(path, payload):
    """Atomic, so the controller never reads half a file."""
    path = Path(path)
    tmp = path.with_name(path.name + ".tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(payload))
        os.replace(tmp, path)
    except OSError as e:
        log(f"could not write {path}: {e}")


class Netwatch:
    def __init__(self, nm, policy, cfg=config, mono=time.monotonic, wall=time.time,
                 write=write_status, address=lan_ip, ping=sd_notify.watchdog):
        self.nm = nm
        self.policy = policy
        self.cfg = cfg
        self.mono = mono
        self.wall = wall
        self.write = write
        self.address = address
        self.ping = ping
        self._assisted_at = None
        self._network = None
        self._since = wall()

    def preflight(self):
        """Why netwatch cannot work yet, or None. Until it can, it touches nothing."""
        cfg = self.cfg
        if not self.nm.interface_managed():
            return f"NetworkManager does not manage {cfg.NET_IFACE}"
        if not self.nm.exists(cfg.NET_WIFI_CONNECTION):
            return f"home Wi-Fi profile '{cfg.NET_WIFI_CONNECTION}' not found (see `nmcli connection show`)"
        psk = read_psk(cfg.NET_HOTSPOT_PSK_PATH)
        if psk is None:
            return f"no valid hotspot password in {cfg.NET_HOTSPOT_PSK_PATH} (run: python netwatch.py --setup)"
        if not self.nm.ensure_hotspot_profile(psk):
            return "could not create the hotspot profile"
        return None

    def _probe(self):
        """Hotspot down, home Wi-Fi up. True if the home Wi-Fi was joined."""
        cfg = self.cfg
        if self.nm.is_active(cfg.NET_HOTSPOT_CONNECTION):
            self.nm.down(cfg.NET_HOTSPOT_CONNECTION)
        self.ping()
        self.nm.up(cfg.NET_WIFI_CONNECTION, cfg.NET_WIFI_TRY_TIMEOUT_SECONDS)
        self.ping()
        return self.nm.is_active(cfg.NET_WIFI_CONNECTION)

    def _assist(self, now):
        """While waiting for the router, ask for the connection explicitly now and then.

        NetworkManager gives up on autoconnect after a few failures, which would turn a router
        that is merely slow into a needless hotspot.
        """
        if self._assisted_at is not None and now - self._assisted_at < self.cfg.NET_ASSIST_SECONDS:
            return
        self._assisted_at = now
        if self.nm.connection_state(self.cfg.NET_WIFI_CONNECTION) == "":
            self.nm.up(self.cfg.NET_WIFI_CONNECTION, self.cfg.NET_ASSIST_TIMEOUT_SECONDS)

    def tick(self):
        cfg = self.cfg
        now = self.mono()
        wifi = self.nm.is_active(cfg.NET_WIFI_CONNECTION)
        hotspot = self.nm.is_active(cfg.NET_HOTSPOT_CONNECTION)
        clients = self.nm.clients() if hotspot else 0
        action = self.policy.step(now, wifi_up=wifi, hotspot_up=hotspot, clients=clients)

        if action == START_HOTSPOT:
            log("starting the hotspot")
            self.nm.up(cfg.NET_HOTSPOT_CONNECTION, 30)
            hotspot = self.nm.is_active(cfg.NET_HOTSPOT_CONNECTION)
            log("hotspot is up" if hotspot else "hotspot did not start, will retry")
            wifi = False
        elif action == PROBE_WIFI:
            log("trying the home Wi-Fi")
            wifi = self._probe()
            hotspot = False if wifi else hotspot
            self.policy.probe_done(self.mono(), wifi)
        elif self.policy.state in (BOOT_WAIT, WIFI_LOST) and not wifi:
            self._assist(now)

        self._publish(wifi, hotspot, clients)

    def _publish(self, wifi, hotspot, clients):
        cfg = self.cfg
        if wifi:
            network, address = "wifi", self.address()
        elif hotspot:
            network, address = "hotspot", cfg.NET_HOTSPOT_ADDRESS.split("/")[0]
        else:
            network, address = "connecting", None
        if network != self._network:
            log(f"network is now {network}" + (f" ({address})" if address else ""))
            self._network = network
            self._since = self.wall()
        self.write(cfg.NET_STATUS_PATH, {
            "network": network, "clients": clients if hotspot else None, "address": address,
            "since": self._since, "updated_at": self.wall(),
        })


def system_uptime():
    try:
        return float(Path("/proc/uptime").read_text().split()[0])
    except (OSError, ValueError, IndexError):
        return 0.0


def run_forever(stop):
    cfg = config
    nm = Nm(cfg)
    policy = NetPolicy(time.monotonic(), system_uptime(), boot_grace=cfg.NET_BOOT_GRACE_SECONDS,
                       retry_interval=cfg.NET_RETRY_INTERVAL_SECONDS,
                       drop_grace=cfg.NET_WIFI_DROP_GRACE_SECONDS, log=log)
    watch = Netwatch(nm, policy, cfg)
    log(f"started, uptime {system_uptime():.0f}s, home profile '{cfg.NET_WIFI_CONNECTION}'")
    if not nm.regulatory_country_set():
        log("WARNING: no Wi-Fi regulatory country is set; the hotspot may not start (raspi-config, Localisation)")
    ready = False
    last_problem = None
    while not stop.is_set():
        problem = watch.preflight()
        if problem:
            if problem != last_problem:
                log(f"cannot work yet: {problem}")
            last_problem = problem
            sd_notify.watchdog()
            stop.wait(10)
            continue
        if last_problem:
            log("preflight passed")
        break  # the profile is verified once up front, after that only the cheap state is polled
    while not stop.is_set():
        try:
            watch.tick()
        except Exception as e:  # a surprise must not end the service: the machine still has to be reachable
            log(f"tick failed: {e!r}")
        if not ready:
            sd_notify.ready()
            ready = True
        sd_notify.watchdog()
        stop.wait(cfg.NET_POLL_SECONDS)


def setup():
    cfg = config
    nm = Nm(cfg)
    if not nm.interface_managed():
        sys.exit(f"NetworkManager does not manage {cfg.NET_IFACE}. Check `nmcli device`.")
    if not nm.exists(cfg.NET_WIFI_CONNECTION):
        sys.exit(f"Home Wi-Fi profile '{cfg.NET_WIFI_CONNECTION}' not found. Check `nmcli connection show`.")
    psk = read_psk(cfg.NET_HOTSPOT_PSK_PATH)
    if psk is None:
        psk = getpass.getpass("Hotspot password (8 to 63 characters): ")
        if not 8 <= len(psk) <= 63:
            sys.exit("The password must be 8 to 63 characters.")
        cfg.NET_HOTSPOT_PSK_PATH.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(cfg.NET_HOTSPOT_PSK_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(psk + "\n")
        print(f"Saved to {cfg.NET_HOTSPOT_PSK_PATH}")
    if not nm.ensure_hotspot_profile(psk):
        sys.exit("Could not create the hotspot profile (run as root).")
    print(f"Hotspot profile '{cfg.NET_HOTSPOT_CONNECTION}' is ready: SSID {cfg.NET_HOTSPOT_SSID}, "
          f"address {cfg.NET_HOTSPOT_ADDRESS.split('/')[0]}, band {cfg.NET_HOTSPOT_BAND}.")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--setup", action="store_true", help="create the hotspot profile and store its password")
    args = parser.parse_args()
    if args.setup:
        setup()
        return
    import signal
    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    run_forever(stop)


if __name__ == "__main__":
    main()
