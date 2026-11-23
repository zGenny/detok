from __future__ import annotations

import datetime as dt
import json
import os
import random
import re
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

WINDOWS = sys.platform == "win32"
MACOS = sys.platform == "darwin"
FROZEN = getattr(sys, "frozen", False)
CREATE_NO_WINDOW = 0x08000000

USERNAME_RE = re.compile(r"^[a-z0-9._]{1,30}$")
TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
PLACEHOLDER_RE = re.compile(r"@(name|nome|username)\b", re.IGNORECASE)
SESSION_COOKIES = ("sessionid", "sessionid_ss", "sid_tt")
DEFAULT_CONFIG = {"default_messages": ["❤️"], "send_after": "12:00", "headless": True, "friends": []}


def _local_appdata() -> Path:
    return Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local"))


def _data_dir() -> Path:
    if os.environ.get("DETOK_HOME"):
        return Path(os.environ["DETOK_HOME"]).expanduser()
    if WINDOWS:
        return _local_appdata() / "Detok"
    if MACOS:
        return Path.home() / "Library/Application Support/Detok"
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share") / "detok"


def _browsers_dir() -> Path:
    if WINDOWS:
        return _local_appdata() / "ms-playwright"
    if MACOS:
        return Path.home() / "Library/Caches/ms-playwright"
    return Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "ms-playwright"


if FROZEN and os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "0") == "0":
    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(_browsers_dir())

DATA = _data_dir()
OLD_DATA = Path.home() / ".local/share/streak-keeper"
CONFIG = DATA / "config.json"
STATE = DATA / "state.json"
PROFILE = DATA / "profile"
LOG = DATA / "log.txt"
SHOTS = DATA / "shots"
AVATARS = DATA / "avatars"
LOCK = DATA / "lock"
AUTO_FLAG = DATA / "auto-send"


class Busy(Exception):
    pass


def ensure_dirs() -> None:
    if not DATA.exists() and OLD_DATA.is_dir() and "DETOK_HOME" not in os.environ:
        DATA.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(OLD_DATA), str(DATA))
    DATA.mkdir(parents=True, exist_ok=True)
    if not WINDOWS:
        os.chmod(DATA, 0o700)
    SHOTS.mkdir(exist_ok=True)
    AVATARS.mkdir(exist_ok=True)


def now() -> dt.datetime:
    return dt.datetime.now().replace(microsecond=0)


def today() -> str:
    return dt.date.today().isoformat()


def yesterday() -> str:
    return (dt.date.today() - dt.timedelta(days=1)).isoformat()


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}


def _write_json(path: Path, data: dict) -> None:
    ensure_dirs()
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def _clean_friend(raw: dict) -> dict:
    username = str(raw.get("username", "")).strip().lstrip("@").lower()
    if not USERNAME_RE.fullmatch(username):
        raise ValueError(f"Invalid username: {username!r} (letters, digits, dot and underscore only)")
    return {
        "username": username,
        "message": str(raw.get("message", "")).strip()[:500],
        "enabled": bool(raw.get("enabled", True)),
    }


def clean_config(raw: dict) -> dict:
    send_after = str(raw.get("send_after", "12:00")).strip()
    if not TIME_RE.fullmatch(send_after):
        raise ValueError(f"Invalid time: {send_after!r} (use HH:MM)")

    friends = {}
    for item in raw.get("friends", []):
        friend = _clean_friend(item)
        friends.setdefault(friend["username"], friend)

    messages = raw.get("default_messages")
    if messages is None:
        messages = [raw.get("default_message", "")]
    messages = [str(m).strip()[:500] for m in messages]
    messages = list(dict.fromkeys(m for m in messages if m))[:100]

    return {
        "default_messages": messages or ["❤️"],
        "send_after": send_after,
        "headless": bool(raw.get("headless", True)),
        "friends": list(friends.values()),
    }


def load_config() -> dict:
    ensure_dirs()
    return clean_config({**DEFAULT_CONFIG, **_read_json(CONFIG)})


def save_config(cfg: dict) -> dict:
    cfg = clean_config(cfg)
    _write_json(CONFIG, cfg)
    return cfg


def load_state() -> dict:
    ensure_dirs()
    state = _read_json(STATE)
    state.setdefault("users", {})
    return state


def save_state(state: dict) -> None:
    _write_json(STATE, state)


def fill(template: str, username: str, nickname: str | None = None) -> str:
    def replace(match: re.Match) -> str:
        if match.group(1).lower() == "username":
            return "@" + username
        return nickname or username

    return PLACEHOLDER_RE.sub(replace, template)


def message_for(cfg: dict, friend: dict, state: dict) -> str:
    template = friend["message"] or random.choice(cfg["default_messages"])
    profile = state.get("profiles", {}).get(friend["username"], {})
    return fill(template, friend["username"], profile.get("nickname"))


def sent_today(state: dict, username: str) -> bool:
    return state["users"].get(username, {}).get("last_sent", "").startswith(today())


def streak(record: dict) -> int:
    last_day = (record.get("last_sent") or "")[:10]
    if last_day in (today(), yesterday()):
        return int(record.get("streak") or 1)
    return 0


def log(message: str) -> None:
    ensure_dirs()
    line = f"{now():%Y-%m-%d %H:%M:%S}  {message}"
    try:
        print(line, flush=True)
    except (OSError, ValueError):
        pass
    with LOG.open("a", encoding="utf-8") as f:
        f.write(line + "\n")
    if LOG.stat().st_size > 512_000:
        lines = LOG.read_text(encoding="utf-8").splitlines()[-2000:]
        LOG.write_text("\n".join(lines) + "\n", encoding="utf-8")


def log_tail(n: int = 80) -> list[str]:
    try:
        return LOG.read_text(encoding="utf-8").splitlines()[-n:]
    except FileNotFoundError:
        return []


WINDOWS_BALLOON = (
    "[reflection.assembly]::loadwithpartialname('System.Windows.Forms')|Out-Null;"
    "$n=New-Object System.Windows.Forms.NotifyIcon;"
    "$n.Icon=[System.Drawing.SystemIcons]::Warning;$n.Visible=$true;"
    "$n.ShowBalloonTip(10000,$env:DETOK_TITLE,$env:DETOK_BODY,'Warning');"
    "Start-Sleep 11;$n.Dispose()"
)


def notify(title: str, body: str) -> None:
    try:
        if WINDOWS:
            subprocess.Popen(
                ["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command", WINDOWS_BALLOON],
                env={**os.environ, "DETOK_TITLE": title, "DETOK_BODY": body},
                creationflags=CREATE_NO_WINDOW,
            )
        elif shutil.which("notify-send"):
            subprocess.run(["notify-send", "-a", "Detok", "-i", "dialog-warning", title, body],
                           capture_output=True, timeout=10)
    except Exception:
        pass


class Lock:
    def __enter__(self):
        ensure_dirs()
        self.file = LOCK.open("a+")
        try:
            self._lock()
        except OSError:
            self.file.close()
            raise Busy("Another operation (sending or login) is already running.")
        return self

    def __exit__(self, *exc):
        try:
            self._unlock()
        finally:
            self.file.close()

    def _lock(self) -> None:
        if WINDOWS:
            import msvcrt
            self.file.seek(0)
            msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(self.file, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock(self) -> None:
        if WINDOWS:
            import msvcrt
            self.file.seek(0)
            msvcrt.locking(self.file.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(self.file, fcntl.LOCK_UN)


def _cookie_db() -> Path | None:
    for path in (PROFILE / "Default/Network/Cookies", PROFILE / "Default/Cookies"):
        if path.exists():
            return path
    return None


def session_expiry() -> dt.datetime | None:
    db = _cookie_db()
    if db is None:
        return None
    names = ",".join(f"'{name}'" for name in SESSION_COOKIES)
    try:
        con = sqlite3.connect(f"{db.as_uri()}?mode=ro&immutable=1", uri=True)
        try:
            (expires,) = con.execute(
                f"SELECT MAX(expires_utc) FROM cookies WHERE host_key LIKE '%tiktok.com' AND name IN ({names})"
            ).fetchone()
        finally:
            con.close()
    except sqlite3.Error:
        return None
    if not expires:
        return None
    chromium_epoch = dt.datetime(1601, 1, 1, tzinfo=dt.timezone.utc)
    expiry = (chromium_epoch + dt.timedelta(microseconds=expires)).astimezone().replace(tzinfo=None, microsecond=0)
    return expiry if expiry > dt.datetime.now() else None


def self_command(*args: str) -> list[str]:
    if FROZEN:
        return [sys.executable, *args]
    return [sys.executable, "-m", "detok", *args]


def has_display() -> bool:
    if WINDOWS or MACOS:
        return True
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))
