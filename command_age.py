"""When a command from the cloud is too old to act on. Pure, so it is easy to test.

A browser can queue a write while the Pi is offline and deliver it long after the operator has moved
on. Ages here are durations (server time minus the time the client stamped), so the Pi clock never
has to be right.
"""

import time


class LocalCommandMark:
    """Remembers when the operator last used the local panel (monotonic, so clock jumps do not matter)."""

    def __init__(self, mono=time.monotonic):
        self._mono = mono
        self._at = None

    def touch(self):
        self._at = self._mono()

    def since(self):
        """Seconds since the last local command, or None if there has not been one."""
        return None if self._at is None else self._mono() - self._at


def cloud_verdict(age_s, since_local_s, max_age_s):
    """None when the command may run, otherwise why not.

    A cloud command that was created before the latest local command is stale even when it is
    young: the operator standing at the machine always wins over a request from somewhere else.
    """
    if age_s > max_age_s:
        return "too_old"
    if since_local_s is not None and age_s > since_local_s:
        return "superseded_by_local"
    return None
