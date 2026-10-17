"""Preheat: bring the empty chamber to the saved target without starting a drying task. Pure logic, no relays.

"Ready" means exactly what the drying warm-up means by it: every zone being checked has held its band (the target
minus the tolerance, and that zone's allowance) for the dwell, judged on the same moving medians (`warmup.py`). So a
chamber that is ready here is one a task's own warm-up confirms after the dwell. The session only judges; the heater
is held by the manual thermostat in `dryer_cloud.ManualControl`, which keeps every safety check it already has.
"""

import warmup

OFF, HEATING, READY, FAILED = "off", "heating", "ready", "failed"


def zone_floors(target_c, tolerance_c, allowance, zones, control):
    """Each zone's band floor: the control zone's is the shared one, the others may lag it by their allowance."""
    floors = {control: target_c - tolerance_c}
    for zone in zones:
        if zone != control:
            floors[zone] = target_c - tolerance_c - float(allowance.get(zone, 0.0))
    return floors


class PreheatSession:
    """One preheat, from the operator asking for it until it ends. Never holds a relay."""

    def __init__(self, *, target_c, tolerance_c, dwell_s, window, max_gap_s, timeout_s, allowance,
                 control, zones, required, mono, wall):
        self.tolerance_c, self.dwell_s, self.window = float(tolerance_c), float(dwell_s), int(window)
        self.max_gap_s, self.timeout_s = float(max_gap_s), float(timeout_s)
        self.allowance, self.control = dict(allowance), control
        self.zone_names, self.required = tuple(zones), tuple(required)
        self.mono, self.wall = mono, wall
        self.started_mono, self.started_wall = mono(), wall()
        self.ready_wall = None      # the first time it was ready; the timeout stops mattering from then on
        self.confirmed = False
        self._newest = None
        self.rebuild(target_c)

    def rebuild(self, target_c):
        """A new target: the hold starts over against the new floors. The timeout keeps counting."""
        self.target_c = float(target_c)
        floors = zone_floors(self.target_c, self.tolerance_c, self.allowance, self.zone_names, self.control)
        self.detector = warmup.ZoneDetector(floors, self.required, self.dwell_s, self.window, self.max_gap_s,
                                            control=self.control)
        self._seen = {}
        self.confirmed = False
        self._newest = None

    def update(self, sensors):
        """Feed every new valid reading (`sensors`: zone -> object with reads, temp_c, temp_time). Returns the status."""
        now = self.mono()
        for zone in self.detector.zones:
            s = sensors.get(zone)
            if s is None or s.temp_c is None or s.reads == self._seen.get(zone):
                continue
            self._seen[zone] = s.reads
            wall = self.wall() - (now - s.temp_time)  # when it was read, not when this tick ran
            self.detector.feed(zone, s.temp_time, wall, s.temp_c)
            self._newest = s.temp_time if self._newest is None else max(self._newest, s.temp_time)
        # no reading inside the gap is never "ready": a failed read must not look like a held target
        fresh = self._newest is not None and now - self._newest <= self.max_gap_s
        self.confirmed = bool(fresh and self.detector.result(self._newest).confirmed)
        if self.confirmed and self.ready_wall is None:
            self.ready_wall = self.wall()
        return self.status

    @property
    def status(self):
        return READY if self.confirmed else HEATING

    def timed_out(self):
        """Never reached the target in time (the same limit as a drying warm-up). Ready once is enough."""
        return self.ready_wall is None and self.mono() - self.started_mono >= self.timeout_s

    def zones_report(self):
        """The same shape as `task/report.zones`, so the web app reads both alike. A zone that was not reading when
        the preheat began is not waited for and is reported as bypassed."""
        out = {}
        for zone in self.detector.zones:
            smoothed = self.detector.smoothed(zone)
            out[zone] = {
                "smoothed_temp_c": round(smoothed, 1) if smoothed is not None else None,
                "floor_c": round(self.detector.floor(zone), 1),
                "in_band": self.detector.in_band(zone),
                "bypassed": True if zone not in self.required else None,
            }
        return out
