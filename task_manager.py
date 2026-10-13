"""Drying task state machine. Runs entirely on local data, never touches the network."""

import re
import sqlite3
import threading
import time

import config
import recovery

TASK_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


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


class Rejected(Exception):
    def __init__(self, reason, result="rejected"):
        super().__init__(reason)
        self.reason = reason
        self.result = result


class TaskManager:
    def __init__(self, store, outputs, sensor, boot_id, clock_synced_fn,
                 mono=time.monotonic, wall=time.time, cfg=config, on_task_start=None):
        self.store = store
        self.out = outputs
        self.sensor = sensor
        self.boot_id = boot_id
        self.clock_synced = clock_synced_fn
        self.mono = mono
        self.wall = wall
        self.cfg = cfg
        self.on_task_start = on_task_start  # e.g. clear manual fan/heater commands
        self.lock = threading.RLock()

        self.task = None            # row of the task that owns the machine, or None
        self.segment_start = None   # monotonic start of the current running segment
        self.elapsed_base = 0.0     # running time accumulated before this segment
        self.last_checkpoint = 0.0
        self.recovery = None        # observation state while deciding after a restart
        self.cooldown_until = None
        self.initialized = False

    # ---------------- queries ----------------
    def busy(self):
        with self.lock:
            return self.task is not None or self.recovery is not None

    def elapsed(self, now=None):
        with self.lock:
            if self.task is None:
                return 0.0
            if self.task["status"] == "running" and self.segment_start is not None:
                return self.elapsed_base + ((now or self.mono()) - self.segment_start)
            return self.task["elapsed_s"] or 0.0

    def machine_status(self):
        with self.lock:
            if self.task is not None:
                return self.task["status"]
            if self.store.unacknowledged_failure():
                return "failed"
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
            return {
                "task_id": t["task_id"],
                "status": t["status"],
                "seq": t["seq"],
                "boot_id": self.boot_id,
                "started_at": int(t["started_wall"] * 1000) if t["started_wall"] else None,
                "ended_at": int(t["ended_wall"] * 1000) if t["ended_wall"] else None,
                "duration_minutes": round(duration / 60.0, 2),
                "elapsed_seconds": round(elapsed, 1),
                "remaining_seconds": round(max(0.0, duration - elapsed), 1),
                "progress_percent": round(min(100.0, 100.0 * elapsed / duration), 1),
                "batch_count": t["batch_count"],
                "row_levels": t["row_levels"],
                "weight_kg": t["weight_kg"],
                "reason": t["failure_reason"],
                "detail": _detail(t["diagnostics"]),
                "acknowledged": bool(t["acknowledged"]),
            }

    # ---------------- startup / shutdown ----------------
    def startup(self, prev_boot_id, clean_shutdown):
        """Load the persisted task and decide whether recovery checks are needed."""
        with self.lock:
            self.out.heater.off()
            task = self.store.active_task()
            if task is None:
                self.initialized = True  # fresh boot with nothing pending is never a failure
                return
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
            if self.task is not None and self.task["status"] == "running":
                self._checkpoint(self.mono())
            self.out.all_off()

    # ---------------- control tick ----------------
    def tick(self):
        with self.lock:
            now = self.mono()
            if self.recovery is not None:
                self._tick_recovery(now)
            elif self.task is not None and self.task["status"] == "running":
                self._tick_running(now)
            elif self.task is not None:  # recovery_required: wait for a person
                self.out.all_off()

            if self.cooldown_until is not None and now >= self.cooldown_until:
                self.cooldown_until = None
                if self.task is None:
                    self.out.fan.off()

    def cancel_cooldown(self):
        with self.lock:
            self.cooldown_until = None

    def _tick_running(self, now):
        temp = self.sensor.temp()
        if temp is None:
            return self._end("failed", "sensor_failure", now,
                             {"last_valid_temp_c": self.sensor.temp_c,
                              "bad_reads": self.sensor.bad_reads})
        if temp >= self.cfg.TEMP_MAX_C:
            return self._end("failed", "over_temperature", now,
                             {"temp_c": temp, "temp_max_c": self.cfg.TEMP_MAX_C})

        # thermostat, never heat without airflow
        self.out.fan.on()
        if temp >= self.cfg.TASK_SETPOINT_C:
            self.out.heater.off()
        elif temp <= self.cfg.TASK_SETPOINT_C - self.cfg.TASK_HYSTERESIS_C and self.out.fan.is_active:
            self.out.heater.on()

        if self.elapsed(now) >= self.task["duration_s"]:
            self._end("completed", None, now)
        elif now - self.last_checkpoint >= self.cfg.CHECKPOINT_SECONDS:
            self._checkpoint(now)

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
        # compare against the earliest readings: the heater is off while we observe,
        # so later readings partly measure the cooling caused by this check itself
        gap = recovery.interruption_gap(task, self.boot_id, now, self.wall(), synced)
        d = recovery.decide_recovery(task, r["readings"][:5], self.sensor.ok(), gap,
                                     r["boot_changed"], r["clean_shutdown"], self.cfg)
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
            self._end("failed", d.reason, now, d.diagnostics, cooldown=False)
        else:
            self.task = self.store.update_task(
                task["task_id"], status="recovery_required", failure_reason=d.reason,
                diagnostics=d.diagnostics,
                event=("recovery_required", f"inspection needed: {d.reason}", d.diagnostics))

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
        self.task = self.store.update_task(
            self.task["task_id"], **self._checkpoint_fields(now, self.elapsed(now)))

    def _begin_running(self, elapsed_base, now, event, **extra):
        self.elapsed_base = elapsed_base
        self.segment_start = now
        self.last_checkpoint = now
        self.cooldown_until = None
        # persisted as running before the thermostat may switch the heater on
        self.task = self.store.update_task(
            self.task["task_id"], status="running", failure_reason=None, event=event,
            **self._checkpoint_fields(now, elapsed_base), **extra)

    def _end(self, status, reason, now, diagnostics=None, cooldown=True, reason_in_event=None):
        self.out.heater.off()
        elapsed = self.elapsed(now)
        self.segment_start = None
        fields = self._checkpoint_fields(now, elapsed)
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
        if cooldown and self.out.fan.is_active:
            self.cooldown_until = now + self.cfg.FAN_COOLDOWN_SECONDS
        else:
            self.out.fan.off()

    # ---------------- commands ----------------
    def handle_command(self, cmd):
        """Validate and apply a task command. Returns the ack dict, or None if already processed."""
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
                self._check_age(cmd)
                handler = {
                    "start": self._cmd_start,
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

    def _check_age(self, cmd):
        requested = cmd.get("requested_at")
        if _number(requested) and self.clock_synced():
            age = self.wall() - requested / 1000.0
            if age > self.cfg.COMMAND_MAX_AGE_SECONDS:
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
        rows = parse_row_levels(cmd.get("row_levels"), cfg.ALLOWED_ROW_LEVELS)
        if w is not None and not (_number(w) and 0 < w <= cfg.MAX_WEIGHT_KG):
            raise Rejected("invalid_weight")  # optional, the web app may not send it
        if not (_whole(count) and 1 <= count <= cfg.MAX_BATCH_COUNT):
            raise Rejected("invalid_batch_count")
        if rows is None:
            raise Rejected("invalid_row_levels")
        if not (_number(dur) and cfg.MIN_DURATION_MIN <= dur <= cfg.MAX_DURATION_MIN):
            raise Rejected("invalid_duration")

        if not self.initialized or self.recovery is not None:
            raise Rejected("not_ready")
        if self.task is not None:
            raise Rejected("task_active")
        if self.store.unacknowledged_failure():
            raise Rejected("unacknowledged_failure")
        if self.out.heater.is_active:
            raise Rejected("heater_in_use")
        temp = self.sensor.temp()
        if temp is None:
            raise Rejected("sensor_unavailable")
        if temp >= cfg.TEMP_MAX_C - cfg.TEMP_RESUME_DROP:
            raise Rejected("too_hot")

        try:
            self.task = self.store.create_task(
                task_id, float(w) if w is not None else None, int(count), rows, float(dur) * 60.0,
                event=("task_accepted", "drying task accepted",
                       {"command_id": cmd["command_id"], "weight_kg": w, "batch_count": count,
                        "row_levels": rows, "duration_minutes": dur, "temp_c": temp,
                        "requested_by": cmd.get("requested_by")}))
        except sqlite3.IntegrityError:
            raise Rejected("task_id_already_used", "duplicate")
        if self.on_task_start:
            self.on_task_start()
        now = self.mono()
        self._begin_running(0.0, now, ("task_running", "drying started", {"temp_c": temp}),
                            started_wall=self.wall())

    def _require_task(self, task_id, statuses):
        if self.task is None:
            raise Rejected("no_active_task")
        if task_id is not None and task_id != self.task["task_id"]:
            raise Rejected("task_mismatch")
        if self.task["status"] not in statuses:
            raise Rejected(f"not_allowed_while_{self.task['status']}")

    def _cmd_cancel(self, cmd, task_id):
        self._require_task(task_id, ("starting", "running", "recovering", "recovery_required"))
        self.recovery = None
        self._end("cancelled", "cancelled_by_user", self.mono())

    def _cmd_abort(self, cmd, task_id):
        self._require_task(task_id, ("recovering", "recovery_required"))
        self.recovery = None
        self._end("cancelled", "aborted_after_interruption", self.mono(), cooldown=False)

    def _cmd_resume(self, cmd, task_id):
        self._require_task(task_id, ("recovery_required",))
        temp = self.sensor.temp()
        if temp is None:
            raise Rejected("sensor_unavailable")
        if temp >= self.cfg.TEMP_MAX_C - self.cfg.TEMP_RESUME_DROP:
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
        self.store.update_task(failed["task_id"], acknowledged=1,
                               event=("task_acknowledged", "failure acknowledged",
                                      {"command_id": cmd["command_id"]}))
