"""Drying task state machine. Runs entirely on local data, never touches the network."""

import re
import sqlite3
import threading
import time
from statistics import median

import config
import recovery
import warmup
from command_age import cloud_verdict
from machine_settings import MAX_KEY, TARGET_KEY, HeaterCutoff, MachineSettings
from task_store import SNAPSHOT_FIELDS

TASK_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
MODES = ("standard", "experiment")
HEATING_STATUSES = ("warming_up", "running")  # the thermostat is live in these
# zones whose warm-up floor is frozen on the task, and the column holding it. The bottom (control) zone's
# floor is always target_c - tolerance_c; the control sensor can never be bypassed.
ZONE_FLOOR_COLUMNS = {"middle": "floor_middle_c", "top": "floor_top_c"}
BYPASSABLE_ZONES = tuple(ZONE_FLOOR_COLUMNS)


def _number(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _whole(v):
    return _number(v) and float(v).is_integer()


def command_key(cmd):
    """Identity of a task/request, used to process each request exactly once.

    The web app may send an explicit command_id; otherwise task_id + command +
    requested_at (a server timestamp) identifies the request.
    """
    if not isinstance(cmd, dict):
        return None
    cid = cmd.get("command_id")
    if isinstance(cid, str) and 0 < len(cid) <= 128:
        return cid
    if not _number(cmd.get("requested_at")) or not isinstance(cmd.get("command"), str):
        return None
    task_id = cmd.get("task_id") if isinstance(cmd.get("task_id"), str) else ""
    return f"{task_id}:{cmd['command']}:{int(cmd['requested_at'])}"[:200]


def parse_row_levels(value, allowed):
    """'1,3' -> '1,3' normalised, or None if invalid."""
    if not isinstance(value, str) or not 0 < len(value) <= 64:
        return None
    try:
        rows = [int(p) for p in value.replace(" ", "").split(",")]
    except ValueError:
        return None
    if len(set(rows)) != len(rows) or any(r not in allowed for r in rows):
        return None
    return ",".join(str(r) for r in sorted(rows))


def parse_bypass_zones(value):
    """'top,middle' -> 'middle,top' normalised ('' for none), or None if invalid. Never includes the bottom."""
    if value is None or value == "":
        return ""
    if not isinstance(value, str) or len(value) > 64:
        return None
    zones = [z.strip() for z in value.split(",")]
    if len(set(zones)) != len(zones) or any(z not in BYPASSABLE_ZONES for z in zones):
        return None
    return ",".join(z for z in BYPASSABLE_ZONES if z in zones)


def _detail(diag):
    """Short human-readable summary of failure diagnostics (full data goes to events)."""
    if not diag:
        return None
    parts = []
    if diag.get("reference_temp_c") is not None and diag.get("current_temp_c") is not None:
        parts.append(f"temp {diag['reference_temp_c']} -> {diag['current_temp_c']} C")
    if diag.get("gap_s") is not None:
        parts.append(f"gap {diag['gap_s']:.0f} s")
    if diag.get("boot_changed"):
        parts.append("reboot" + ("" if diag.get("clean_shutdown") else ", unclean shutdown"))
    if diag.get("clock_synced") is False:
        parts.append("clock not synced")
    if "temp_c" in diag:
        parts.append(f"temp {diag['temp_c']} C")
    if "last_valid_temp_c" in diag:
        parts.append(f"last valid temp {diag['last_valid_temp_c']} C")
    return "; ".join(parts)[:200] or None


def session_snapshot(cfg, settings):
    """Everything a session runs with, frozen at start so later config edits cannot change it.

    The target and the maximum are the saved settings of the moment. A later target is followed by the
    running task (`TaskManager._retarget`); a raised maximum waits for the next task and a lowered one
    still applies at once (`TaskManager.task_limit`). Tolerance, dwell and the rest never change.
    """
    target, tolerance = float(settings.get(TARGET_KEY)), float(cfg.TARGET_TOLERANCE_C)
    allowance = getattr(cfg, "ZONE_FLOOR_ALLOWANCE_C", {})
    return {
        "target_c": target,
        "tolerance_c": tolerance,
        "hysteresis_c": float(cfg.TASK_HYSTERESIS_C),
        "dwell_s": float(cfg.DWELL_SECONDS),
        "median_window": int(cfg.MEDIAN_WINDOW),
        "warmup_timeout_s": float(cfg.WARMUP_TIMEOUT_SECONDS),
        "dip_limit_s": float(cfg.DIP_WARNING_LIMIT_SECONDS),
        "max_temp_c": float(settings.get(MAX_KEY)),
        # each upper zone's own floor: the shared band floor, lowered by what that zone is allowed to lag
        **{col: target - tolerance - float(allowance.get(zone, 0.0)) for zone, col in ZONE_FLOOR_COLUMNS.items()},
    }


def retarget_fields(task, new_target):
    """The task columns that move when the operator saves a new target mid-task.

    Only the target and the zone floors that hang off it. Tolerance, dwell, hysteresis, the warm-up timeout
    and the safe shutdown stay as the session started. A zone's floor is `target - tolerance - allowance`, so
    keeping its distance below the target keeps its allowance; a zone without a floor stays without one."""
    old = float(task["target_c"])
    fields = {"target_c": float(new_target)}
    for col in ZONE_FLOOR_COLUMNS.values():
        floor = task.get(col)
        if floor is not None:
            fields[col] = float(new_target) - (old - float(floor))
    return fields


def snapshot_valid(task):
    """A task whose frozen settings are missing or nonsensical must never drive the heater."""
    try:
        v = {k: task[k] for k in SNAPSHOT_FIELDS}
    except (KeyError, TypeError):
        return False
    if not all(_number(x) for x in v.values()):
        return False
    # the zone floors are optional (older firmware never had them), but a floor that is there must make sense
    for col in ZONE_FLOOR_COLUMNS.values():
        floor = task.get(col)
        if floor is not None and not (_number(floor) and 0 < floor < v["target_c"]):
            return False
    return (0 < v["target_c"] < v["max_temp_c"] and v["tolerance_c"] > 0 and v["hysteresis_c"] > 0
            and v["dwell_s"] > 0 and v["median_window"] >= 1 and v["warmup_timeout_s"] > 0
            and v["dip_limit_s"] > 0)


class Rejected(Exception):
    def __init__(self, reason, result="rejected"):
        super().__init__(reason)
        self.reason = reason
        self.result = result


class TaskManager:
    def __init__(self, store, outputs, sensor, boot_id, clock_synced_fn,
                 mono=time.monotonic, wall=time.time, cfg=config, on_task_start=None, zones=None):
        self.store = store
        self.out = outputs
        self.sensor = sensor        # the control sensor: thermostat, heater interlock and recovery use only this
        # hardware.SensorArray with the middle and top sensors too, or None for a single-sensor machine
        self.zones = zones
        self.boot_id = boot_id
        self.clock_synced = clock_synced_fn
        self.mono = mono
        self.wall = wall
        self.cfg = cfg
        self.on_task_start = on_task_start  # e.g. clear manual fan/heater commands
        self.server_now_ms = None   # callable returning Firebase server time in ms, or None
        self.local_mark = None      # command_age.LocalCommandMark, set by the controller
        self.lock = threading.RLock()

        self.task = None            # row of the task that owns the machine, or None
        self.segment_start = None   # monotonic start of the current running segment
        self.elapsed_base = 0.0     # running time accumulated before this segment
        self.last_checkpoint = 0.0
        self.warm_base = 0.0        # powered-on warm-up time accumulated before this segment
        self.warm_segment_start = None
        self.detector = None        # warmup.ZoneDetector, rebuilt from the task's own snapshot
        self.dips = None            # warmup.DipTracker, drying phase only
        self._seen = {}             # zone -> sensor.reads already handed to the detector
        self._zone_faults = set()   # zones whose fault was logged during this drying session
        self._log_buf = []          # temperature samples waiting for the next checkpoint
        self.recovery = None        # observation state while deciding after a restart
        self.cooling = None         # {"start": monotonic} while the fan runs the machine down
        self.settings = MachineSettings(store, cfg)  # persisted on the Pi, loaded here on every boot
        self.cutoff = HeaterCutoff(store, cfg.HEATER_CUTOFF_MARGIN_C)
        self.initialized = False
        if cfg.TARGET_TOLERANCE_C < cfg.TASK_HYSTERESIS_C:
            msg = ("TARGET_TOLERANCE_C is below TASK_HYSTERESIS_C: normal heater cycling can leave the "
                   "band, so the target may never be confirmed")
            print("warning:", msg)
            store.log_event("config_warning", msg)
        if zones is not None and not zones.configured():
            msg = ("sensor ids for " + ", ".join(zones.unconfigured()) + " are placeholders in config.py "
                   "(ZONE_SENSOR_IDS): every task start is rejected until they are real")
            print("warning:", msg)
            store.log_event("config_warning", msg)

    # ---------------- chamber zones ----------------
    @property
    def control_zone(self):
        return self.zones.control_zone if self.zones is not None else getattr(self.cfg, "CONTROL_ZONE", "bottom")

    def _sensor_for(self, zone):
        return self.zones.sensors[zone] if self.zones is not None else self.sensor

    def hottest(self):
        """The reading over-temperature and cooldown go by: the hottest healthy sensor. Without a
        healthy control sensor it is None, exactly like a single sensor that has failed."""
        return self.zones.hottest() if self.zones is not None else self.sensor.temp()

    @staticmethod
    def _bypassed(task):
        return {z for z in ((task or {}).get("bypass_zones") or "").split(",") if z}

    def _required_zones(self, task):
        """The zones warm-up has to see in band: the control one, plus the middle and top unless this
        session has no floor for them (older firmware) or the operator started without them."""
        required = [self.control_zone]
        if self.zones is not None:
            bypass = self._bypassed(task)
            for zone, col in ZONE_FLOOR_COLUMNS.items():
                if zone in self.zones.sensors and task.get(col) is not None and zone not in bypass:
                    required.append(zone)
        return tuple(required)

    @property
    def safe_temp_c(self):
        """Where post-task cooling ends."""
        return self.settings.get("safe_temp_c")

    @property
    def heater_cutoff_c(self):
        return self.settings.get("heater_cutoff_c")

    @property
    def temp_max_c(self):
        """The saved safe shutdown temperature: the over-temperature limit for manual heating and for new tasks."""
        return self.settings.get(MAX_KEY)

    def task_limit(self, task):
        """The over-temperature limit a task is held to: the maximum it started with, or the saved maximum
        if the operator has lowered it since. Raising the maximum never loosens a task that is running."""
        frozen = (task or {}).get("max_temp_c")
        return self.temp_max_c if not _number(frozen) else min(frozen, self.temp_max_c)

    # ---------------- queries ----------------
    def busy(self):
        with self.lock:
            return self.task is not None or self.recovery is not None

    def cooling_active(self):
        with self.lock:
            return self.cooling is not None and self.task is None

    def elapsed(self, now=None):
        with self.lock:
            if self.task is None:
                return 0.0
            if self.task["status"] == "running" and self.segment_start is not None:
                return self.elapsed_base + ((now or self.mono()) - self.segment_start)
            return self.task["elapsed_s"] or 0.0

    def warm_elapsed(self, now=None):
        """Powered-on warm-up seconds. Only the warm-up timeout uses this."""
        with self.lock:
            if self.task is None:
                return 0.0
            if self.task["status"] == "warming_up" and self.warm_segment_start is not None:
                return self.warm_base + ((now or self.mono()) - self.warm_segment_start)
            return self.task["warmup_elapsed_s"] or 0.0

    def machine_status(self):
        with self.lock:
            if self.task is not None:
                return self.task["status"]
            if self.store.unacknowledged_failure():
                return "failed"
            if self.cooling is not None:
                return "cooling"
            return "ready" if self.initialized else "starting"

    def snapshot(self):
        """Current task info for Firebase (task/report). reported_at is added by the sync."""
        with self.lock:
            t = self.task or self.store.latest_task()
            if t is None:
                return None
            live = self.task is not None and t["task_id"] == self.task["task_id"]
            elapsed = self.elapsed() if live else (t["elapsed_s"] or 0.0)
            duration = t["duration_s"]
            dips, dip_count = self.store.list_excursions(t["task_id"])
            if t["drying_started_wall"]:
                warmup_s = t["drying_started_wall"] - (t["started_wall"] or t["drying_started_wall"])
            elif live and t["status"] == "warming_up" and t["started_wall"]:
                warmup_s = self.wall() - t["started_wall"]
            else:
                warmup_s = None
            live_detector = self.detector if live else None
            # the bottom (control) sensor's, as it always was; the other zones are in "zones"
            smoothed = live_detector.smoothed(live_detector.control) if live_detector is not None else None
            return {
                "task_id": t["task_id"],
                "status": t["status"],
                "seq": t["seq"],
                "boot_id": self.boot_id,
                # session_started: the task was accepted and the heater began warming the chamber
                "started_at": int(t["started_wall"] * 1000) if t["started_wall"] else None,
                # drying clock start, backdated to the first sample of the dwell that confirmed the target
                "drying_started_at": int(t["drying_started_wall"] * 1000) if t["drying_started_wall"] else None,
                "target_confirmed_at": int(t["target_confirmed_wall"] * 1000) if t["target_confirmed_wall"] else None,
                "warmup_seconds": round(max(0.0, warmup_s), 1) if warmup_s is not None else None,
                "mode": t["mode"],
                "target_temp_c": t["target_c"],
                "tolerance_c": t["tolerance_c"],
                "dwell_seconds": t["dwell_s"],
                "warmup_timeout_seconds": t["warmup_timeout_s"],
                "smoothed_temp_c": round(smoothed, 1) if smoothed is not None else None,
                "zones": self._zone_report(t, live_detector),
                "bypass_zones": t.get("bypass_zones") or None,  # zones the operator started without
                "dips": [{"start": int(d["start_wall"] * 1000),
                          "end": int(d["end_wall"] * 1000) if d["end_wall"] else None,
                          "min_temp_c": round(d["min_c"], 1)} for d in dips] or None,
                "dip_count": dip_count or None,
                "dip_warning": True if t["dip_warning"] else None,
                "ended_at": int(t["ended_wall"] * 1000) if t["ended_wall"] else None,
                "duration_minutes": round(duration / 60.0, 2),
                "elapsed_seconds": round(elapsed, 1),
                "remaining_seconds": round(max(0.0, duration - elapsed), 1),
                "progress_percent": round(min(100.0, 100.0 * elapsed / duration), 1),
                "batch_count": t["batch_count"],
                "row_levels": t["row_levels"],
                "weight_kg": t["weight_kg"],
                "completed_rows": t["completed_rows"] or None,  # None removes the key
                "reason": t["failure_reason"],
                "detail": _detail(t["diagnostics"]),
                "acknowledged": bool(t["acknowledged"]),
                "batches": self._batch_snapshots(t["task_id"], elapsed),
            }

    def _zone_report(self, task, detector):
        """Per-zone warm-up picture for the report: smoothed reading, the zone's floor and whether it is in
        band right now. None unless this is a live three-zone session (the key is then removed)."""
        if detector is None or self.zones is None:
            return None
        bypass = self._bypassed(task)
        out = {}
        for zone in detector.zones:
            smoothed = detector.smoothed(zone)
            out[zone] = {
                "smoothed_temp_c": round(smoothed, 1) if smoothed is not None else None,
                "floor_c": round(detector.floor(zone), 1),
                "in_band": detector.in_band(zone),
                "bypassed": True if zone in bypass else None,
            }
        return out

    def _batch_snapshots(self, task_id, task_elapsed):
        """Per-batch timing for batches added mid-task, each measured from when it was added."""
        out = []
        for b in self.store.list_batches(task_id):
            elapsed = min(b["duration_s"], max(0.0, task_elapsed - b["offset_s"]))
            out.append({
                "batch_id": b["batch_id"],
                "row_level": b["row_level"],
                "offset_seconds": round(b["offset_s"], 1),
                "duration_minutes": round(b["duration_s"] / 60.0, 2),
                "elapsed_seconds": round(elapsed, 1),
                "remaining_seconds": round(b["duration_s"] - elapsed, 1),
                "progress_percent": round(100.0 * elapsed / b["duration_s"], 1),
            })
        return out

    # ---------------- startup / shutdown ----------------
    def startup(self, prev_boot_id, clean_shutdown):
        """Load the persisted task and decide whether recovery checks are needed."""
        with self.lock:
            self.out.heater.off()
            task = self.store.active_task()
            if task is None:
                self.initialized = True  # fresh boot with nothing pending is never a failure
                # rebooted while hot (or blind): cool down before going idle
                self._begin_cooling(self.mono())
                return
            if task["target_c"] is None and task["started_wall"] is not None:
                # accepted by firmware from before warm-up existed: it was already drying, so keep
                # that clock and freeze the settings it is running with now
                task = self.store.update_task(
                    task["task_id"], drying_started_wall=task["started_wall"], **session_snapshot(self.cfg, self.settings),
                    event=("session_backfilled", "task from older firmware, settings frozen at upgrade", None))
            if task["status"] == "recovery_required":
                self.out.all_off()
                self.task = task
                self.initialized = True
                return
            boot_changed = prev_boot_id != self.boot_id
            self.out.all_off()
            self.task = self.store.update_task(
                task["task_id"], status="recovering",
                event=("task_recovering", "controller restarted during a task, checking it",
                       {"previous_status": task["status"], "boot_changed": boot_changed,
                        "clean_shutdown": clean_shutdown}))
            self.recovery = {
                "start": self.mono(),
                "boot_changed": boot_changed,
                "clean_shutdown": clean_shutdown,
                "readings": [],
                "seen_reads": self.sensor.reads,
            }
            self.initialized = True

    def shutdown(self):
        with self.lock:
            if self.task is not None and self.task["status"] in HEATING_STATUSES:
                self._checkpoint(self.mono())
            self.out.all_off()

    # ---------------- control tick ----------------
    def tick(self):
        with self.lock:
            try:
                self._tick(self.mono())
            except Exception:
                # whatever went wrong, the relay must not stay on because of it
                self.out.heater.off()
                self.store.log_event("controller_error", "control tick raised, heater forced off")
                raise

    def _tick(self, now):
        task = self.task
        if self.recovery is not None:
            self._tick_recovery(now)
        elif task is not None and task["status"] in HEATING_STATUSES:
            if not snapshot_valid(task):
                self._end("failed", "invalid_session", now, {"detail": "session settings missing or invalid"})
            elif task["status"] == "warming_up":
                self._tick_warming(now)
            else:
                self._tick_running(now)
        elif task is not None:  # recovery_required: wait for a person
            self.out.all_off()

        if self.cooling is not None and self.task is None and self.recovery is None:
            self._tick_cooling(now)

    def set_safe_temp(self, value):
        """Apply the cooldown temperature on its own. Invalid values are ignored."""
        with self.lock:
            if not self.settings.valid("safe_temp_c", value):
                self.settings.apply({"safe_temp_c": value})  # logs the rejection
                return False
            self.settings.apply({"safe_temp_c": value})
            return True

    def apply_settings(self, payload):
        with self.lock:
            changed = self.settings.apply(payload)
            if TARGET_KEY in changed and self.task is not None:
                self._retarget()
            return changed

    def _retarget(self):
        """The saved target is the one target there is: a running task follows it at once.

        Warm-up starts its hold over against the new floors. While drying, an open dip below the old band is
        closed, and a raise gets a grace (`DipTracker.settle_until`) so heating up to it is not logged as a dip.
        The thermostat reads `target_c` on every tick, so it needs nothing here."""
        t = self.task
        new, old = self.settings.get(TARGET_KEY), t["target_c"]
        if not _number(old) or new == old:
            return
        if new + float(self.cfg.TEMP_MAX_GAP_C) > self.task_limit(t):
            # only when the saved shutdown was raised after this task started: the task keeps the limit it
            # started with, so a target that close to it would let normal overshoot trip it
            self.store.log_event("task_target_kept", "new target is too close to this task's safe shutdown "
                                 "temperature, the task keeps its own", t["task_id"],
                                 {"target_c": old, "requested_c": new, "temp_max_c": self.task_limit(t)})
            return
        status = t["status"]
        self.task = self.store.update_task(
            t["task_id"], **retarget_fields(t, new),
            event=("task_target_changed", f"target changed from {old:.1f} C to {new:.1f} C, the task follows",
                   {"from_c": old, "to_c": new, "phase": status}))
        if status == "warming_up":
            self._new_detector()
        elif status == "running":
            wall = self.wall()
            if self.dips is not None and self.dips.open:
                self.store.save_excursion(t["task_id"], self.dips.start, wall, self.dips.min_c)
                self.store.log_event("dip_closed", "target changed, the excursion below the old band is closed",
                                     t["task_id"], {"start_wall": self.dips.start, "end_wall": wall,
                                                    "min_temp_c": round(self.dips.min_c, 2)})
            self._new_detector()
            task = self.task
            settle = wall + float(task["warmup_timeout_s"]) if new > old else None
            self.dips = warmup.DipTracker(task["target_c"] - task["tolerance_c"], task["dip_limit_s"], settle)

    def _begin_cooling(self, now):
        """Heater off, fan on until the measured temperature is safe. No-op if already safe."""
        self.out.heater.off()
        temp = self.hottest()  # the chamber is safe only once its hottest zone is
        if temp is not None and temp <= self.safe_temp_c:
            self.cooling = None
            self.out.fan.off()
            return
        self.cooling = {"start": now}
        self.out.fan.on()
        self.store.log_event("cooling_started", "cooling: heater off, fan on until safe temperature",
                             data={"temp_c": temp, "safe_temp_c": self.safe_temp_c})

    def _tick_cooling(self, now):
        self.out.heater.off()  # never heat while cooling
        self.out.fan.on()
        waited = now - self.cooling["start"]
        temp = self.hottest()
        if temp is not None and temp <= self.safe_temp_c:
            reason, msg = "cooling_complete", "cooling complete, fan off"
        elif waited >= self.cfg.COOLDOWN_MAX_SECONDS:
            reason, msg = "cooling_timeout", "cooling time limit reached, fan off"
        elif temp is None and waited >= self.cfg.COOLDOWN_NO_SENSOR_SECONDS:
            reason, msg = "cooling_ended_no_sensor", "no temperature reading while cooling, fan off"
        else:
            return
        self.cooling = None
        self.out.fan.off()
        self.store.log_event(reason, msg, data={"temp_c": temp, "safe_temp_c": self.safe_temp_c,
                                                "cooling_s": round(waited, 1)})

    def _control_heat(self, now):
        """Fault checks and the thermostat, shared by warm-up and drying. True while the task goes on.

        Everything here uses the session's frozen settings and the raw reading, so it works
        without the network and a failed read can never look like a good one.
        """
        t = self.task
        temp = self.sensor.temp()
        if temp is None:
            self._end("failed", "sensor_failure", now,
                      {"last_valid_temp_c": self.sensor.temp_c, "bad_reads": self.sensor.bad_reads})
            return False
        # the hottest healthy sensor decides, so a hot spot the heater's own sensor cannot see still stops it
        hot = self.hottest()
        hot = temp if hot is None else hot
        limit = self.task_limit(t)
        if hot >= limit:
            diag = {"temp_c": hot, "temp_max_c": limit}
            if self.zones is not None:
                diag["zone_temps_c"] = self.zones.temps()
            self._end("failed", "over_temperature", now, diag)
            return False
        if self.zones is not None and t["status"] == "warming_up":
            # warm-up cannot be confirmed without a zone it is waiting for, and the heater is already on:
            # stop safely, the operator can start again without that sensor
            for zone in self._required_zones(t):
                if zone != self.control_zone and not self.zones.healthy(zone):
                    sensor = self.zones.sensors[zone]
                    self._end("failed", "zone_sensor_failure", now,
                              {"zone": zone, "last_valid_temp_c": sensor.temp_c, "bad_reads": sensor.bad_reads})
                    return False

        # thermostat, never heat without airflow. The pause only pauses heating: the timer,
        # checkpoints and fan carry on, and the fault checks above stay independent of it. It goes by the
        # hottest sensor (`hot`), the thermostat below by the control sensor (`temp`).
        self.out.fan.on()
        if self.out.heater.latched:  # runtime guard: stays failed until the operator acknowledges
            self._end("failed", self.out.heater.latched, now, {"temp_c": temp})
            return False
        cut = self.cutoff.update(hot, self.heater_cutoff_c)
        if cut or temp >= t["target_c"]:
            self.out.heater.off()
        elif temp <= t["target_c"] - t["hysteresis_c"] and self.out.fan.is_active:
            self.out.heater.on()
        return True

    def _new_detector(self):
        t = self.task
        control = self.control_zone
        floors = {control: t["target_c"] - t["tolerance_c"]}
        if self.zones is not None:
            for zone, col in ZONE_FLOOR_COLUMNS.items():
                if zone in self.zones.sensors and t.get(col) is not None:
                    floors[zone] = t[col]  # smoothed and logged even when bypassed, but only required ones count
        self.detector = warmup.ZoneDetector(floors, self._required_zones(t), t["dwell_s"], t["median_window"],
                                            self.cfg.DWELL_MAX_GAP_SECONDS, control=control)
        self._seen = {zone: self._sensor_for(zone).reads for zone in floors}

    def _sample(self, phase):
        """Feed every new valid reading to the detector and the temperature log.

        Returns None when no zone had a new reading. Otherwise (verdict, control_wall): the joint verdict
        of the detector as of the newest reading, and the wall time of the control sensor's new reading
        (None if only the upper zones had one), which is what dip tracking goes by.
        """
        task_id = self.task["task_id"]
        control = self.detector.control
        newest = control_wall = None
        for zone in self.detector.zones:
            s = self._sensor_for(zone)
            if s.reads == self._seen[zone] or s.temp_c is None:
                continue
            self._seen[zone] = s.reads
            mono = s.temp_time
            wall = self.wall() - (self.mono() - mono)  # when it was read, not when this tick ran
            res = self.detector.feed(zone, mono, wall, s.temp_c)
            self._log_buf.append((task_id, wall, mono, s.temp_c, res.smoothed, phase, zone))
            newest = mono if newest is None else max(newest, mono)
            if zone == control:
                control_wall = wall
        if newest is None:
            return None
        if phase == "warming_up" and self.zones is not None:
            for zone in self.detector.newly_in_band():
                self.store.log_event("warmup_zone_reached", f"{zone} reached the warm-up band", task_id,
                                     {"zone": zone, "smoothed_c": round(self.detector.smoothed(zone), 2),
                                      "floor_c": round(self.detector.floor(zone), 2),
                                      "warmup_s": round(self.warm_elapsed(self.mono()), 1)})
        return self.detector.result(newest), control_wall

    def _note_zone_faults(self):
        """While drying, a middle or top sensor that stops reading only warns: sensor 1 still holds the
        thermostat. Logged once when it fails and once when it recovers; the live flag is in state."""
        if self.zones is None:
            return
        t = self.task
        for zone in self.zones.others:
            if zone in self._bypassed(t):
                continue  # the operator started without it and was told
            if not self.zones.healthy(zone):
                if zone not in self._zone_faults:
                    self._zone_faults.add(zone)
                    self.store.log_event("zone_sensor_fault", f"{zone} temperature sensor stopped giving valid "
                                         "readings, drying continues on the others", t["task_id"], {"zone": zone})
            elif zone in self._zone_faults:
                self._zone_faults.discard(zone)
                self.store.log_event("zone_sensor_restored", f"{zone} temperature sensor reads again",
                                     t["task_id"], {"zone": zone})

    def _flush_log(self):
        buf, self._log_buf = self._log_buf, []
        self.store.append_temp_samples(buf)

    def _tick_warming(self, now):
        if not self._control_heat(now):
            return
        t = self.task
        got = self._sample("warming_up")
        if got is not None and got[0].confirmed:
            return self._start_drying(got[0], now)
        if self.warm_elapsed(now) >= t["warmup_timeout_s"]:
            return self._end("failed", "warmup_timeout", now,
                             {"temp_c": self.sensor.temp_c, "target_c": t["target_c"],
                              "warmup_s": round(self.warm_elapsed(now), 1)})
        if now - self.last_checkpoint >= self.cfg.CHECKPOINT_SECONDS:
            self._checkpoint(now)

    def _start_drying(self, res, now):
        """The target held for the whole dwell: the drying clock starts at its first sample."""
        wall = self.wall()
        base = max(0.0, now - res.first_mono)
        self._begin_running(
            base, now,
            ("target_reached", "target temperature confirmed, drying started",
             {"temp_c": self.sensor.temp_c, "smoothed_c": round(res.smoothed, 2),
              "drying_started_wall": res.first_wall, "dwell_s": self.task["dwell_s"]}),
            drying_started_wall=res.first_wall, target_confirmed_wall=wall,
            warmup_elapsed_s=self.warm_elapsed(now))

    def _tick_running(self, now):
        if not self._control_heat(now):
            return
        got = self._sample("drying")
        if got is not None and got[1] is not None and got[0].full:
            self._track_dips(got[0].smoothed, got[1])
        self._note_zone_faults()

        if self.elapsed(now) >= self.task["duration_s"]:
            self._end("completed", None, now)
        elif now - self.last_checkpoint >= self.cfg.CHECKPOINT_SECONDS:
            self._checkpoint(now)

    def _track_dips(self, smoothed, wall):
        """Log excursions below the band. The timer keeps running; only a long dip raises a flag."""
        t = self.task
        kind, start, end, min_c, warn = self.dips.feed(wall, smoothed)
        if kind:
            self.store.save_excursion(t["task_id"], start, end, min_c)
            msg = ("temperature dropped below the drying band" if kind == "opened"
                   else "temperature back in the drying band")
            self.store.log_event(f"dip_{kind}", msg, t["task_id"],
                                 {"start_wall": start, "end_wall": end, "min_temp_c": round(min_c, 2)})
        if warn and not t["dip_warning"]:
            self.task = self.store.update_task(
                t["task_id"], dip_warning=1,
                event=("dip_warning", "temperature stayed below the drying band too long, drying continues",
                       {"limit_s": t["dip_limit_s"], "min_temp_c": round(min_c, 2)}))

    def _tick_recovery(self, now):
        r = self.recovery
        self.out.all_off()
        if self.sensor.reads != r["seen_reads"]:
            r["seen_reads"] = self.sensor.reads
            if self.sensor.ok():
                r["readings"].append(self.sensor.temp_c)

        waited = now - r["start"]
        if waited < self.cfg.RECOVERY_OBSERVE_SECONDS:
            return
        synced = self.clock_synced()
        if r["boot_changed"] and not synced and waited < self.cfg.RECOVERY_CLOCK_WAIT_SECONDS:
            return

        task = self.task
        if task["drying_started_wall"] is None:
            return self._recover_warming(task, r, now)
        # compare against the earliest readings: the heater is off while we observe,
        # so later readings partly measure the cooling caused by this check itself
        gap = recovery.interruption_gap(task, self.boot_id, now, self.wall(), synced)
        d = recovery.decide_recovery(task, r["readings"][:5], self.sensor.ok(), gap,
                                     r["boot_changed"], r["clean_shutdown"], self.cfg,
                                     max_temp_c=self.task_limit(task))
        d.diagnostics["clock_synced"] = synced
        self.recovery = None

        if d.action == recovery.RESUME:
            self._begin_running(task["elapsed_s"] or 0.0, now,
                                ("task_resumed", "resumed automatically after a short interruption",
                                 d.diagnostics))
        elif d.action == recovery.COMPLETE:
            self.segment_start = None
            self._end("completed", None, now, d.diagnostics, reason_in_event=d.reason)
        elif d.action == recovery.FAILED:
            self._end("failed", d.reason, now, d.diagnostics)
        else:
            self.task = self.store.update_task(
                task["task_id"], status="recovery_required", failure_reason=d.reason,
                diagnostics=d.diagnostics,
                event=("recovery_required", f"inspection needed: {d.reason}", d.diagnostics))

    def _recover_warming(self, task, r, now):
        """A restart during warm-up loses no drying time, so only the sensor and the hard limit decide.

        Warm-up always resumes as warm-up: the dwell starts over (the target is never taken as
        reached from before the restart) and the warm-up timeout keeps counting from its checkpoint.
        """
        self.recovery = None
        readings = r["readings"]
        current = median(readings) if self.sensor.ok() and len(readings) >= self.cfg.RECOVERY_MIN_READS else None
        diag = {"current_temp_c": round(current, 2) if current is not None else None,
                "readings": len(readings), "boot_changed": r["boot_changed"],
                "clean_shutdown": r["clean_shutdown"], "clock_synced": self.clock_synced(),
                "warmup_elapsed_s": round(task["warmup_elapsed_s"] or 0.0, 1)}
        if not snapshot_valid(task):
            return self._end("failed", "invalid_session", now, diag)
        if current is None:
            return self._end("failed", "sensor_unavailable", now, diag)
        if current >= self.task_limit(task):
            return self._end("failed", "over_temperature", now, dict(diag, temp_c=current))
        self._begin_warming(task["warmup_elapsed_s"] or 0.0, now,
                            ("task_resumed", "warm-up resumed after a restart, target not yet confirmed", diag))

    # ---------------- transitions ----------------
    def _checkpoint_fields(self, now, elapsed):
        fields = {
            "elapsed_s": elapsed,
            "checkpoint_wall": self.wall(),
            "checkpoint_mono": now,
            "checkpoint_boot_id": self.boot_id,
            "checkpoint_clock_synced": int(bool(self.clock_synced())),
        }
        temp = self.sensor.temp()
        if temp is not None:
            fields["last_temp_c"] = temp
            fields["last_temp_wall"] = self.wall()
        return fields

    def _checkpoint(self, now):
        self.last_checkpoint = now
        fields = self._checkpoint_fields(now, self.elapsed(now))
        if self.task["status"] == "warming_up":
            fields["warmup_elapsed_s"] = self.warm_elapsed(now)
        self._flush_log()
        if self.dips is not None and self.dips.open:
            self.store.save_excursion(self.task["task_id"], self.dips.start, None, self.dips.min_c)
        self.task = self.store.update_task(self.task["task_id"], **fields)

    def _begin_warming(self, warm_base, now, event, **extra):
        self.warm_base = warm_base
        self.warm_segment_start = now
        self.segment_start = None
        self.dips = None
        self.last_checkpoint = now
        self.cooling = None
        # persisted as warming_up before the thermostat may switch the heater on
        self.task = self.store.update_task(
            self.task["task_id"], status="warming_up", failure_reason=None, event=event,
            **self._checkpoint_fields(now, self.task["elapsed_s"] or 0.0),
            warmup_elapsed_s=warm_base, **extra)
        self._new_detector()

    def _begin_running(self, elapsed_base, now, event, **extra):
        self.elapsed_base = elapsed_base
        self.segment_start = now
        self.warm_segment_start = None
        self.last_checkpoint = now
        self.cooling = None
        # persisted as running before the thermostat may switch the heater on
        self.task = self.store.update_task(
            self.task["task_id"], status="running", failure_reason=None, event=event,
            **self._checkpoint_fields(now, elapsed_base), **extra)
        self._new_detector()
        self.dips = warmup.DipTracker(self.task["target_c"] - self.task["tolerance_c"],
                                      self.task["dip_limit_s"])
        open_dip = self.store.open_excursion(self.task["task_id"])
        if open_dip:  # restarted while below the band: the same excursion goes on
            self.dips.restore(open_dip["start_wall"], open_dip["min_c"])

    def _end(self, status, reason, now, diagnostics=None, reason_in_event=None):
        self.out.heater.off()
        elapsed = self.elapsed(now)
        self.segment_start = None
        fields = self._checkpoint_fields(now, elapsed)
        if self.task["status"] == "warming_up":
            fields["warmup_elapsed_s"] = self.warm_elapsed(now)
        self.warm_segment_start = None
        self._flush_log()
        if self.dips is not None and self.dips.open:
            self.store.save_excursion(self.task["task_id"], self.dips.start, self.wall(), self.dips.min_c)
        self.detector = self.dips = None
        self._zone_faults.clear()
        msg = {
            "completed": "drying task completed",
            "failed": f"task failed: {reason}",
            "cancelled": f"task cancelled: {reason}",
        }[status]
        data = dict(diagnostics or {}, elapsed_s=round(fields["elapsed_s"], 1))
        if reason_in_event:
            data["note"] = reason_in_event
        self.store.update_task(
            self.task["task_id"], status=status, failure_reason=reason, diagnostics=diagnostics,
            ended_wall=self.wall(), event=(f"task_{status}", msg, data), **fields)
        self.task = None
        self._begin_cooling(now)

    # ---------------- commands ----------------
    def handle_command(self, cmd, cloud=False):
        """Validate and apply a task command. Returns the ack dict, or None if already processed.

        `cloud` marks a request that came through Firebase, which can arrive long after it was made
        and so gets the strict age rules. The local panel is answered at once and is not aged this way.
        """
        cid = command_key(cmd)
        if cid is None:
            return None  # nothing to acknowledge against
        cmd = dict(cmd, command_id=cid)
        action = cmd.get("command")
        task_id = cmd.get("task_id") if isinstance(cmd.get("task_id"), str) else None
        with self.lock:
            if self.store.seen_command(cid):
                return None  # duplicate delivery of an already handled command
            result, reason = "accepted", None
            try:
                self._check_age(cmd, cloud)
                handler = {
                    "start": self._cmd_start,
                    "add_batch": self._cmd_add_batch,
                    "complete_batch": self._cmd_complete_batch,
                    "cancel": self._cmd_cancel,
                    "resume": self._cmd_resume,
                    "abort": self._cmd_abort,
                    "acknowledge": self._cmd_acknowledge,
                }.get(action)
                if handler is None:
                    raise Rejected("unknown_action")
                handler(cmd, task_id)
            except Rejected as e:
                result, reason = e.result, e.reason
            self.store.record_command(cid, action, task_id, result, reason)
            if result != "accepted":
                self.store.log_event(f"command_{result}", f"{action} {result}: {reason}", task_id,
                                     {"command_id": cid, "command": action, "reason": reason})
            return {"command_id": cid, "command": action, "task_id": task_id,
                    "result": result, "reason": reason}

    def _check_age(self, cmd, cloud=False):
        requested = cmd.get("requested_at")
        issued = cmd.get("issued_at")
        server_ms = self.server_now_ms() if self.server_now_ms else None
        limit = self.cfg.COMMAND_MAX_AGE_SECONDS
        if cloud:
            limit = self.cfg.CLOUD_COMMAND_MAX_AGE_SECONDS
            if _number(issued) and server_ms is not None:
                # issued_at is when the browser made the request; requested_at is only when it was
                # delivered, which is exactly what an offline outage stretches
                since_local = self.local_mark.since() if self.local_mark else None
                if cloud_verdict((server_ms - issued) / 1000.0, since_local, limit):
                    raise Rejected("stale_command")
                return
        if _number(requested) and server_ms is not None:
            age = (server_ms - requested) / 1000.0  # both sides are server time, so the Pi clock is irrelevant
            if age > limit:
                raise Rejected("stale_command")
        elif _number(requested) and self.clock_synced():
            age = self.wall() - requested / 1000.0
            if age > limit:
                raise Rejected("stale_command")

    def _cmd_start(self, cmd, task_id):
        cfg = self.cfg
        if task_id is None or not TASK_ID_RE.match(task_id):
            raise Rejected("invalid_task_id")
        if self.task is not None and self.task["task_id"] == task_id:
            raise Rejected("duplicate_start", "duplicate")
        if self.store.task_exists(task_id):
            raise Rejected("task_id_already_used", "duplicate")

        w, count, dur = (cmd.get(k) for k in ("weight_kg", "batch_count", "duration_minutes"))
        mode = cmd.get("mode")
        if mode is not None and mode not in MODES:
            raise Rejected("invalid_mode")  # recorded for audit only, it does not change how the task runs
        rows = parse_row_levels(cmd.get("row_levels"), cfg.ALLOWED_ROW_LEVELS)
        if w is not None and not (_number(w) and 0 < w <= cfg.MAX_WEIGHT_KG):
            raise Rejected("invalid_weight")  # optional, the web app may not send it
        if not (_whole(count) and 1 <= count <= cfg.MAX_BATCH_COUNT):
            raise Rejected("invalid_batch_count")
        if rows is None:
            raise Rejected("invalid_row_levels")
        if not (_number(dur) and cfg.MIN_DURATION_MIN <= dur <= cfg.MAX_DURATION_MIN):
            raise Rejected("invalid_duration")
        bypass = parse_bypass_zones(cmd.get("bypass_zones"))
        if bypass is None:
            raise Rejected("invalid_bypass_zones")  # the bottom sensor cannot be bypassed

        if not self.initialized or self.recovery is not None:
            raise Rejected("not_ready")
        if self.task is not None:
            raise Rejected("task_active")
        if self.store.unacknowledged_failure():
            raise Rejected("unacknowledged_failure")
        if self.out.heater.is_active:
            raise Rejected("heater_in_use")
        if self.out.heater.latched:
            raise Rejected("heater_fault")
        temp = self.sensor.temp()
        if temp is None:
            raise Rejected("sensor_unavailable")
        unhealthy = []
        if self.zones is not None:
            if not self.zones.configured():
                raise Rejected("zones_unconfigured")  # a placeholder id: nothing says which sensor is where
            unhealthy = [z for z in self.zones.others if not self.zones.healthy(z)]
            if any(z not in bypass.split(",") for z in unhealthy):
                raise Rejected("zone_sensor_unavailable")  # the operator may start without it, knowingly
        hot = self.hottest()
        if (temp if hot is None else hot) >= self.temp_max_c - cfg.TEMP_RESUME_DROP:
            raise Rejected("too_hot")

        snap = session_snapshot(cfg, self.settings)
        if self.zones is None:
            snap = {k: v for k, v in snap.items() if k not in ZONE_FLOOR_COLUMNS.values()}  # nothing to track
        else:
            snap["bypass_zones"] = bypass or None
        try:
            self.task = self.store.create_task(
                task_id, float(w) if w is not None else None, int(count), rows, float(dur) * 60.0,
                mode=mode, snapshot=snap,
                event=("task_accepted", "drying task accepted",
                       {"command_id": cmd["command_id"], "weight_kg": w, "batch_count": count,
                        "row_levels": rows, "duration_minutes": dur, "temp_c": temp, "mode": mode,
                        "requested_by": cmd.get("requested_by"), **snap,
                        **({"zone_temps_c": self.zones.temps(), "zones_unhealthy": unhealthy}
                           if self.zones is not None else {})}))
        except sqlite3.IntegrityError:
            raise Rejected("task_id_already_used", "duplicate")
        if self.on_task_start:
            self.on_task_start()
        now = self.mono()
        # the drying clock does not run yet: the chamber has to reach and hold the target first
        self._begin_warming(0.0, now, ("task_warming_up", "warming up, drying starts once the target holds",
                                       {"temp_c": temp, "target_c": snap["target_c"]}),
                            started_wall=self.wall())

    def _cmd_add_batch(self, cmd, task_id):
        """Add one batch to the running task without touching the relays or the running clock.

        The batch is timed from now. The task is only extended, never shortened, so batches
        already drying keep their own end times.
        """
        cfg = self.cfg
        if task_id is None or not TASK_ID_RE.match(task_id):
            raise Rejected("invalid_task_id")
        batch_id = cmd.get("batch_id")
        if not isinstance(batch_id, str) or not TASK_ID_RE.match(batch_id):
            raise Rejected("invalid_batch_id")
        w, dur = cmd.get("weight_kg"), cmd.get("duration_minutes")
        rows = parse_row_levels(cmd.get("row_levels"), cfg.ALLOWED_ROW_LEVELS)
        if w is not None and not (_number(w) and 0 < w <= cfg.MAX_WEIGHT_KG):
            raise Rejected("invalid_weight")
        if rows is None or "," in rows:
            raise Rejected("invalid_row_levels")  # exactly one row per added batch
        if not (_number(dur) and cfg.MIN_DURATION_MIN <= dur <= cfg.MAX_DURATION_MIN):
            raise Rejected("invalid_duration")

        if not self.initialized or self.recovery is not None:
            raise Rejected("not_ready")
        if self.task is None or self.task["status"] != "running":
            raise Rejected("task_not_running")
        if task_id != self.task["task_id"]:
            raise Rejected("task_mismatch")
        if self.store.batch_exists(task_id, batch_id):
            raise Rejected("duplicate_batch", "duplicate")
        current_rows = self.task["row_levels"].split(",")
        if rows in current_rows:
            raise Rejected("row_occupied")
        if self.task["batch_count"] + 1 > cfg.MAX_BATCH_COUNT:
            raise Rejected("invalid_batch_count")
        if self.sensor.temp() is None:
            raise Rejected("sensor_unavailable")  # no too_hot check: the thermostat already holds the heat

        now = self.mono()
        offset = self.elapsed(now)
        batch_s = float(dur) * 60.0
        task_s = max(self.task["duration_s"], offset + batch_s)
        if task_s > cfg.MAX_DURATION_MIN * 60.0:
            raise Rejected("invalid_duration")
        # a row freed by an earlier confirmation is being reused
        done = [r for r in (self.task["completed_rows"] or "").split(",") if r and r != rows]
        self.task = self.store.add_batch(
            task_id, batch_id, int(rows), float(w) if w is not None else None, batch_s, offset,
            task_s, ",".join(sorted(current_rows + [rows], key=int)), self.task["batch_count"] + 1,
            completed_rows=",".join(done),
            event=("batch_added", "batch added to the running task",
                   {"command_id": cmd["command_id"], "batch_id": batch_id, "row_level": rows,
                    "duration_minutes": dur, "offset_s": round(offset, 1),
                    "task_duration_s": task_s, "requested_by": cmd.get("requested_by")}))

    def _cmd_complete_batch(self, cmd, task_id):
        """The operator confirmed the batch on one row is dry.

        Frees the row. Other rows keep drying on the thermostat and the schedule is left alone.
        When the last row is freed the task ends: heater off at once, fan cools the machine.
        """
        rows = parse_row_levels(cmd.get("row_levels"), self.cfg.ALLOWED_ROW_LEVELS)
        if rows is None or "," in rows:
            raise Rejected("invalid_row_levels")  # exactly one row per confirmation
        if not self.initialized or self.recovery is not None:
            raise Rejected("not_ready")
        self._require_task(task_id, ("running", "recovery_required"))

        active = self.task["row_levels"].split(",")
        done = [r for r in (self.task["completed_rows"] or "").split(",") if r]
        if rows not in active:
            if rows in done:
                raise Rejected("row_already_completed", "duplicate")
            raise Rejected("row_not_in_task")

        active.remove(rows)
        done = sorted(done + [rows], key=int)
        now = self.mono()
        event = ("batch_completed", "batch confirmed dry by the operator",
                 {"command_id": cmd["command_id"], "row_level": int(rows),
                  "rows_remaining": len(active), "requested_by": cmd.get("requested_by")})
        if active:
            self.task = self.store.update_task(
                self.task["task_id"], row_levels=",".join(active),
                completed_rows=",".join(done), event=event)
            return
        self.task = self.store.update_task(
            self.task["task_id"], completed_rows=",".join(done), event=event)
        self._end("completed", None, now, {"completed_by": "operator"})

    def _require_task(self, task_id, statuses):
        if self.task is None:
            raise Rejected("no_active_task")
        if task_id is not None and task_id != self.task["task_id"]:
            raise Rejected("task_mismatch")
        if self.task["status"] not in statuses:
            raise Rejected(f"not_allowed_while_{self.task['status']}")

    def _cmd_cancel(self, cmd, task_id):
        self._require_task(task_id, ("starting", "warming_up", "running", "recovering", "recovery_required"))
        self.recovery = None
        self._end("cancelled", "cancelled_by_user", self.mono())

    def _cmd_abort(self, cmd, task_id):
        self._require_task(task_id, ("recovering", "recovery_required"))
        self.recovery = None
        self._end("cancelled", "aborted_after_interruption", self.mono())

    def _cmd_resume(self, cmd, task_id):
        self._require_task(task_id, ("recovery_required",))
        temp = self.sensor.temp()
        if temp is None:
            raise Rejected("sensor_unavailable")
        if temp >= self.task_limit(self.task) - self.cfg.TEMP_RESUME_DROP:
            raise Rejected("too_hot")
        self._begin_running(self.task["elapsed_s"] or 0.0, self.mono(),
                            ("task_resumed", "resumed after explicit confirmation",
                             {"command_id": cmd["command_id"], "elapsed_s": self.task["elapsed_s"],
                              "previous_reason": self.task["failure_reason"]}))

    def _cmd_acknowledge(self, cmd, task_id):
        failed = self.store.unacknowledged_failure()
        if failed is None:
            raise Rejected("nothing_to_acknowledge")
        if task_id is not None and task_id != failed["task_id"]:
            raise Rejected("task_mismatch")
        self.out.heater.clear_latch()
        self.store.update_task(failed["task_id"], acknowledged=1,
                               event=("task_acknowledged", "failure acknowledged",
                                      {"command_id": cmd["command_id"]}))
