"""System-tray app: a replacement for Drobo's DDAssist.

Runs the drobo-web server on localhost in a background thread and shows a tray icon whose
colour follows the unit's health, with desktop notifications for events. Click the icon to
open the dashboard. Settings live in %APPDATA%\\DroboWeb\\settings.json
(~/.config/drobo-web/settings.json elsewhere).
"""
from __future__ import annotations

import json
import logging
import logging.handlers
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path

from .config import Config
from .events import SEVERITY_ORDER

log = logging.getLogger("droboweb.tray")

APP = "DroboWeb"
TASK_NAME = "Drobo Web"
DEFAULT_PORT = 8780
COLOURS = {"ok": (34, 184, 102), "rebuilding": (224, 168, 0), "warning": (224, 168, 0),
           "critical": (229, 72, 77), "offline": (120, 128, 140), "starting": (47, 140, 255)}
LABELS = {"ok": "Healthy", "rebuilding": "Rebuilding", "warning": "Needs attention",
          "critical": "Critical", "offline": "Can't reach the Drobo", "starting": "Starting…"}


def data_dir() -> Path:
    if os.name == "nt":
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming")) / APP
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "drobo-web"
    base.mkdir(parents=True, exist_ok=True)
    return base


# ---------------------------------------------------------------- settings

TRAY_DEFAULTS = {"notifications": True, "notify_min_severity": "warning", "port": DEFAULT_PORT,
                 "device": "auto", "report_usage": False}


def load_settings(path: Path) -> dict:
    s = dict(TRAY_DEFAULTS)
    try:
        s.update(json.loads(path.read_text()))
    except (OSError, ValueError):
        pass
    return s


def save_settings(path: Path, s: dict) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(s, indent=2))
    tmp.replace(path)


def build_config(settings: dict, ddir: Path, demo: str = "") -> Config:
    cfg = Config.from_env()
    cfg.host = "127.0.0.1"
    cfg.db_path = str(ddir / "droboweb.sqlite3")
    for k, v in settings.items():
        if hasattr(cfg, k) and k not in ("host", "db_path"):
            setattr(cfg, k, type(getattr(cfg, k))(v))
    if demo:
        cfg.demo = demo
        cfg.db_path = ":memory:"  # never mix demo data into the real history
    return cfg


# ---------------------------------------------------------------- icon

def make_icon(state: str, size: int = 64):
    from PIL import Image, ImageDraw
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    s = size / 64
    d.rounded_rectangle([2 * s, 2 * s, 62 * s, 62 * s], radius=12 * s, fill=(28, 36, 48, 255))
    for i in range(2):  # drive bays
        y = (11 + i * 14) * s
        d.rounded_rectangle([9 * s, y, 44 * s, y + 9 * s], radius=2 * s, fill=(70, 84, 104, 255))
    c = COLOURS.get(state, COLOURS["offline"])
    d.ellipse([27 * s, 27 * s, 63 * s, 63 * s], fill=c + (255,), outline=(28, 36, 48, 255), width=max(1, int(4 * s)))
    return img


def summarize(monitor) -> tuple[str, str]:
    """(state, one-line tooltip) from the monitor."""
    if monitor is None or (monitor.polls == 0 and not monitor.reachable):
        return "starting", "Drobo Web: starting…"
    if not monitor.reachable:
        return "offline", f"Drobo Web: can't reach the Drobo"
    s = monitor.snapshot
    state = s.health
    pct = f", {s.to_dict()['used_pct']}% full" if s.total_bytes else ""
    return state, f"{s.name or 'Drobo'}: {LABELS.get(state, state)}{pct}"[:120]


# ---------------------------------------------------------------- start at login (Windows)

def _exe_command() -> str:
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}"'
    return f'"{sys.executable}" -m droboweb tray'


def autostart_enabled() -> bool:
    if os.name != "nt":
        return False
    r = subprocess.run(["schtasks", "/Query", "/TN", TASK_NAME], capture_output=True, text=True,
                       creationflags=0x08000000)
    return r.returncode == 0


def set_autostart(on: bool) -> None:
    if os.name != "nt":
        return
    if on:
        # A scheduled task with highest privileges starts elevated at logon without a UAC prompt.
        cmd = ["schtasks", "/Create", "/TN", TASK_NAME, "/TR", _exe_command(), "/SC", "ONLOGON",
               "/RL", "HIGHEST", "/F"]
    else:
        cmd = ["schtasks", "/Delete", "/TN", TASK_NAME, "/F"]
    subprocess.run(cmd, capture_output=True, text=True, creationflags=0x08000000, check=True)


# ---------------------------------------------------------------- app

def _port_in_use(port: int) -> bool:
    with socket.socket() as s:
        try:
            s.bind(("127.0.0.1", port))
            return False
        except OSError:
            return True


def _is_droboweb(port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=2) as r:
            return b"reachable" in r.read()
    except Exception:  # noqa: BLE001
        return False


def _setup_logging(ddir: Path) -> None:
    if sys.stdout is None:  # windowed .exe has no console
        sys.stdout = open(os.devnull, "w")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w")
    h = logging.handlers.RotatingFileHandler(ddir / "droboweb.log", maxBytes=1_000_000, backupCount=3,
                                             encoding="utf-8")
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.addHandler(h)
    root.setLevel(logging.INFO)


class TrayApp:
    def __init__(self, demo: str = ""):
        self.ddir = data_dir()
        self.settings_path = self.ddir / "settings.json"
        self.settings = load_settings(self.settings_path)
        self.demo = demo
        self.cfg = build_config(self.settings, self.ddir, demo)
        self.server = None
        self.monitor = None
        self.icon = None
        self.state = "starting"
        self.url = f"http://127.0.0.1:{self.cfg.port}/"

    # -- server
    def start_server(self) -> None:
        import uvicorn

        from .app import create_app
        from .monitor import Monitor
        self.monitor = Monitor(self.cfg)
        self.monitor.listeners.append(self.on_event)
        app = create_app(self.cfg, self.monitor)
        self.server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=self.cfg.port,
                                                    log_config=None, log_level="warning"))
        threading.Thread(target=self.server.run, name="server", daemon=True).start()

    # -- notifications
    def on_event(self, ev, unit: str) -> None:
        if not self.settings.get("notifications", True) or self.icon is None:
            return
        floor = SEVERITY_ORDER.get(self.settings.get("notify_min_severity", "warning"), 1)
        if SEVERITY_ORDER.get(ev.severity, 0) < floor:
            return
        title = {"critical": f"{unit}: critical", "warning": f"{unit}: warning"}.get(ev.severity, unit)
        try:
            self.icon.notify(ev.message, title)
        except Exception:  # noqa: BLE001
            log.exception("notify failed")

    # -- menu actions
    def open_dashboard(self, *_):
        webbrowser.open(self.url)

    def toggle(self, key: str):
        def fn(*_):
            self.settings[key] = not self.settings.get(key, False)
            save_settings(self.settings_path, self.settings)
            if key == "report_usage":
                self.cfg.report_usage = self.settings[key]
                if self.settings[key] and self.icon:
                    self.icon.notify("Drobo Web will now report this host's volume usage to the Drobo, "
                                     "replacing Drobo Dashboard. It won't while Dashboard's service is running.",
                                     "Usage reporting on")
            if self.icon:
                self.icon.update_menu()
        return fn

    def toggle_autostart(self, *_):
        try:
            set_autostart(not autostart_enabled())
        except Exception as e:  # noqa: BLE001
            log.warning("autostart: %s", e)
            if self.icon:
                self.icon.notify(f"Couldn't change start-at-login: {e}", "Drobo Web")
        if self.icon:
            self.icon.update_menu()

    def open_folder(self, *_):
        if os.name == "nt":
            os.startfile(self.ddir)  # noqa: S606
        else:
            webbrowser.open(self.ddir.as_uri())

    def quit(self, *_):
        if self.server:
            self.server.should_exit = True
        if self.icon:
            self.icon.stop()

    def status_text(self, *_):
        return summarize(self.monitor)[1]

    def usage_text(self, *_):
        u = self.monitor.usage if self.monitor else {}
        if not self.settings.get("report_usage"):
            return "Report volume usage to the Drobo"
        st = u.get("status", "off")
        return {"active": "Report volume usage to the Drobo (active)",
                "blocked": "Report volume usage to the Drobo (paused, see dashboard)",
                "error": "Report volume usage to the Drobo (error, see dashboard)"}.get(
            st, "Report volume usage to the Drobo (waiting)")

    def build_menu(self):
        import pystray
        M = pystray.MenuItem
        items = [
            M(self.status_text, None, enabled=False),
            M("Open dashboard", self.open_dashboard, default=True),
            pystray.Menu.SEPARATOR,
            M("Notifications", self.toggle("notifications"), checked=lambda _: self.settings.get("notifications", True)),
            M(self.usage_text, self.toggle("report_usage"), checked=lambda _: self.settings.get("report_usage", False)),
        ]
        if os.name == "nt":
            items.append(M("Start when I log in", self.toggle_autostart, checked=lambda _: autostart_enabled()))
        items += [M("Open settings & log folder", self.open_folder), pystray.Menu.SEPARATOR, M("Quit", self.quit)]
        return pystray.Menu(*items)

    # -- loop
    def watch(self) -> None:
        last = None
        while self.icon is not None and self.server is not None and not self.server.should_exit:
            state, tip = summarize(self.monitor)
            if (state, tip) != last:
                self.icon.icon = make_icon(state)
                self.icon.title = tip
                self.icon.update_menu()
                last = (state, tip)
            time.sleep(2)

    def run(self) -> None:
        import pystray
        _setup_logging(self.ddir)
        if _port_in_use(self.cfg.port):
            if _is_droboweb(self.cfg.port):  # already running: just open it
                self.open_dashboard()
                return
            raise SystemExit(f"Port {self.cfg.port} is in use by another program. "
                             f"Set \"port\" in {self.settings_path}.")
        self.start_server()
        self.icon = pystray.Icon(APP, make_icon("starting"), "Drobo Web: starting…", self.build_menu())

        def setup(icon):
            icon.visible = True
            threading.Thread(target=self.watch, name="tray-watch", daemon=True).start()

        self.icon.run(setup=setup)
        if self.server:
            self.server.should_exit = True


def main(demo: str = "") -> None:
    TrayApp(demo).run()
