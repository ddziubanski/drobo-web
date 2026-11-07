"""Send events to a webhook and/or by email."""
from __future__ import annotations

import json
import logging
import smtplib
import urllib.request
from email.message import EmailMessage

from .events import SEVERITY_ORDER, Event

log = logging.getLogger("droboweb.notify")
ICON = {"info": "ℹ️", "warning": "⚠️", "critical": "🔴"}


def _payload(fmt: str, ev: Event, unit: str) -> tuple[bytes, dict]:
    text = f"{ICON.get(ev.severity, '')} [{unit}] {ev.message}"
    if fmt == "discord":
        body = {"content": text}
    elif fmt == "slack":
        body = {"text": text}
    elif fmt == "teams":
        body = {"@type": "MessageCard", "@context": "https://schema.org/extensions",
                "summary": ev.message, "themeColor": {"critical": "D32F2F", "warning": "F9A825"}.get(ev.severity, "1976D2"),
                "title": f"Drobo {unit}: {ev.severity}", "text": ev.message}
    elif fmt == "ntfy":
        return text.encode(), {"Title": f"Drobo {unit}",
                               "Priority": {"critical": "5", "warning": "4"}.get(ev.severity, "3")}
    else:
        body = {"unit": unit, **ev.to_dict()}
    return json.dumps(body).encode(), {"Content-Type": "application/json"}


class Notifier:
    def __init__(self, cfg):
        self.cfg = cfg

    def wants(self, ev: Event) -> bool:
        return SEVERITY_ORDER.get(ev.severity, 0) >= SEVERITY_ORDER.get(self.cfg.notify_min_severity, 1)

    def send(self, ev: Event, unit: str) -> None:
        if not self.wants(ev):
            return
        if self.cfg.webhook_url:
            try:
                data, headers = _payload(self.cfg.webhook_format, ev, unit)
                req = urllib.request.Request(self.cfg.webhook_url, data=data, headers=headers, method="POST")
                urllib.request.urlopen(req, timeout=10).close()
            except Exception as e:  # noqa: BLE001
                log.warning("webhook failed: %s", e)
        if self.cfg.smtp_host and self.cfg.mail_to:
            try:
                msg = EmailMessage()
                msg["Subject"] = f"[Drobo {unit}] {ev.severity.upper()}: {ev.message}"
                msg["From"] = self.cfg.mail_from or self.cfg.smtp_user or "droboweb@localhost"
                msg["To"] = self.cfg.mail_to
                msg.set_content(f"{ev.message}\n\nSeverity: {ev.severity}\nEvent: {ev.kind}\n")
                with smtplib.SMTP(self.cfg.smtp_host, self.cfg.smtp_port, timeout=15) as s:
                    if self.cfg.smtp_starttls:
                        s.starttls()
                    if self.cfg.smtp_user:
                        s.login(self.cfg.smtp_user, self.cfg.smtp_password)
                    s.send_message(msg)
            except Exception as e:  # noqa: BLE001
                log.warning("email failed: %s", e)
