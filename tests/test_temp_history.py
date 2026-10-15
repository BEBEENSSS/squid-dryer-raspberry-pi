import math

from temp_history import RemoteWindow, backoff_delay, clean_readings, payload, placed_ms, record_key


def test_keys_are_fixed_width_so_they_sort_by_time():
    assert record_key(1_800_000_000_123) == "1800000000123"
    assert record_key(5) == "0000000000005"
    times = [999_999_999_999, 1_000_000_000_000, 1_800_000_000_000, 9_999_999_999_999]
    assert [record_key(t) for t in times] == sorted(record_key(t) for t in reversed(times))


def test_a_sensor_without_a_reading_is_left_out_not_zeroed():
    got = clean_readings({"bottom": 31.5, "middle": None, "top": float("nan")})
    assert got == {"bottom": 31.5}
    # 0 C is a real temperature
    assert clean_readings({"bottom": 0.0, "middle": -1.5}) == {"bottom": 0.0, "middle": -1.5}
    assert clean_readings({"bottom": True, "middle": math.inf, "top": "40"}) == {}
    assert clean_readings({}) == {}


def test_payload_carries_the_time_and_only_the_sensors_that_read():
    row = {"t_ms": 1_800_000_000_123, "bottom": 31.456, "middle": None, "top": 28.0}
    assert payload(row) == {"t": 1_800_000_000_123, "bottom": 31.46, "top": 28.0}
    assert payload({"t_ms": 7, "bottom": None, "middle": None, "top": None}) == {"t": 7}


def test_a_reading_from_before_the_clock_synced_is_dated_by_how_long_ago_it_was():
    # taken at mono 100; now it is mono 160 and the (now trusted) wall clock says 2_000_000 s
    assert placed_ms(100.0, 2_000_000.0, 160.0) == 1_999_940_000


def test_backoff_doubles_up_to_the_cap_and_jitter_stays_inside_it():
    mid = lambda: 0.5  # noqa: E731  (no jitter)
    assert backoff_delay(0, 5, 120, rand=mid) == 0.0
    assert [backoff_delay(n, 5, 120, rand=mid) for n in range(1, 7)] == [5, 10, 20, 40, 80, 120]
    assert backoff_delay(500, 5, 120, rand=mid) == 120
    assert backoff_delay(3, 5, 120, jitter=0.2, rand=lambda: 0.0) == 16.0
    assert backoff_delay(3, 5, 120, jitter=0.2, rand=lambda: 1.0) == 24.0
    assert backoff_delay(10, 5, 120, jitter=0.2, rand=lambda: 1.0) == 120  # the cap holds with jitter


def keys(*times):
    return [record_key(t) for t in times]


def test_nothing_is_deleted_while_within_the_limit_or_before_the_window_is_known():
    w = RemoteWindow(limit=3)
    assert w.plan(keys(4)) == []  # unknown window plans nothing
    w.load(keys(1, 2))
    assert w.plan(keys(3)) == []  # exactly at the limit
    assert w.plan([]) == []


def test_only_the_excess_oldest_keys_are_deleted():
    w = RemoteWindow(limit=3)
    w.load(keys(1, 2, 3))
    assert w.plan(keys(4), keys(4)[0]) == keys(1)
    assert w.plan(keys(4, 5), keys(4)[0]) == keys(1, 2)
    w.commit(keys(4), keys(1))
    assert len(w) == 3 and w.known


def test_a_record_that_is_still_to_be_sent_is_never_deleted():
    w = RemoteWindow(limit=3)
    w.load(keys(10, 11, 12))
    # a backlog older than everything stored: the oldest keys overall are the ones being sent
    assert w.plan(keys(1, 2), keys(1)[0]) == []
    # the oldest pending record is older than the stored keys: nothing at or after it may go
    assert w.plan(keys(13), keys(5)[0]) == []
    # but stored keys older than the oldest pending one may
    w.load(keys(1, 2, 3))
    assert w.plan(keys(4), keys(4)[0]) == keys(1)


def test_a_trim_only_request_after_a_drain_uses_no_floor():
    w = RemoteWindow(limit=3)
    w.load(keys(1, 2, 3, 4, 5))
    assert w.plan([], None) == keys(1, 2)
    assert w.plan([], None, chunk=1) == keys(1)


def test_an_unknown_outcome_invalidates_the_window():
    w = RemoteWindow(limit=2)
    w.load(keys(1, 2))
    w.invalidate()
    assert not w.known and w.plan(keys(3), keys(3)[0]) == []
    w.commit(keys(3))  # committing to an unknown window changes nothing
    assert not w.known
