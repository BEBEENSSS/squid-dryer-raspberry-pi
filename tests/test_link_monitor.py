from types import SimpleNamespace

import config
from link_monitor import FIREBASE_UNAVAILABLE, OFFLINE, SYNCED, UNKNOWN, LinkMonitor, probe_network


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def make(internet=True, dns=True):
    clock = Clock()
    probes = []

    def probe():
        probes.append(clock.now)
        return {"internet": internet, "dns": dns if internet else None}

    cfg = SimpleNamespace(INTERNET_PROBE_BACKOFF_BASE=10, INTERNET_PROBE_BACKOFF_MAX=120)
    link = LinkMonitor(probe=probe, mono=clock, cfg=cfg, rand=lambda: 0.5)
    return link, clock, probes


def test_unknown_until_something_has_been_tried():
    link, _, _ = make()
    assert link.status() == UNKNOWN


def test_synced_after_a_successful_round_trip():
    link, _, _ = make()
    link.firebase_ok("state")
    assert link.status() == SYNCED


def test_a_failure_is_unknown_until_probed_then_internet_decides_which_failure_it_is():
    link, _, probes = make(internet=True)
    link.firebase_ok("state")
    link.firebase_failed("state", TimeoutError("timed out"))
    assert link.status() == UNKNOWN and not probes  # reporting a failure never blocks on a probe
    assert link.check_internet()
    assert link.status() == FIREBASE_UNAVAILABLE
    assert link.snapshot()["internet"] is True and "timed out" in link.snapshot()["error"]

    link, _, _ = make(internet=False)
    link.firebase_failed("state", ConnectionError("down"))
    link.check_internet()
    assert link.status() == OFFLINE and link.snapshot()["internet"] is False


def test_dns_trouble_is_reported_as_detail_while_the_internet_still_counts_as_up():
    link, _, _ = make(internet=True, dns=False)
    link.firebase_failed("history", OSError("name resolution"))
    link.check_internet()
    snap = link.snapshot()
    assert snap["status"] == FIREBASE_UNAVAILABLE and snap["dns"] is False


def test_probing_backs_off_while_failures_continue():
    link, clock, probes = make(internet=False)
    link.firebase_failed("state", ConnectionError("down"))
    assert link.check_internet()                 # immediately
    clock.now += 5
    assert not link.check_internet()             # too soon
    clock.now += 5
    assert link.check_internet()                 # 10 s after the first
    clock.now += 19
    assert not link.check_internet()
    clock.now += 1
    assert link.check_internet()                 # then 20 s
    assert len(probes) == 3


def test_no_probe_runs_while_everything_works():
    link, _, probes = make()
    link.firebase_ok("state")
    assert not link.check_internet() and not probes


def test_synced_needs_every_channel_to_work():
    link, _, _ = make(internet=True)
    link.firebase_ok("state")
    link.firebase_failed("history", TimeoutError("slow"))  # heartbeat fine, uploads not
    link.check_internet()
    assert link.status() == FIREBASE_UNAVAILABLE
    link.firebase_ok("history")
    assert link.status() == SYNCED


def test_recovery_clears_the_probe_and_starts_the_next_failure_fresh():
    link, clock, probes = make(internet=False)
    link.firebase_failed("state", ConnectionError("down"))
    link.check_internet()
    link.firebase_ok("state")
    assert link.status() == SYNCED and link.snapshot()["internet"] is None
    link.firebase_failed("state", ConnectionError("down again"))
    assert link.check_internet() and len(probes) == 2  # no leftover backoff from the earlier outage


def test_listeners_hear_which_channel_recovered_and_a_broken_one_does_not_break_the_link():
    link, _, _ = make()
    heard = []
    link.on_recovered(lambda channel: 1 / 0)
    link.on_recovered(heard.append)
    link.firebase_failed("state", ConnectionError("x"))
    link.firebase_ok("history")      # a channel that never failed is not a recovery
    link.firebase_ok("state")
    assert heard == ["state"]


def test_a_probe_that_raises_counts_as_no_internet():
    link, _, _ = make()
    link._probe = lambda: 1 / 0
    link.firebase_failed("state", ConnectionError("x"))
    link.check_internet()
    assert link.status() == OFFLINE


# ---- the real probe, with the network replaced ----
def cfg(targets):
    return SimpleNamespace(INTERNET_PROBE_TARGETS=targets, INTERNET_PROBE_TIMEOUT=0.2,
                           DATABASE_URL=config.DATABASE_URL)


class Conn:
    def close(self):
        pass


def test_probe_uses_ip_literals_and_stops_at_the_first_target_that_answers():
    tried = []

    def connect(addr, timeout):
        tried.append(addr)
        if addr[0] == "1.1.1.1":
            raise OSError("unreachable")
        return Conn()

    got = probe_network(cfg((("1.1.1.1", 443), ("8.8.8.8", 53), ("9.9.9.9", 443))), connect=connect,
                        resolve=lambda host, port: [])
    assert got == {"internet": True, "dns": True}
    assert tried == [("1.1.1.1", 443), ("8.8.8.8", 53)]


def test_probe_finds_no_internet_when_every_target_refuses_and_skips_the_dns_check():
    resolved = []

    def connect(addr, timeout):
        raise OSError("unreachable")

    got = probe_network(cfg((("1.1.1.1", 443),)), connect=connect, resolve=lambda *a: resolved.append(a))
    assert got == {"internet": False, "dns": None} and not resolved


def test_probe_reports_a_dns_failure_separately_from_internet():
    def resolve(host, port):
        raise OSError("name or service not known")

    got = probe_network(cfg((("1.1.1.1", 443),)), connect=lambda addr, timeout: Conn(), resolve=resolve)
    assert got == {"internet": True, "dns": False}


def test_probe_does_not_wait_forever_for_a_resolver_that_hangs():
    import threading
    import time
    release = threading.Event()

    def resolve(host, port):
        release.wait(5)
        return []

    started = time.monotonic()
    got = probe_network(cfg((("1.1.1.1", 443),)), connect=lambda addr, timeout: Conn(), resolve=resolve)
    release.set()
    assert got["dns"] is False and time.monotonic() - started < 2
