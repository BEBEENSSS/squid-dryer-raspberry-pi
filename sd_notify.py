"""Minimal systemd notify client (stdlib only). Does nothing when not run by systemd."""

import os
import socket


def notify(message):
    path = os.environ.get("NOTIFY_SOCKET")
    if not path:
        return False
    if path.startswith("@"):  # abstract socket namespace
        path = "\0" + path[1:]
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
            sock.sendto(message.encode(), path)
        return True
    except OSError:
        return False


def ready():
    return notify("READY=1")


def watchdog():
    return notify("WATCHDOG=1")
