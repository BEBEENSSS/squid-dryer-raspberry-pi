import config
from machine_settings import MachineSettings


def test_defaults_when_nothing_saved(rig):
    st = rig.tm.settings
    assert st.get("heater_cutoff_c") == config.HEATER_CUTOFF_C
    assert st.get("manual_target_temp_c") == config.MANUAL_TARGET_TEMP_C
    assert st.get("safe_temp_c") == config.COOLDOWN_SAFE_TEMP_C


def test_settings_survive_reboot_without_network(rig):
    changed = rig.tm.apply_settings({"manual_target_temp_c": 42.5, "manual_target_humidity": 55,
                                     "heater_cutoff_c": 52, "safe_temp_c": 35, "updated_at": 100})
    assert set(changed) == {"manual_target_temp_c", "manual_target_humidity", "heater_cutoff_c", "safe_temp_c"}
    st = rig.reboot(downtime=5).tm.settings  # nothing but local storage is available here
    assert st.get("manual_target_temp_c") == 42.5
    assert st.get("manual_target_humidity") == 55
    assert st.get("heater_cutoff_c") == 52
    assert st.get("safe_temp_c") == 35
    assert st.updated_at == 100


def test_invalid_values_rejected_individually(rig):
    rig.tm.apply_settings({"heater_cutoff_c": 52})
    changed = rig.tm.apply_settings({"heater_cutoff_c": 75, "manual_target_temp_c": "hot",
                                     "manual_target_humidity": 50, "safe_temp_c": True})
    assert changed == ["manual_target_humidity"]
    assert rig.tm.settings.get("heater_cutoff_c") == 52
    assert rig.tm.settings.get("manual_target_temp_c") == config.MANUAL_TARGET_TEMP_C


def test_corrupt_saved_values_fall_back_to_defaults(rig):
    rig.store.set_meta(heater_cutoff_c="banana", manual_target_temp_c=999, safe_temp_c=None)
    st = MachineSettings(rig.store)
    assert st.get("heater_cutoff_c") == config.HEATER_CUTOFF_C
    assert st.get("manual_target_temp_c") == config.MANUAL_TARGET_TEMP_C
    assert st.get("safe_temp_c") == config.COOLDOWN_SAFE_TEMP_C


def test_stale_dashboard_write_cannot_overwrite_newer_settings(rig):
    rig.tm.apply_settings({"manual_target_temp_c": 44, "updated_at": 200})
    assert rig.tm.apply_settings({"manual_target_temp_c": 33, "updated_at": 150}) == []
    assert rig.tm.apply_settings({"manual_target_temp_c": 33, "updated_at": 200}) == []
    assert rig.tm.settings.get("manual_target_temp_c") == 44
    assert rig.tm.apply_settings({"manual_target_temp_c": 33, "updated_at": 201}) == ["manual_target_temp_c"]


def test_legacy_bare_safe_temp(rig):
    assert rig.tm.set_safe_temp(35) and rig.tm.safe_temp_c == 35.0
    assert not rig.tm.set_safe_temp(90) and rig.tm.safe_temp_c == 35.0


def test_task_cutoff_pauses_heater_but_task_keeps_running_and_completes(rig):
    rig.tm.apply_settings({"heater_cutoff_c": 40, "updated_at": 1})
    rig.start(duration_min=30, row_levels="1")
    heater_states = set()
    for _ in range(2000):
        rig.run(1)
        assert rig.oven.temp < 41.5
        heater_states.add(rig.out.heater.is_active)
        if rig.task()["status"] != "running":
            break
    assert heater_states == {True, False}  # it cycled instead of staying off or on
    assert rig.task()["status"] == "completed"


def test_task_over_temperature_fault_is_independent_of_cutoff(rig):
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


def test_cutoff_latch_is_runtime_state_but_the_limit_persists(rig):
    rig.tm.apply_settings({"heater_cutoff_c": 45})
    rig.tm.cutoff.update(46, 45)
    assert rig.tm.cutoff.active
    new = rig.reboot(downtime=5, temp=40)
    assert not new.tm.cutoff.active and new.tm.heater_cutoff_c == 45


# ---------------- target and maximum ----------------
def pair(rig):
    st = rig.tm.settings
    return st.get("manual_target_temp_c"), st.get("temp_max_c")


def test_target_defaults_to_5_below_the_firmware_ceiling_max(rig):
    assert pair(rig) == (config.TEMP_MAX_C - config.TEMP_RESUME_DROP, config.TEMP_MAX_C) == (55.0, 60.0)
    assert rig.tm.temp_max_c == 60.0


def test_target_and_max_change_independently(rig):
    assert rig.tm.apply_settings({"manual_target_temp_c": 45, "updated_at": 100}) == ["manual_target_temp_c"]
    assert pair(rig) == (45, 60)  # a target write leaves the maximum alone
    assert rig.tm.apply_settings({"temp_max_c": 52, "updated_at": 101}) == ["temp_max_c"]
    assert pair(rig) == (45, 52)  # and a maximum write leaves the target alone


def test_max_must_stay_5_above_the_target(rig):
    # in range by itself, but the saved target is 55
    assert rig.tm.apply_settings({"temp_max_c": 58, "updated_at": 100}) == []
    assert pair(rig) == (55, 60)
    assert set(rig.tm.apply_settings({"manual_target_temp_c": 45, "temp_max_c": 50, "updated_at": 101})) \
        == {"manual_target_temp_c", "temp_max_c"}
    assert rig.tm.apply_settings({"manual_target_temp_c": 46, "updated_at": 102}) == []  # 4 C below the max
    assert pair(rig) == (45, 50)
    assert set(rig.tm.apply_settings({"manual_target_temp_c": 44, "temp_max_c": 49, "updated_at": 103})) \
        == {"manual_target_temp_c", "temp_max_c"}  # exactly 5 C is fine
    # a pair that breaks the gap is refused whole, so neither half lands
    assert rig.tm.apply_settings({"manual_target_temp_c": 50, "temp_max_c": 52, "updated_at": 104}) == []
    assert pair(rig) == (44, 49)


def test_max_range_is_35_to_the_firmware_ceiling(rig):
    assert rig.tm.apply_settings({"manual_target_temp_c": 30, "temp_max_c": 34.9, "updated_at": 100}) \
        == ["manual_target_temp_c"]
    assert rig.tm.apply_settings({"temp_max_c": 60.1, "updated_at": 101}) == []
    assert rig.tm.apply_settings({"temp_max_c": 35, "updated_at": 102}) == ["temp_max_c"]
    assert pair(rig) == (30, 35)


def test_target_and_max_survive_a_reboot_without_network(rig):
    rig.tm.apply_settings({"manual_target_temp_c": 44, "temp_max_c": 52, "updated_at": 100})
    st = rig.reboot(downtime=5).tm.settings  # nothing but local storage is available here
    assert (st.get("manual_target_temp_c"), st.get("temp_max_c")) == (44, 52)
    assert st.stamps["temp_max_c"] == 100 and st.updated_at == 100


def test_a_saved_pair_that_breaks_the_gap_keeps_the_max_and_lowers_the_target(rig):
    rig.store.set_meta(manual_target_temp_c=55, temp_max_c=50)
    st = MachineSettings(rig.store)
    assert (st.get("manual_target_temp_c"), st.get("temp_max_c")) == (45, 50)


def test_a_corrupt_saved_max_falls_back_to_the_ceiling(rig):
    rig.store.set_meta(manual_target_temp_c=40, temp_max_c="hot")
    st = MachineSettings(rig.store)
    assert (st.get("manual_target_temp_c"), st.get("temp_max_c")) == (40, config.TEMP_MAX_C)


def cloud(stamp, marks, **values):
    return dict(values, updated_at=stamp, changed_at=marks)


def test_a_cloud_write_never_undoes_a_newer_local_change_to_a_setting_it_did_not_touch(rig):
    rig.tm.apply_settings(cloud(100, {"manual_target_temp_c": 100, "temp_max_c": 100},
                                manual_target_temp_c=45, temp_max_c=60))
    assert pair(rig) == (45, 60)
    rig.tm.apply_settings({"temp_max_c": 50, "updated_at": 200})  # the local panel lowers the maximum
    assert pair(rig) == (45, 50)
    # the web changes only the target; the maximum in Firebase is the old one, with its old time
    changed = rig.tm.apply_settings(cloud(300, {"manual_target_temp_c": 300, "temp_max_c": 100},
                                          manual_target_temp_c=42, temp_max_c=60))
    assert changed == ["manual_target_temp_c"] and pair(rig) == (42, 50)
    # the same old write delivered again changes nothing
    assert rig.tm.apply_settings(cloud(100, {"manual_target_temp_c": 100, "temp_max_c": 100},
                                       manual_target_temp_c=45, temp_max_c=60)) == []
    # a web write newer than the local change wins
    assert rig.tm.apply_settings(cloud(400, {"temp_max_c": 400}, manual_target_temp_c=42, temp_max_c=58)) \
        == ["temp_max_c"]
    assert pair(rig) == (42, 58)


def test_the_newest_request_wins_even_when_it_repeats_a_value(rig):
    rig.tm.apply_settings(cloud(100, {"manual_target_temp_c": 100, "temp_max_c": 100},
                                manual_target_temp_c=45, temp_max_c=60))
    assert rig.tm.apply_settings({"temp_max_c": 50, "updated_at": 200}) == ["temp_max_c"]  # local
    # the web asks for 60 again, later: that is a new request, not a stale one
    assert rig.tm.apply_settings(cloud(300, {"temp_max_c": 300}, temp_max_c=60)) == ["temp_max_c"]
    assert rig.tm.temp_max_c == 60


def test_a_fresh_database_takes_its_first_settings_from_the_cloud(rig):
    changed = rig.tm.apply_settings(cloud(100, {"temp_max_c": 100}, manual_target_temp_c=40, temp_max_c=50))
    assert set(changed) == {"manual_target_temp_c", "temp_max_c"}  # the target has no time of its own yet
    # now it has: an unmarked field is no longer a request
    assert rig.tm.apply_settings(cloud(200, {"temp_max_c": 200}, manual_target_temp_c=33, temp_max_c=50)) == []
    assert pair(rig) == (40, 50)


def test_a_refused_request_is_not_revived_by_a_later_change_or_a_restart(rig):
    refused = cloud(100, {"temp_max_c": 100}, temp_max_c=52)  # the saved target is 55
    assert rig.tm.apply_settings(refused) == []
    rig.tm.apply_settings({"manual_target_temp_c": 40, "updated_at": 150})
    assert rig.tm.apply_settings(refused) == []  # delivered again: 52 would fit now, but the request is spent
    assert pair(rig) == (40, 60)
    st = rig.reboot(downtime=5).tm.settings
    assert rig.tm.apply_settings(refused) == [] and st.get("temp_max_c") == 60


def test_a_task_runs_on_the_saved_target_and_max(rig):
    rig.tm.apply_settings({"manual_target_temp_c": 40, "temp_max_c": 46, "updated_at": 100})
    rig.start(warmup=True)
    t = rig.task()
    assert (t["target_c"], t["max_temp_c"]) == (40.0, 46.0)
    assert rig.tm.snapshot()["target_temp_c"] == 40.0
    # a new target waits for the next task
    rig.tm.apply_settings({"manual_target_temp_c": 35, "updated_at": 101})
    assert rig.task()["target_c"] == 40.0


def test_the_task_thermostat_holds_the_saved_target_and_stays_under_the_max(rig):
    rig.tm.apply_settings({"manual_target_temp_c": 40, "temp_max_c": 46, "updated_at": 100})
    rig.start(duration_min=30)
    peak = 0.0
    for _ in range(300):
        rig.run(1)
        peak = max(peak, rig.oven.temp)
    assert rig.task()["status"] == "running"
    assert 38 <= peak < 43  # held around 40, not the old fixed 55, and nowhere near the maximum


def test_the_task_over_temperature_fault_is_the_saved_max(rig):
    rig.tm.apply_settings({"manual_target_temp_c": 40, "temp_max_c": 46, "updated_at": 100})
    rig.start(duration_min=30)
    rig.run(3)
    rig.oven.fixed = 46.0  # well under the old fixed 60
    rig.run(3)
    t = rig.task()
    assert (t["status"], t["failure_reason"]) == ("failed", "over_temperature")
    assert t["diagnostics"]["temp_max_c"] == 46.0
    assert not rig.out.heater.is_active


def test_lowering_the_max_applies_to_a_running_task_at_once(rig):
    rig.start(duration_min=30)  # started with 55 / 60
    rig.oven.fixed = 50.0
    rig.run(3)
    assert rig.task()["status"] == "running"
    rig.tm.apply_settings({"manual_target_temp_c": 40, "temp_max_c": 48, "updated_at": 100})
    rig.run(3)
    assert (rig.task()["status"], rig.task()["failure_reason"]) == ("failed", "over_temperature")


def test_raising_the_max_never_loosens_a_running_task(rig):
    rig.tm.apply_settings({"manual_target_temp_c": 40, "temp_max_c": 50, "updated_at": 100})
    rig.start(duration_min=30)
    rig.tm.apply_settings({"temp_max_c": 60, "updated_at": 101})
    rig.oven.fixed = 50.0
    rig.run(3)
    assert (rig.task()["status"], rig.task()["failure_reason"]) == ("failed", "over_temperature")


def test_a_start_is_refused_within_5_c_of_the_saved_max(rig):
    rig.tm.apply_settings({"manual_target_temp_c": 40, "temp_max_c": 46, "updated_at": 100})
    rig.oven.fixed = 41.0  # the max minus the 5 C margin
    rig.run(3)
    assert rig.start(warmup=True)["reason"] == "too_hot"
    rig.oven.fixed = 40.9
    rig.run(3)
    assert rig.start(task_id="T2", warmup=True)["result"] == "accepted"


def test_restart_during_a_task_uses_the_saved_max(rig):
    rig.tm.apply_settings({"manual_target_temp_c": 40, "temp_max_c": 46, "updated_at": 100})
    rig.start(duration_min=30)
    rig.run(10)
    r = rig.reboot(downtime=10, temp=47.0)  # hotter than the saved max, cooler than the old 60
    r.oven.fixed = 47.0
    r.run(config.RECOVERY_OBSERVE_SECONDS + 5)
    t = r.task()
    assert (t["status"], t["failure_reason"]) == ("recovery_required", "over_temperature")
    assert t["diagnostics"]["current_temp_c"] == 47.0 and not r.out.heater.is_active
