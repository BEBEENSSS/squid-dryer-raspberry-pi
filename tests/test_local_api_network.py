import json

from .test_local_api import api, call, login, web  # noqa: F401  (fixtures)


def test_network_request_needs_a_session(api):
    assert call(api, "POST", "/api/network/request", {"mode": "hotspot"})[0].status == 401
    assert api.queue.empty()


def test_valid_request_is_queued_for_the_control_loop_and_not_applied_here(api):
    _, cookie = login(api)
    resp, raw = call(api, "POST", "/api/network/request", {"mode": "wifi", "command_id": "n1"}, cookie)
    assert resp.status == 202 and json.loads(raw)["command_id"] == "n1"
    assert api.queue.get_nowait() == ("network_local", {"command_id": "n1", "mode": "wifi"})


def test_unknown_mode_is_refused_before_it_reaches_the_queue(api):
    _, cookie = login(api)
    resp, raw = call(api, "POST", "/api/network/request", {"mode": "reboot"}, cookie)
    assert resp.status == 400 and json.loads(raw)["error"] == "invalid_mode"
    assert api.queue.empty()
