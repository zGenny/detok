from __future__ import annotations

import base64
import hmac
import ipaddress
import json
import os
import random
import subprocess
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from urllib.parse import urlsplit

from . import __version__, store, timer

MAX_BODY = 2_000_000


class Jobs:
    def __init__(self):
        self._lock = threading.Lock()
        self._process = None
        self._kind = None

    def running(self) -> str | None:
        if self._process is not None and self._process.poll() is None:
            return self._kind
        return None

    def ensure_idle(self) -> None:
        if self.running():
            raise store.Busy(f"Another operation is already running ({self._kind}).")

    def start(self, kind: str, *args: str) -> None:
        with self._lock:
            self.ensure_idle()
            store.ensure_dirs()
            extra = {"creationflags": store.CREATE_NO_WINDOW} if store.WINDOWS else {}
            with store.LOG.open("a", encoding="utf-8") as log_file:
                self._process = subprocess.Popen(
                    store.self_command(kind, *args),
                    stdout=subprocess.DEVNULL,
                    stderr=log_file,
                    env={**os.environ, "DETOK_HOME": str(store.DATA)},
                    **extra,
                )
            self._kind = kind


jobs = Jobs()


def run_scheduler() -> None:
    time.sleep(180)
    while True:
        if store.AUTO_FLAG.exists():
            try:
                jobs.start("send")
            except store.Busy:
                pass
        wait = (timer.next_hour() - store.now()).total_seconds() + random.randint(0, 120)
        time.sleep(max(wait, 60))


def image_type(data: bytes) -> str:
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[4:12] in (b"ftypavif", b"ftypheic"):
        return "image/avif"
    return "application/octet-stream"


def friend_status(friend: dict, state: dict) -> dict:
    username = friend["username"]
    record = state["users"].get(username, {})
    done = store.sent_today(state, username)
    return {
        **friend,
        "profile": state.get("profiles", {}).get(username),
        "last_message": record.get("message"),
        "last_sent": record.get("last_sent"),
        "sent_today": done,
        "streak": store.streak(record),
        "verified": record.get("verified"),
        "last_error": None if done else record.get("last_error"),
        "has_shot": (store.SHOTS / f"{username}.png").exists(),
    }


def snapshot() -> dict:
    cfg, state = store.load_config(), store.load_state()
    expiry = store.session_expiry()
    return {
        "config": cfg,
        "friends": [friend_status(f, state) for f in cfg["friends"]],
        "session_expiry": expiry.isoformat(sep=" ") if expiry else None,
        "timer": timer.status(),
        "job": jobs.running(),
        "log": store.log_tail(80),
        "can_login": store.has_display(),
        "version": __version__,
    }


def save_config(body: dict) -> None:
    store.save_config(body)
    store.log("Settings saved.")


def start_login(body: dict) -> None:
    if not store.has_display():
        raise ValueError("There's no screen here: import a session exported from your PC.")
    jobs.start("login")


def import_session(body: dict) -> None:
    if not isinstance(body.get("cookies"), list):
        raise ValueError("Invalid session file (use 'detok session export' on your PC).")
    jobs.ensure_idle()
    store.ensure_dirs()
    upload = store.DATA / "session-import.json"
    upload.write_text(json.dumps(body), encoding="utf-8")
    if not store.WINDOWS:
        upload.chmod(0o600)
    jobs.start("session", "import", str(upload), "--delete")


def check_accounts(body: dict) -> None:
    usernames = [u for u in body.get("usernames", []) if isinstance(u, str) and store.USERNAME_RE.fullmatch(u)]
    jobs.start("check", *usernames)


def send_now(body: dict) -> None:
    jobs.start("send", "--force")


def toggle_timer(body: dict) -> None:
    if body.get("on"):
        timer.enable()
        store.log("Automatic sending turned on.")
    else:
        timer.disable()
        store.log("Automatic sending turned off.")


ACTIONS = {
    "/api/config": save_config,
    "/api/login": start_login,
    "/api/session": import_session,
    "/api/check": check_accounts,
    "/api/send": send_now,
    "/api/timer": toggle_timer,
}


class Handler(BaseHTTPRequestHandler):
    allowed_hosts: set[str] = set()
    password = ""

    def log_message(self, *args):
        pass

    def reply(self, code: int, body: bytes, content_type: str = "application/json; charset=utf-8",
              headers: dict | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def reply_json(self, code: int, data) -> None:
        self.reply(code, json.dumps(data, ensure_ascii=False).encode())

    def password_ok(self) -> bool:
        auth = self.headers.get("Authorization", "")
        if not auth.startswith("Basic "):
            return False
        try:
            given = base64.b64decode(auth[6:]).decode().partition(":")[2]
        except ValueError:
            return False
        return hmac.compare_digest(given.encode(), self.password.encode())

    def authorized(self) -> bool:
        if self.password:
            if self.password_ok():
                return True
            self.reply(401, b'{"error": "password required"}',
                       headers={"WWW-Authenticate": 'Basic realm="Detok", charset="UTF-8"'})
            return False
        if self.headers.get("Host", "") not in self.allowed_hosts:
            self.reply_json(403, {"error": "host not allowed"})
            return False
        return True

    def send_file(self, folder, name: str, suffix: str, content_type: str | None = None) -> bool:
        path = folder / f"{name}{suffix}"
        if not store.USERNAME_RE.fullmatch(name) or not path.exists():
            return False
        data = path.read_bytes()
        self.reply(200, data, content_type or image_type(data))
        return True

    def do_GET(self):
        if not self.authorized():
            return
        path = urlsplit(self.path).path
        if path == "/":
            page = resources.files("detok").joinpath("static/index.html").read_bytes()
            return self.reply(200, page, "text/html; charset=utf-8")
        if path == "/api/state":
            return self.reply_json(200, snapshot())
        if path.startswith("/avatar/") and self.send_file(store.AVATARS, path[8:], ".img"):
            return
        if path.startswith("/shot/") and self.send_file(store.SHOTS, path[6:], ".png", "image/png"):
            return
        self.reply_json(404, {"error": "not found"})

    def do_POST(self):
        if not self.authorized():
            return
        if self.headers.get("X-Detok") != "1":
            return self.reply_json(403, {"error": "request not allowed"})
        action = ACTIONS.get(urlsplit(self.path).path)
        if action is None:
            return self.reply_json(404, {"error": "not found"})
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY:
                raise ValueError("request too large")
            body = json.loads(self.rfile.read(length)) if length else {}
            action(body)
        except (ValueError, store.Busy, RuntimeError) as e:
            return self.reply_json(400, {"error": str(e)})
        self.reply_json(200, snapshot())


def is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def serve(host: str = "127.0.0.1", port: int = 8765, open_browser: bool = True) -> None:
    store.ensure_dirs()
    Handler.password = os.environ.get("DETOK_PASSWORD", "")
    if not is_loopback(host) and not Handler.password:
        raise SystemExit(f"Listening on {host} requires a password: set the DETOK_PASSWORD variable.")
    Handler.allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}"}

    server = ThreadingHTTPServer((host, port), Handler)
    url = f"http://{'127.0.0.1' if host in ('0.0.0.0', '::') else host}:{port}/"
    print(f"Detok {__version__}: {url}  (Ctrl+C to quit)  ·  data in {store.DATA}", flush=True)

    if timer.backend() == "builtin":
        threading.Thread(target=run_scheduler, daemon=True).start()
    if open_browser and store.has_display():
        threading.Timer(0.5, webbrowser.open, args=(url,)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
