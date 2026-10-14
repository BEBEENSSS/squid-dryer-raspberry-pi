"""Background Firebase sync. Never blocks the control loop and never holds its lock during I/O."""

import threading
import time

import config
from task_manager import command_key

SERVER_TS = {".sv": "timestamp"}


def firebase_base():
    """Real Firebase reference for this device (imported lazily so tests need no credentials)."""
    import firebase_admin
    from firebase_admin import credentials, db

    if not firebase_admin._apps:
        firebase_admin.initialize_app(
            credentials.Certificate(config.KEY_PATH),
            {"databaseURL": config.DATABASE_URL, "httpTimeout": 10},
        )
    return db.reference(f"devices/{config.DEVICE_ID}")


class CloudSync:
    def __init__(self, store_factory, tm, status_fn, commands, boot_id,
                 base_factory=firebase_base, mono=time.monotonic, cfg=config):
        self.store_factory = store_factory  # store is opened inside the sync thread
        self.tm = tm
        self.status_fn = status_fn          # dict of live machine state (fan, heater, temp, ...)
        self.commands = commands            # queue.Queue read by the control loop
        self.boot_id = boot_id
        self.base_factory = base_factory
        self.mono = mono
        self.cfg = cfg

        self._base = None
        self._store = None
        self._lock = threading.Lock()
        self._pending_writes = {}           # e.g. {"commands/heater": False}
        self._last_task_cmd = None
        self._last_manual = None
        self._last_safe_temp = None
        self._last_publish = None
        self._last_settings_poll = None
        self._last_sig = None               # machine state as last published, minus temp
        self._state_dirty = False
        self._offline_since = None
        self._wake = threading.Event()      # set by the stream listeners and notify()
        self._listeners = []

        self.online = False
        self.ever_ok = False
        self.last_ok = None                 # monotonic time of last successful write

    # ---------------- api for the control loop ----------------
    def request_write(self, path, value):
        with self._lock:
            self._pending_writes[path] = value

    def notify(self):
        """Machine state changed: publish it now instead of waiting for the next timer."""
        self._state_dirty = True
        self._wake.set()

    def seconds_since_ok(self):
        return None if self.last_ok is None else self.mono() - self.last_ok

    # ---------------- thread ----------------
    def run(self, stop):
        delay = self.cfg.SYNC_INTERVAL
        while not stop.is_set():
            if self.sync_once():
                delay = self.cfg.SYNC_INTERVAL
            else:
                delay = min(max(delay * 2, self.cfg.SYNC_INTERVAL), self.cfg.SYNC_BACKOFF_MAX)
            # a stream event or notify() cuts the wait short; the timer is the polling backup
            self._wake.wait(delay)
            self._wake.clear()
        self._stop_listeners()

    def store(self):
        if self._store is None:
            self._store = self.store_factory()
        return self._store

    def sync_once(self):
        try:
            if self._base is None:
                self._base = self.base_factory()
            self._start_listeners()
            self._poll_commands()
            self._publish()
        except Exception as e:
            self._went_offline(e)
            return False
        return True

    # ---------------- internals ----------------
    def _start_listeners(self):
        """Stream commands and task/request only as a doorbell; the parsing stays in the poll.

        If streaming is unavailable or silently dies, the timed poll still delivers everything.
        """
        if self._listeners or not self.cfg.COMMAND_LISTEN:
            return
        for path in ("commands", "task/request"):
            try:
                self._listeners.append(self._base.child(path).listen(lambda _event: self._wake.set()))
            except Exception as e:
                print(f"could not stream {path}, polling instead:", e)

    def _stop_listeners(self):
        listeners, self._listeners = self._listeners, []
        for listener in listeners:
            try:
                listener.close()
            except Exception:
                pass

    def _poll_commands(self):
        # manual commands first: they are the latency-sensitive ones
        cmds = self._base.child("commands").get() or {}
        if isinstance(cmds, dict):
            manual = (cmds.get("fan") is True, cmds.get("heater") is True)
            if manual != self._last_manual:
                self._last_manual = manual
                self.commands.put(("manual", manual))

        request = self._base.child("task/request").get()
        key = command_key(request)
        if key is not None and key != self._last_task_cmd:
            self._last_task_cmd = key
            self.commands.put(("task", request))

        now = self.mono()
        if (self._last_settings_poll is None
                or now - self._last_settings_poll >= self.cfg.SETTINGS_POLL_SECONDS):
            settings = self._base.child("settings").get()
            self._last_settings_poll = now
            if isinstance(settings, dict) and "safe_temp_c" in settings:
                safe = settings["safe_temp_c"]
                if safe != self._last_safe_temp:
                    self._last_safe_temp = safe
                    self.commands.put(("settings", safe))

    def _publish(self):
        store = self.store()
        now = self.mono()
        with self._lock:
            writes = dict(self._pending_writes)
        events = store.pending_events(self.cfg.EVENT_BATCH)

        # cleared before the status is read, so a change made during the write publishes again
        dirty, self._state_dirty = self._state_dirty, False
        status = self.status_fn()
        sig = {k: v for k, v in status.items() if k != "temp"}  # temp keeps its own cadence

        due = (
            writes or events or dirty or sig != self._last_sig
            or not self.online or self._last_publish is None
            or now - self._last_publish >= self.cfg.TEMP_PUBLISH_SECONDS
        )
        if not due:
            return

        updates = dict(writes)
        for e in events:
            updates[f"events/{e['id']:010d}"] = {
                "type": e["type"],
                "message": e["message"],
                "task_id": e["task_id"],
                "data": e["data"],
                "at": int(e["wall_ts"] * 1000),
                "clock_synced": bool(e["clock_synced"]),
                "boot_id": e["boot_id"],
            }
        for k, v in status.items():
            updates[f"state/{k}"] = v
        report = self.tm.snapshot()
        if report is not None:
            report["reported_at"] = SERVER_TS
        updates["task/report"] = report
        ack = store.last_command()
        if ack:
            updates["task/ack"] = {
                "command_id": ack["command_id"], "command": ack["action"], "task_id": ack["task_id"],
                "result": ack["result"], "reason": ack["reason"],
                "at": int(ack["processed_wall"] * 1000),
            }
        last = store.last_event()
        if last:
            updates["state/last_event"] = (last["message"] or last["type"])[:200]
        updates["state/boot_id"] = self.boot_id
        updates["state/pending_events"] = max(0, store.pending_count() - len(events))
        updates["state/last_seen"] = SERVER_TS

        self._base.update(updates)

        store.mark_uploaded([e["id"] for e in events])
        with self._lock:
            for path, value in writes.items():
                if self._pending_writes.get(path) == value:
                    del self._pending_writes[path]
        self._last_publish = now
        self._last_sig = sig
        self.last_ok = now
        self.ever_ok = True
        if not self.online:
            self.online = True
            if self._offline_since is not None:
                store.log_event("connection_restored", "Firebase connection restored",
                                data={"offline_s": round(now - self._offline_since, 1),
                                      "reboot_during_outage": False})
            self._offline_since = None

    def _went_offline(self, err):
        if self.online or self._offline_since is None:
            print("Firebase unreachable:", err)
            self._offline_since = self.mono()
            try:
                self.store().log_event("connection_lost", "Firebase unreachable", data={"error": str(err)[:200]})
            except Exception as e:
                print("could not log connection loss:", e)
        self.online = False
        self._stop_listeners()  # reopened by the next successful sync
