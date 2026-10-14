import config
from heater_guard import HeaterGuard, NO_TEMPERATURE_RISE, RUNTIME_EXCEEDED


def run(guard, seconds, temp_fn, is_on=True, start=0.0):
    """Tick once a second. Returns the first latched reason and the time it happened."""
    for i in range(int(seconds)):
        t = start + i
        reason = guard.update(is_on, temp_fn(t), t)
        if reason:
            return reason, t
    return None, None


def test_warming_heater_trips_the_runtime_cap_only():
    g = HeaterGuard()
    # +0.01 C/s keeps the chamber warming, so only the hard cap can fire
    reason, t = run(g, config.HEATER_MAX_CONTINUOUS_SECONDS + 60, lambda t: 25 + 0.01 * t)
    assert reason == RUNTIME_EXCEEDED
    assert t == config.HEATER_MAX_CONTINUOUS_SECONDS
    assert g.latched == RUNTIME_EXCEEDED


def test_no_rise_trips_early():
    g = HeaterGuard()
    reason, t = run(g, 3600, lambda t: 30.0)
    assert reason == NO_TEMPERATURE_RISE
    assert t == config.HEATER_RISE_CHECK_SECONDS


def test_slow_but_steady_warming_is_fine():
    g = HeaterGuard()
    # 2.5 C every 9 minutes never leaves a 10 minute window without a 2 C rise
    reason, _ = run(g, 40 * 60, lambda t: 25 + (t // 540) * 2.5)
    assert reason is None


def test_any_off_resets_the_continuous_period():
    g = HeaterGuard()
    for cycle in range(5):
        base = cycle * 3000
        assert run(g, 2400, lambda t: 25 + 0.01 * t, start=base)[0] is None
        assert g.update(False, 40.0, base + 2401) is None  # thermostat switches off


def test_latched_guard_stays_latched_until_cleared():
    g = HeaterGuard()
    run(g, 3600, lambda t: 30.0)
    assert g.update(False, 30.0, 4000) is None
    assert g.latched == NO_TEMPERATURE_RISE
    g.clear()
    assert g.latched is None
    assert run(g, 60, lambda t: 30.0, start=5000)[0] is None


def test_missing_reading_does_not_count_as_no_rise():
    g = HeaterGuard()
    assert run(g, config.HEATER_RISE_CHECK_SECONDS + 100, lambda t: None)[0] is None


def test_safe_heater_refuses_without_fan(rig):
    assert rig.out.heater.on() is False
    assert not rig.out.heater.is_active
    rig.out.fan.on()
    assert rig.out.heater.on() is True and rig.out.heater.is_active


def test_safe_heater_refuses_without_valid_reading(rig):
    rig.out.attach_sensor(rig.sensor)
    rig.out.fan.on()
    rig.oven.broken = True
    for _ in range(config.SENSOR_BAD_LIMIT):
        rig.sensor.poll()
    assert rig.out.heater.on() is False


def test_check_cuts_the_relay_when_the_fan_stops(rig):
    rig.out.fan.on()
    rig.out.heater.on()
    rig.out.fan.off()
    rig.out.heater.check()
    assert not rig.out.heater.is_active


def test_latched_heater_cannot_be_switched_on(rig):
    rig.out.attach_sensor(rig.sensor)
    rig.out.fan.on()
    rig.oven.fixed = 30.0
    rig.sensor.poll()
    rig.out.heater.on()
    for _ in range(config.HEATER_RISE_CHECK_SECONDS + 2):
        rig.clock.advance(1)
        rig.out.heater.guard.update(True, 30.0, rig.clock.mono)
    assert rig.out.heater.latched == NO_TEMPERATURE_RISE
    assert rig.out.heater.on() is False and not rig.out.heater.is_active


def test_task_fails_on_latch_and_acknowledge_clears_it(rig):
    rig.out.attach_sensor(rig.sensor)
    assert rig.start("T1", warmup=True)["result"] == "accepted"
    rig.out.heater.guard.latched = RUNTIME_EXCEEDED
    rig.run(2)
    t = rig.task("T1")
    assert t["status"] == "failed" and t["failure_reason"] == RUNTIME_EXCEEDED
    assert not rig.out.heater.is_active
    assert rig.cmd("start", "T2", duration_minutes=5, batch_count=1, row_levels="1")["reason"] == "unacknowledged_failure"
    assert rig.cmd("acknowledge", "T1")["result"] == "accepted"
    assert rig.out.heater.latched is None
