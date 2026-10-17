import http.client
import json
import queue
import subprocess
import sys
from pathlib import Path

import pytest

import local_api
from local_api import LocalApi, parse_manual, parse_settings, parse_task_request


class FakeStore:
    def temp_minutes(self, task_id):
        if task_id != "T1":
            return []
        return [{"minute": 10, "n": 2, "sum_c": 100.0, "min_c": 49.0, "max_c": 51.0, "raw_last": 50.5}]

    ROWS = [
        {"rid": "1800000000000", "t_ms": 1_800_000_000_000, "bottom": 30.1, "middle": 29.0, "top": 28.0},
        {"rid": "1800000030000", "t_ms": 1_800_000_030_000, "bottom": 30.4, "middle": None, "top": 28.2},
    ]

    def temp_history_since(self, since_ms, carry=False):
        rows = [r for r in self.ROWS if r["t_ms"] >= since_ms]
        before = [r for r in self.ROWS if r["t_ms"] < since_ms]
        return ([before[-1]] if carry and before else []) + rows


@pytest.fixture
def web(tmp_path):
    root = tmp_path / "web"
    (root / "assets").mkdir(parents=True)
    (root / "index.html").write_text("<html>app</html>")
    (root / "assets" / "app.js").write_text("console.log(1)")
    (tmp_path / "secret.txt").write_text("do not serve")
    return root


@pytest.fixture
def api(tmp_path, web):
    q = queue.Queue()
    snap = {"state": {"fan": False}, "pi_now_ms": 1}
    server = LocalApi(get_snapshot=lambda: snap, commands=q, store=FakeStore(), pin_path=tmp_path / "pin",
                      web_root=web, host="127.0.0.1", port=0)
    server.auth.set_pin("2468")
    assert server.start()
    server.queue = q
    yield server
    server.stop()


def call(api, method, path, body=None, cookie=None, ctype="application/json"):
    conn = http.client.HTTPConnection("127.0.0.1", api.port, timeout=5)
    headers = {}
    data = None
    if body is not None:
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        headers["Content-Type"] = ctype
    if cookie:
        headers["Cookie"] = cookie
    conn.request(method, path, body=data, headers=headers)
    resp = conn.getresponse()
    raw = resp.read()
    conn.close()
    return resp, raw


def login(api, pin="2468"):
    resp, _ = call(api, "POST", "/api/login", {"pin": pin})
    set_cookie = resp.getheader("Set-Cookie")
    return resp, (set_cookie.split(";")[0] if set_cookie else None)


def test_snapshot_is_open_and_not_cached(api):
    resp, raw = call(api, "GET", "/api/snapshot")
    assert resp.status == 200 and json.loads(raw)["state"]["fan"] is False
    assert resp.getheader("Cache-Control") == "no-store"


def test_writes_need_a_session(api):
    for path, body in (("/api/commands", {"fan": True, "heater": False}),
                       ("/api/settings", {"safe_temp_c": 40}),
                       ("/api/task/request", {"command": "cancel"})):
        assert call(api, "POST", path, body)[0].status == 401
    assert api.queue.empty()


def test_login_sets_an_httponly_strict_cookie_and_unlocks_writes(api):
    resp, cookie = login(api)
    assert resp.status == 200
    flags = resp.getheader("Set-Cookie")
    assert "HttpOnly" in flags and "SameSite=Strict" in flags
    resp, raw = call(api, "POST", "/api/commands", {"fan": True, "heater": False, "command_id": "c1"}, cookie)
    assert resp.status == 202 and json.loads(raw)["command_id"] == "c1"
    assert api.queue.get_nowait() == ("manual", (True, False, "c1", "local", False))
    assert json.loads(call(api, "GET", "/api/session", cookie=cookie)[1])["authenticated"] is True


def test_wrong_pin_is_refused_then_rate_limited(api):
    for _ in range(local_api.LOGIN_FAILS):
        assert login(api, "0000")[0].status == 401
    resp, _ = login(api, "2468")  # even the right PIN waits out the window
    assert resp.status == 429


def test_logout_ends_the_session(api):
    _, cookie = login(api)
    call(api, "POST", "/api/logout", {}, cookie)
    assert call(api, "POST", "/api/commands", {"fan": True, "heater": False}, cookie)[0].status == 401


def test_no_pin_set_means_control_is_refused(tmp_path, web):
    server = LocalApi(get_snapshot=lambda: {}, commands=queue.Queue(), store=FakeStore(),
                      pin_path=tmp_path / "none", web_root=web, host="127.0.0.1", port=0)
    assert server.start()
    try:
        assert login(server)[0].status == 403
    finally:
        server.stop()


def test_heater_without_fan_is_refused_before_it_reaches_the_queue(api):
    _, cookie = login(api)
    resp, raw = call(api, "POST", "/api/commands", {"fan": False, "heater": True}, cookie)
    assert resp.status == 400 and json.loads(raw)["error"] == "heater_needs_fan"
    assert api.queue.empty()


def test_non_json_and_oversized_bodies_are_rejected(api):
    _, cookie = login(api)
    assert call(api, "POST", "/api/commands", b"fan=1", cookie, ctype="text/plain")[0].status == 400
    assert call(api, "POST", "/api/commands", b"x" * 20000, cookie)[0].status == 400


def test_settings_are_range_checked_and_queued_for_the_control_loop(api):
    _, cookie = login(api)
    assert call(api, "POST", "/api/settings", {"heater_cutoff_c": 99}, cookie)[0].status == 400
    assert call(api, "POST", "/api/settings", {"bogus": 1}, cookie)[0].status == 400
    assert call(api, "POST", "/api/settings", {"heater_cutoff_c": 50}, cookie)[0].status == 202
    assert api.queue.get_nowait() == ("settings_local", {"heater_cutoff_c": 50.0})


def test_the_temperature_order_is_checked_for_the_values_sent_together(api):
    _, cookie = login(api)
    # in range each, but only 3 C apart: the safe shutdown temperature must be at least 5 C above the target
    resp, raw = call(api, "POST", "/api/settings", {"manual_target_temp_c": 50, "temp_max_c": 53}, cookie)
    assert resp.status == 400 and json.loads(raw)["error"] == "temperature_order"
    # the pause has to sit above the target and below the safe shutdown temperature
    for body in ({"manual_target_temp_c": 50, "heater_cutoff_c": 50},
                 {"manual_target_temp_c": 48, "heater_cutoff_c": 47},
                 {"heater_cutoff_c": 55, "temp_max_c": 55}):
        resp, raw = call(api, "POST", "/api/settings", body, cookie)
        assert resp.status == 400 and json.loads(raw)["error"] == "temperature_order", body
    assert call(api, "POST", "/api/settings", {"temp_max_c": 99}, cookie)[0].status == 400
    assert api.queue.empty()
    # a valid set is queued whole
    body = {"manual_target_temp_c": 47, "heater_cutoff_c": 50, "temp_max_c": 52}
    assert call(api, "POST", "/api/settings", body, cookie)[0].status == 202
    assert api.queue.get_nowait() == ("settings_local", {k: float(v) for k, v in body.items()})
    # one alone is queued as given: the machine checks it against the others it has saved
    assert call(api, "POST", "/api/settings", {"temp_max_c": 52}, cookie)[0].status == 202
    assert api.queue.get_nowait() == ("settings_local", {"temp_max_c": 52.0})


def test_task_request_is_whitelisted_and_gets_a_command_id(api):
    _, cookie = login(api)
    assert call(api, "POST", "/api/task/request", {"command": "format_disk"}, cookie)[0].status == 400
    assert call(api, "POST", "/api/task/request", {"command": "cancel", "evil": 1}, cookie)[0].status == 400
    resp, raw = call(api, "POST", "/api/task/request",
                     {"command": "start", "task_id": "t-1", "duration_minutes": 60, "issued_at": 5}, cookie)
    assert resp.status == 202
    kind, payload = api.queue.get_nowait()
    assert kind == "task_local" and payload["task_id"] == "t-1" and payload["command_id"]
    assert "issued_at" not in payload  # a Firebase-only field


def test_task_logs_use_the_firebase_shape(api):
    resp, raw = call(api, "GET", "/api/task-logs/T1")
    assert json.loads(raw) == {"00000010": {"t": 600000, "avg": 50.0, "min": 49.0, "max": 51.0, "raw": 50.5, "n": 2}}
    assert call(api, "GET", "/api/task-logs/..%2Fetc")[0].status == 400


def test_temp_history_uses_the_firebase_shape_and_leaves_out_a_dead_sensor(api):
    resp, raw = call(api, "GET", "/api/temp-history")
    assert resp.status == 200 and resp.getheader("Cache-Control") == "no-store"
    assert json.loads(raw) == {
        "1800000000000": {"t": 1_800_000_000_000, "bottom": 30.1, "middle": 29.0, "top": 28.0},
        "1800000030000": {"t": 1_800_000_030_000, "bottom": 30.4, "top": 28.2},  # no "middle": null
    }


def test_temp_history_since_returns_only_newer_records_and_null_when_there_are_none(api):
    _, raw = call(api, "GET", "/api/temp-history?since=1800000030000")
    assert list(json.loads(raw)) == ["1800000030000"]
    _, raw = call(api, "GET", "/api/temp-history?since=1900000000000")
    assert json.loads(raw) is None
    assert call(api, "GET", "/api/temp-history?since=abc")[0].status == 400


def test_temp_history_carry_adds_the_newest_record_from_before_the_window(api):
    # records are written only on change, so a steady chamber has none inside the window to hold the line from
    _, raw = call(api, "GET", "/api/temp-history?since=1800000030000&carry=1")
    assert list(json.loads(raw)) == ["1800000000000", "1800000030000"]
    _, raw = call(api, "GET", "/api/temp-history?since=1900000000000&carry=1")
    assert list(json.loads(raw)) == ["1800000030000"]  # nothing newer, so the last record alone
    _, raw = call(api, "GET", "/api/temp-history?since=1700000000000&carry=1")
    assert list(json.loads(raw)) == ["1800000000000", "1800000030000"]  # nothing before the window to carry


def test_static_files_spa_fallback_and_cache_headers(api):
    resp, raw = call(api, "GET", "/")
    assert resp.status == 200 and b"app" in raw and resp.getheader("Cache-Control") == "no-cache"
    resp, raw = call(api, "GET", "/drying")  # a client-side route
    assert resp.status == 200 and raw == b"<html>app</html>"
    resp, raw = call(api, "GET", "/assets/app.js")
    assert resp.getheader("Content-Type").startswith("text/javascript")
    assert "immutable" in resp.getheader("Cache-Control")


def test_path_traversal_never_leaves_the_web_root(api):
    for path in ("/../secret.txt", "/%2e%2e/secret.txt", "/..%2fsecret.txt", "/assets/../../secret.txt"):
        resp, raw = call(api, "GET", path)
        assert b"do not serve" not in raw


def test_unknown_api_path_is_a_json_404_not_the_app_shell(api):
    resp, raw = call(api, "GET", "/api/nope")
    assert resp.status == 404 and json.loads(raw)["error"] == "not_found"


def test_missing_web_build_says_so(tmp_path):
    server = LocalApi(get_snapshot=lambda: {}, commands=queue.Queue(), store=FakeStore(),
                      pin_path=tmp_path / "pin", web_root=tmp_path / "empty", host="127.0.0.1", port=0)
    assert server.start()
    try:
        assert call(server, "GET", "/")[0].status == 503
    finally:
        server.stop()


@pytest.mark.skipif(sys.platform == "win32", reason="Windows lets two servers share a port (SO_REUSEADDR)")
def test_port_in_use_does_not_raise(api, tmp_path, web):
    second = LocalApi(get_snapshot=lambda: {}, commands=queue.Queue(), store=FakeStore(),
                      pin_path=tmp_path / "pin", web_root=web, host="127.0.0.1", port=api.port)
    assert second.start() is False


def test_parsers():
    assert parse_manual({"fan": True, "heater": True})[0][:2] == (True, True)
    assert parse_manual({"fan": 1, "heater": False})[1] == "fan_and_heater_must_be_booleans"
    # a preheat is the fan and the heater together, so it cannot be asked for without both
    assert parse_manual({"fan": True, "heater": True, "preheat": True, "command_id": "p1"})[0] == (True, True, "p1", True)
    assert parse_manual({"fan": True, "heater": False, "preheat": True})[1] == "preheat_needs_fan_and_heater"
    assert parse_manual({"fan": True, "heater": True, "preheat": "yes"})[1] == "preheat_must_be_boolean"
    assert parse_settings({"safe_temp_c": True})[1] == "out_of_range:safe_temp_c"
    assert parse_task_request({"command": "start", "task_id": "bad id!"})[1] == "invalid_task_id"


def test_the_api_never_imports_hardware_or_the_task_manager():
    code = ("import sys; import local_api; "
            "bad = [m for m in ('hardware', 'gpiozero', 'task_manager') if m in sys.modules]; "
            "sys.exit(1 if bad else 0)")
    root = Path(local_api.__file__).parent
    assert subprocess.run([sys.executable, "-c", code], cwd=root).returncode == 0
