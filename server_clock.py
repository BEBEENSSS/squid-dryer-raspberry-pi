"""Estimate of Firebase server time, for ordering and ageing commands.

The Pi has no battery-backed clock, so its wall clock can be wrong for minutes after boot.
A server timestamp is read once, then carried forward on the monotonic clock, which never jumps.
"""

import time


class ServerClock:
    def __init__(self, mono=time.monotonic):
        self._mono = mono
        self._ref = None  # (server_ms, monotonic seconds when it was read)

    def set(self, server_ms):
        self._ref = (float(server_ms), self._mono())

    def now_ms(self):
        """Server time in ms, or None when Firebase has not been reached since this boot."""
        if self._ref is None:
            return None
        server_ms, at = self._ref
        return server_ms + (self._mono() - at) * 1000.0

    def age_seconds(self):
        """How old the reading is, or None. Used to decide when to refresh it."""
        return None if self._ref is None else self._mono() - self._ref[1]
