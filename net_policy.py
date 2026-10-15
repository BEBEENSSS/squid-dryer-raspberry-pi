"""When to raise the hotspot and when to try the home Wi-Fi again. Pure: no I/O, no clock of its own.

Wi-Fi is the default and the hotspot only a fallback. The rules, in order of importance:
  * Never leave the hotspot while a phone is connected to it.
  * "Wi-Fi but no internet" is still Wi-Fi: internet is not an input here at all.
  * Do not scan while the hotspot is up (one radio); the only way to find out whether the router is
    back is to take the hotspot down and try to join it, which is the `probe_wifi` action.

A hotspot an operator asked for (`request_hotspot`) follows its own, shorter rules: it gives up if
nobody joins within `connect_timeout`, and leaves `empty_grace` after the last phone has gone.
`reason` says why the current mode was entered, so the web app can explain a return to Wi-Fi.
"""

BOOT_WAIT = "boot_wait"    # just powered on: the router may still be starting
WIFI = "wifi"
WIFI_LOST = "wifi_lost"    # was on Wi-Fi, link dropped; NetworkManager is retrying by itself
HOTSPOT = "hotspot"
PROBING = "probing"        # hotspot is down while the home Wi-Fi is being tried

NONE = "none"
START_HOTSPOT = "start_hotspot"
PROBE_WIFI = "probe_wifi"

HOTSPOT_RETRY_SECONDS = 10  # a hotspot that failed to start is requested again this often


class NetPolicy:
    def __init__(self, now, uptime, *, boot_grace, retry_interval, drop_grace,
                 connect_timeout=300.0, empty_grace=60.0, log=None):
        self.boot_grace = boot_grace
        self.retry_interval = retry_interval
        self.drop_grace = drop_grace
        self.connect_timeout = connect_timeout
        self.empty_grace = empty_grace
        self.reason = "boot"
        self.requested = False       # the hotspot was asked for, not raised as a fallback
        self._client_seen = False    # a phone has joined this requested hotspot
        self._requested_at = None
        self._probe_reason = "wifi_back"
        self._log = log or (lambda message: None)
        self.state = BOOT_WAIT
        # a netwatch restarted long after boot must not wait out a grace period that already passed
        self._boot_deadline = now + max(0.0, boot_grace - uptime)
        self._lost_since = None
        self._last_client_seen = now
        self._hotspot_requested = None

    def _go(self, state, reason):
        if state != self.state:
            self._log(f"{self.state} -> {state} ({reason})")
        self.state = state

    def _need_hotspot(self, now, reason):
        self.requested = False
        self.reason = "fallback"
        self._go(HOTSPOT, reason)
        self._last_client_seen = now
        self._hotspot_requested = now
        return START_HOTSPOT

    def step(self, now, *, wifi_up, hotspot_up, clients):
        """`clients` is the number of stations on the hotspot, or None when it could not be read."""
        if self.state == PROBING:
            return NONE  # the caller is blocked in the probe and reports back with probe_done()

        if self.state == BOOT_WAIT:
            if wifi_up:
                self.reason = "boot"
                self._go(WIFI, "joined the home Wi-Fi")
            elif now >= self._boot_deadline:
                return self._need_hotspot(now, "home Wi-Fi did not appear within the boot grace period")
            return NONE

        if self.state == WIFI:
            if not wifi_up:
                self._lost_since = now
                self._go(WIFI_LOST, "Wi-Fi link dropped")
            return NONE

        if self.state == WIFI_LOST:
            if wifi_up:
                self._go(WIFI, "Wi-Fi link is back")
            elif now - self._lost_since >= self.drop_grace:
                self._probe_reason = "wifi_back"
                self._go(PROBING, "automatic reconnect did not work, trying once explicitly")
                return PROBE_WIFI
            return NONE

        # HOTSPOT
        if wifi_up and not self.requested:
            self.reason = "wifi_back"
            self._go(WIFI, "joined the home Wi-Fi")
            return NONE
        if not hotspot_up:
            if self.requested and now - self._requested_at >= self.connect_timeout:
                return self._give_up(now, "no_client_timeout", "requested hotspot never came up")
            due = self._hotspot_requested is None or now - self._hotspot_requested >= HOTSPOT_RETRY_SECONDS
            if due:
                self._hotspot_requested = now
                return START_HOTSPOT
            return NONE
        # an unreadable client count is treated as "someone is connected": the safe way to be wrong
        if clients is None or clients > 0:
            self._last_client_seen = now
            self._client_seen = True
            return NONE
        if self.requested:
            if not self._client_seen:
                if now - self._requested_at >= self.connect_timeout:
                    return self._give_up(now, "no_client_timeout",
                                         f"nobody joined the hotspot within {self.connect_timeout:.0f}s")
            elif now - self._last_client_seen >= self.empty_grace:
                return self._give_up(now, "clients_left",
                                     f"hotspot empty for {self.empty_grace:.0f}s")
            return NONE
        if now - self._last_client_seen >= self.retry_interval:
            self._probe_reason = "wifi_back"
            self._go(PROBING, f"hotspot empty for {self.retry_interval:.0f}s, trying the home Wi-Fi")
            return PROBE_WIFI
        return NONE

    def _give_up(self, now, reason, detail):
        self._probe_reason = reason
        self._go(PROBING, detail)
        return PROBE_WIFI

    def request_hotspot(self, now):
        """An operator asked for the hotspot. Returns the action to run right away."""
        was_hotspot = self.state == HOTSPOT
        self.requested = True
        self.reason = "requested"
        self._client_seen = False
        self._requested_at = now
        self._last_client_seen = now
        self._go(HOTSPOT, "hotspot requested")
        if was_hotspot:
            return NONE  # already up as a fallback: keep the radio as it is, only the rules change
        self._hotspot_requested = now
        return START_HOTSPOT

    def request_wifi(self, now):
        """An operator asked to leave the hotspot. Returns the action to run right away."""
        if self.state in (WIFI, PROBING):
            return NONE
        self.requested = False
        self._probe_reason = "switched_back"
        self._go(PROBING, "Wi-Fi requested")
        return PROBE_WIFI

    def probe_done(self, now, joined):
        """The caller reports the result of a `probe_wifi` action."""
        self.requested = False
        if joined:
            self.reason = self._probe_reason
            self._go(WIFI, "home Wi-Fi is back")
        else:
            self.reason = "fallback"
            self._go(HOTSPOT, "home Wi-Fi still unreachable")
            self._last_client_seen = now  # the next probe is a full interval away
            self._hotspot_requested = None  # raise the hotspot again at once
