"""When to raise the hotspot and when to try the home Wi-Fi again. Pure: no I/O, no clock of its own.

Wi-Fi is the default and the hotspot only a fallback. The rules, in order of importance:
  * Never leave the hotspot while a phone is connected to it.
  * "Wi-Fi but no internet" is still Wi-Fi: internet is not an input here at all.
  * Do not scan while the hotspot is up (one radio); the only way to find out whether the router is
    back is to take the hotspot down and try to join it, which is the `probe_wifi` action.
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
    def __init__(self, now, uptime, *, boot_grace, retry_interval, drop_grace, log=None):
        self.boot_grace = boot_grace
        self.retry_interval = retry_interval
        self.drop_grace = drop_grace
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
                self._go(PROBING, "automatic reconnect did not work, trying once explicitly")
                return PROBE_WIFI
            return NONE

        # HOTSPOT
        if wifi_up:
            self._go(WIFI, "joined the home Wi-Fi")
            return NONE
        if not hotspot_up:
            due = self._hotspot_requested is None or now - self._hotspot_requested >= HOTSPOT_RETRY_SECONDS
            if due:
                self._hotspot_requested = now
                return START_HOTSPOT
            return NONE
        # an unreadable client count is treated as "someone is connected": the safe way to be wrong
        if clients is None or clients > 0:
            self._last_client_seen = now
            return NONE
        if now - self._last_client_seen >= self.retry_interval:
            self._go(PROBING, f"hotspot empty for {self.retry_interval:.0f}s, trying the home Wi-Fi")
            return PROBE_WIFI
        return NONE

    def probe_done(self, now, joined):
        """The caller reports the result of a `probe_wifi` action."""
        if joined:
            self._go(WIFI, "home Wi-Fi is back")
        else:
            self._go(HOTSPOT, "home Wi-Fi still unreachable")
            self._last_client_seen = now  # the next probe is a full interval away
            self._hotspot_requested = None  # raise the hotspot again at once
