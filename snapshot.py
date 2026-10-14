"""The picture of the machine served to the local panel. Pure: it only assembles what it is given.

It mirrors the Firebase layout (`state`, `commands`, `settings`, `task/request|report|ack`) on
purpose, so the web app feeds the same parsers whichever way it reaches the machine.
"""

import machine_settings


def ack_payload(ack):
    """task/ack as the web app reads it, from a `commands_seen` row (None when nothing was processed)."""
    if not ack:
        return None
    return {
        "command_id": ack["command_id"], "command": ack["action"], "task_id": ack["task_id"],
        "result": ack["result"], "reason": ack["reason"], "at": int(ack["processed_wall"] * 1000),
    }


def settings_payload(settings):
    values = {key: settings.get(key) for key in machine_settings.FIELDS}
    values["updated_at"] = settings.updated_at or None
    return values


def build_snapshot(*, state, manual, settings, request, report, ack, pi_now_ms, cloud_online,
                   loop_age_s, wifi=None):
    """`state` is the same dict that is published to Firebase `state/*` (None values dropped)."""
    report = dict(report) if report else None
    if report is not None:
        report["reported_at"] = pi_now_ms
    return {
        "pi_now_ms": pi_now_ms,
        "loop_age_s": round(loop_age_s, 2),
        "cloud_online": bool(cloud_online),
        "wifi": wifi,
        "state": {k: v for k, v in state.items() if v is not None} | {"last_seen": pi_now_ms},
        "commands": manual,
        "settings": settings_payload(settings),
        "task": {"request": request, "report": report, "ack": ack_payload(ack)},
    }
