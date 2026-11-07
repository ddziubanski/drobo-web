"""Turn consecutive snapshots into human-readable events."""
from __future__ import annotations

import time
from dataclasses import dataclass, field

SEVERITY_ORDER = {"info": 0, "warning": 1, "critical": 2}


@dataclass
class Event:
    severity: str
    kind: str
    message: str
    ts: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return dict(ts=self.ts, severity=self.severity, kind=self.kind, message=self.message)


def _led_sev(led: str) -> str:
    if led.startswith("red"):
        return "critical"
    if led == "yellow":
        return "warning"
    return "info"


def diff(prev, cur, temp_warn: int, cap_warn: float) -> list[Event]:
    ev: list[Event] = []
    unit = cur.name or "Drobo"
    if prev is None:
        ev.append(Event("info", "connected",
                        f"Monitoring {unit} (firmware {cur.firmware}, {len([b for b in cur.bays if b.present])} drives)"))
        if cur.health != "ok":
            ev.append(Event("critical" if cur.health == "critical" else "warning", "health",
                            f"{unit} health is {cur.health}: {', '.join(cur.status) or 'see drive lights'}"))
        return ev

    if prev.health != cur.health:
        sev = {"critical": "critical", "warning": "warning", "rebuilding": "warning"}.get(cur.health, "info")
        ev.append(Event(sev, "health", f"{unit} health changed: {prev.health} → {cur.health}"))

    pb = {b.slot: b for b in prev.bays}
    for b in cur.bays:
        o = pb.get(b.slot)
        if o is None:
            continue
        n = b.slot + 1
        if o.present and not b.present:
            ev.append(Event("warning", "drive_removed", f"Bay {n}: drive removed ({o.model})"))
        elif b.present and not o.present:
            ev.append(Event("info", "drive_added", f"Bay {n}: drive inserted ({b.model})"))
        elif b.present and o.model != b.model:
            ev.append(Event("info", "drive_replaced", f"Bay {n}: {o.model} replaced by {b.model}"))
        if o.led != b.led and b.present and o.present:
            # flashing LEDs alternate between polls; only report changes in severity
            if _led_sev(o.led) != _led_sev(b.led):
                ev.append(Event(_led_sev(b.led) if _led_sev(b.led) != "info" else "info", "drive_led",
                                f"Bay {n}: light {o.led} → {b.led} ({b.model})"))
        if b.temperature_c is not None and o.temperature_c is not None:
            if b.temperature_c >= temp_warn > o.temperature_c:
                ev.append(Event("warning", "temperature", f"Bay {n}: {b.temperature_c} °C (threshold {temp_warn} °C)"))
            elif o.temperature_c >= temp_warn and b.temperature_c <= temp_warn - 2:
                ev.append(Event("info", "temperature", f"Bay {n}: back to {b.temperature_c} °C"))

    new = set(cur.status) - set(prev.status)
    gone = set(prev.status) - set(cur.status)
    for s in sorted(new):
        sev = "critical" if s in ("Red alert", "Bad disk", "Too many missing disks") else "warning"
        if s in ("Data protection in progress", "Format in progress", "New firmware installed"):
            sev = "info"
        ev.append(Event(sev, "status", f"{unit}: {s}"))
    for s in sorted(gone):
        ev.append(Event("info", "status", f"{unit}: cleared — {s}"))

    def pct(s):
        return 100 * s.used_bytes / s.total_bytes if s.total_bytes else 0

    if pct(cur) >= cap_warn > pct(prev):
        ev.append(Event("warning", "capacity", f"{unit} is {pct(cur):.1f}% full (threshold {cap_warn:g}%)"))
    return ev
