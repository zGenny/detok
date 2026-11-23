from __future__ import annotations

import datetime as dt
import functools
import os
import shutil
import subprocess
import sys
from pathlib import Path

from . import store


def _run(*cmd: str) -> subprocess.CompletedProcess:
    extra = {"creationflags": store.CREATE_NO_WINDOW} if store.WINDOWS else {}
    return subprocess.run(cmd, capture_output=True, text=True, **extra)


def next_hour() -> dt.datetime:
    return store.now().replace(minute=0, second=0) + dt.timedelta(hours=1)


class Systemd:
    units = Path.home() / ".config/systemd/user"
    service = units / "detok.service"
    timer = units / "detok.timer"

    @staticmethod
    def _quote(value: str) -> str:
        return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'

    def _ctl(self, *args: str) -> subprocess.CompletedProcess:
        return _run("systemctl", "--user", *args)

    def enable(self) -> None:
        self.units.mkdir(parents=True, exist_ok=True)
        command = " ".join(self._quote(arg) for arg in store.self_command("send"))
        self.service.write_text(
            "[Unit]\n"
            "Description=Detok: daily messages for your TikTok streaks\n"
            "After=network-online.target\n\n"
            "[Service]\n"
            "Type=oneshot\n"
            f"Environment={self._quote(f'DETOK_HOME={store.DATA}')}\n"
            f"ExecStart={command}\n"
            "TimeoutStartSec=15min\n"
        )
        self.timer.write_text(
            "[Unit]\n"
            "Description=Detok every hour\n\n"
            "[Timer]\n"
            "OnCalendar=hourly\n"
            "OnStartupSec=3min\n"
            "Persistent=true\n"
            "RandomizedDelaySec=120\n\n"
            "[Install]\n"
            "WantedBy=timers.target\n"
        )
        self._ctl("daemon-reload")
        result = self._ctl("enable", "--now", "detok.timer")
        if result.returncode:
            raise RuntimeError(result.stderr.strip())

    def disable(self) -> None:
        self._ctl("disable", "--now", "detok.timer")
        self.service.unlink(missing_ok=True)
        self.timer.unlink(missing_ok=True)
        self._ctl("daemon-reload")

    def status(self) -> dict:
        if self._ctl("is-active", "detok.timer").stdout.strip() != "active":
            return {"enabled": False, "next": ""}
        upcoming = self._ctl("show", "detok.timer", "-p", "NextElapseUSecRealtime", "--value").stdout.strip()
        return {"enabled": True, "next": upcoming}


class WindowsTask:
    name = "Detok"
    next_run_labels = ("next run time", "ora prossima esecuzione", "prossima esecuzione")

    @property
    def launcher(self) -> Path:
        return store.DATA / "detok-send.vbs"

    def enable(self) -> None:
        store.ensure_dirs()
        command = " ".join(f'""{arg}""' for arg in store.self_command("send"))
        self.launcher.write_text(f'CreateObject("WScript.Shell").Run "{command}", 0, False\n', encoding="utf-8")
        start = (dt.datetime.now() + dt.timedelta(minutes=2)).strftime("%H:%M")
        result = _run("schtasks", "/Create", "/F", "/TN", self.name, "/SC", "HOURLY", "/ST", start,
                      "/TR", f'wscript.exe "{self.launcher}"')
        if result.returncode:
            raise RuntimeError((result.stderr or result.stdout).strip())

    def disable(self) -> None:
        _run("schtasks", "/Delete", "/F", "/TN", self.name)
        self.launcher.unlink(missing_ok=True)

    def status(self) -> dict:
        result = _run("schtasks", "/Query", "/TN", self.name, "/FO", "LIST")
        if result.returncode:
            return {"enabled": False, "next": ""}
        upcoming = ""
        for line in result.stdout.splitlines():
            label, _, value = line.partition(":")
            if label.strip().lower() in self.next_run_labels:
                upcoming = value.strip()
        return {"enabled": True, "next": upcoming}


class Builtin:
    def enable(self) -> None:
        store.ensure_dirs()
        store.AUTO_FLAG.write_text("on\n")

    def disable(self) -> None:
        store.AUTO_FLAG.unlink(missing_ok=True)

    def status(self) -> dict:
        if not store.AUTO_FLAG.exists():
            return {"enabled": False, "next": ""}
        return {"enabled": True, "next": f"{next_hour():%Y-%m-%d %H:%M}"}


BACKENDS = {"systemd": Systemd, "windows": WindowsTask, "builtin": Builtin}


@functools.cache
def backend() -> str:
    forced = os.environ.get("DETOK_SCHEDULER", "").strip().lower()
    if forced in BACKENDS:
        return forced
    if store.WINDOWS:
        return "windows"
    if sys.platform.startswith("linux") and shutil.which("systemctl"):
        if _run("systemctl", "--user", "show-environment").returncode == 0:
            return "systemd"
    return "builtin"


def enable() -> None:
    BACKENDS[backend()]().enable()


def disable() -> None:
    BACKENDS[backend()]().disable()


def status() -> dict:
    name = backend()
    return {**BACKENDS[name]().status(), "backend": name}
