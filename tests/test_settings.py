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
