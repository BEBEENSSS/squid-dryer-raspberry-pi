"""Target detection on simulated readings (pure logic, no hardware)."""

import warmup

TARGET, TOL, DWELL, WINDOW, STEP = 55.0, 2.5, 60.0, 5, 2.0
FLOOR = TARGET - TOL
T0 = 1_800_000_000.0  # wall clock of the first sample; mono is the same offset by 1000


def detector():
    return warmup.TargetDetector(TARGET, TOL, DWELL, WINDOW, max_gap_s=STEP * 2.5)


def feed_all(det, readings, start_s=0.0):
    """Feed one reading every STEP seconds. Returns the list of Samples."""
    out = []
    for i, raw in enumerate(readings):
        t = start_s + i * STEP
        out.append(det.feed(1000.0 + t, T0 + t, raw))
    return out


def first_confirmed(samples):
    return next((i for i, s in enumerate(samples) if s.confirmed), None)


def test_single_spike_is_ignored_by_the_median():
    readings = [30.0] * 10 + [58.0] + [30.0] * 10
    samples = feed_all(detector(), readings)
    assert max(s.smoothed for s in samples) < FLOOR
    assert first_confirmed(samples) is None


def test_brief_spike_that_passes_the_median_still_needs_the_dwell():
    # 3 of 5 samples high is enough to move the median, but nowhere near a 60 s dwell
    readings = [30.0] * 10 + [56.0] * 3 + [30.0] * 20
    samples = feed_all(detector(), readings)
    assert any(s.smoothed >= FLOOR for s in samples)
    assert first_confirmed(samples) is None


def test_oscillation_around_target_triggers_only_after_a_continuous_dwell():
    # the thermostat saws between 53 and 55: always inside the band (floor 52.5)
    readings = [53.0, 55.0] * 40
    samples = feed_all(detector(), readings)
    idx = first_confirmed(samples)
    assert idx is not None
    first = next(i for i, s in enumerate(samples) if s.full and s.smoothed >= FLOOR)
    # confirmed exactly one dwell after the first qualifying sample, not before
    assert samples[idx].first_mono == 1000.0 + first * STEP
    assert (idx - first) * STEP >= DWELL
    assert (idx - 1 - first) * STEP < DWELL


def test_dropping_out_of_the_band_restarts_the_dwell():
    in_band = [54.0] * 20                      # 40 s inside the band, then it sags below
    sag = [48.0] * 6
    back = [54.0] * 40
    samples = feed_all(detector(), in_band + sag + back)
    idx = first_confirmed(samples)
    assert idx is not None
    # the earlier 40 s do not count: the confirmed window starts after the sag
    assert samples[idx].first_mono > 1000.0 + (len(in_band) + len(sag)) * STEP
    assert samples[idx].first_mono - 1000.0 >= (len(in_band) + len(sag)) * STEP


def test_confirmation_is_backdated_to_the_first_sample_of_the_dwell():
    samples = feed_all(detector(), [54.0] * 40)
    s = samples[first_confirmed(samples)]
    assert s.first_wall - T0 == s.first_mono - 1000.0   # same instant on both clocks
    assert s.first_wall - T0 == (WINDOW - 1) * STEP     # the first sample with a full window


def test_missing_samples_restart_the_dwell():
    # a 10 s hole in the readings (failed reads): the earlier samples prove nothing about the gap
    first = feed_all(detector(), [54.0] * 20)
    det = detector()
    feed_all(det, [54.0] * 20)
    after_gap = [det.feed(1000.0 + 40.0 + 10.0 + i * STEP, T0 + 50.0 + i * STEP, 54.0) for i in range(40)]
    assert first_confirmed(first) is None
    idx = first_confirmed(after_gap)
    assert idx is not None
    assert after_gap[idx].first_mono >= 1000.0 + 50.0  # nothing from before the gap is used


def test_one_outlier_inside_the_band_does_not_break_the_dwell():
    readings = [54.0] * 12 + [40.0] + [54.0] * 30
    samples = feed_all(detector(), readings)
    assert first_confirmed(samples) is not None
    assert samples[first_confirmed(samples)].first_mono == 1000.0 + (WINDOW - 1) * STEP


def test_never_confirms_while_the_window_is_not_full():
    samples = feed_all(detector(), [60.0] * (WINDOW - 1))
    assert not any(s.full or s.confirmed for s in samples)


def test_dip_tracker_logs_an_excursion_and_warns_after_the_limit():
    dips = warmup.DipTracker(FLOOR, limit_s=600)
    assert dips.feed(0.0, 54.0)[0] is None
    kind, start, end, low, warn = dips.feed(10.0, 50.0)
    assert (kind, start, end, low, warn) == ("opened", 10.0, None, 50.0, False)
    assert dips.feed(300.0, 47.0)[3:] == (47.0, False)
    assert dips.feed(700.0, 49.0)[4] is True              # longer than the limit
    kind, start, end, low, warn = dips.feed(800.0, 54.0)
    assert (kind, start, end, low, warn) == ("closed", 10.0, 800.0, 47.0, True)
    assert not dips.open


def test_a_short_dip_does_not_warn():
    dips = warmup.DipTracker(FLOOR, limit_s=600)
    dips.feed(0.0, 50.0)
    assert dips.feed(60.0, 54.0)[4] is False


def test_a_raised_target_gets_a_settling_grace_that_ends_in_band_or_at_the_deadline():
    # the chamber is below the new floor only because it has not heated up yet: not an excursion
    dips = warmup.DipTracker(FLOOR, limit_s=600, settle_until=500.0)
    assert dips.feed(0.0, 40.0) == (None, None, None, None, False)
    assert dips.feed(300.0, 45.0)[0] is None and not dips.open
    assert dips.feed(350.0, 53.0)[0] is None              # reached the band: the grace is over
    assert dips.settle_until is None
    assert dips.feed(360.0, 50.0)[0] == "opened"          # a later drop is a real dip

    stuck = warmup.DipTracker(FLOOR, limit_s=600, settle_until=500.0)
    assert stuck.feed(499.0, 44.0)[0] is None
    assert stuck.feed(500.0, 44.0)[0] == "opened"          # never got there: the dip is reported
