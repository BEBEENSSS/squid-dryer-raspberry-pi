"""Target-temperature detection for the warm-up phase. Pure logic, no I/O.

A raw reading is never trusted on its own: readings go through a moving median, and the
target only counts as reached once the smoothed value has stayed in the band for the whole
dwell. Missing samples restart the dwell, so a failed read can never look like "reached".
"""

from collections import deque
from dataclasses import dataclass
from statistics import median


@dataclass
class Sample:
    smoothed: float
    full: bool                  # the median window is full, so the value can be acted on
    confirmed: bool = False     # the target has been held for the whole dwell
    first_mono: float = None    # first sample of the qualifying dwell window
    first_wall: float = None


class TargetDetector:
    def __init__(self, target_c, tolerance_c, dwell_s, window, max_gap_s):
        self.band_floor = target_c - tolerance_c
        self.dwell_s = dwell_s
        self.window = max(1, int(window))
        self.max_gap_s = max_gap_s
        self.reset()

    def reset(self):
        self._values = deque(maxlen=self.window)
        self._last_mono = None
        self._since = None      # (mono, wall) of the first sample of the current dwell
        self.smoothed = None

    def feed(self, mono, wall, raw):
        """Add one valid reading."""
        if self._last_mono is not None and mono - self._last_mono > self.max_gap_s:
            self.reset()  # readings went missing: nothing is known about the gap
        self._last_mono = mono
        self._values.append(raw)
        self.smoothed = median(self._values)
        full = len(self._values) == self.window

        if not full or self.smoothed < self.band_floor:
            self._since = None  # dropped out of the band: the dwell starts over
            return Sample(self.smoothed, full)
        if self._since is None:
            self._since = (mono, wall)
        if mono - self._since[0] >= self.dwell_s:
            return Sample(self.smoothed, full, True, *self._since)
        return Sample(self.smoothed, full)


class DipTracker:
    """Excursions below the band after drying has started. They never stop the timer."""

    def __init__(self, band_floor_c, limit_s):
        self.band_floor = band_floor_c
        self.limit_s = limit_s
        self.start = None
        self.min_c = None

    @property
    def open(self):
        return self.start is not None

    def restore(self, start_wall, min_c):
        self.start, self.min_c = start_wall, min_c

    def feed(self, wall, smoothed):
        """Returns ("opened" | "closed" | None, start, end, min_c, warning)."""
        if smoothed < self.band_floor:
            if self.start is None:
                self.start, self.min_c = wall, smoothed
                return "opened", self.start, None, self.min_c, False
            self.min_c = min(self.min_c, smoothed)
            return None, self.start, None, self.min_c, wall - self.start > self.limit_s
        if self.start is None:
            return None, None, None, None, False
        done = ("closed", self.start, wall, self.min_c, wall - self.start > self.limit_s)
        self.start = self.min_c = None
        return done
