"""Durable local record of tasks, events and processed commands (SQLite)."""

import json
import sqlite3
import threading
import time
from pathlib import Path

# statuses that mean a task still owns the machine
ACTIVE_STATUSES = ("starting", "running", "recovering", "recovery_required")

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
"""


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
            # databases created before completed_rows existed
            cols = [r["name"] for r in c.execute("PRAGMA table_info(tasks)")]
            if "completed_rows" not in cols:
                c.execute("ALTER TABLE tasks ADD COLUMN completed_rows TEXT NOT NULL DEFAULT ''")

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
    def create_task(self, task_id, weight_kg, batch_count, row_levels, duration_s, event=None):
        """Insert a new task as 'starting'. Raises sqlite3.IntegrityError on a duplicate id."""
        with self._conn() as c:
            c.execute(
                "INSERT INTO tasks(task_id, weight_kg, batch_count, row_levels, duration_s, status, "
                "accepted_wall, seq) VALUES(?, ?, ?, ?, ?, 'starting', ?, 1)",
                (task_id, weight_kg, batch_count, row_levels, duration_s, self._wall()),
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
