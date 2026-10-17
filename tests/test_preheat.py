"""Preheat: the fan and heater held at the saved target until the chamber is ready, with no task running."""

import config
import preheat
from tests.test_sync import _manual_rig

ASK = dict(command_id="p1", source="online", want_preheat=True)


# ---------------- the pure session ----------------
class FakeSensor:
    def __init__(self):
        self.reads, self.temp_c, self.temp_time = 0, None, 0.0

    def reading(self, temp, at):
        self.reads, self.temp_c, self.temp_time = self.reads + 1, temp, at


def make_session(clock, **kw):
    args = dict(target_c=50, tolerance_c=2.5, dwell_s=10, window=3, max_gap_s=5, timeout_s=100,
                allowance={"top": 2.0}, control="bottom", zones=("bottom", "top"), required=("bottom", "top"),
                mono=lambda: clock[0], wall=lambda: 1e9 + clock[0])
    args.update(kw)
    return preheat.PreheatSession(**args)


def step(clock, session, sensors, **temps):
    clock[0] += 2
    for zone, temp in temps.items():
        sensors[zone].reading(temp, clock[0])
    return session.update(sensors)


def test_ready_only_once_every_zone_has_held_its_own_band_for_the_dwell():
    clock, sensors = [0.0], {"bottom": FakeSensor(), "top": FakeSensor()}
    session = make_session(clock)
    seen = []
    for _ in range(12):
        seen.append(step(clock, session, sensors, bottom=48.0, top=46.0))  # bottom floor 47.5, top floor 45.5
    assert seen[:3] == [preheat.HEATING] * 3
    assert seen[-1] == preheat.READY and session.ready_wall is not None
    assert preheat.READY in seen and seen.index(preheat.READY) * 2 >= 10  # not before the dwell

    report = session.zones_report()
    assert {z: (r["floor_c"], r["in_band"]) for z, r in report.items()} == {"bottom": (47.5, True), "top": (45.5, True)}


def test_one_zone_below_its_floor_keeps_it_heating():
    clock, sensors = [0.0], {"bottom": FakeSensor(), "top": FakeSensor()}
    session = make_session(clock)
    for _ in range(20):
        step(clock, session, sensors, bottom=49.0, top=45.0)  # the top is under its 45.5
    assert session.status == preheat.HEATING and session.ready_wall is None
    assert session.zones_report()["top"]["in_band"] is False


def test_a_zone_that_was_not_reading_at_the_start_is_not_waited_for_and_says_so():
    clock, sensors = [0.0], {"bottom": FakeSensor(), "top": FakeSensor()}
    session = make_session(clock, required=("bottom",))
    for _ in range(12):
        step(clock, session, sensors, bottom=48.0)
    assert session.status == preheat.READY
    assert session.zones_report()["top"]["bypassed"] is True and session.zones_report()["bottom"]["bypassed"] is None


def test_silence_is_never_ready():
    clock, sensors = [0.0], {"bottom": FakeSensor(), "top": FakeSensor()}
    session = make_session(clock)
    for _ in range(12):
        step(clock, session, sensors, bottom=48.0, top=46.0)
    assert session.status == preheat.READY
    clock[0] += 6  # no new reading for longer than the gap
    assert session.update(sensors) == preheat.HEATING


def test_a_new_target_starts_the_hold_over_and_moves_the_floors():
    clock, sensors = [0.0], {"bottom": FakeSensor(), "top": FakeSensor()}
    session = make_session(clock)
    for _ in range(12):
        step(clock, session, sensors, bottom=48.0, top=46.0)
    assert session.status == preheat.READY
    session.rebuild(55)
    assert session.confirmed is False and session.target_c == 55
    assert {z: r["floor_c"] for z, r in session.zones_report().items()} == {"bottom": 52.5, "top": 50.5}
    assert step(clock, session, sensors, bottom=48.0, top=46.0) == preheat.HEATING


def test_the_timeout_counts_until_it_was_ready_once_and_never_after():
    clock, sensors = [0.0], {"bottom": FakeSensor(), "top": FakeSensor()}
    cold = make_session(clock)
    clock[0] = 99
    assert not cold.timed_out()
    clock[0] = 100
    assert cold.timed_out()

    clock[0] = 0
    warm = make_session(clock)
    for _ in range(12):
        step(clock, warm, sensors, bottom=48.0, top=46.0)
    clock[0] += 500
    assert warm.ready_wall is not None and not warm.timed_out()  # it got there; holding it is not a timeout


# ---------------- through the manual thermostat ----------------
def run_manual(rig, manual, seconds):
    for _ in range(seconds):
        rig.run(1)
        manual.tick()


def test_asking_for_a_preheat_switches_the_fan_and_heater_on_and_reaches_ready(rig):
    manual, _, _ = _manual_rig(rig)
    rig.tm.apply_settings({"manual_target_temp_c": 50, "updated_at": 1})
    manual.apply(False, False, **ASK)  # the operator asked for nothing but the preheat
    assert rig.out.fan.is_active and rig.out.heater.is_active and manual.enabled
    assert manual.preheat_status == preheat.HEATING and manual.preheat_active
    assert manual.wanted == {"fan": True, "heater": True, "preheat": True}

    run_manual(rig, manual, 400)
    assert manual.preheat_status == preheat.READY
    assert manual.preheat.ready_wall is not None
    assert 45 <= rig.oven.temp <= 52   # held around the saved target, not run away
    assert rig.out.fan.is_active and manual.enabled
    events = [e["message"] for e in rig.store.pending_events(1000)]
    assert any("preheat started" in m for m in events) and any("preheat ready" in m for m in events)


def test_a_preheat_that_never_gets_there_stops_with_the_heater_off(rig, monkeypatch):
    monkeypatch.setattr(config, "WARMUP_TIMEOUT_SECONDS", 30)
    manual, sync, _ = _manual_rig(rig)
    manual.apply(True, True, **ASK)
    rig.oven.fixed = 30.0  # the chamber does not warm
    run_manual(rig, manual, 40)
    assert (manual.preheat_status, manual.preheat_reason) == (preheat.FAILED, "preheat_timeout")
    assert not rig.out.heater.is_active and not manual.enabled and manual.block == "preheat_timeout"
    assert manual.wanted["preheat"] is False  # the local panel's picture of the request is cleared too
    assert sync._pending_writes.get("commands/preheat") is False  # the request is cleared, so the switch returns off


def test_a_failed_sensor_ends_the_preheat_with_its_reason(rig):
    manual, _, _ = _manual_rig(rig)
    manual.apply(True, True, **ASK)
    rig.oven.broken = True
    run_manual(rig, manual, 15)
    assert (manual.preheat_status, manual.preheat_reason) == (preheat.FAILED, "sensor_fault")
    assert not rig.out.heater.is_active


def test_the_safe_shutdown_ends_the_preheat(rig):
    manual, _, _ = _manual_rig(rig)
    manual.apply(True, True, **ASK)
    rig.oven.fixed = rig.tm.temp_max_c + 1
    run_manual(rig, manual, 6)
    assert (manual.preheat_status, manual.preheat_reason) == (preheat.FAILED, "over_temperature")
    assert not rig.out.heater.is_active


def test_stopping_it_switches_everything_off_and_forgets_a_failure(rig):
    manual, _, _ = _manual_rig(rig)
    manual.apply(True, True, **ASK)
    assert manual.preheat_status == preheat.HEATING
    manual.apply(False, False, command_id="p2", source="online")
    assert manual.preheat_status == preheat.OFF and manual.preheat is None
    assert not rig.out.heater.is_active and not rig.out.fan.is_active and not manual.enabled

    rig.oven.broken = True
    rig.run(15)  # long enough for the sensor to count as failed
    manual.apply(True, True, command_id="p3", source="online", want_preheat=True)
    assert (manual.preheat_status, manual.preheat_reason) == (preheat.FAILED, "sensor_fault")  # refused, with why
    manual.apply(False, False, command_id="p4", source="online")
    assert (manual.preheat_status, manual.preheat_reason) == (preheat.OFF, None)


def test_the_reboot_block_still_applies_and_says_why(rig):
    manual, _, _ = _manual_rig(rig, online=False)
    manual.apply(True, True, **ASK)
    assert not rig.out.heater.is_active
    assert (manual.preheat_status, manual.preheat_reason) == (preheat.FAILED, "reboot_block")


def test_asking_again_while_it_runs_keeps_the_same_session(rig):
    manual, _, _ = _manual_rig(rig)
    manual.apply(True, True, **ASK)
    first = manual.preheat
    manual.apply(True, True, command_id="p9", source="online", want_preheat=True)
    assert manual.preheat is first  # the timeout is not reset by a repeated request


def test_a_new_saved_target_restarts_the_hold_against_the_new_band(rig):
    manual, _, _ = _manual_rig(rig)
    rig.tm.apply_settings({"manual_target_temp_c": 47, "updated_at": 1})
    manual.apply(True, True, **ASK)
    run_manual(rig, manual, 300)
    assert manual.preheat_status == preheat.READY
    rig.tm.apply_settings({"manual_target_temp_c": 53, "updated_at": 2})
    run_manual(rig, manual, 2)
    assert manual.preheat.target_c == 53 and manual.preheat_status == preheat.HEATING
    assert rig.out.heater.is_active  # the thermostat follows the same saved target
    run_manual(rig, manual, 400)
    assert manual.preheat_status == preheat.READY and manual.preheat.zones_report()["bottom"]["floor_c"] == 50.5


def test_it_is_refused_while_a_task_runs_or_the_machine_cools(rig):
    manual, _, _ = _manual_rig(rig)
    assert rig.start()["result"] == "accepted"
    manual.apply(True, True, **ASK)
    assert (manual.preheat_status, manual.preheat_reason) == (preheat.FAILED, "task_active")
    assert manual.preheat is None


def test_the_middle_and_top_are_waited_for_and_one_that_fails_stops_it(zrig):
    manual, _, _ = _manual_rig(zrig)
    zrig.tm.apply_settings({"manual_target_temp_c": 50, "updated_at": 1})
    manual.apply(True, True, **ASK)
    assert set(manual.preheat.required) == {"bottom", "middle", "top"}
    zrig.oven.zone_broken.add("top")
    run_manual(zrig, manual, 30)
    assert (manual.preheat_status, manual.preheat_reason) == (preheat.FAILED, "zone_sensor_failure")
    assert not zrig.out.heater.is_active


def test_a_middle_sensor_that_is_not_reading_at_the_start_is_left_out_and_reported(zrig):
    manual, _, _ = _manual_rig(zrig)
    zrig.oven.zone_broken.add("middle")
    run_manual(zrig, manual, 30)  # long enough for the sensor to count as failed
    manual.apply(True, True, **ASK)
    assert set(manual.preheat.required) == {"bottom", "top"}
    assert manual.preheat.zones_report()["middle"]["bypassed"] is True


# ---------------- handing over to a drying task ----------------
def test_a_task_starts_from_a_preheating_chamber_and_the_preheat_hands_over(rig):
    manual, _, _ = _manual_rig(rig)
    rig.tm.preheating = lambda: manual.preheat_active
    manual.apply(True, True, **ASK)
    run_manual(rig, manual, 30)
    assert rig.out.heater.is_active  # the preheat's heater is on: without the preheat this start is heater_in_use

    ack = rig.start(warmup=True)
    assert ack["result"] == "accepted"
    assert rig.task()["status"] == "warming_up"
    assert not rig.out.heater.is_active  # off at the hand-over, the task's thermostat decides from here
    manual.tick()
    assert manual.preheat_status == preheat.OFF and not manual.enabled  # the task owns the machine now


def test_a_chamber_held_at_the_target_is_not_too_hot_for_the_start_but_a_hot_spot_still_is(rig):
    manual, _, _ = _manual_rig(rig)
    rig.tm.preheating = lambda: manual.preheat_active
    rig.tm.apply_settings({"manual_target_temp_c": 55, "heater_cutoff_c": 58, "temp_max_c": 60, "updated_at": 1})
    manual.apply(True, True, **ASK)
    rig.oven.fixed = 55.5  # at or above the 55 that a plain start refuses (shutdown 60 minus 5)
    run_manual(rig, manual, 4)
    assert rig.tm.held_at_target(55.5)
    assert rig.start(task_id="T-hot", warmup=True)["result"] == "accepted"


def test_the_heater_pause_or_no_preheat_still_means_too_hot(rig):
    manual, _, _ = _manual_rig(rig)
    rig.tm.preheating = lambda: manual.preheat_active
    rig.tm.apply_settings({"manual_target_temp_c": 55, "heater_cutoff_c": 58, "temp_max_c": 60, "updated_at": 1})
    rig.oven.fixed = 55.5
    rig.sensor.poll()
    assert not rig.tm.held_at_target(55.5)  # nothing is preheating
    assert rig.start(task_id="T-cold", warmup=True)["reason"] == "too_hot"

    manual.apply(True, True, **ASK)
    rig.oven.fixed = 58.5  # a hot spot: the pause is active
    run_manual(rig, manual, 4)
    assert rig.tm.cutoff.active and not rig.tm.held_at_target(58.5)
    assert rig.start(task_id="T-spot", warmup=True)["reason"] == "too_hot"
