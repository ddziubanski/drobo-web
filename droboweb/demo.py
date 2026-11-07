"""Demo mode: replays a real capture but animates it so charts and alerts have
something to show. Bytes are patched in place, so the real parsers do all the work.

Scenarios:
  healthy  - temperatures drift, used capacity creeps up
  failing  - like healthy, then after ~2 minutes bay 3 goes red, redundancy is lost
             and data protection (relayout) starts counting down
"""
from __future__ import annotations

import math
import struct
import time
from importlib import resources

from .scsi import BLINK_PREFIX, NET_CDB, ReplayTransport

DEFAULT_FIXTURE = resources.files("droboweb") / "fixtures" / "b800i-fw3.45.json"


class DemoTransport(ReplayTransport):
    def __init__(self, fixture=None, scenario: str = "healthy", speed: float = 1.0):
        super().__init__(fixture or DEFAULT_FIXTURE)
        self.name = f"demo:{scenario}"
        self.scenario = scenario
        self.speed = speed
        self.t0 = time.time()
        self.blink_until = 0.0

    def write(self, cdb: bytes, data: bytes) -> None:
        super().write(cdb, data)
        if bytes(cdb[:4]) == BLINK_PREFIX:
            self.blink_until = time.time() + 10  # a real B800i flashes for about 10 s
        elif bytes(cdb) == NET_CDB:
            # the unit takes the new page; reads return it with the read-side header
            self.responses["3a31"] = self.responses["3a31"][:4] + bytes(data[4:])

    def _t(self) -> float:
        return (time.time() - self.t0) * self.speed

    def _failing(self) -> float | None:
        """Seconds since the simulated failure, or None."""
        if self.scenario != "failing":
            return None
        t = self._t() - 120
        return t if t >= 0 else None

    def patch(self, key: str, data: bytes) -> bytes:
        b = bytearray(data)
        t = self._t()
        if key == "3a03":
            for i in range(b[4]):
                off = 4 + 1 + i * 72
                if b[off + 19] == 0x80:
                    continue
                base = b[off + 70]
                b[off + 70] = max(20, min(70, base + round(2 * math.sin(t / 300 + i))))
            f = self._failing()
            if f is not None:
                off = 4 + 1 + 2 * 72
                b[off + 19] = 0x01 if int(t) % 2 else 0x04  # red, flashing red/green
            if time.time() < self.blink_until:
                # what a real B800i reports during Blink Lights: every bay 0x05, empty ones 0x85
                for i in range(b[4]):
                    off = 4 + 1 + i * 72
                    b[off + 19] = (b[off + 19] & 0x80) | 0x05
        elif key == "3a02":
            free, used, total, unprot = struct.unpack_from(">QQQQ", b, 4)
            grow = int(t * 50_000_000)  # ~50 MB per second of demo time
            grow = min(grow, free - 1)
            struct.pack_into(">QQ", b, 4, free - grow, used + grow)
        elif key == "3a05":
            # keep the demo unit's clock current (it stores local time, UTC-offset hours west)
            tz = b[8] if b[9] == 0 else struct.unpack_from(">H", b, 8)[0]
            struct.pack_into(">L", b, 4, int(time.time()) - tz * 3600)
        elif key == "3a09":
            f = self._failing()
            if f is not None:
                bits, _ = struct.unpack_from(">LL", b, 4)
                remaining = max(0, int(5_000_000 - f * 10_000))
                bits |= 0x0040 | 0x0004 | (0x0200 if remaining else 0)
                struct.pack_into(">LL", b, 4, bits, remaining)
        return bytes(b)


def backfill(store, snap, days: float = 7, step: float = 600) -> None:
    """Seed a demo database with plausible history ending at `snap`."""
    import copy
    import random
    rnd = random.Random(42)
    now = snap.taken_at
    n = int(days * 86400 / step)
    for i in range(n, 0, -1):
        s = copy.deepcopy(snap)
        t = now - i * step
        s.taken_at = t
        delta = int(i * step / 86400 * 35e9 + rnd.uniform(-2e9, 2e9))  # ~35 GB/day growth
        s.used_bytes = max(0, snap.used_bytes - delta)
        s.free_bytes = snap.total_bytes - s.used_bytes
        hour = (t / 3600) % 24
        for b in s.bays:
            if b.temperature_c is not None:
                b.temperature_c = round(b.temperature_c - 2 + 3 * math.sin((hour - 9) / 24 * 2 * math.pi)
                                        + rnd.uniform(-0.6, 0.6))
        store.add_sample(s)
