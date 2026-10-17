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


def zone_state(zones):
    """The state/* keys for the middle and top sensors. `temp` and `sensor_ok` stay the bottom sensor's.

    A temperature is None (the key is removed) while its sensor is unhealthy, like `temp` itself. `humidity` is
    the top sensor's (the DHT22) and goes the same way.
    """
    temps = zones.temps()

    def one(zone):
        return round(temps[zone], 1) if temps.get(zone) is not None else None

    humidity = zones.humidity()  # the top sensor's, in %RH; None (key removed) while it is unhealthy

    return {
        "temp_middle": one("middle"),
        "temp_top": one("top"),
        "humidity": round(humidity, 1) if humidity is not None else None,
        "sensor_middle_ok": zones.healthy("middle"),
        "sensor_top_ok": zones.healthy("top"),
        "zones_configured": zones.configured(),  # False while the ids in config.py are placeholders
    }


def minute_entries(rows):
    """task_logs entries, {"00001234": entry}, from per-zone minute rows (zone defaults to bottom).

    The bottom sensor's summary stays flat, exactly as it was with one sensor, so older readers keep
    working; the middle and top ones are sub-objects. A minute is always built from every zone's row.
    """
    by_minute = {}
    for r in rows:
        by_minute.setdefault(r["minute"], {})[r.get("zone", "bottom")] = r
    out = {}
    for minute, zones in sorted(by_minute.items()):
        entry = {"t": minute * 60000}
        for zone, r in sorted(zones.items()):
            summary = {"avg": round(r["sum_c"] / r["n"], 2), "min": round(r["min_c"], 2),
                       "max": round(r["max_c"], 2), "raw": round(r["raw_last"], 2), "n": r["n"]}
            if zone == "bottom":
                entry.update(summary)
            else:
                entry[zone] = summary
        out[f"{minute:08d}"] = entry
    return out


def settings_payload(settings):
    values = {key: settings.get(key) for key in machine_settings.FIELDS}
    values["updated_at"] = settings.updated_at or None
    return values


def build_snapshot(*, state, manual, settings, request, report, ack, pi_now_ms, cloud_online,
                   loop_age_s, wifi=None, network_ack=None, link=None):
    """`state` is the same dict that is published to Firebase `state/*` (None values dropped).

    `link` is link_monitor.LinkMonitor.snapshot(): whether the internet and Firebase are really reachable.
    Only the local panel can show it, because the cloud never hears from a machine that cannot reach it."""
    report = dict(report) if report else None
    if report is not None:
        report["reported_at"] = pi_now_ms
    return {
        "pi_now_ms": pi_now_ms,
        "loop_age_s": round(loop_age_s, 2),
        "cloud_online": bool(cloud_online),
        "link": link,
        "wifi": wifi,
        "network": {"ack": network_ack},
        "state": {k: v for k, v in state.items() if v is not None} | {"last_seen": pi_now_ms},
        "commands": manual,
        "settings": settings_payload(settings),
        "task": {"request": request, "report": report, "ack": ack_payload(ack)},
    }
