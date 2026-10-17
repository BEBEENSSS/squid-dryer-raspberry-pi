"""Durable local record of tasks, events and processed commands (SQLite)."""

import json
import sqlite3
import threading
import time
from pathlib import Path

from temp_history import HUMIDITY, SENSORS, placed_ms, record_key

# statuses that mean a task still owns the machine
ACTIVE_STATUSES = ("starting", "warming_up", "running", "recovering", "recovery_required")

# Columns added after the first release, applied to existing databases on open. The *_c / *_s
# snapshot columns are copied from config when a task starts and never change afterwards.
TASK_MIGRATIONS = {
    "drying_started_wall": "REAL",      # backdated to the first sample of the qualifying dwell
    "target_confirmed_wall": "REAL",    # when the dwell finished
    "warmup_elapsed_s": "REAL NOT NULL DEFAULT 0",  # powered-on warm-up time, for the timeout
    "dip_warning": "INTEGER NOT NULL DEFAULT 0",
    "mode": "TEXT",
    "target_c": "REAL",
    "tolerance_c": "REAL",
    "hysteresis_c": "REAL",
    "dwell_s": "REAL",
    "median_window": "INTEGER",
    "warmup_timeout_s": "REAL",
    "dip_limit_s": "REAL",
    "max_temp_c": "REAL",
    # three-zone warm-up. Optional on purpose: a task saved by older firmware has none of them and keeps
    # warming on the bottom sensor alone, as it was accepted.
    "floor_middle_c": "REAL",
    "floor_top_c": "REAL",
    "bypass_zones": "TEXT",     # comma list of zones the operator started without, e.g. "top"
}
SNAPSHOT_FIELDS = ("target_c", "tolerance_c", "hysteresis_c", "dwell_s", "median_window",
                   "warmup_timeout_s", "dip_limit_s", "max_temp_c")
OPTIONAL_SNAPSHOT_FIELDS = ("floor_middle_c", "floor_top_c", "bypass_zones")

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    task_id TEXT PRIMARY KEY,
    weight_kg REAL,
    batch_count INTEGER NOT NULL,
    row_levels TEXT NOT NULL,
    duration_s REAL NOT NULL,
    status TEXT NOT NULL,
    accepted_wall REAL NOT NULL,
    started_wall REAL,
    elapsed_s REAL NOT NULL DEFAULT 0,
    last_temp_c REAL,
    last_temp_wall REAL,
    checkpoint_wall REAL,
    checkpoint_mono REAL,
    checkpoint_boot_id TEXT,
    checkpoint_clock_synced INTEGER NOT NULL DEFAULT 0,
    failure_reason TEXT,
    diagnostics_json TEXT,
    ended_wall REAL,
    acknowledged INTEGER NOT NULL DEFAULT 0,
    seq INTEGER NOT NULL DEFAULT 0,
    completed_rows TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS task_batches (
    task_id TEXT NOT NULL,
    batch_id TEXT NOT NULL,
    row_level INTEGER NOT NULL,
    weight_kg REAL,
    duration_s REAL NOT NULL,
    offset_s REAL NOT NULL,
    PRIMARY KEY (task_id, batch_id)
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT,
    type TEXT NOT NULL,
    message TEXT,
    data_json TEXT,
    wall_ts REAL NOT NULL,
    clock_synced INTEGER NOT NULL,
    boot_id TEXT,
    uploaded INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS events_pending ON events (uploaded, id);
CREATE TABLE IF NOT EXISTS commands_seen (
    command_id TEXT PRIMARY KEY,
    action TEXT,
    task_id TEXT,
    result TEXT NOT NULL,
    reason TEXT,
    processed_wall REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS temp_log (
    task_id TEXT NOT NULL,
    wall REAL NOT NULL,
    mono REAL NOT NULL,
    raw_c REAL NOT NULL,
    smoothed_c REAL NOT NULL,
    phase TEXT NOT NULL,
    zone TEXT NOT NULL DEFAULT 'bottom'
);
CREATE INDEX IF NOT EXISTS temp_log_task ON temp_log (task_id, wall);
CREATE TABLE IF NOT EXISTS task_excursions (
    task_id TEXT NOT NULL,
    start_wall REAL NOT NULL,
    end_wall REAL,
    min_c REAL NOT NULL,
    PRIMARY KEY (task_id, start_wall)
);
CREATE TABLE IF NOT EXISTS temp_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    rid TEXT UNIQUE,          -- record id and Firebase key (see temp_history.record_key); NULL until the time is trusted
    boot_id TEXT NOT NULL,
    mono REAL NOT NULL,
    wall REAL NOT NULL,
    clock_synced INTEGER NOT NULL,
    t_ms INTEGER,             -- collection time; NULL while taken with an unsynced clock
    bottom REAL,
    middle REAL,
    top REAL,
    humidity REAL,            -- %RH of the top sensor (DHT22) in the same read; NULL when it had no valid reading
    gap INTEGER NOT NULL DEFAULT 0,     -- 1: not a continuation of the record before it (first since a start, or after every sensor failed)
    synced INTEGER NOT NULL DEFAULT 0   -- 1 once Firebase has confirmed the record
);
CREATE INDEX IF NOT EXISTS temp_history_pending ON temp_history (t_ms) WHERE synced=0;
CREATE INDEX IF NOT EXISTS temp_history_time ON temp_history (t_ms);
"""


TEMP_MINUTES_SQL = """
CREATE TABLE temp_minutes (
    task_id TEXT NOT NULL,
    zone TEXT NOT NULL DEFAULT 'bottom',
    minute INTEGER NOT NULL,
    n INTEGER NOT NULL,
    sum_c REAL NOT NULL,
    min_c REAL NOT NULL,
    max_c REAL NOT NULL,
    raw_last REAL NOT NULL,
    uploaded INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (task_id, zone, minute)
)"""


def _row(cur_row):
    if cur_row is None:
        return None
    d = dict(cur_row)
    if "diagnostics_json" in d:
        d["diagnostics"] = json.loads(d.pop("diagnostics_json") or "null")
    if "data_json" in d:
        d["data"] = json.loads(d.pop("data_json") or "null")
    return d


class TaskStore:
    def __init__(self, path, boot_id, clock_synced_fn, wall=time.time):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.boot_id = boot_id
        self._clock_synced = clock_synced_fn
        self._wall = wall
        self._local = threading.local()
        with self._conn() as c:
            c.executescript(SCHEMA)
            self._ensure_temp_minutes(c)
            # databases created before completed_rows existed
            cols = [r["name"] for r in c.execute("PRAGMA table_info(tasks)")]
            if "completed_rows" not in cols:
                c.execute("ALTER TABLE tasks ADD COLUMN completed_rows TEXT NOT NULL DEFAULT ''")
            for name, sql_type in TASK_MIGRATIONS.items():
                if name not in cols:
                    c.execute(f"ALTER TABLE tasks ADD COLUMN {name} {sql_type}")
            # databases created before the middle and top sensors: every sample so far is the bottom sensor's
            if "zone" not in [r["name"] for r in c.execute("PRAGMA table_info(temp_log)")]:
                c.execute("ALTER TABLE temp_log ADD COLUMN zone TEXT NOT NULL DEFAULT 'bottom'")
            # databases created before records were written only on change: every row so far continues the last
            if "gap" not in [r["name"] for r in c.execute("PRAGMA table_info(temp_history)")]:
                c.execute("ALTER TABLE temp_history ADD COLUMN gap INTEGER NOT NULL DEFAULT 0")
            # databases created before the top sensor measured humidity: those records simply have none
            if "humidity" not in [r["name"] for r in c.execute("PRAGMA table_info(temp_history)")]:
                c.execute("ALTER TABLE temp_history ADD COLUMN humidity REAL")

    @staticmethod
    def _ensure_temp_minutes(c):
        cols = [r["name"] for r in c.execute("PRAGMA table_info(temp_minutes)")]
        if not cols:
            c.execute(TEMP_MINUTES_SQL)
        elif "zone" not in cols:
            # one-time rebuild, atomic: the key now includes the zone and every existing row is the bottom's
            c.execute("BEGIN IMMEDIATE")
            c.execute("ALTER TABLE temp_minutes RENAME TO temp_minutes_old")
            c.execute(TEMP_MINUTES_SQL)
            c.execute(
                "INSERT INTO temp_minutes(task_id, zone, minute, n, sum_c, min_c, max_c, raw_last, uploaded) "
                "SELECT task_id, 'bottom', minute, n, sum_c, min_c, max_c, raw_last, uploaded FROM temp_minutes_old")
            c.execute("DROP TABLE temp_minutes_old")

    # ---------------- connection ----------------
    def _conn(self):
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.path, timeout=5, isolation_level="DEFERRED")
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=FULL")
            conn.execute("PRAGMA busy_timeout=5000")
            self._local.conn = conn
        return conn

    def close(self):
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None

    # ---------------- meta ----------------
    def get_meta(self, key, default=None):
        r = self._conn().execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return r["value"] if r else default

    def set_meta(self, **values):
        with self._conn() as c:
            c.executemany(
                "INSERT INTO meta(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                [(k, None if v is None else str(v)) for k, v in values.items()],
            )

    # ---------------- events ----------------
    def _insert_event(self, c, type_, message=None, task_id=None, data=None):
        c.execute(
            "INSERT INTO events(task_id, type, message, data_json, wall_ts, clock_synced, boot_id) "
            "VALUES(?, ?, ?, ?, ?, ?, ?)",
            (task_id, type_, message, json.dumps(data) if data is not None else None,
             self._wall(), int(bool(self._clock_synced())), self.boot_id),
        )

    def log_event(self, type_, message=None, task_id=None, data=None):
        with self._conn() as c:
            self._insert_event(c, type_, message, task_id, data)

    def pending_events(self, limit=50):
        rows = self._conn().execute(
            "SELECT * FROM events WHERE uploaded=0 ORDER BY id LIMIT ?", (limit,)
        ).fetchall()
        return [_row(r) for r in rows]

    def pending_count(self):
        return self._conn().execute("SELECT COUNT(*) FROM events WHERE uploaded=0").fetchone()[0]

    def last_event(self):
        return _row(self._conn().execute("SELECT * FROM events ORDER BY id DESC LIMIT 1").fetchone())

    def mark_uploaded(self, ids):
        if not ids:
            return
        with self._conn() as c:
            c.executemany("UPDATE events SET uploaded=1 WHERE id=?", [(i,) for i in ids])

    # ---------------- tasks ----------------
    def create_task(self, task_id, weight_kg, batch_count, row_levels, duration_s, event=None,
                    mode=None, snapshot=None):
        """Insert a new task as 'starting'. Raises sqlite3.IntegrityError on a duplicate id.

        `snapshot` holds the SNAPSHOT_FIELDS values the session runs with from now on, plus the
        OPTIONAL_SNAPSHOT_FIELDS (zone floors, bypassed zones) when there are any.
        """
        snapshot = snapshot or {}
        fields = SNAPSHOT_FIELDS + OPTIONAL_SNAPSHOT_FIELDS
        with self._conn() as c:
            c.execute(
                "INSERT INTO tasks(task_id, weight_kg, batch_count, row_levels, duration_s, status, "
                "accepted_wall, seq, mode, " + ", ".join(fields) + ") "
                "VALUES(?, ?, ?, ?, ?, 'starting', ?, 1, ?" + ", ?" * len(fields) + ")",
                (task_id, weight_kg, batch_count, row_levels, duration_s, self._wall(), mode,
                 *(snapshot.get(k) for k in fields)),
            )
            if event:
                self._insert_event(c, *event[:2], task_id=task_id, data=event[2] if len(event) > 2 else None)
        return self.get_task(task_id)

    def update_task(self, task_id, event=None, **fields):
        """Change task fields (and optionally log an event) in one transaction."""
        if "diagnostics" in fields:
            fields["diagnostics_json"] = json.dumps(fields.pop("diagnostics"))
        sets = ", ".join(f"{k}=?" for k in fields)
        with self._conn() as c:
            c.execute(
                f"UPDATE tasks SET {sets + ', ' if sets else ''}seq=seq+1 WHERE task_id=?",
                (*fields.values(), task_id),
            )
            if event:
                self._insert_event(c, *event[:2], task_id=task_id, data=event[2] if len(event) > 2 else None)
        return self.get_task(task_id)

    def add_batch(self, task_id, batch_id, row_level, weight_kg, duration_s, offset_s,
                  task_duration_s, row_levels, batch_count, event=None, completed_rows=None):
        """Record a batch added to a running task and extend the task, in one transaction."""
        with self._conn() as c:
            c.execute(
                "INSERT INTO task_batches(task_id, batch_id, row_level, weight_kg, duration_s, offset_s) "
                "VALUES(?, ?, ?, ?, ?, ?)",
                (task_id, batch_id, row_level, weight_kg, duration_s, offset_s),
            )
            c.execute(
                "UPDATE tasks SET duration_s=?, row_levels=?, batch_count=?, seq=seq+1 WHERE task_id=?",
                (task_duration_s, row_levels, batch_count, task_id),
            )
            if completed_rows is not None:
                c.execute("UPDATE tasks SET completed_rows=? WHERE task_id=?", (completed_rows, task_id))
            if event:
                self._insert_event(c, *event[:2], task_id=task_id, data=event[2] if len(event) > 2 else None)
        return self.get_task(task_id)

    def list_batches(self, task_id):
        rows = self._conn().execute(
            "SELECT * FROM task_batches WHERE task_id=? ORDER BY offset_s, rowid", (task_id,)).fetchall()
        return [dict(r) for r in rows]

    def batch_exists(self, task_id, batch_id):
        return self._conn().execute(
            "SELECT 1 FROM task_batches WHERE task_id=? AND batch_id=?", (task_id, batch_id)).fetchone() is not None

    def get_task(self, task_id):
        return _row(self._conn().execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone())

    def task_exists(self, task_id):
        return self._conn().execute("SELECT 1 FROM tasks WHERE task_id=?", (task_id,)).fetchone() is not None

    def active_task(self):
        q = "SELECT * FROM tasks WHERE status IN (%s) ORDER BY accepted_wall DESC LIMIT 1" % (
            ",".join("?" * len(ACTIVE_STATUSES)))
        return _row(self._conn().execute(q, ACTIVE_STATUSES).fetchone())

    def unacknowledged_failure(self):
        return _row(self._conn().execute(
            "SELECT * FROM tasks WHERE status='failed' AND acknowledged=0 "
            "ORDER BY accepted_wall DESC LIMIT 1").fetchone())

    def latest_task(self):
        return _row(self._conn().execute(
            "SELECT * FROM tasks ORDER BY accepted_wall DESC, rowid DESC LIMIT 1").fetchone())

    # ---------------- temperature log ----------------
    def append_temp_samples(self, rows):
        """rows: (task_id, wall, mono, raw_c, smoothed_c, phase[, zone]). Also folds them into minute summaries.

        A row without a zone is the bottom sensor's, which is what every caller wrote before there were three.
        """
        if not rows:
            return
        rows = [tuple(r) + ("bottom",) if len(r) == 6 else tuple(r) for r in rows]
        with self._conn() as c:
            c.executemany(
                "INSERT INTO temp_log(task_id, wall, mono, raw_c, smoothed_c, phase, zone) VALUES(?, ?, ?, ?, ?, ?, ?)",
                rows)
            c.executemany(
                "INSERT INTO temp_minutes(task_id, zone, minute, n, sum_c, min_c, max_c, raw_last, uploaded) "
                "VALUES(?, ?, ?, 1, ?, ?, ?, ?, 0) "
                "ON CONFLICT(task_id, zone, minute) DO UPDATE SET n=n+1, sum_c=sum_c+excluded.sum_c, "
                "min_c=min(min_c, excluded.min_c), max_c=max(max_c, excluded.max_c), "
                "raw_last=excluded.raw_last, uploaded=0",
                [(t, z, int(w // 60), s, s, s, raw) for t, w, _m, raw, s, _p, z in rows])

    def temp_log(self, task_id, zone=None):
        """Every logged sample of a task, oldest first; one zone's only when `zone` is given."""
        q, args = "SELECT * FROM temp_log WHERE task_id=?", [task_id]
        if zone is not None:
            q, args = q + " AND zone=?", args + [zone]
        return [dict(r) for r in self._conn().execute(q + " ORDER BY wall, rowid", args)]

    def temp_minutes(self, task_id):
        """Per-minute summaries for one task, oldest first, one row per zone (see snapshot.minute_entries)."""
        return [dict(r) for r in self._conn().execute(
            "SELECT zone, minute, n, sum_c, min_c, max_c, raw_last FROM temp_minutes WHERE task_id=? "
            "ORDER BY minute, zone", (task_id,))]

    def pending_temp_minutes(self, limit=100):
        """Zone-minute rows not uploaded yet (or changed since), oldest first."""
        return [dict(r) for r in self._conn().execute(
            "SELECT * FROM temp_minutes WHERE uploaded=0 ORDER BY task_id, minute, zone LIMIT ?", (limit,))]

    def temp_minute_rows(self, keys):
        """Every zone's row for each (task_id, minute), so a minute is always uploaded whole."""
        rows = []
        for task_id, minute in keys:
            rows += [dict(r) for r in self._conn().execute(
                "SELECT * FROM temp_minutes WHERE task_id=? AND minute=? ORDER BY zone", (task_id, minute))]
        return rows

    def mark_temp_minutes_uploaded(self, items):
        """items: (task_id, minute, n[, zone]). A minute that gained samples meanwhile stays pending."""
        if not items:
            return
        items = [(i[0], i[1], i[2], i[3] if len(i) > 3 else "bottom") for i in items]
        with self._conn() as c:
            c.executemany(
                "UPDATE temp_minutes SET uploaded=1 WHERE task_id=? AND minute=? AND n=? AND zone=?", items)

    # ---------------- temperature history (rolling chart data, see temp_sync.py) ----------------
    @staticmethod
    def _free_t(c, t_ms):
        """The first time at or after `t_ms` whose record id is unused, so two readings never share a key."""
        while c.execute("SELECT 1 FROM temp_history WHERE rid=?", (record_key(t_ms),)).fetchone():
            t_ms += 1
        return t_ms

    def add_temp_reading(self, readings, mono, gap=False):
        """Store one reading per sensor ({zone: °C, "humidity": %RH}, only what has one). Returns the row id.

        `gap` marks a record that does not continue the one before it, so a chart breaks the line there.
        With a synced clock the record gets its time and id at once. Otherwise it waits, with its monotonic
        time, for place_temp_history: a wall clock that is not synced would date it wrongly.
        """
        wall, synced = self._wall(), bool(self._clock_synced())
        with self._conn() as c:
            t_ms = self._free_t(c, int(round(wall * 1000))) if synced else None
            cur = c.execute(
                "INSERT INTO temp_history(rid, boot_id, mono, wall, clock_synced, t_ms, bottom, middle, top, humidity, gap) "
                "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (record_key(t_ms) if synced else None, self.boot_id, mono, wall, int(synced), t_ms,
                 *(readings.get(zone) for zone in SENSORS), readings.get(HUMIDITY), int(bool(gap))))
            return cur.lastrowid

    def place_temp_history(self, mono_now):
        """Date this boot's readings that were taken before the clock synced. Call once it has.

        Readings of an earlier boot are left alone: how its monotonic clock related to the wall clock is gone,
        so any time given to them would be a guess.
        """
        wall_now = self._wall()
        with self._conn() as c:
            rows = c.execute(
                "SELECT id, mono FROM temp_history WHERE boot_id=? AND t_ms IS NULL AND mono<=? ORDER BY mono",
                (self.boot_id, mono_now)).fetchall()
            for r in rows:
                t_ms = self._free_t(c, placed_ms(r["mono"], wall_now, mono_now))
                c.execute("UPDATE temp_history SET t_ms=?, rid=? WHERE id=?", (t_ms, record_key(t_ms), r["id"]))
        return len(rows)

    def pending_temp_history(self, limit=100):
        """Readings Firebase has not confirmed, oldest first. Ones without a trusted time are not offered."""
        return [dict(r) for r in self._conn().execute(
            "SELECT * FROM temp_history WHERE synced=0 AND t_ms IS NOT NULL ORDER BY t_ms, id LIMIT ?", (limit,))]

    def temp_history_pending_count(self):
        """Readings still to deliver: dated ones, and this boot's that are only waiting for the clock."""
        return self._conn().execute(
            "SELECT COUNT(*) FROM temp_history WHERE synced=0 AND (t_ms IS NOT NULL OR boot_id=?)",
            (self.boot_id,)).fetchone()[0]

    def mark_temp_history_synced(self, ids):
        """Only for rows whose upload Firebase has acknowledged."""
        if not ids:
            return
        with self._conn() as c:
            c.executemany("UPDATE temp_history SET synced=1 WHERE id=?", [(i,) for i in ids])

    def temp_history_since(self, since_ms, limit=100_000, carry=False):
        """Dated readings from `since_ms` on, oldest first, delivered or not (the local panel shows both).

        Records are written only on change, so a steady chamber may have none inside a window. `carry` adds the
        newest one from before `since_ms`: the value the chart holds from the left edge of the window.
        """
        rows = [dict(r) for r in self._conn().execute(
            "SELECT rid, t_ms, bottom, middle, top, humidity, gap FROM temp_history WHERE t_ms IS NOT NULL AND t_ms>=? "
            "ORDER BY t_ms LIMIT ?", (since_ms, limit))]
        if carry:
            before = self._conn().execute(
                "SELECT rid, t_ms, bottom, middle, top, humidity, gap FROM temp_history WHERE t_ms IS NOT NULL AND t_ms<? "
                "ORDER BY t_ms DESC LIMIT 1", (since_ms,)).fetchone()
            if before is not None:
                rows.insert(0, dict(before))
        return rows

    def prune_temp_history(self, keep, buffer_max):
        """Drop delivered readings beyond the newest `keep`. Undelivered ones go only past `buffer_max`, the cap
        that stops a months-long outage from filling the disk; returns how many of those were dropped."""
        with self._conn() as c:
            c.execute(
                "DELETE FROM temp_history WHERE synced=1 AND t_ms <= "
                "(SELECT t_ms FROM temp_history WHERE synced=1 ORDER BY t_ms DESC LIMIT 1 OFFSET ?)", (keep,))
            over = c.execute("SELECT COUNT(*) FROM temp_history WHERE synced=0").fetchone()[0] - buffer_max
            if over <= 0:
                return 0
            c.execute(
                "DELETE FROM temp_history WHERE id IN "
                "(SELECT id FROM temp_history WHERE synced=0 ORDER BY COALESCE(t_ms, 0), id LIMIT ?)", (over,))
            return over

    # ---------------- temperature excursions ----------------
    def save_excursion(self, task_id, start_wall, end_wall, min_c):
        with self._conn() as c:
            c.execute(
                "INSERT INTO task_excursions(task_id, start_wall, end_wall, min_c) VALUES(?, ?, ?, ?) "
                "ON CONFLICT(task_id, start_wall) DO UPDATE SET end_wall=excluded.end_wall, min_c=excluded.min_c",
                (task_id, start_wall, end_wall, min_c))

    def open_excursion(self, task_id):
        r = self._conn().execute(
            "SELECT * FROM task_excursions WHERE task_id=? AND end_wall IS NULL "
            "ORDER BY start_wall DESC LIMIT 1", (task_id,)).fetchone()
        return dict(r) if r else None

    def list_excursions(self, task_id, limit=20):
        """The most recent excursions, oldest first, plus the total count."""
        rows = self._conn().execute(
            "SELECT * FROM task_excursions WHERE task_id=? ORDER BY start_wall DESC LIMIT ?",
            (task_id, limit)).fetchall()
        total = self._conn().execute(
            "SELECT COUNT(*) FROM task_excursions WHERE task_id=?", (task_id,)).fetchone()[0]
        return [dict(r) for r in reversed(rows)], total

    # ---------------- commands ----------------
    def seen_command(self, command_id):
        return _row(self._conn().execute(
            "SELECT * FROM commands_seen WHERE command_id=?", (command_id,)).fetchone())

    def record_command(self, command_id, action, task_id, result, reason=None):
        with self._conn() as c:
            c.execute(
                "INSERT OR IGNORE INTO commands_seen(command_id, action, task_id, result, reason, "
                "processed_wall) VALUES(?, ?, ?, ?, ?, ?)",
                (command_id, action, task_id, result, reason, self._wall()),
            )

    def last_command(self):
        return _row(self._conn().execute(
            "SELECT * FROM commands_seen ORDER BY processed_wall DESC, rowid DESC LIMIT 1").fetchone())
