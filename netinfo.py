"""LAN address of this Pi, published so the online app can link to the local panel."""

import json
import socket
import time
from pathlib import Path


def lan_ip():
    """The address used for outbound traffic, or None. A UDP connect sends no packets."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))
            ip = s.getsockname()[0]
    except OSError:
        return None
    return None if ip.startswith("127.") or ip == "0.0.0.0" else ip


def read_network_status(path, max_age_s, wall=time.time):
    """What netwatch.py last wrote, or None when it is missing, unreadable or too old.

    A stale file means netwatch is not running, and showing its last word as current would be
    worse than showing nothing.
    """
    try:
        data = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("network") not in ("wifi", "hotspot", "connecting"):
        return None
    updated = data.get("updated_at")
    if not isinstance(updated, (int, float)) or abs(wall() - updated) > max_age_s:
        return None
    return {"network": data["network"], "clients": data.get("clients"), "address": data.get("address")}
