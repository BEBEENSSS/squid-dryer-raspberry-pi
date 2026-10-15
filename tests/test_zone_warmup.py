"""Warm-up confirmation across the three zones (pure logic, no hardware)."""

import warmup

TARGET, TOL, DWELL, WINDOW, STEP = 55.0, 2.5, 60.0, 5, 2.0
FLOOR = TARGET - TOL
GAP = STEP * 2.5
T0 = 1_800_000_000.0
ALL = ("bottom", "middle", "top")


def detector(required=ALL, floors=None):
    return warmup.ZoneDetector(floors or {z: FLOOR for z in ALL}, required, DWELL, WINDOW, GAP)


def run(det, series, steps=None):
    """series: {zone: [reading or None, ...]}, one slot per STEP. None means no reading in that slot.

    Returns the joint verdict after every step.
    """
    steps = steps or max(len(v) for v in series.values())
    out = []
    for i in range(steps):
        t = i * STEP
        for zone, vals in series.items():
            if i < len(vals) and vals[i] is not None:
                det.feed(zone, 1000.0 + t, T0 + t, vals[i])
        out.append(det.result(1000.0 + t))
    return out


def first_confirmed(samples):
    return next((i for i, s in enumerate(samples) if s.confirmed), None)


def first_in_band(readings):
    """Index at which a lone detector with this zone's floor first counts the zone as in band."""
    lone = warmup.TargetDetector(FLOOR, 0.0, DWELL, WINDOW, GAP)
    for i, raw in enumerate(readings):
        s = lone.feed(1000.0 + i * STEP, T0 + i * STEP, raw)
        if s.full and s.smoothed >= FLOOR:
            return i
    return None


def test_the_last_zone_to_qualify_starts_the_dwell():
    warm = [54.0] * 90
    top = [40.0] * 30 + [54.0] * 60            # the top lags: 60 s behind the others
    samples = run(detector(), {"bottom": warm, "middle": warm, "top": top})
    lag = first_in_band(top)
    idx = first_confirmed(samples)
    assert idx is not None
    # confirmed exactly one dwell after the top qualified, not after the bottom did
    assert (idx - lag) * STEP >= DWELL > (idx - 1 - lag) * STEP
    assert samples[idx].first_mono == 1000.0 + lag * STEP
    assert samples[idx].first_wall - T0 == samples[idx].first_mono - 1000.0
    assert first_confirmed(samples[:lag + int(DWELL / STEP)]) is None


def test_one_zone_leaving_its_band_restarts_the_joint_dwell():
    warm = [54.0] * 100
    middle = [54.0] * 20 + [45.0] * 8 + [54.0] * 72   # 40 s in band, then a sag, then back
    samples = run(detector(), {"bottom": warm, "middle": middle, "top": warm})
    idx = first_confirmed(samples)
    assert idx is not None
    assert samples[idx].first_mono > 1000.0 + 28 * STEP  # nothing from before the sag counts


def test_a_zone_without_fresh_readings_resets_and_never_counts_as_reached():
    warm = [54.0] * 90
    top = [54.0] * 10 + [None] * 20 + [54.0] * 60       # 40 s of silence from the top sensor
    samples = run(detector(), {"bottom": warm, "middle": warm, "top": top})
    resume = first_in_band([54.0] * 60)                  # window refill after the gap
    idx = first_confirmed(samples)
    assert idx is not None
    assert first_confirmed(samples[:30]) is None         # the early readings prove nothing about the gap
    assert samples[idx].first_mono == 1000.0 + (30 + resume) * STEP


def test_a_bypassed_zone_is_ignored_but_still_smoothed():
    warm = [54.0] * 60
    det = detector(required=("bottom", "middle"))
    samples = run(det, {"bottom": warm, "middle": warm, "top": [30.0] * 60})
    assert first_confirmed(samples) is not None
    assert det.smoothed("top") == 30.0 and not det.in_band("top")


def test_nothing_is_confirmed_before_every_zone_has_a_full_window():
    warm = [54.0] * (WINDOW - 1)
    samples = run(detector(), {"bottom": warm, "middle": warm, "top": warm})
    assert not any(s.confirmed or s.full for s in samples)


def test_no_readings_at_all_is_never_confirmed():
    assert not detector().result(5000.0).confirmed


def test_one_required_zone_behaves_exactly_like_the_single_detector():
    readings = [53.0, 55.0] * 40
    zoned = run(detector(required=("bottom",)), {"bottom": readings})
    lone = warmup.TargetDetector(TARGET, TOL, DWELL, WINDOW, GAP)
    single = [lone.feed(1000.0 + i * STEP, T0 + i * STEP, raw) for i, raw in enumerate(readings)]
    assert first_confirmed(zoned) == first_confirmed(single)
    i = first_confirmed(single)
    assert (zoned[i].first_mono, zoned[i].first_wall, zoned[i].smoothed) == \
           (single[i].first_mono, single[i].first_wall, single[i].smoothed)


def test_smoothed_and_full_are_the_control_zones():
    series = {"bottom": [50.0] * 10, "middle": [54.0] * 10, "top": [54.0] * 10}
    det = detector()
    samples = run(det, series)
    assert samples[-1].smoothed == 50.0 and samples[-1].full
    assert det.smoothed("middle") == 54.0


def test_each_zone_is_reported_as_reached_once_in_the_order_it_gets_there():
    det = detector()
    run(det, {"bottom": [54.0] * 12, "middle": [30.0] * 4 + [54.0] * 8, "top": [30.0] * 12})
    assert det.newly_in_band() == ["bottom", "middle"]
    assert det.newly_in_band() == []                     # each zone is reported once
    # the top gets there later: only it is new
    for i in range(12, 24):
        t = i * STEP
        for zone in ALL:
            det.feed(zone, 1000.0 + t, T0 + t, 54.0)
    assert det.newly_in_band() == ["top"]


def test_per_zone_floors_are_respected():
    floors = {"bottom": FLOOR, "middle": FLOOR, "top": FLOOR - 4.0}   # the top may lag 4 C more
    det = warmup.ZoneDetector(floors, ALL, DWELL, WINDOW, GAP)
    samples = run(det, {"bottom": [54.0] * 60, "middle": [54.0] * 60, "top": [49.0] * 60})
    assert first_confirmed(samples) is not None
    strict = run(detector(), {"bottom": [54.0] * 60, "middle": [54.0] * 60, "top": [49.0] * 60})
    assert first_confirmed(strict) is None
