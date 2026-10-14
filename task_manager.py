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
        self.cooling = None         # {"start": monotonic} while the fan runs the machine down
        self.safe_temp_c = self._load_safe_temp()
        self.initialized = False

    def _load_safe_temp(self):
        saved = self.store.get_meta("safe_temp_c")
        try:
            value = float(saved)
        except (TypeError, ValueError):
            return self.cfg.COOLDOWN_SAFE_TEMP_C
        return value if self._safe_temp_ok(value) else self.cfg.COOLDOWN_SAFE_TEMP_C

    def _safe_temp_ok(self, value):
        return (_number(value)
                and self.cfg.COOLDOWN_SAFE_TEMP_MIN_C <= value <= self.cfg.COOLDOWN_SAFE_TEMP_MAX_C)

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
                "completed_rows": t["completed_rows"] or None,  # None removes the key
                "reason": t["failure_reason"],
                "detail": _detail(t["diagnostics"]),
                "acknowledged": bool(t["acknowledged"]),
                "batches": self._batch_snapshots(t["task_id"], elapsed),
            }

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

            if self.cooling is not None and self.task is None and self.recovery is None:
                self._tick_cooling(now)

    def set_safe_temp(self, value):
        """Apply the safe shutdown temperature from the web app. Invalid values are ignored."""
        with self.lock:
            if not self._safe_temp_ok(value):
                self.store.log_event("settings_rejected", f"safe temperature {value!r} out of range",
                                     data={"safe_temp_c": value if _number(value) else None})
                return False
            value = float(value)
            if value == self.safe_temp_c:
                return True
            self.safe_temp_c = value
            self.store.set_meta(safe_temp_c=value)
            self.store.log_event("settings_applied", f"safe shutdown temperature {value:.1f} C",
                                 data={"safe_temp_c": value})
            return True

    def _begin_cooling(self, now):
        """Heater off, fan on until the measured temperature is safe. No-op if already safe."""
        self.out.heater.off()
        temp = self.sensor.temp()
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
        temp = self.sensor.temp()
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
            self._end("failed", d.reason, now, d.diagnostics)
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
        self.cooling = None
        # persisted as running before the thermostat may switch the heater on
        self.task = self.store.update_task(
            self.task["task_id"], status="running", failure_reason=None, event=event,
            **self._checkpoint_fields(now, elapsed_base), **extra)

    def _end(self, status, reason, now, diagnostics=None, reason_in_event=None):
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
        self._begin_cooling(now)

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
        self._require_task(task_id, ("starting", "running", "recovering", "recovery_required"))
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
