"""Pure rules for the rolling temperature history. No I/O, no clock, no threads, so they are tested without Firebase.

A record is keyed by its collection time in ms, zero-padded so the keys sort by time. The key is the record id:
sending the same record twice writes the same path, so a retry can never create a duplicate, and a backlog that
arrives late still lands at the time it was taken.

Records are written only when something changed (`worth_recording`), so the time between two records says nothing
about an outage. A record flagged `gap` is the one that says it: nothing was recorded before it that it continues.
The web app (src/lib/temp-history.ts) applies the same rule to its live readings and draws a break before a `gap`.
"""

import math
import random

SENSORS = ("bottom", "middle", "top")  # bottom = sensor 1 (GPIO4), middle = sensor 2 (GPIO22), top = sensor 3 (GPIO23)
# Not a tunable: the web app builds the same keys (src/lib/temp-history.ts), so the width is part of the contract.
KEY_DIGITS = 13


def record_key(t_ms):
    return f"{int(t_ms):0{KEY_DIGITS}d}"


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def clean_readings(temps):
    """{zone: °C or None} -> only the zones that have a real reading. A missing sensor is left out, never 0."""
    return {zone: float(temps[zone]) for zone in SENSORS if _number(temps.get(zone))}


def worth_recording(last, readings, min_change):
    """Whether `readings` ({zone: °C}) earn a new record after the previous one, which held `last`.

    Every sensor is judged by the same limit against its own value in that record: one moving `min_change` or
    more is enough, and a sensor that started or stopped reading counts as a change too (a fault must show). With
    no previous record to continue (`last` is None) the answer is always yes.
    """
    if last is None or readings.keys() != last.keys():
        return True
    return any(abs(readings[zone] - last[zone]) >= min_change - 1e-9 for zone in readings)


def payload(row):
    """The Firebase record for a local row (a dict with t_ms, the three sensor columns and optionally gap)."""
    out = {"t": int(row["t_ms"])}
    for zone in SENSORS:
        if _number(row.get(zone)):
            out[zone] = round(float(row[zone]), 2)
    if row.get("gap"):
        out["gap"] = True
    return out


def placed_ms(row_mono, wall_now, mono_now):
    """Collection time of a reading taken while the clock was not synced, once it is: the monotonic clock
    measures how long ago it was, and the wall clock now is trusted."""
    return int(round((wall_now - (mono_now - row_mono)) * 1000))


def backoff_delay(failures, base, cap, jitter=0.2, rand=random.random):
    """Seconds to wait after `failures` failed attempts in a row: doubling from `base`, never above `cap`.

    Jitter keeps a fleet of machines (or the two sync threads) from retrying in step after a shared outage.
    """
    if failures <= 0:
        return 0.0
    delay = min(cap, base * 2 ** min(failures - 1, 30))
    return max(0.0, min(cap, delay + (rand() * 2 - 1) * delay * jitter))


class RemoteWindow:
    """The keys the Pi believes are under temp_history, so old ones can be trimmed without reading everything.

    The Pi is the only writer, so the list stays exact as long as every request's outcome is known. After one
    that is not (a timeout can hide a commit) the window is invalidated and trimming waits for a fresh load.
    """

    def __init__(self, limit):
        self.limit = limit
        self._keys = None  # None: unknown

    @property
    def known(self):
        return self._keys is not None

    def __len__(self):
        return len(self._keys or ())

    def load(self, keys):
        self._keys = sorted(str(k) for k in keys)

    def invalidate(self):
        self._keys = None

    def plan(self, new_keys=(), floor_key=None, chunk=500):
        """Old keys to delete in the same request that stores `new_keys`: only as many as the count is over the
        limit, oldest first, and never one at or after the oldest record the Pi still has to send (`floor_key`)
        or one it is sending now. An unknown window plans nothing."""
        if self._keys is None:
            return []
        new = list(new_keys)
        excess = len(set(self._keys) | set(new)) - self.limit
        if excess <= 0:
            return []
        protected = [k for k in (*new, floor_key) if k is not None]
        floor = min(protected) if protected else None
        oldest = sorted(set(self._keys) | set(new))[:excess]
        return [k for k in oldest if floor is None or k < floor][:chunk]

    def commit(self, new_keys=(), deleted=()):
        """Record a request that is known to have succeeded."""
        if self._keys is None:
            return
        self._keys = sorted((set(self._keys) | set(new_keys)) - set(deleted))
