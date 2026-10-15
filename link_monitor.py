"""What the Pi's link to the cloud really is, judged from results and not from the state of the Wi-Fi interface.

    synced                Firebase answered the last request on every channel (state heartbeat, temperature uploads)
    firebase_unavailable  a request failed or timed out, but the internet itself is reachable
    offline               a request failed and nothing on the internet is reachable
    unknown               nothing has been tried yet, or a failure has not been probed yet

A connected Wi-Fi interface proves neither, so it is never consulted. "Firebase answered" comes from the actual
operations, which report here; "the internet is reachable" comes from a short TCP probe that only runs while
something is failing, with backoff, so a long outage costs a handful of tiny connections.

Nothing here touches the control loop. Callers are the sync threads, and the probe has a hard time limit.
"""

import random
import socket
import threading
import time
from urllib.parse import urlsplit

import config
from temp_history import backoff_delay

SYNCED = "synced"
FIREBASE_UNAVAILABLE = "firebase_unavailable"
OFFLINE = "offline"
UNKNOWN = "unknown"


def _resolves(host, timeout, resolve):
    """True if `host` resolves within `timeout`. getaddrinfo has no timeout of its own, so it runs in a helper."""
    result = []

    def work():
        try:
            resolve(host, 443)
            result.append(True)
        except OSError:
            result.append(False)

    worker = threading.Thread(target=work, daemon=True, name="dns-probe")
    worker.start()
    worker.join(timeout)
    return bool(result and result[0])


def probe_network(cfg=config, connect=socket.create_connection, resolve=socket.getaddrinfo):
    """{"internet": bool, "dns": bool | None}. `internet` means a TCP connection to one of
    INTERNET_PROBE_TARGETS opened (IP literals, so a DNS outage cannot read as no internet). `dns` says whether
    the Firebase host name resolves, and is None when there is no internet to ask."""
    internet = False
    for host, port in cfg.INTERNET_PROBE_TARGETS:
        try:
            connect((host, port), cfg.INTERNET_PROBE_TIMEOUT).close()
            internet = True
            break
        except OSError:
            continue
    if not internet:
        return {"internet": False, "dns": None}
    firebase_host = urlsplit(cfg.DATABASE_URL).hostname
    dns = _resolves(firebase_host, cfg.INTERNET_PROBE_TIMEOUT, resolve) if firebase_host else None
    return {"internet": True, "dns": dns}


class LinkMonitor:
    def __init__(self, probe=probe_network, mono=time.monotonic, cfg=config, rand=random.random):
        self._probe = probe
        self._mono = mono
        self._cfg = cfg
        self._rand = rand
        self._lock = threading.Lock()
        self._channels = {}         # name -> {"ok": bool, "at": mono, "error": str | None}
        self._probe_result = None   # newest probe of the current failure, or None
        self._probe_at = None
        self._probes = 0            # probes made during the current failure, for the backoff
        self._probing = False
        self._status = UNKNOWN
        self._changed_at = mono()
        self._recovered = []        # callbacks run, with the channel's name, when a failing channel answers again

    # ---------------- results reported by the sync threads ----------------
    def firebase_ok(self, channel):
        self._report(channel, True, None)

    def firebase_failed(self, channel, error):
        self._report(channel, False, str(error)[:200] or type(error).__name__)

    def on_recovered(self, callback):
        self._recovered.append(callback)

    def _report(self, channel, ok, error):
        with self._lock:
            was_failing = channel in self._channels and not self._channels[channel]["ok"]
            self._channels[channel] = {"ok": ok, "at": self._mono(), "error": error}
            if ok and not self._failing_locked():
                self._probe_result, self._probe_at, self._probes = None, None, 0  # the next failure starts fresh
            self._refresh_locked()
            recovered = ok and was_failing
        if recovered:
            # per channel, not once everything is clean: the heartbeat getting through is the cue for the
            # uploader to stop waiting out its backoff, even though its own channel has not tried again yet
            for callback in list(self._recovered):
                try:
                    callback(channel)
                except Exception as e:  # a listener must never break a sync thread
                    print("link recovery callback failed:", e)

    # ---------------- internet probe ----------------
    def probe_due(self):
        with self._lock:
            if not self._failing_locked() or self._probing:
                return False
            if self._probe_at is None:
                return True
            wait = backoff_delay(self._probes, self._cfg.INTERNET_PROBE_BACKOFF_BASE,
                                 self._cfg.INTERNET_PROBE_BACKOFF_MAX, rand=self._rand)
            return self._mono() - self._probe_at >= wait

    def check_internet(self):
        """Probe if something is failing and the backoff allows. Slow (bounded by the probe timeouts), so it
        belongs in a sync thread, never the control loop. Returns True when a probe ran."""
        if not self.probe_due():
            return False
        with self._lock:
            if self._probing:
                return False
            self._probing = True
        try:
            result = self._probe()
        except Exception as e:
            result = {"internet": False, "dns": None, "error": str(e)[:100]}
        with self._lock:
            self._probing = False
            self._probe_result = result
            self._probe_at = self._mono()
            self._probes += 1
            self._refresh_locked()
        return True

    # ---------------- reading ----------------
    def status(self):
        with self._lock:
            return self._status

    def snapshot(self):
        """For the local panel: the status and the evidence behind it."""
        with self._lock:
            failed = [c for c in self._channels.values() if not c["ok"]]
            probe = self._probe_result
            return {
                "status": self._status,
                "age_s": round(self._mono() - self._changed_at, 1),
                "internet": None if probe is None else bool(probe.get("internet")),
                "dns": None if probe is None else probe.get("dns"),
                "error": failed[-1]["error"] if failed else None,
            }

    def _failing_locked(self):
        return any(not c["ok"] for c in self._channels.values())

    def _status_locked(self):
        if not self._channels:
            return UNKNOWN
        if not self._failing_locked():
            return SYNCED
        if self._probe_result is None:
            return UNKNOWN
        return FIREBASE_UNAVAILABLE if self._probe_result.get("internet") else OFFLINE

    def _refresh_locked(self):
        status = self._status_locked()
        if status != self._status:
            self._status = status
            self._changed_at = self._mono()
