"""The recorder and uploader end to end, against the real SQLite store and a fake Firebase.

The numbers in the comments name the scenarios of the original request (1-12 in its "test" section).
"""

import queue
from types import SimpleNamespace

import pytest

import config
from cloud_sync import CloudSync
from link_monitor import FIREBASE_UNAVAILABLE, OFFLINE, SYNCED, LinkMonitor
from task_store import TaskStore
from temp_history import record_key
from temp_sync import Gauge, TempRecorder, TempUploader
from tests.conftest import Clock, FakeBase


def make_cfg(**over):
    """The real config with a small history, so a handful of readings exercise batching and trimming.

    The change limit is 0 here: these tests are about delivering, trimming and buffering, and want a record for
    every sample. The tests of recording only on change ask for the real limit.
    """
    cfg = SimpleNamespace(**{k: getattr(config, k) for k in dir(config) if k.isupper()})
    cfg.__dict__.update(TEMP_HISTORY_RETENTION=10, TEMP_HISTORY_BATCH=4, TEMP_LOCAL_KEEP=10, TEMP_BUFFER_MAX=1000,
                        TEMP_SYNC_BACKOFF_BASE=5, TEMP_SYNC_BACKOFF_MAX=120, TEMP_SYNC_IDLE_SECONDS=10,
                        INTERNET_PROBE_BACKOFF_BASE=10, INTERNET_PROBE_BACKOFF_MAX=120,
                        TEMP_HISTORY_MIN_CHANGE_C=0.0)
    cfg.__dict__.update(over)
    return cfg


class Zones:
    """Stands in for SensorArray.temps(): a sensor with no healthy reading is None."""

    def __init__(self):
        self.values = {"bottom": 30.0, "middle": 29.0, "top": 28.0}

    def temps(self):
        return dict(self.values)


class SpyBase(FakeBase):
    """Remembers which temp_history records each request deleted."""

    def __init__(self):
        super().__init__()
        self.deleted = []

    def _apply(self, values):
        self.deleted.append([p.split("/", 1)[1] for p, v in values.items() if v is None and p.startswith("temp_history/")])
        super()._apply(values)


class Env:
    def __init__(self, tmp_path, base=None, internet=True, **cfg):
        self.tmp_path = tmp_path
        self.clock = Clock()
        self.cfg = make_cfg(**cfg)
        self.base = base or FakeBase()
        self.internet = internet
        self.boot = "boot-1"
        self.zones = Zones()
        self.pending = Gauge()
        self.link = LinkMonitor(probe=lambda: {"internet": self.internet, "dns": True}, mono=lambda: self.clock.mono,
                                cfg=self.cfg, rand=lambda: 0.5)
        self.stores = []
        self._build()

    def _build(self):
        self.store = TaskStore(self.tmp_path / "dryer.db", self.boot, lambda: self.clock.synced,
                               wall=lambda: self.clock.wall)
        self.stores.append(self.store)
        self.uploader = TempUploader(lambda: self.store, lambda: self.base, self.link, self.pending, cfg=self.cfg,
                                     mono=lambda: self.clock.mono, rand=lambda: 0.5)
        self.recorder = TempRecorder(lambda: self.store, self.zones, lambda: self.clock.synced, self.pending,
                                     cfg=self.cfg, mono=lambda: self.clock.mono, wake=self.uploader.wake)
        self.link.on_recovered(self.uploader.recovered)

    def reboot(self, synced=True):
        """The Pi comes back: new boot id, a monotonic clock that starts over, the same database file."""
        self.store.close()
        self.boot = "boot-2"
        self.clock.mono = 5.0
        self.clock.synced = synced
        self._build()

    def record(self, n=1):
        for _ in range(n):
            self.clock.advance(self.cfg.TEMP_HISTORY_INTERVAL_SECONDS)
            self.recorder.sample_once()

    def settle(self, limit=100):
        """Upload until a request has nothing left to do."""
        for _ in range(limit):
            before = self.base.updates
            self.uploader.step()
            if self.base.updates == before:
                return
        raise AssertionError("the uploader never settled")

    def remote(self):
        return sorted(self.base.data.get("temp_history", {}))

    def local_keys(self):
        return [r["rid"] for r in self.store.temp_history_since(0)]

    def pending_rows(self):
        return self.store.temp_history_pending_count()


@pytest.fixture
def env(tmp_path):
    e = Env(tmp_path)
    yield e
    for s in e.stores:
        s.close()


def test_each_record_has_the_sensors_that_read_and_a_dead_sensor_is_left_out(env):
    env.record()
    env.zones.values["middle"] = None  # unhealthy: Sensor.temp() gives None
    env.record()
    env.zones.values = {"bottom": None, "middle": None, "top": None}
    env.clock.advance(30)
    assert env.recorder.sample_once() is None  # nothing was measured, so nothing is recorded
    rows = env.store.temp_history_since(0)
    assert [(r["bottom"], r["middle"], r["top"]) for r in rows] == [(30.0, 29.0, 28.0), (30.0, None, 28.0)]
    env.settle()
    last = env.base.data["temp_history"][rows[1]["rid"]]
    assert last == {"t": rows[1]["t_ms"], "bottom": 30.0, "top": 28.0}  # no middle key at all, not null or 0


# ---------------- recording only on change ----------------
@pytest.fixture
def steady(tmp_path):
    """The real 1 C limit: a sample is recorded only if a sensor moved."""
    e = Env(tmp_path, TEMP_HISTORY_MIN_CHANGE_C=1.0)
    yield e
    for s in e.stores:
        s.close()


def sample(e, **values):
    """One sample 30 s later, with some sensors set to a new value (None: no reading). Returns the row id or None."""
    e.zones.values.update(values)
    e.clock.advance(e.cfg.TEMP_HISTORY_INTERVAL_SECONDS)
    return e.recorder.sample_once()


def stored(e):
    return [(r["bottom"], r["middle"], r["top"], r["gap"]) for r in e.store.temp_history_since(0)]


def test_a_steady_temperature_is_recorded_once_however_long_it_stays(steady):
    assert sample(steady) is not None                  # the first sample has nothing to compare with
    assert [sample(steady) for _ in range(120)] == [None] * 120   # an hour of the same readings
    assert stored(steady) == [(30.0, 29.0, 28.0, 1)]


def test_a_change_under_1_c_is_not_recorded_and_1_c_is_in_either_direction(steady):
    sample(steady)                                     # recorded: 30.0
    assert sample(steady, bottom=30.4) is None
    assert sample(steady, bottom=30.99) is None
    assert sample(steady, bottom=29.2) is None         # 0.8 lower
    assert sample(steady, bottom=31.0) is not None     # exactly 1 C higher than the last RECORDED value
    assert sample(steady, bottom=31.9) is None         # the next one is judged against 31.0, not against 30.99
    assert sample(steady, bottom=30.0) is not None     # exactly 1 C lower
    assert [row[0] for row in stored(steady)] == [30.0, 31.0, 30.0]


def test_a_slow_drift_is_recorded_when_it_adds_up_to_1_c_since_the_last_record(steady):
    sample(steady)
    for temp in (30.2, 30.4, 30.6, 30.8):
        assert sample(steady, bottom=temp) is None     # each step is small, and so is the total so far
    assert sample(steady, bottom=31.0) is not None     # the total since the last record reached 1 C
    assert [row[0] for row in stored(steady)] == [30.0, 31.0]


@pytest.mark.parametrize("zone", ["bottom", "middle", "top"])
def test_the_same_limit_applies_to_every_sensor(steady, zone):
    sample(steady)
    base = steady.zones.values[zone]
    assert sample(steady, **{zone: base + 0.9}) is None
    assert sample(steady, **{zone: base + 1.0}) is not None
    assert sample(steady, **{zone: base + 1.0 - 1.9}) is not None   # and 1 C or more lower
    assert len(stored(steady)) == 3


def test_a_record_holds_every_sensors_current_reading_not_only_the_one_that_moved(steady):
    sample(steady)
    sample(steady, top=26.5)                           # the top moved 1.5 C; the others drifted a little
    steady.zones.values.update(bottom=30.3, middle=29.4)
    sample(steady, top=26.5)
    assert sample(steady, top=27.6) is not None
    assert stored(steady)[-1][:3] == (30.3, 29.4, 27.6)


def test_a_sensor_that_starts_or_stops_reading_is_a_change_however_small(steady):
    sample(steady)
    assert sample(steady, middle=None) is not None     # a fault must show up
    assert sample(steady, middle=None) is None         # and being dead is not a new change
    assert sample(steady, middle=29.0) is not None     # coming back is
    assert [row[1] for row in stored(steady)] == [29.0, None, 29.0]
    assert [row[3] for row in stored(steady)] == [1, 0, 0]  # a sensor dropping out is not a break in the history


def test_the_first_record_after_a_restart_is_kept_and_flagged_even_if_nothing_changed(steady):
    sample(steady)
    sample(steady, bottom=32.0)
    steady.reboot()                                    # the service restarted: the comparison baseline is gone
    assert sample(steady) is not None                  # same 32.0 as before, but nothing says it continued
    assert sample(steady) is None
    assert [row[3] for row in stored(steady)] == [1, 0, 1]


def test_the_first_record_after_every_sensor_failed_is_flagged_even_if_nothing_changed(steady):
    sample(steady)
    assert sample(steady, bottom=None, middle=None, top=None) is None   # nothing measured, nothing recorded
    assert sample(steady, bottom=None, middle=None, top=None) is None
    assert len(stored(steady)) == 1
    assert sample(steady, bottom=30.0, middle=29.0, top=28.0) is not None
    assert [row[3] for row in stored(steady)] == [1, 1]


def test_the_gap_flag_reaches_firebase_only_on_the_records_that_have_it(steady):
    sample(steady)
    sample(steady, bottom=33.0)
    steady.settle()
    first, second = (steady.base.data["temp_history"][k] for k in steady.remote())
    assert first["gap"] is True and "gap" not in second


def test_a_steady_temperature_sends_nothing_to_firebase(steady):
    sample(steady)
    steady.settle()
    updates = steady.base.updates
    for _ in range(100):
        sample(steady)
        steady.uploader.tick()
    assert steady.base.updates == updates and steady.pending_rows() == 0
    assert len(steady.remote()) == 1


def test_a_reading_taken_before_the_clock_synced_is_still_only_recorded_when_it_changed(steady):
    steady.clock.synced = False
    sample(steady)
    assert sample(steady) is None
    assert sample(steady, bottom=32.0) is not None
    steady.clock.advance(30)
    steady.clock.wall = 1_900_000_000.0
    steady.clock.synced = True
    steady.recorder.sample_once()                      # places the two waiting rows; nothing new changed
    steady.settle()
    assert len(steady.remote()) == 2


def test_the_local_history_can_carry_the_record_from_before_a_window(steady):
    sample(steady)
    sample(steady, bottom=33.0)
    newest = steady.store.temp_history_since(0)[-1]["t_ms"]
    assert [r["t_ms"] for r in steady.store.temp_history_since(newest + 1)] == []
    carried = steady.store.temp_history_since(newest + 1, carry=True)
    assert [(r["t_ms"], r["bottom"]) for r in carried] == [(newest, 33.0)]
    assert [r["t_ms"] for r in steady.store.temp_history_since(0, carry=True)] == \
        [r["t_ms"] for r in steady.store.temp_history_since(0)]


def test_a_database_from_before_the_gap_flag_is_upgraded_and_its_old_rows_continue(tmp_path):
    import sqlite3
    path = tmp_path / "old.db"
    db = sqlite3.connect(path)
    db.executescript("""
        CREATE TABLE temp_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT, rid TEXT UNIQUE, boot_id TEXT NOT NULL, mono REAL NOT NULL,
            wall REAL NOT NULL, clock_synced INTEGER NOT NULL, t_ms INTEGER, bottom REAL, middle REAL, top REAL,
            synced INTEGER NOT NULL DEFAULT 0);
        INSERT INTO temp_history(rid, boot_id, mono, wall, clock_synced, t_ms, bottom, synced)
            VALUES ('1000000000000', 'b', 1, 1, 1, 1000000000000, 30.5, 1);
    """)
    db.commit()
    db.close()
    store = TaskStore(path, "boot-new", lambda: True)
    try:
        assert [(r["bottom"], r["gap"]) for r in store.temp_history_since(0)] == [(30.5, 0)]
        store.add_temp_reading({"bottom": 31.0}, 2.0, gap=True)
        assert [r["gap"] for r in store.temp_history_since(0)] == [0, 1]
    finally:
        store.close()


def test_a_new_reading_wakes_the_uploader(env):
    assert not env.uploader._wake.is_set()
    env.record()
    assert env.uploader._wake.is_set()


# 5, 6
def test_collection_continues_with_no_firebase_and_the_buffer_survives_a_restart_and_a_reboot(env):
    env.base.fail = True
    env.record(5)
    assert env.uploader.step() == "failed"
    assert env.pending_rows() == 5 and env.pending.value == 5
    assert env.remote() == []

    env.reboot()  # the service restarted, the Pi rebooted
    assert env.pending_rows() == 5
    assert len(env.store.pending_temp_history()) == 5
    env.base.fail = False
    env.settle()
    assert len(env.remote()) == 5 and env.pending_rows() == 0


# 7, 8, 11
def test_reconnecting_uploads_the_backlog_oldest_first_and_only_then_forgets_it(env):
    env.base.fail = True
    env.record(10)
    assert env.uploader.step() == "failed"
    assert env.base.data.get("temp_history") is None

    env.base.fail = False
    assert [env.uploader.step() for _ in range(3)] == ["more", "more", "idle"]  # 4 + 4 + 2
    expected = [record_key(round((1_800_000_000 + 30 * i) * 1000)) for i in range(1, 11)]
    assert env.remote() == expected                    # chronological, at the time each was taken
    assert env.local_keys() == expected
    assert env.pending_rows() == 0 and env.pending.value == 0
    first = env.base.data["temp_history"][expected[0]]
    # the first record since the recorder started continues nothing, and says so
    assert first == {"t": 1_800_000_030_000, "bottom": 30.0, "middle": 29.0, "top": 28.0, "gap": True}


# 8, 9
def test_a_failure_partway_through_a_backlog_keeps_every_unsent_reading(env):
    env.record(10)
    assert env.uploader.step() == "more"               # the first 4 are delivered
    env.base.fail_updates = 1
    assert env.uploader.step() == "failed"             # the second request dies
    assert env.pending_rows() == 6
    assert len(env.remote()) == 4
    # only delivered rows are marked: the next request starts exactly where the last confirmed one ended
    assert [r["t_ms"] for r in env.store.pending_temp_history(1)] == [1_800_000_150_000]
    env.settle()
    assert len(env.remote()) == 10 and env.pending_rows() == 0


# 9, 10
def test_a_lost_reply_is_retried_without_duplicating_anything(env):
    env.record(3)
    env.base.ack_lost = True
    assert env.uploader.step() == "failed"             # stored remotely, but the Pi cannot know
    assert len(env.remote()) == 3 and env.pending_rows() == 3
    stored = dict(env.base.data["temp_history"])
    assert env.uploader.step() == "idle"               # the retry writes the same keys again
    assert env.base.data["temp_history"] == stored
    assert env.remote() == env.local_keys() and env.pending_rows() == 0


# 3
def test_the_retention_limit_is_enforced_when_new_readings_exceed_it(env):
    for _ in range(14):
        env.record()
        env.settle()
        assert len(env.remote()) <= 10
    assert env.remote() == env.local_keys()            # the newest 10, in order
    assert len(env.remote()) == 10
    assert env.remote()[-1] == record_key(round((1_800_000_000 + 30 * 14) * 1000))


# 4
def test_nothing_is_deleted_when_no_new_readings_arrive_however_much_time_passes(env):
    env.record(12)
    env.settle()
    updates, kept = env.base.updates, dict(env.base.data["temp_history"])
    env.clock.advance(30 * 24 * 3600)  # a month with no readings
    for _ in range(5):
        assert env.uploader.step() == "idle"
        assert env.uploader.tick() == env.cfg.TEMP_SYNC_IDLE_SECONDS
    assert env.base.updates == updates                 # not even a request
    assert env.base.data["temp_history"] == kept


def test_trimming_never_touches_a_record_at_or_after_the_oldest_unsent_one(tmp_path):
    e = Env(tmp_path, base=SpyBase())
    try:
        e.base.fail = True
        e.record(25)                                   # an outage three times the limit long
        assert e.uploader.step() == "failed"
        e.base.fail = False
        for _ in range(20):
            floor = (e.store.pending_temp_history(4) or [{"rid": None}])[0]["rid"]
            before = e.base.updates
            e.uploader.step()
            if e.base.updates == before:
                break
            if floor is not None:
                assert all(key < floor for key in e.base.deleted[-1]), "deleted a record still to be sent"
        assert e.pending_rows() == 0
        assert e.remote() == e.local_keys() and len(e.remote()) == 10
        assert e.remote()[-1] == record_key(round((1_800_000_000 + 30 * 25) * 1000))
    finally:
        for s in e.stores:
            s.close()


class NoListBase(FakeBase):
    """Uploads work, but listing what is stored does not."""

    list_works = False

    def child(self, path):
        ref = super().child(path)
        plain_get = ref.get

        def get(shallow=False):
            if shallow and not self.list_works:
                raise ConnectionError("cannot list")
            return plain_get(shallow)

        ref.get = get
        return ref


def test_trimming_is_best_effort_and_never_holds_up_an_upload(tmp_path):
    e = Env(tmp_path, base=NoListBase())
    try:
        e.record(12)
        e.settle()
        assert len(e.remote()) == 12 and e.pending_rows() == 0   # delivered although it could not trim
        assert not e.uploader.window.known
        e.base.list_works = True
        e.record()
        e.settle()
        assert len(e.remote()) == 10                             # the next upload could, and caught up
    finally:
        for s in e.stores:
            s.close()


# time placement
def test_readings_taken_before_the_clock_synced_wait_for_it_and_are_dated_by_how_long_ago_they_were(env):
    env.clock.synced = False
    env.record(3)                                      # monotonic 1030, 1060, 1090; the wall clock is not to be trusted
    assert env.uploader.step() == "idle" and env.base.updates == 0
    assert env.pending_rows() == 3 and env.pending.value == 3   # waiting, though nothing can be sent yet
    assert env.store.pending_temp_history() == []

    env.clock.advance(30)                              # monotonic 1120
    env.clock.wall = 1_900_000_000.0                   # NTP syncs and the wall clock jumps to the true time
    env.clock.synced = True
    env.recorder.sample_once()                         # places the three and records a fourth, correctly dated
    env.settle()
    assert env.remote() == [record_key(1_900_000_000_000 + ms) for ms in (-90_000, -60_000, -30_000, 0)]
    assert env.pending_rows() == 0


def test_readings_from_a_boot_that_never_synced_are_kept_but_never_sent_with_a_guessed_time(env):
    env.clock.synced = False
    env.record(3)
    env.reboot(synced=True)
    env.record(2)
    env.settle()
    assert len(env.remote()) == 2                      # only this boot's two
    assert env.pending_rows() == 0                     # the old three do not count as waiting for anything
    total = env.store._conn().execute("SELECT COUNT(*) FROM temp_history").fetchone()[0]
    assert total == 5                                  # but they were not thrown away


def test_two_readings_in_the_same_millisecond_get_different_ids(env):
    env.store.add_temp_reading({"bottom": 30.0}, 1.0)
    env.store.add_temp_reading({"bottom": 31.0}, 2.0)  # the fake wall clock has not moved
    assert len(set(env.local_keys())) == 2


# buffer limits
def test_the_buffer_cap_drops_the_oldest_unsent_readings_and_says_so(tmp_path):
    e = Env(tmp_path, TEMP_BUFFER_MAX=5)
    try:
        e.base.fail = True
        e.record(8)
        assert e.pending_rows() == 5
        assert e.local_keys()[0] == record_key(round((1_800_000_000 + 30 * 4) * 1000))  # 1-3 were dropped
        assert "temp_buffer_overflow" in [ev["type"] for ev in e.store.pending_events(100)]
    finally:
        for s in e.stores:
            s.close()


def test_local_history_keeps_delivered_readings_up_to_the_limit_but_never_prunes_unsent_ones(env):
    env.record(15)
    env.settle()
    assert len(env.local_keys()) == 10
    env.base.fail = True
    env.record(20)                                     # more than the local keep, none delivered
    assert env.pending_rows() == 20


# backoff and recovery
def test_failed_uploads_back_off_and_new_data_does_not_cut_the_wait_short(env):
    env.base.fail_updates = 99                         # requests are made, and fail
    env.record(2)
    assert env.uploader.tick() == 5
    attempts = env.base.updates
    env.uploader.wake()                                # a new reading
    assert env.uploader.tick() == 5                    # still backing off: no request
    assert env.base.updates == attempts
    env.clock.advance(5)
    assert env.uploader.tick() == 10                   # the second failure doubles it
    assert env.base.updates == attempts + 1


def test_a_recovered_link_ends_the_backoff_at_once(env):
    env.base.fail_updates = 99
    env.record(2)
    env.uploader.tick()
    env.base.fail_updates = 0
    assert env.uploader.tick() > 0                     # still backing off
    env.link.firebase_failed("state", ConnectionError("down"))
    env.link.firebase_ok("state")                      # the heartbeat got through
    assert env.uploader.tick() == env.cfg.TEMP_SYNC_IDLE_SECONDS   # tried straight away and succeeded
    assert len(env.remote()) == 2


# 12 (and the three-way status)
def test_firebase_down_with_internet_up_is_told_apart_from_no_internet(tmp_path):
    for internet, expected in ((True, FIREBASE_UNAVAILABLE), (False, OFFLINE)):
        e = Env(tmp_path / str(internet), internet=internet)
        try:
            e.base.fail = True
            e.record(2)
            e.uploader.tick()
            assert e.link.status() == expected
            e.base.fail = False
            e.uploader.recovered("state")
            e.uploader.tick()
            assert e.link.status() == SYNCED
        finally:
            for s in e.stores:
                s.close()


def test_a_failing_firebase_with_internet_up_leaves_the_drying_task_alone(rig):
    clock = rig.clock
    cfg = make_cfg()
    base = FakeBase()
    base.fail = True
    link = LinkMonitor(probe=lambda: {"internet": True, "dns": True}, mono=lambda: clock.mono, cfg=cfg,
                       rand=lambda: 0.5)
    status = lambda: {"fan": rig.out.fan.is_active, "heater": rig.out.heater.is_active,  # noqa: E731
                      "status": rig.tm.machine_status()}
    sync = CloudSync(lambda: rig.store, rig.tm, status, queue.Queue(), rig.boot_id, base_factory=lambda: base,
                     mono=lambda: clock.mono, link=link)
    pending = Gauge()
    zones = SimpleNamespace(temps=lambda: {"bottom": rig.sensor.temp()})
    uploader = TempUploader(lambda: rig.store, lambda: base, link, pending, cfg=cfg, mono=lambda: clock.mono,
                            rand=lambda: 0.5)
    recorder = TempRecorder(lambda: rig.store, zones, lambda: clock.synced, pending, cfg=cfg,
                            mono=lambda: clock.mono, wake=uploader.wake)
    link.on_recovered(uploader.recovered)

    rig.start(duration_min=5)
    for second in range(1, 321):                       # the whole task, with Firebase refusing everything
        rig.run(1)
        sync.sync_once()
        if second % 30 == 0:
            recorder.sample_once()
            uploader.tick()
    assert rig.task()["status"] == "completed" and rig.task()["elapsed_s"] >= 300
    assert link.status() == FIREBASE_UNAVAILABLE       # the internet is fine, Firebase is not
    assert pending.value >= 10

    base.fail = False                                  # Firebase answers again
    sync.sync_once()                                   # the heartbeat getting through is the cue
    for _ in range(20):
        uploader.tick()
    assert link.status() == SYNCED and pending.value == 0
    assert len(base.data["temp_history"]) == 10        # trimmed to the limit, chronologically
