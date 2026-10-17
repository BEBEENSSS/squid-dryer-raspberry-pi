import config
from machine_settings import MachineSettings, limits_error, shutdown_warning


def limits(st):
    """(target, heater pause, safe shutdown) as the machine uses them."""
    return st.get("manual_target_temp_c"), st.get("heater_cutoff_c"), st.get("temp_max_c")


def test_defaults_when_nothing_saved(rig):
    st = rig.tm.settings
    assert st.get("heater_cutoff_c") == config.HEATER_CUTOFF_C
    assert st.get("manual_target_temp_c") == config.MANUAL_TARGET_TEMP_C
    assert st.get("safe_temp_c") == config.COOLDOWN_SAFE_TEMP_C
    assert limits_error(*limits(st)) is None  # the defaults obey the order they are held to


def test_settings_survive_reboot_without_network(rig):
    changed = rig.tm.apply_settings({"manual_target_temp_c": 47.5, "manual_target_humidity": 55,
                                     "heater_cutoff_c": 52, "safe_temp_c": 35, "updated_at": 100})
    assert set(changed) == {"manual_target_temp_c", "manual_target_humidity", "heater_cutoff_c", "safe_temp_c"}
    st = rig.reboot(downtime=5).tm.settings  # nothing but local storage is available here
    assert st.get("manual_target_temp_c") == 47.5
    assert st.get("manual_target_humidity") == 55
    assert st.get("heater_cutoff_c") == 52
    assert st.get("safe_temp_c") == 35
    assert st.updated_at == 100


def test_invalid_values_rejected_individually(rig):
    rig.tm.apply_settings({"manual_target_temp_c": 47, "heater_cutoff_c": 52})
    changed = rig.tm.apply_settings({"heater_cutoff_c": 75, "manual_target_temp_c": "hot",
                                     "manual_target_humidity": 50, "safe_temp_c": True})
    assert changed == ["manual_target_humidity"]
    assert rig.tm.settings.get("heater_cutoff_c") == 52
    assert rig.tm.settings.get("manual_target_temp_c") == 47


def test_corrupt_saved_values_fall_back_to_defaults(rig):
    rig.store.set_meta(heater_cutoff_c="banana", manual_target_temp_c=999, safe_temp_c=None)
    st = MachineSettings(rig.store)
    assert st.get("heater_cutoff_c") == config.HEATER_CUTOFF_C
    assert st.get("manual_target_temp_c") == config.MANUAL_TARGET_TEMP_C
    assert st.get("safe_temp_c") == config.COOLDOWN_SAFE_TEMP_C


def test_stale_dashboard_write_cannot_overwrite_newer_settings(rig):
    rig.tm.apply_settings({"manual_target_temp_c": 50, "updated_at": 200})
    assert rig.tm.apply_settings({"manual_target_temp_c": 46, "updated_at": 150}) == []
    assert rig.tm.apply_settings({"manual_target_temp_c": 46, "updated_at": 200}) == []
    assert rig.tm.settings.get("manual_target_temp_c") == 50
    assert rig.tm.apply_settings({"manual_target_temp_c": 46, "updated_at": 201}) == ["manual_target_temp_c"]


def test_legacy_bare_safe_temp(rig):
    assert rig.tm.set_safe_temp(35) and rig.tm.safe_temp_c == 35.0
    assert not rig.tm.set_safe_temp(90) and rig.tm.safe_temp_c == 35.0


# ---------------- the heater pause ----------------
def test_the_pause_goes_by_the_hottest_sensor_so_a_hot_spot_pauses_heating(zrig):
    zrig.tm.apply_settings({"manual_target_temp_c": 47, "heater_cutoff_c": 50, "updated_at": 1})
    zrig.oven.zone_drop["top"] = -6.0  # the top reads 6 C warmer than the bottom sensor the thermostat goes by
    zrig.start(duration_min=30)
    bottom_peak = top_peak = 0.0
    heater_states = set()
    for _ in range(2000):
        zrig.run(1)
        bottom_peak = max(bottom_peak, zrig.oven.temp)
        top_peak = max(top_peak, zrig.oven.temp + 6.0)
        heater_states.add(zrig.out.heater.is_active)
        if zrig.task()["status"] != "running":
            break
    # the bottom sensor never reached its target: it was the hot top sensor that stopped the heater
    assert bottom_peak < 47 and top_peak < 52
    assert heater_states == {True, False}  # it cycled instead of staying off or on
    assert zrig.task()["status"] == "completed"  # a pause never ends the task


def test_a_pause_above_the_target_stays_out_of_the_way_of_normal_regulation(rig):
    rig.tm.apply_settings({"manual_target_temp_c": 47, "heater_cutoff_c": 50, "updated_at": 1})
    rig.start(duration_min=30)
    for _ in range(300):
        rig.run(1)
        assert not rig.tm.cutoff.active  # the thermostat holds the target; the pause is only for what it cannot see
    assert rig.task()["status"] == "running"


def test_task_over_temperature_fault_is_independent_of_the_pause(rig):
    rig.start(duration_min=30)
    rig.oven.fixed = config.TEMP_MAX_C
    rig.run(3)
    t = rig.task()
    assert (t["status"], t["failure_reason"]) == ("failed", "over_temperature")
    assert not rig.out.heater.is_active


def test_task_sensor_fault_still_fails_task(rig):
    rig.start(duration_min=30)
    rig.run(3)
    rig.oven.broken = True
    rig.run(config.SENSOR_STALE_SECONDS + 5)
    assert rig.task()["status"] == "failed" and not rig.out.heater.is_active


def test_pause_latch_is_runtime_state_but_the_limit_persists(rig):
    rig.tm.apply_settings({"manual_target_temp_c": 47, "heater_cutoff_c": 52})
    rig.tm.cutoff.update(53, 52)
    assert rig.tm.cutoff.active
    new = rig.reboot(downtime=5, temp=40)
    assert not new.tm.cutoff.active and new.tm.heater_cutoff_c == 52


# ---------------- the order: target < heater pause < safe shutdown ----------------
def test_the_defaults_are_5_below_the_firmware_ceiling_and_the_pause_sits_between(rig):
    assert limits(rig.tm.settings) == (config.TEMP_MAX_C - config.TEMP_RESUME_DROP, config.HEATER_CUTOFF_C,
                                       config.TEMP_MAX_C) == (55.0, 58.0, 60.0)
    assert rig.tm.temp_max_c == 60.0


def test_the_ranges_leave_room_for_every_legal_target():
    # the highest target must still have a legal safe shutdown temperature and a pause between the two
    assert config.MANUAL_TARGET_TEMP_MAX_C + config.TEMP_MAX_GAP_C <= config.TEMP_MAX_MAX_C
    assert config.MANUAL_TARGET_TEMP_MIN_C + config.TEMP_MAX_GAP_C == config.TEMP_MAX_MIN_C
    assert config.MANUAL_TARGET_TEMP_MIN_C + config.CUTOFF_TARGET_GAP_C == config.HEATER_CUTOFF_MIN_C
    assert config.TEMP_MAX_MAX_C - config.CUTOFF_SHUTDOWN_GAP_C == config.HEATER_CUTOFF_MAX_C
    assert config.TEMP_MAX_GAP_C >= config.CUTOFF_TARGET_GAP_C + config.CUTOFF_SHUTDOWN_GAP_C


def test_each_temperature_changes_independently_when_the_order_allows_it(rig):
    assert rig.tm.apply_settings({"manual_target_temp_c": 47, "updated_at": 100}) == ["manual_target_temp_c"]
    assert limits(rig.tm.settings) == (47, 58, 60)  # a target write leaves the others alone
    assert rig.tm.apply_settings({"temp_max_c": 59, "updated_at": 101}) == ["temp_max_c"]
    assert limits(rig.tm.settings) == (47, 58, 59)  # a shutdown write leaves the others alone
    assert rig.tm.apply_settings({"heater_cutoff_c": 52, "updated_at": 102}) == ["heater_cutoff_c"]
    assert limits(rig.tm.settings) == (47, 52, 59)


def test_the_safe_shutdown_must_stay_5_above_the_target_and_above_the_pause(rig):
    # in range by itself, but the saved target is 55 and the saved pause 58
    assert rig.tm.apply_settings({"temp_max_c": 58, "updated_at": 100}) == []
    assert limits(rig.tm.settings) == (55, 58, 60)
    # lowering it needs the pause lowered with it: 52 leaves no room for a pause of 58
    assert rig.tm.apply_settings({"manual_target_temp_c": 47, "temp_max_c": 52, "updated_at": 101}) == []
    assert set(rig.tm.apply_settings({"manual_target_temp_c": 47, "heater_cutoff_c": 50, "temp_max_c": 52,
                                      "updated_at": 102})) == {"manual_target_temp_c", "heater_cutoff_c", "temp_max_c"}
    assert rig.tm.apply_settings({"manual_target_temp_c": 48, "updated_at": 103}) == []  # 4 C below the shutdown
    assert limits(rig.tm.settings) == (47, 50, 52)  # exactly 5 C was fine
    # a trio that breaks the order is refused whole, so no part of it lands
    assert rig.tm.apply_settings({"manual_target_temp_c": 50, "heater_cutoff_c": 51, "temp_max_c": 52,
                                  "updated_at": 104}) == []
    assert limits(rig.tm.settings) == (47, 50, 52)


def test_the_pause_must_sit_between_the_target_and_the_safe_shutdown(rig):
    assert rig.tm.apply_settings({"heater_cutoff_c": 55, "updated_at": 100}) == []  # level with the target
    assert rig.tm.apply_settings({"heater_cutoff_c": 50, "updated_at": 101}) == []  # below it: a second, lower target
    assert rig.tm.apply_settings({"heater_cutoff_c": 56, "updated_at": 102}) == ["heater_cutoff_c"]  # exactly 1 C above
    assert rig.tm.apply_settings({"heater_cutoff_c": 60, "updated_at": 103}) == []  # no room under the shutdown
    assert rig.tm.apply_settings({"heater_cutoff_c": 59, "updated_at": 104}) == ["heater_cutoff_c"]  # exactly 1 C below
    assert limits(rig.tm.settings) == (55, 59, 60)
    # a target that squeezes the pause out is refused too
    assert rig.tm.apply_settings({"manual_target_temp_c": 55, "heater_cutoff_c": 55, "updated_at": 105}) == []


def test_every_temperature_has_its_range(rig):
    assert rig.tm.apply_settings({"manual_target_temp_c": 55.1, "updated_at": 100}) == []
    assert rig.tm.apply_settings({"manual_target_temp_c": 44.9, "updated_at": 101}) == []
    assert rig.tm.apply_settings({"temp_max_c": 60.1, "updated_at": 102}) == []
    assert rig.tm.apply_settings({"heater_cutoff_c": 59.1, "updated_at": 103}) == []
    assert set(rig.tm.apply_settings({"manual_target_temp_c": 45, "heater_cutoff_c": 46, "temp_max_c": 50,
                                      "updated_at": 104})) == {"manual_target_temp_c", "heater_cutoff_c", "temp_max_c"}
    assert rig.tm.apply_settings({"temp_max_c": 49.9, "updated_at": 105}) == []
    assert rig.tm.apply_settings({"heater_cutoff_c": 45.9, "updated_at": 106}) == []
    assert limits(rig.tm.settings) == (45, 46, 50)


def test_the_temperatures_survive_a_reboot_without_network(rig):
    rig.tm.apply_settings({"manual_target_temp_c": 47, "heater_cutoff_c": 50, "temp_max_c": 52, "updated_at": 100})
    st = rig.reboot(downtime=5).tm.settings  # nothing but local storage is available here
    assert limits(st) == (47, 50, 52)
    assert st.stamps["temp_max_c"] == 100 and st.updated_at == 100


def test_a_saved_set_that_breaks_the_order_keeps_the_shutdown_and_lowers_the_others(rig):
    rig.store.set_meta(manual_target_temp_c=55, heater_cutoff_c=58, temp_max_c=50)
    st = MachineSettings(rig.store)
    assert limits(st) == (45, 49, 50)  # the protective limit never moves up to suit the others
    rig.store.set_meta(manual_target_temp_c=50, heater_cutoff_c=58, temp_max_c=56)
    assert limits(MachineSettings(rig.store)) == (50, 55, 56)  # only the pause had to give way
    assert "config_warning" in [e["type"] for e in rig.store.pending_events(100)]


def test_values_saved_under_the_old_wider_ranges_move_up_to_the_new_minimum(rig):
    # all valid before the ranges narrowed (target from 30, pause from 30, maximum from 35)
    rig.store.set_meta(manual_target_temp_c=40, heater_cutoff_c=40, temp_max_c=46)
    assert limits(MachineSettings(rig.store)) == (45, 46, 50)
    migrated = [e for e in rig.store.pending_events(100) if e["type"] == "config_migrated"]
    assert len(migrated) == 3


def test_a_corrupt_saved_max_falls_back_to_the_ceiling(rig):
    rig.store.set_meta(manual_target_temp_c=47, temp_max_c="hot")
    assert limits(MachineSettings(rig.store)) == (47, 58, config.TEMP_MAX_C)


def test_limits_error_skips_the_checks_it_has_no_values_for():
    assert limits_error(55, 58, 60) is None
    assert "at least 5 C above the target" in limits_error(55, None, 59)
    assert "at least 1 C above the target" in limits_error(55, 55, None)
    assert "at least 1 C below the safe shutdown" in limits_error(None, 60, 60)
    assert limits_error(None, 59, 60) is None
    assert limits_error(None, None, None) is None


def test_shutdown_warning_is_the_margin_below_the_safe_shutdown_temperature():
    assert not shutdown_warning(57.9, 60, 2)
    assert shutdown_warning(58.0, 60, 2)
    assert shutdown_warning(59.9, 60, 2)
    assert not shutdown_warning(60.0, 60, 2)  # at it the shutdown has happened, which heater_block reports
    assert not shutdown_warning(None, 60, 2)  # no healthy sensor: that is a sensor fault, not a warning


def cloud(stamp, marks, **values):
    return dict(values, updated_at=stamp, changed_at=marks)


def test_a_cloud_write_never_undoes_a_newer_local_change_to_a_setting_it_did_not_touch(rig):
    rig.tm.apply_settings(cloud(100, {"manual_target_temp_c": 100, "temp_max_c": 100},
                                manual_target_temp_c=47, temp_max_c=60))
    assert limits(rig.tm.settings) == (47, 58, 60)
    # the local panel lowers the safe shutdown temperature, and the pause with it
    rig.tm.apply_settings({"temp_max_c": 52, "heater_cutoff_c": 50, "updated_at": 200})
    assert limits(rig.tm.settings) == (47, 50, 52)
    # the web changes only the target; the shutdown in Firebase is the old one, with its old time
    changed = rig.tm.apply_settings(cloud(300, {"manual_target_temp_c": 300, "temp_max_c": 100},
                                          manual_target_temp_c=46, temp_max_c=60))
    assert changed == ["manual_target_temp_c"] and limits(rig.tm.settings) == (46, 50, 52)
    # the same old write delivered again changes nothing
    assert rig.tm.apply_settings(cloud(100, {"manual_target_temp_c": 100, "temp_max_c": 100},
                                       manual_target_temp_c=47, temp_max_c=60)) == []
    # a web write newer than the local change wins
    assert rig.tm.apply_settings(cloud(400, {"temp_max_c": 400}, manual_target_temp_c=46, temp_max_c=56)) \
        == ["temp_max_c"]
    assert limits(rig.tm.settings) == (46, 50, 56)


def test_the_newest_request_wins_even_when_it_repeats_a_value(rig):
    rig.tm.apply_settings(cloud(100, {"manual_target_temp_c": 100, "temp_max_c": 100},
                                manual_target_temp_c=47, temp_max_c=60))
    # local
    assert set(rig.tm.apply_settings({"temp_max_c": 52, "heater_cutoff_c": 50, "updated_at": 200})) \
        == {"temp_max_c", "heater_cutoff_c"}
    # the web asks for 60 again, later: that is a new request, not a stale one
    assert rig.tm.apply_settings(cloud(300, {"temp_max_c": 300}, temp_max_c=60)) == ["temp_max_c"]
    assert rig.tm.temp_max_c == 60


def test_a_fresh_database_takes_its_first_settings_from_the_cloud(rig):
    changed = rig.tm.apply_settings(cloud(100, {"temp_max_c": 100}, manual_target_temp_c=47, heater_cutoff_c=50,
                                          temp_max_c=52))
    # the target and the pause have no time of their own yet
    assert set(changed) == {"manual_target_temp_c", "heater_cutoff_c", "temp_max_c"}
    # now they have: an unmarked field is no longer a request
    assert rig.tm.apply_settings(cloud(200, {"temp_max_c": 200}, manual_target_temp_c=46, temp_max_c=52)) == []
    assert limits(rig.tm.settings) == (47, 50, 52)


def test_a_refused_request_is_not_revived_by_a_later_change_or_a_restart(rig):
    refused = cloud(100, {"temp_max_c": 100}, temp_max_c=59)  # the saved target is 55
    assert rig.tm.apply_settings(refused) == []
    rig.tm.apply_settings({"manual_target_temp_c": 47, "updated_at": 150})
    assert rig.tm.apply_settings(refused) == []  # delivered again: 59 would fit now, but the request is spent
    assert limits(rig.tm.settings) == (47, 58, 60)
    st = rig.reboot(downtime=5).tm.settings
    assert rig.tm.apply_settings(refused) == [] and st.get("temp_max_c") == 60


# ---------------- a task and the saved temperatures ----------------
def test_a_task_runs_on_the_saved_target_and_max(rig):
    rig.tm.apply_settings({"manual_target_temp_c": 47, "heater_cutoff_c": 50, "temp_max_c": 52, "updated_at": 100})
    rig.start(warmup=True)
    t = rig.task()
    assert (t["target_c"], t["max_temp_c"]) == (47.0, 52.0)
    assert rig.tm.snapshot()["target_temp_c"] == 47.0
    # a new target is the running task's target too; the safe shutdown stays the one it started with
    rig.tm.apply_settings({"manual_target_temp_c": 46, "updated_at": 101})
    assert (rig.task()["target_c"], rig.task()["max_temp_c"]) == (46.0, 52.0)


def test_the_task_thermostat_holds_the_saved_target_and_stays_under_the_max(rig):
    rig.tm.apply_settings({"manual_target_temp_c": 47, "heater_cutoff_c": 50, "temp_max_c": 52, "updated_at": 100})
    rig.start(duration_min=30)
    peak = 0.0
    for _ in range(300):
        rig.run(1)
        peak = max(peak, rig.oven.temp)
    assert rig.task()["status"] == "running"
    assert 45 <= peak < 49  # held around 47, not the old fixed 55, and nowhere near the safe shutdown temperature


def test_the_task_over_temperature_fault_is_the_saved_max(rig):
    rig.tm.apply_settings({"manual_target_temp_c": 47, "heater_cutoff_c": 50, "temp_max_c": 52, "updated_at": 100})
    rig.start(duration_min=30)
    rig.run(3)
    rig.oven.fixed = 52.0  # well under the old fixed 60
    rig.run(3)
    t = rig.task()
    assert (t["status"], t["failure_reason"]) == ("failed", "over_temperature")
    assert t["diagnostics"]["temp_max_c"] == 52.0
    assert not rig.out.heater.is_active


def test_lowering_the_max_applies_to_a_running_task_at_once(rig):
    rig.start(duration_min=30)  # started with 55 / 58 / 60
    rig.oven.fixed = 53.0
    rig.run(3)
    assert rig.task()["status"] == "running"
    rig.tm.apply_settings({"manual_target_temp_c": 47, "heater_cutoff_c": 50, "temp_max_c": 52, "updated_at": 100})
    rig.run(3)
    assert (rig.task()["status"], rig.task()["failure_reason"]) == ("failed", "over_temperature")


def test_raising_the_max_never_loosens_a_running_task(rig):
    rig.tm.apply_settings({"manual_target_temp_c": 47, "heater_cutoff_c": 50, "temp_max_c": 52, "updated_at": 100})
    rig.start(duration_min=30)
    rig.tm.apply_settings({"temp_max_c": 60, "updated_at": 101})
    rig.oven.fixed = 52.0
    rig.run(3)
    assert (rig.task()["status"], rig.task()["failure_reason"]) == ("failed", "over_temperature")


def test_a_start_is_refused_within_5_c_of_the_saved_max(rig):
    rig.tm.apply_settings({"manual_target_temp_c": 47, "heater_cutoff_c": 50, "temp_max_c": 52, "updated_at": 100})
    rig.oven.fixed = 47.0  # the safe shutdown temperature minus the 5 C margin
    rig.run(3)
    assert rig.start(warmup=True)["reason"] == "too_hot"
    rig.oven.fixed = 46.9
    rig.run(3)
    assert rig.start(task_id="T2", warmup=True)["result"] == "accepted"


def test_restart_during_a_task_uses_the_saved_max(rig):
    rig.tm.apply_settings({"manual_target_temp_c": 47, "heater_cutoff_c": 50, "temp_max_c": 52, "updated_at": 100})
    rig.start(duration_min=30)
    rig.run(10)
    r = rig.reboot(downtime=10, temp=53.0)  # hotter than the saved shutdown temperature, cooler than the old 60
    r.oven.fixed = 53.0
    r.run(config.RECOVERY_OBSERVE_SECONDS + 5)
    t = r.task()
    assert (t["status"], t["failure_reason"]) == ("recovery_required", "over_temperature")
    assert t["diagnostics"]["current_temp_c"] == 53.0 and not r.out.heater.is_active
