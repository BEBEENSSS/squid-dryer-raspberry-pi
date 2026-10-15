"""Operator requests to switch between the home Wi-Fi and the machine's own hotspot.

The controller only validates the request, answers it (`network/ack`) and hands it to netwatch
through a file. netwatch owns the radio. Nothing here touches the relays or the drying task, so a
switch never interrupts a run. Pure apart from the injected read/write callables.
"""

import time
import uuid

import config
from netinfo import read_network_status, write_network_request

MODES = ("hotspot", "wifi")
# why a cloud request that failed its age check is refused, in the words the web app maps to text
VERDICT_REASONS = {"too_old": "stale_command", "superseded_by_local": "superseded_by_local",
                   "unverifiable": "unverifiable"}


def parse_network_request(body):
    """(payload, None) or (None, error). Shared by the local API; Firebase requests go through `handle`."""
    if not isinstance(body, dict) or body.get("mode") not in MODES:
        return None, "invalid_mode"
    command_id = body.get("command_id")
    if command_id is None:
        command_id = str(uuid.uuid4())
    elif not isinstance(command_id, str) or not 1 <= len(command_id) <= 128:
        return None, "invalid_command_id"
    return {"command_id": command_id, "mode": body["mode"]}, None


def network_ack_payload(ack):
    """network/ack as the web app reads it (None values dropped), or None before any request."""
    if not ack:
        return None
    return {k: v for k, v in ack.items() if v is not None}


class NetworkControl:
    def __init__(self, *, now_ms, cfg=config, wall=time.time, read_status=None, write_request=None):
        self.now_ms = now_ms
        self.cfg = cfg
        self.wall = wall
        self.read_status = read_status or (
            lambda: read_network_status(cfg.NET_STATUS_PATH, cfg.NET_STATUS_MAX_AGE_SECONDS))
        self.write_request = write_request or write_network_request
        self.last_ack = None   # newest answer, published as network/ack and in the local snapshot
        self._seen = None      # newest command_id, so a request still sitting in Firebase is answered once

    def _answer(self, command_id, mode, result, reason=None):
        self.last_ack = {"command_id": command_id, "mode": mode, "result": result, "reason": reason,
                         "at": int(self.now_ms())}
        return self.last_ack

    def _already(self, mode, status):
        if mode == "wifi":
            return status["network"] == "wifi"
        # a fallback hotspot may still be converted into a requested one, which has its own timers
        return status["network"] == "hotspot" and status.get("requested") is True

    def handle(self, request, verdict=None):
        """Answer one request and pass it on. `verdict` is why a cloud request is too old, or None.

        Returns the ack, or None when there is nothing to answer (no usable id, or already answered).
        """
        command_id = request.get("command_id") if isinstance(request, dict) else None
        if not isinstance(command_id, str) or not 1 <= len(command_id) <= 128 or command_id == self._seen:
            return None
        self._seen = command_id
        mode = request.get("mode")
        if mode not in MODES:
            return self._answer(command_id, None, "rejected", "invalid")
        if verdict is not None:
            # a request that cannot be proved recent must not take the machine off its network
            return self._answer(command_id, mode, "rejected", VERDICT_REASONS.get(verdict, "stale_command"))
        status = self.read_status()
        if status is None:
            return self._answer(command_id, mode, "rejected", "netwatch_unavailable")
        if self._already(mode, status):
            return self._answer(command_id, mode, "rejected", "already_in_mode")
        wall = self.wall()
        # the delay lets this ack reach Firebase (or the local panel) before the radio changes
        delivered = self.write_request(self.cfg.NET_REQUEST_PATH, {
            "command_id": command_id, "mode": mode, "requested_at": wall,
            "not_before": wall + self.cfg.NET_SWITCH_DELAY_SECONDS})
        if not delivered:
            return self._answer(command_id, mode, "rejected", "write_failed")
        return self._answer(command_id, mode, "accepted")
