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

    @property
    def since(self):
        """(mono, wall) of the first sample of the current in-band stretch, or None when out of band."""
        return self._since

    @property
    def last_mono(self):
        return self._last_mono

    @property
    def full(self):
        return len(self._values) == self.window

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


class ZoneDetector:
    """Warm-up across the chamber: every required zone has to hold its own band at the same time.

    Each zone gets its own moving median and its own in-band stretch (a TargetDetector with the zone's
    floor). The target counts as reached once the LAST zone to qualify has stayed in band for the dwell,
    so the drying clock is backdated to the first sample at which all of them held. A zone with no fresh
    reading inside max_gap_s is reset, like a gap in a single detector: a failed read never counts as
    "reached". Zones in `floors` but not in `required` (a bypassed sensor) are still smoothed, for the log.
    """

    def __init__(self, floors, required, dwell_s, window, max_gap_s, control="bottom"):
        self.required = tuple(z for z in floors if z in required)
        self.control = control
        self.dwell_s = dwell_s
        self.max_gap_s = max_gap_s
        self._det = {zone: TargetDetector(floor, 0.0, dwell_s, window, max_gap_s) for zone, floor in floors.items()}
        self._reported = set()

    @property
    def zones(self):
        return tuple(self._det)

    def feed(self, zone, mono, wall, raw):
        """Add one valid reading for a zone. Returns that zone's own Sample (never the joint verdict)."""
        return self._det[zone].feed(mono, wall, raw)

    def smoothed(self, zone):
        return self._det[zone].smoothed

    def in_band(self, zone):
        return self._det[zone].since is not None

    def floor(self, zone):
        return self._det[zone].band_floor

    def newly_in_band(self):
        """Zones that entered their band since the last call, bottom to top. Each is reported once."""
        fresh = [z for z in self._det if z not in self._reported and self.in_band(z)]
        self._reported.update(fresh)
        return fresh

    def result(self, now):
        """The joint verdict at monotonic time `now`, in the shape of a single detector's Sample.

        `smoothed` and `full` are the control zone's, so dip tracking and the report work as before.
        """
        control = self._det[self.control]
        smoothed, full = control.smoothed, control.full
        since = []
        for zone in self.required:
            det = self._det[zone]
            if det.last_mono is None or now - det.last_mono > self.max_gap_s:
                det.reset()  # nothing is known about the gap
                return Sample(smoothed, full)
            if det.since is None:
                return Sample(smoothed, full)
            since.append(det.since)
        if not since:
            return Sample(smoothed, full)
        first = max(since, key=lambda s: s[0])  # the zone that qualified last starts the joint dwell
        if now - first[0] >= self.dwell_s:
            return Sample(smoothed, full, True, *first)
        return Sample(smoothed, full)


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
