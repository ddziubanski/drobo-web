"""Scrub identifying data from raw responses before they are shared.

Replaces the unit serial (in VPD 0x80 and in every target IQN), the unit name and the
volume names. Lengths are preserved so the byte layout stays identical.
"""
from __future__ import annotations

import re

from .protocol import VOL_REC, _s

DEMO_SERIAL = "DEMO00000001"


def _same_len(new: str, old_len: int) -> bytes:
    b = new.encode()[:old_len]
    return b + b"0" * (old_len - len(b))


def redact(responses: dict[str, bytes], unit_name: str = "B800i-Demo",
           volume_names: list[str] | None = None) -> dict[str, bytes]:
    out = {k: bytearray(v) for k, v in responses.items()}

    serial = ""
    if "vpd80" in out and len(out["vpd80"]) > 4:
        v = out["vpd80"]
        serial = _s(bytes(v[4:4 + v[3]]))
        v[4:4 + len(serial)] = _same_len(DEMO_SERIAL, len(serial))

    if serial:
        repl_up = _same_len(DEMO_SERIAL, len(serial))
        for key, buf in out.items():
            for old, new in ((serial.encode(), repl_up), (serial.lower().encode(), repl_up.lower())):
                i = buf.find(old)
                while i >= 0:
                    buf[i:i + len(old)] = new
                    i = buf.find(old, i + len(old))

    # IQNs embed a serial-derived token: iqn.2005-06.com.drobo:b800i.<token>.idN
    iqn_tok = re.compile(rb"(iqn\.[0-9]{4}-[0-9]{2}\.[a-z0-9.-]+:[a-z0-9-]+\.)([a-z0-9]+)(\.id[0-9]+)")
    for buf in out.values():
        for m in list(iqn_tok.finditer(bytes(buf))):
            tok = m.group(2)
            buf[m.start(2):m.end(2)] = _same_len(DEMO_SERIAL.lower(), len(tok))

    if "3a05" in out:
        n = unit_name.encode()[:31]
        out["3a05"][10:42] = n + b"\0" * (32 - len(n))

    names = volume_names or ["Media", "Data", "Apps", "Backups", "Archive", "Scratch", "Projects", "Spare"]
    vol_i = 0
    for key in sorted(k for k in out if k.startswith("ea")):
        buf = out[key]
        if len(buf) < 3:
            continue
        for i in range(buf[1]):
            r = 3 + i * VOL_REC
            if r + VOL_REC > len(buf):
                break
            nm = names[vol_i % len(names)].encode("utf-16-be")[:128]
            buf[r + 0x85:r + 0x105] = nm + b"\0" * (128 - len(nm))
            vol_i += 1

    return {k: bytes(v) for k, v in out.items()}
