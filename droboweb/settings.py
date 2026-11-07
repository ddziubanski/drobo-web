"""Editable settings: the schema behind the Settings page, plus where they are saved.

Precedence, highest first:
  1. command-line flags (``droboweb serve --device …``)
  2. DROBOWEB_* environment variables
  3. the settings file written by the Settings page
  4. built-in defaults

A setting that comes from (1) or (2) is shown on the Settings page but can't be changed
there, so the page never pretends to change something the next restart would undo.
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .config import Config

log = logging.getLogger("droboweb.settings")

SETTINGS_FILE = "droboweb-settings.json"


@dataclass(frozen=True)
class Field:
    key: str
    label: str
    group: str
    kind: str = "text"          # text | int | float | bool | choice | password | email | url
    help: str = ""
    min: float | None = None
    max: float | None = None
    choices: tuple[tuple[str, str], ...] = ()
    unit: str = ""
    placeholder: str = ""
    restart: bool = False       # takes effect only after a restart
    default: Any = None         # for fields that aren't Config attributes
    get: Callable[[], Any] | None = field(default=None, compare=False)   # virtual (not stored) fields
    set: Callable[[Any], None] | None = field(default=None, compare=False)

    @property
    def secret(self) -> bool:
        return self.kind == "password"


GROUPS = {
    "email": ("Email alerts", "Send alerts by email, like Drobo Dashboard's email notifications."),
    "webhook": ("Webhook alerts", "Post alerts to a chat or push service."),
    "alerts": ("Alert rules", "Which events are sent, and the thresholds that raise them."),
    "monitoring": ("Monitoring", "How the unit is found and how often it's read."),
    "usage": ("Volume usage reporting",
              "Tell the Drobo how full each volume's filesystem is, as Drobo Dashboard does. This is the one "
              "write drobo-web can make, and it's off by default. It pauses itself if Dashboard's service is "
              "running, or if none of the Drobo's volumes are mounted on this host."),
    "access": ("Access", "Ask for a user name and password before showing the dashboard or the API."),
    "desktop": ("This computer", "Tray icon and notification settings for this computer."),
}

SEVERITIES = (("info", "Everything (info, warning, critical)"), ("warning", "Warnings and critical"),
              ("critical", "Critical only"))

FIELDS: tuple[Field, ...] = (
    Field("smtp_host", "SMTP server", "email", placeholder="smtp.example.com",
          help="Leave empty to turn email alerts off."),
    Field("smtp_port", "Port", "email", "int", min=1, max=65535),
    Field("smtp_starttls", "Use STARTTLS", "email", "bool"),
    Field("smtp_user", "User name", "email", help="Leave empty if the server doesn't need a login."),
    Field("smtp_password", "Password", "email", "password"),
    Field("mail_from", "From", "email", "email", placeholder="drobo@example.com",
          help="Defaults to the user name."),
    Field("mail_to", "To", "email", "email", placeholder="you@example.com",
          help="Separate several addresses with commas."),
    Field("webhook_url", "Webhook URL", "webhook", "url", placeholder="https://…",
          help="Leave empty to turn webhook alerts off."),
    Field("webhook_format", "Format", "webhook", "choice",
          choices=(("json", "Generic JSON"), ("discord", "Discord"), ("slack", "Slack"),
                   ("teams", "Microsoft Teams"), ("ntfy", "ntfy"))),
    Field("notify_min_severity", "Send", "alerts", "choice", choices=SEVERITIES,
          help="Applies to email, webhook and desktop notifications."),
    Field("temp_warn_c", "Drive temperature warning", "alerts", "int", min=30, max=80, unit="°C"),
    Field("capacity_warn_pct", "Capacity warning", "alerts", "float", min=1, max=100, unit="%"),
    Field("device", "Device", "monitoring", placeholder="auto",
          help="auto finds the Drobo by itself. Otherwise /dev/sgN (Linux) or \\\\.\\PhysicalDriveN (Windows)."),
    Field("poll_seconds", "Read the unit every", "monitoring", "float", min=2, max=3600, unit="s"),
    Field("sample_seconds", "Store a history point every", "monitoring", "float", min=10, max=3600, unit="s"),
    Field("retention_days", "Keep history for", "monitoring", "int", min=1, max=3650, unit="days"),
    Field("report_usage", "Report volume usage to the Drobo", "usage", "bool"),
    Field("usage_seconds", "Report every", "usage", "float", min=5, max=3600, unit="s"),
    Field("auth_user", "User name", "access", help="Leave empty for no login."),
    Field("auth_password", "Password", "access", "password"),
)

_EMAIL = re.compile(r"^[^@\s,]+@[^@\s,]+$")
_DEVICE = re.compile(r"^(auto|/dev/[\w./-]+|\\\\\.\\[\w]+)$", re.I)


class SettingsError(ValueError):
    def __init__(self, errors: dict[str, str]):
        super().__init__("; ".join(f"{k}: {v}" for k, v in errors.items()))
        self.errors = errors


def _coerce(f: Field, v: Any) -> Any:
    """Validate and convert one submitted value. Raises ValueError with a readable message."""
    if f.kind == "bool":
        if isinstance(v, bool):
            return v
        if isinstance(v, str) and v.lower() in ("true", "false", "1", "0", "on", "off", "yes", "no"):
            return v.lower() in ("true", "1", "on", "yes")
        raise ValueError("must be on or off")
    if f.kind in ("int", "float"):
        if isinstance(v, bool) or v is None or (isinstance(v, str) and not v.strip()):
            raise ValueError("must be a number")
        try:
            n = float(v)
        except (TypeError, ValueError):
            raise ValueError("must be a number") from None
        if f.kind == "int":
            if n != int(n):
                raise ValueError("must be a whole number")
            n = int(n)
        if f.min is not None and n < f.min or f.max is not None and n > f.max:
            raise ValueError(f"must be between {f.min:g} and {f.max:g}")
        return n
    if not isinstance(v, str):
        raise ValueError("must be text")
    v = v.strip()
    if len(v) > 2048:
        raise ValueError("is too long")
    if f.kind == "choice":
        if v not in dict(f.choices):
            raise ValueError("isn't one of the choices")
    elif f.kind == "email" and v:
        bad = [a for a in (x.strip() for x in v.split(",")) if not _EMAIL.match(a)]
        if bad:
            raise ValueError(f"isn't an email address: {bad[0] or '(empty)'}")
    elif f.kind == "url" and v and not re.match(r"^https?://[^\s/]+", v):
        raise ValueError("must start with http:// or https://")
    elif f.key == "device" and not _DEVICE.match(v or "auto"):
        raise ValueError("must be auto, /dev/sgN or \\\\.\\PhysicalDriveN")
    if f.key == "device" and not v:
        v = "auto"
    if f.kind != "password" and any(c in v for c in "\r\n"):
        raise ValueError("can't contain line breaks")
    return v


def default_path(cfg: Config) -> Path | None:
    """Where the server keeps its settings: next to the history database."""
    if cfg.settings_path:
        return Path(cfg.settings_path)
    if cfg.demo or cfg.db_path == ":memory:":
        return None  # demo and test runs don't write anything to disk
    return Path(cfg.db_path).resolve().parent / SETTINGS_FILE


class SettingsStore:
    """The editable settings of a running instance, applied live to its Config."""

    def __init__(self, cfg: Config, path: Path | None, fields: tuple[Field, ...] = FIELDS,
                 locked: dict[str, str] | None = None, env: dict[str, str] | None = None):
        self.cfg = cfg
        self.path = path
        self.fields = {f.key: f for f in fields}
        self.listeners: list[Callable[[set[str]], None]] = []
        self.values: dict[str, Any] = {}       # fields that aren't Config attributes (e.g. tray-only)
        self._lock = threading.Lock()
        env = os.environ if env is None else env
        self.locked: dict[str, str] = {}
        for k in self.fields:
            name = f"DROBOWEB_{k.upper()}"
            if env.get(name):
                self.locked[k] = f"environment variable {name}"
        self.locked.update(locked or cfg.extra.get("locked", {}))
        self._load()

    # -------------------------------------------------------------- storage
    def _read_file(self) -> dict:
        if not self.path:
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as e:
            log.warning("ignoring unreadable settings file %s: %s", self.path, e)
            return {}

    def _load(self) -> None:
        for k, v in self._read_file().items():
            f = self.fields.get(k)
            if f is None or k in self.locked or f.set is not None:
                continue
            try:
                self._put(f, _coerce(f, v))
            except ValueError as e:
                log.warning("settings file: ignoring %s (%s)", k, e)

    def _write_file(self, changes: dict[str, Any]) -> None:
        if not self.path:
            return
        data = self._read_file()            # keep keys this store doesn't know about
        data.update(changes)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)  # holds passwords
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
        os.replace(tmp, self.path)

    # -------------------------------------------------------------- values
    def get(self, key: str) -> Any:
        f = self.fields[key]
        if f.get is not None:
            return f.get()
        if hasattr(self.cfg, key):
            return getattr(self.cfg, key)
        return self.values.get(key, f.default)

    def _put(self, f: Field, v: Any) -> None:
        if f.set is not None:
            f.set(v)
        elif hasattr(self.cfg, f.key):
            setattr(self.cfg, f.key, v)
        else:
            self.values[f.key] = v

    def update(self, changes: dict[str, Any]) -> set[str]:
        """Validate every change, then apply and save them all, or none. Returns the keys that changed."""
        errors, clean = {}, {}
        for k, v in changes.items():
            f = self.fields.get(k)
            if f is None:
                errors[k] = "isn't a setting"
            elif k in self.locked:
                errors[k] = f"is set by the {self.locked[k]}"
            else:
                try:
                    clean[k] = _coerce(f, v)
                except ValueError as e:
                    errors[k] = str(e)
        if not errors and ("auth_user" in clean or "auth_password" in clean):
            user = clean.get("auth_user", self.get("auth_user") if "auth_user" in self.fields else "")
            pw = clean.get("auth_password", self.get("auth_password") if "auth_password" in self.fields else "")
            if user and not pw:
                errors["auth_password"] = "is needed when a user name is set"
        if errors:
            raise SettingsError(errors)
        with self._lock:
            changed = {k for k, v in clean.items() if self.get(k) != v}
            stored = {k: clean[k] for k in changed if self.fields[k].set is None}
            self._write_file(stored)
            for k in changed:
                self._put(self.fields[k], clean[k])
        if changed:
            log.info("settings changed: %s", ", ".join(sorted(changed)))
            for fn in list(self.listeners):
                try:
                    fn(changed)
                except Exception:  # noqa: BLE001
                    log.exception("settings listener failed")
        return changed

    def describe(self) -> dict:
        """The schema and current values, for the Settings page. Passwords are never sent back."""
        defaults = Config()
        groups: dict[str, dict] = {}
        for f in self.fields.values():
            title, desc = GROUPS.get(f.group, (f.group.title(), ""))
            g = groups.setdefault(f.group, {"id": f.group, "title": title, "description": desc, "fields": []})
            v = self.get(f.key)
            item = {"key": f.key, "label": f.label, "kind": f.kind, "help": f.help, "unit": f.unit,
                    "placeholder": f.placeholder, "restart": f.restart,
                    "locked": self.locked.get(f.key)}
            if f.min is not None:
                item["min"], item["max"] = f.min, f.max
            if f.choices:
                item["choices"] = [{"value": a, "label": b} for a, b in f.choices]
            if f.secret:
                item["value"], item["is_set"] = "", bool(v)
            else:
                item["value"] = v
                d = getattr(defaults, f.key, f.default)
                if d is not None:
                    item["default"] = d
            g["fields"].append(item)
        return {"saved_to": str(self.path) if self.path else None, "groups": list(groups.values())}
