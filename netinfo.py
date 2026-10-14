"""LAN address of this Pi, published so the online app can link to the local panel."""

import socket


def lan_ip():
    """The address used for outbound traffic, or None. A UDP connect sends no packets."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))
            ip = s.getsockname()[0]
    except OSError:
        return None
    return None if ip.startswith("127.") or ip == "0.0.0.0" else ip
