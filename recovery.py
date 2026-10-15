"""Decide what to do with a task that was active when the controller last stopped.

Pure logic, no I/O. A temperature drop alone cannot prove a power cut, so the
decision combines it with reboot detection (boot_id), the clean-shutdown flag
and the measured interruption gap.
"""

from dataclasses import dataclass, field
from statistics import median

import config

RESUME = "resume"
COMPLETE = "complete"
FAILED = "failed"
RECOVERY_REQUIRED = "recovery_required"


@dataclass
class Decision:
    action: str
    reason: str
    diagnostics: dict = field(default_factory=dict)


def interruption_gap(task, boot_id, mono_now, wall_now, clock_synced_now):
    """Seconds since the last checkpoint, or None if it cannot be measured reliably."""
    if task.get("checkpoint_boot_id") == boot_id and task.get("checkpoint_mono") is not None:
        gap = mono_now - task["checkpoint_mono"]  # same boot: monotonic clock is exact
    elif clock_synced_now and task.get("checkpoint_clock_synced") and task.get("checkpoint_wall"):
        gap = wall_now - task["checkpoint_wall"]
    else:
        return None
    return gap if gap >= 0 else None


def decide_recovery(task, readings, sensor_ok, gap_s, boot_changed, clean_shutdown, cfg=config, max_temp_c=None):
    # the task's over-temperature limit; the firmware ceiling when the caller does not know a lower one
    limit = cfg.TEMP_MAX_C if max_temp_c is None else max_temp_c
    ref = task.get("last_temp_c")
    current = median(readings) if sensor_ok and len(readings) >= cfg.RECOVERY_MIN_READS else None
    drop = round(ref - current, 2) if ref is not None and current is not None else None
    diag = {
        "reference_temp_c": ref,
        "current_temp_c": round(current, 2) if current is not None else None,
        "temp_drop_c": drop,
        "readings": len(readings),
        "gap_s": round(gap_s, 1) if gap_s is not None else None,
        "boot_changed": boot_changed,
        "clean_shutdown": clean_shutdown,
        "elapsed_s": round(task.get("elapsed_s") or 0, 1),
        "duration_s": task.get("duration_s"),
        "drop_tolerance_c": cfg.TEMP_DROP_TOLERANCE_C,
        "drop_fail_c": cfg.TEMP_DROP_FAIL_C,
    }

    # heating time was already proven before the interruption
    if (task.get("elapsed_s") or 0) >= task["duration_s"]:
        return Decision(COMPLETE, "duration_reached_before_interruption", diag)

    if current is None:
        return Decision(RECOVERY_REQUIRED, "sensor_unavailable", diag)
    if current >= limit:
        return Decision(RECOVERY_REQUIRED, "over_temperature", diag)
    if ref is None or not (-55.0 <= ref <= 125.0):  # DS18B20 range
        return Decision(RECOVERY_REQUIRED, "no_reference_temp", diag)

    if drop >= cfg.TEMP_DROP_FAIL_C:
        reason = "possible_power_loss" if boot_changed and not clean_shutdown else "interrupted_heat_lost"
        return Decision(FAILED, reason, diag)

    if gap_s is None:
        return Decision(RECOVERY_REQUIRED, "elapsed_time_unverifiable", diag)
    if gap_s > cfg.RESUME_MAX_GAP_SECONDS:
        return Decision(RECOVERY_REQUIRED, "interruption_too_long", diag)
    if drop > cfg.TEMP_DROP_TOLERANCE_C:
        return Decision(RECOVERY_REQUIRED, "temperature_drop_uncertain", diag)

    return Decision(RESUME, "checks_passed", diag)
