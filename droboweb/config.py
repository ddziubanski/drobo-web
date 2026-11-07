"""Settings, read from environment variables (DROBOWEB_*) with CLI overrides."""
from __future__ import annotations

import os
from dataclasses import dataclass, field, fields


def _env(name: str, default, cast=str):
    v = os.environ.get(f"DROBOWEB_{name}")
    if v is None or v == "":
        return default
    if cast is bool:
        return v.strip().lower() in ("1", "true", "yes", "on")
    return cast(v)


@dataclass
class Config:
    device: str = "auto"            # /dev/sgN, or "auto" to find the Drobo
    demo: str = ""                  # "", "healthy" or "failing"
    demo_speed: float = 1.0         # demo clock multiplier (failing scenario trips at 120 s / speed)
    fixture: str = ""               # replay a specific fixture instead of the built-in one
    host: str = "0.0.0.0"
    port: int = 8080
    poll_seconds: float = 10.0
    sample_seconds: float = 60.0    # how often a history point is stored
    db_path: str = "droboweb.sqlite3"
    retention_days: int = 90
    temp_warn_c: int = 50
    capacity_warn_pct: float = 85.0
    auth_user: str = ""
    auth_password: str = ""
    webhook_url: str = ""
    webhook_format: str = "json"    # json | discord | slack | teams | ntfy
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_starttls: bool = True
    mail_from: str = ""
    mail_to: str = ""
    notify_min_severity: str = "warning"  # info | warning | critical
    extra: dict = field(default_factory=dict)

    @classmethod
    def from_env(cls) -> "Config":
        c = cls()
        for f in fields(cls):
            if f.name == "extra":
                continue
            cur = getattr(c, f.name)
            setattr(c, f.name, _env(f.name.upper(), cur, type(cur) if not isinstance(cur, bool) else bool))
        return c
