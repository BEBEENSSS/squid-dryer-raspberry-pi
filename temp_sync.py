"""Rolling temperature history: sample every sensor on a timer, record a sample only when it changed, keep what
Firebase has not confirmed, send it later.

    TempRecorder   its own thread. Reads the three sensors, writes a row to SQLite when one moved. Never touches
                   the network.
    TempUploader   its own thread. Sends pending rows to Firebase oldest first and marks one delivered only
                   after Firebase has answered. Failures leave the rows where they are and back off.

Neither runs on the control loop or on the command thread, so a dead network, a slow Firebase or a huge backlog
cannot delay a relay decision or a command. Each opens its own store (SQLite connections are per thread).
"""

import random
import threading
import time

import config
from temp_history import RemoteWindow, backoff_delay, clean_readings, payload, worth_recording

START_DELAY_SECONDS = 3  # lets the sensor threads take their first reading before the first record


class Gauge:
    """A number one thread writes and others read (readings still waiting to be delivered)."""

    def __init__(self, value=0):
        self.value = value


class TempRecorder:
    def __init__(self, store_factory, zones, clock_synced, pending, cfg=config, mono=time.monotonic, wake=None):
        self.store_factory = store_factory
        self.zones = zones
        self.clock_synced = clock_synced
        self.pending = pending
        self.cfg = cfg
        self.mono = mono
        self.wake = wake                # called after a new row, so the uploader sends it at once
        self._store = None
        self._unplaced = True           # rows may be waiting for a trusted clock (also from before a restart)
        # The readings of the newest record, what the next sample is compared with. None: there is nothing to
        # continue, which is true at start-up and after every sensor failed, and the next record is flagged `gap`.
        self._last = None

    def store(self):
        if self._store is None:
            self._store = self.store_factory()
        return self._store

    def sample_once(self):
        """Record the sample if it changed. Returns the row id, or None when nothing was recorded: no sensor has
        a fresh, valid reading (that leaves a gap in the history instead of a value nobody measured), or no
        sensor moved by `TEMP_HISTORY_MIN_CHANGE_C` since the previous record."""
        store = self.store()
        if self._unplaced and self.clock_synced():
            store.place_temp_history(self.mono())
            self._unplaced = False
        readings = clean_readings(self.zones.temps())  # None for a stale or failed sensor, so it is left out
        if not readings:
            self._last = None  # whatever is measured next does not continue what came before
            return None
        if not worth_recording(self._last, readings, self.cfg.TEMP_HISTORY_MIN_CHANGE_C):
            return None
        if not self.clock_synced():
            self._unplaced = True
        row_id = store.add_temp_reading(readings, self.mono(), gap=self._last is None)
        self._last = readings
        dropped = store.prune_temp_history(self.cfg.TEMP_LOCAL_KEEP, self.cfg.TEMP_BUFFER_MAX)
        if dropped:
            # the only place an undelivered reading is ever given up, and it says so
            store.log_event("temp_buffer_overflow", f"{dropped} unsent temperature readings dropped, buffer full",
                            data={"dropped": dropped, "buffer_max": self.cfg.TEMP_BUFFER_MAX})
        self.pending.value = store.temp_history_pending_count()
        if self.wake is not None:
            self.wake()
        return row_id

    def run(self, stop):
        delay = START_DELAY_SECONDS
        while not stop.wait(delay):
            try:
                self.sample_once()
            except Exception as e:  # a full disk or a locked database must not end the recorder
                print("temperature recorder:", e)
            delay = self.cfg.TEMP_HISTORY_INTERVAL_SECONDS


class TempUploader:
    def __init__(self, store_factory, base_factory, link, pending, cfg=config, mono=time.monotonic,
                 rand=random.random):
        self.store_factory = store_factory
        self.base_factory = base_factory
        self.link = link
        self.pending = pending
        self.cfg = cfg
        self.mono = mono
        self.rand = rand
        self.window = RemoteWindow(cfg.TEMP_HISTORY_RETENTION)
        self._store = None
        self._base = None
        self._wake = threading.Event()
        self._failures = 0
        self._retry_at = 0.0            # monotonic: no attempt before it
        self._trim_owed = False         # an upload left Firebase over the limit
        self._load_warned = False

    # ---------------- cues from other threads ----------------
    def wake(self):
        """A new reading is waiting. It never cuts a backoff short: the retry schedule is what keeps a dead
        link from being hammered."""
        self._wake.set()

    def recovered(self, channel=None):
        """Another part of the sync proved Firebase is reachable again: try now instead of waiting."""
        if channel == "history":
            return
        self._retry_at = 0.0
        self._wake.set()

    # ---------------- thread ----------------
    def store(self):
        if self._store is None:
            self._store = self.store_factory()
        return self._store

    def tick(self):
        """One pass of the loop. Returns how many seconds to wait before the next (0: go again at once)."""
        wait = self._retry_at - self.mono()
        if wait > 0:
            return wait  # backing off
        try:
            result = self.step()
        except Exception as e:  # local trouble (the database), not the network: retry later, say nothing to the link
            print("temperature uploader:", e)
            result = "failed"
        if result == "more":
            return 0.0
        if result == "failed":
            self._failures += 1
            self._retry_at = self.mono() + backoff_delay(
                self._failures, self.cfg.TEMP_SYNC_BACKOFF_BASE, self.cfg.TEMP_SYNC_BACKOFF_MAX, rand=self.rand)
            self.link.check_internet()  # which kind of outage is it? (bounded by the probe timeouts)
            return max(0.0, self._retry_at - self.mono())
        self.link.check_internet()  # something else may be failing while there is nothing to send
        return self.cfg.TEMP_SYNC_IDLE_SECONDS

    def run(self, stop):
        while not stop.is_set():
            wait = self.tick()
            if wait > 0:
                # new data or a recovered link ends the wait early; tick() then decides whether a backoff still holds
                self._wake.wait(wait)
                self._wake.clear()

    def step(self):
        """One request. "more": call again at once. "idle": nothing left to do. "failed": retry after a backoff.

        One multi-path update carries the new records and the deletions of the oldest ones, so Firebase never
        shows the new data without the trim or the reverse. Rows are marked delivered only after it returns.
        """
        store = self.store()
        rows = store.pending_temp_history(self.cfg.TEMP_HISTORY_BATCH)
        if not rows and not self._trim_owed:
            return "idle"
        new_keys = [r["rid"] for r in rows]
        try:
            if self._base is None:
                self._base = self.base_factory()
            base = self._base
            self._load_window(base)
            # never delete at or after the oldest record that is still to be sent
            doomed = self.window.plan(new_keys, new_keys[0] if new_keys else None, self.cfg.TEMP_HISTORY_TRIM_CHUNK)
            if not rows and not doomed:
                self._trim_owed = False
                return "idle"
            updates = {f"temp_history/{r['rid']}": payload(r) for r in rows}
            updates.update({f"temp_history/{key}": None for key in doomed})
            base.update(updates)
        except Exception as e:
            # a timeout can hide a commit, so what Firebase holds is unknown until it is read again
            self.window.invalidate()
            self.link.firebase_failed("history", e)
            return "failed"

        self.window.commit(new_keys, doomed)
        store.mark_temp_history_synced([r["id"] for r in rows])
        store.prune_temp_history(self.cfg.TEMP_LOCAL_KEEP, self.cfg.TEMP_BUFFER_MAX)
        self.pending.value = store.temp_history_pending_count()
        self.link.firebase_ok("history")
        self._failures = 0
        # trimming that the protected backlog or the chunk size held back is finished by a request of its own
        self._trim_owed = self.window.known and len(self.window) > self.cfg.TEMP_HISTORY_RETENTION
        return "more" if len(rows) >= self.cfg.TEMP_HISTORY_BATCH or self._trim_owed else "idle"

    def _load_window(self, base):
        """Read which records Firebase holds, once, to know what can be trimmed. Best effort: failing to read
        them only postpones trimming, it never blocks an upload."""
        if self.window.known:
            return
        try:
            keys = base.child("temp_history").get(shallow=True)
        except Exception as e:
            if not self._load_warned:
                self._load_warned = True
                print("temperature history: cannot list remote records yet, trimming waits:", e)
            return
        self._load_warned = False
        self.window.load(keys.keys() if isinstance(keys, dict) else [])
