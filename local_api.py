"""Local web panel and API, served from the controller process with the standard library only.

It never touches GPIO. It does exactly two things: it puts requests on the same queue Firebase
feeds, so the control loop applies them through the same safety rules, and it serves the latest
snapshot the control loop published. This module must not import hardware, gpiozero or
task_manager (a test enforces it).

    python local_api.py --set-pin      choose the PIN that unlocks control from the browser
"""

import hashlib
import hmac
import json
import mimetypes
import os
import re
import secrets
import sys
import threading
import time
import uuid
from collections import defaultdict, deque
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

import config
import machine_settings
import temp_history
from network_control import parse_network_request
from snapshot import minute_entries

SESSION_COOKIE = "sd_session"
SESSION_SECONDS = 12 * 3600
MAX_BODY = 16 * 1024
LOGIN_FAILS = 5            # per IP ...
LOGIN_WINDOW_SECONDS = 60  # ... per minute
ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
TASK_COMMANDS = {"start", "add_batch", "complete_batch", "cancel", "resume", "abort", "acknowledge"}
# the same fields the database rules allow on task/request, plus issued_at which only Firebase needs
TASK_FIELDS = {"command", "task_id", "command_id", "duration_minutes", "batch_count", "row_levels",
               "batch_id", "weight_kg", "mode", "requested_by", "issued_at", "bypass_zones"}

mimetypes.add_type("text/javascript", ".js")
mimetypes.add_type("image/svg+xml", ".svg")


# ---------------- PIN ----------------
def hash_pin(pin, salt=None):
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.scrypt(pin.encode(), salt=salt, n=2 ** 14, r=8, p=1, dklen=32)
    return f"scrypt${salt.hex()}${digest.hex()}"


def verify_pin(pin, stored):
    try:
        scheme, salt, digest = stored.strip().split("$")
        if scheme != "scrypt":
            return False
        candidate = hash_pin(pin, bytes.fromhex(salt)).split("$")[2]
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(candidate, digest)


class Auth:
    """PIN check, in-memory sessions and a per-IP limit on wrong guesses."""

    def __init__(self, pin_path, mono=time.monotonic):
        self.pin_path = Path(pin_path)
        self.mono = mono
        self._sessions = {}
        self._fails = defaultdict(deque)
        self._lock = threading.Lock()

    def pin_set(self):
        return self.pin_path.is_file() and self.pin_path.stat().st_size > 0

    def set_pin(self, pin):
        self.pin_path.parent.mkdir(parents=True, exist_ok=True)
        self.pin_path.write_text(hash_pin(pin))
        try:
            os.chmod(self.pin_path, 0o600)
        except OSError:
            pass

    def _prune(self, ip, now):
        q = self._fails[ip]
        while q and now - q[0] > LOGIN_WINDOW_SECONDS:
            q.popleft()
        return q

    def login(self, ip, pin):
        """Returns (token, None) or (None, error)."""
        now = self.mono()
        with self._lock:
            if len(self._prune(ip, now)) >= LOGIN_FAILS:
                return None, "too_many_attempts"
        if not self.pin_set():
            return None, "pin_not_set"
        if not isinstance(pin, str) or not verify_pin(pin, self.pin_path.read_text()):
            with self._lock:
                self._fails[ip].append(now)
            return None, "wrong_pin"
        token = secrets.token_urlsafe(32)
        with self._lock:
            self._sessions[token] = now + SESSION_SECONDS
        return token, None

    def valid(self, token):
        if not token:
            return False
        with self._lock:
            expires = self._sessions.get(token)
            if expires is None:
                return False
            if expires < self.mono():
                del self._sessions[token]
                return False
            return True

    def logout(self, token):
        with self._lock:
            self._sessions.pop(token, None)


# ---------------- validation (pure) ----------------
def _is_number(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and v == v and abs(v) != float("inf")


def parse_manual(body):
    """Returns ((fan, heater, command_id), None) or (None, error)."""
    if not isinstance(body, dict) or not isinstance(body.get("fan"), bool) or not isinstance(body.get("heater"), bool):
        return None, "fan_and_heater_must_be_booleans"
    if body["heater"] and not body["fan"]:
        return None, "heater_needs_fan"  # the machine enforces it again, this just answers faster
    command_id = body.get("command_id")
    if command_id is None:
        command_id = str(uuid.uuid4())
    elif not isinstance(command_id, str) or not 1 <= len(command_id) <= 128:
        return None, "invalid_command_id"
    return (body["fan"], body["heater"], command_id), None


def parse_settings(body):
    if not isinstance(body, dict) or not body:
        return None, "no_settings"
    out = {}
    for key, value in body.items():
        if key not in machine_settings.FIELDS:
            return None, f"unknown_setting:{key}"
        _, _, lo, hi = machine_settings.FIELDS[key]
        if not _is_number(value) or not getattr(config, lo) <= value <= getattr(config, hi):
            return None, f"out_of_range:{key}"
        out[key] = float(value)
    return out, None


def parse_task_request(body):
    if not isinstance(body, dict) or body.get("command") not in TASK_COMMANDS:
        return None, "unknown_command"
    extra = set(body) - TASK_FIELDS
    if extra:
        return None, f"unknown_field:{sorted(extra)[0]}"
    for key in ("task_id", "batch_id"):
        if key in body and not (isinstance(body[key], str) and ID_RE.match(body[key])):
            return None, f"invalid_{key}"
    payload = {k: v for k, v in body.items() if k != "issued_at"}
    payload.setdefault("command_id", str(uuid.uuid4()))
    return payload, None


# ---------------- server ----------------
class LocalApi:
    def __init__(self, *, get_snapshot, commands, store, pin_path=None, web_root=None, host=None,
                 port=None, mono=time.monotonic, local_mark=None):
        self.local_mark = local_mark       # touched on every accepted command, so older cloud ones lose
        self.get_snapshot = get_snapshot   # callable returning the latest snapshot dict or None
        self.commands = commands           # the queue the control loop reads
        self.store = store
        self.auth = Auth(pin_path or config.PIN_PATH, mono)
        self.web_root = Path(web_root or config.WEB_ROOT)
        self.host = host if host is not None else config.LOCAL_API_HOST
        self.port = config.LOCAL_API_PORT if port is None else port
        self._server = None

    def _mark_local(self):
        if self.local_mark is not None:
            self.local_mark.touch()

    def start(self):
        """Returns True when listening. A port that cannot be bound never stops the controller."""
        try:
            self._server = ThreadingHTTPServer((self.host, self.port), self._handler())
        except OSError as e:
            print(f"local API could not listen on {self.host}:{self.port}: {e}")
            return False
        self._server.daemon_threads = True
        self.port = self._server.server_address[1]
        threading.Thread(target=self._server.serve_forever, daemon=True, name="local-api").start()
        return True

    def stop(self):
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None

    # ---- handler ----
    def _handler(self):
        api = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "SquidDry"
            protocol_version = "HTTP/1.1"

            def log_message(self, *_):
                pass

            # -- helpers --
            def _send(self, status, body=b"", ctype="application/json", headers=None):
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("X-Content-Type-Options", "nosniff")
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(body)

            def _json(self, status, data, headers=None):
                h = {"Cache-Control": "no-store"}
                h.update(headers or {})
                self._send(status, json.dumps(data, separators=(",", ":")).encode(), headers=h)

            def _token(self):
                jar = cookies.SimpleCookie(self.headers.get("Cookie", ""))
                return jar[SESSION_COOKIE].value if SESSION_COOKIE in jar else None

            def _authed(self):
                return api.auth.valid(self._token())

            def _body(self):
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                except ValueError:
                    return None
                ctype = self.headers.get("Content-Type", "").split(";")[0].strip()
                if ctype != "application/json" or not 0 < length <= MAX_BODY:
                    return None
                try:
                    return json.loads(self.rfile.read(length))
                except (ValueError, UnicodeDecodeError):
                    return None

            # -- routes --
            def do_HEAD(self):
                self.do_GET()

            def do_GET(self):
                path = unquote(urlsplit(self.path).path)
                if path == "/api/snapshot":
                    snap = api.get_snapshot()
                    return self._json(200, snap) if snap else self._json(503, {"error": "starting"})
                if path == "/api/session":
                    return self._json(200, {"authenticated": self._authed(), "pin_set": api.auth.pin_set()})
                if path == "/api/temp-history":
                    # the same shape as Firebase temp_history, so the web app parses both alike
                    try:
                        since = int(parse_qs(urlsplit(self.path).query).get("since", ["0"])[0])
                    except ValueError:
                        return self._json(400, {"error": "invalid_since"})
                    rows = api.store.temp_history_since(max(0, since))
                    return self._json(200, {r["rid"]: temp_history.payload(r) for r in rows} or None)
                if path.startswith("/api/task-logs/"):
                    task_id = path[len("/api/task-logs/"):]
                    if not ID_RE.match(task_id):
                        return self._json(400, {"error": "invalid_task_id"})
                    return self._json(200, minute_entries(api.store.temp_minutes(task_id)) or None)
                if path.startswith("/api/"):
                    return self._json(404, {"error": "not_found"})
                self._static(path)

            def do_POST(self):
                path = urlsplit(self.path).path
                if path == "/api/login":
                    body = self._body()
                    ip = self.client_address[0]
                    token, err = api.auth.login(ip, body.get("pin") if isinstance(body, dict) else None)
                    if err:
                        status = {"too_many_attempts": 429, "pin_not_set": 403}.get(err, 401)
                        return self._json(status, {"error": err})
                    cookie = (f"{SESSION_COOKIE}={token}; HttpOnly; SameSite=Strict; Path=/; "
                              f"Max-Age={SESSION_SECONDS}")
                    return self._json(200, {"authenticated": True}, {"Set-Cookie": cookie})
                if path == "/api/logout":
                    api.auth.logout(self._token())
                    return self._json(200, {"authenticated": False},
                                      {"Set-Cookie": f"{SESSION_COOKIE}=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0"})
                if path in ("/api/commands", "/api/settings", "/api/task/request", "/api/network/request"):
                    if not self._authed():
                        return self._json(401, {"error": "login_required"})
                    body = self._body()
                    if body is None:
                        return self._json(400, {"error": "invalid_json_body"})
                    if path == "/api/commands":
                        parsed, err = parse_manual(body)
                        if err:
                            return self._json(400, {"error": err})
                        api._mark_local()
                        api.commands.put(("manual", parsed + ("local",)))
                        return self._json(202, {"command_id": parsed[2]})
                    if path == "/api/settings":
                        parsed, err = parse_settings(body)
                        if err:
                            return self._json(400, {"error": err})
                        api._mark_local()
                        api.commands.put(("settings_local", parsed))
                        return self._json(202, {"ok": True})
                    if path == "/api/network/request":
                        # not a machine command, so it does not outrank older cloud commands
                        parsed, err = parse_network_request(body)
                        if err:
                            return self._json(400, {"error": err})
                        api.commands.put(("network_local", parsed))
                        return self._json(202, {"command_id": parsed["command_id"]})
                    parsed, err = parse_task_request(body)
                    if err:
                        return self._json(400, {"error": err})
                    api._mark_local()
                    api.commands.put(("task_local", parsed))
                    return self._json(202, {"command_id": parsed["command_id"]})
                self._json(404, {"error": "not_found"})

            # -- static files --
            def _static(self, path):
                root = api.web_root.resolve()
                index = root / "index.html"
                if not index.is_file():
                    return self._send(503, b"Web panel not installed on this machine yet.", "text/plain")
                target = (root / path.lstrip("/")).resolve()
                if target.is_file() and (target == root or root in target.parents):
                    served = target
                else:
                    served = index  # BrowserRouter: any unknown page is the app shell
                ctype = mimetypes.guess_type(served.name)[0] or "application/octet-stream"
                cache = "public, max-age=31536000, immutable" if "/assets/" in path and served != index else "no-cache"
                self._send(200, served.read_bytes(), ctype, {"Cache-Control": cache})

        return Handler


def set_pin_interactively():
    import getpass
    pin = getpass.getpass("New PIN (at least 4 characters): ")
    if len(pin) < 4 or pin != getpass.getpass("Repeat it: "):
        sys.exit("PINs differ or are too short. Nothing changed.")
    Auth(config.PIN_PATH).set_pin(pin)
    print(f"PIN saved to {config.PIN_PATH}")


if __name__ == "__main__":
    if "--set-pin" in sys.argv:
        set_pin_interactively()
    else:
        print(__doc__)
