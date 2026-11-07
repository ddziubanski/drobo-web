"""Prometheus text exposition (no client library needed)."""
from __future__ import annotations

HEALTH = {"ok": 0, "rebuilding": 1, "warning": 2, "critical": 3}
LED = {"off": 0, "green": 1, "yellow": 2, "red": 3, "red-green": 4, "red-yellow": 5, "red-off": 6, "identify": 7, "empty": -1}


def _esc(v: str) -> str:
    return str(v).replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


def render(mon) -> str:
    lines: list[str] = []

    def m(name, help_, typ, samples):
        lines.append(f"# HELP drobo_{name} {help_}")
        lines.append(f"# TYPE drobo_{name} {typ}")
        for labels, value in samples:
            lab = ",".join(f'{k}="{_esc(v)}"' for k, v in labels.items())
            lines.append(f"drobo_{name}{{{lab}}} {value}" if lab else f"drobo_{name} {value}")

    m("up", "1 if the last poll succeeded", "gauge", [({}, int(mon.reachable))])
    m("poll_failures_total", "Failed polls since start", "counter", [({}, mon.failures)])
    s = mon.snapshot
    if s is None:
        return "\n".join(lines) + "\n"
    u = {"unit": s.name, "serial": s.serial}
    m("info", "Unit information", "gauge", [({**u, "firmware": s.firmware, "protocol": s.protocol}, 1)])
    m("health", "0 ok, 1 rebuilding, 2 warning, 3 critical", "gauge", [(u, HEALTH.get(s.health, 3))])
    m("status_bits", "Raw unit status flags", "gauge", [(u, s.status_bits)])
    m("relayout_remaining", "Blocks left to re-protect (0 when idle)", "gauge", [(u, s.relayout_remaining)])
    m("capacity_total_bytes", "Protected capacity", "gauge", [(u, s.total_bytes)])
    m("capacity_used_bytes", "Used capacity", "gauge", [(u, s.used_bytes)])
    m("capacity_free_bytes", "Free capacity", "gauge", [(u, s.free_bytes)])
    m("capacity_unprotected_bytes", "Unprotected capacity", "gauge", [(u, s.unprotected_bytes)])
    bays = [b for b in s.bays]
    m("bay_present", "1 if a drive is in the bay", "gauge",
      [({**u, "bay": b.slot + 1}, int(b.present)) for b in bays])
    m("identifying", "1 while Blink Lights is flashing the front panel", "gauge", [(u, int(s.identifying))])
    m("bay_led", "Bay light: 1 green, 2 yellow, 3 red, 4-6 flashing red, 7 blinking (identify, "
      "last light unknown), -1 empty", "gauge",
      [({**u, "bay": b.slot + 1}, LED.get(b.led, 0)) for b in bays])
    m("drive_size_bytes", "Drive physical size", "gauge",
      [({**u, "bay": b.slot + 1, "model": b.model}, b.physical_bytes) for b in bays if b.present])
    m("drive_temperature_celsius", "Drive temperature as reported by the Drobo", "gauge",
      [({**u, "bay": b.slot + 1, "model": b.model}, b.temperature_c) for b in bays if b.temperature_c is not None])
    m("volume_size_bytes", "Volume (LUN) size", "gauge",
      [({**u, "volume": v.name, "index": v.index}, v.size_bytes) for v in s.volumes])
    m("volume_used_bytes", "Volume used bytes as reported by its host", "gauge",
      [({**u, "volume": v.name, "index": v.index}, v.used_bytes) for v in s.volumes if v.used_bytes is not None])
    return "\n".join(lines) + "\n"
