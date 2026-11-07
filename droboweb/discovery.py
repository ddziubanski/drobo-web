"""Find Drobo management devices among the host's SCSI generic devices."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from .protocol import subpage
from .scsi import DeviceTransport

SYS_SG = Path("/sys/class/scsi_generic")
VENDOR_HINTS = ("drobo", "trusted", "datarobo")


@dataclass
class Candidate:
    sg: str
    vendor: str
    model: str
    rev: str
    iscsi_target: str
    block: str
    likely: bool


def _read(p: Path) -> str:
    try:
        return p.read_text().strip()
    except OSError:
        return ""


def candidates(root: Path = SYS_SG) -> list[Candidate]:
    out = []
    if not root.exists():
        return out
    for sg in sorted(root.iterdir(), key=lambda p: int(p.name[2:]) if p.name[2:].isdigit() else 0):
        dev = sg / "device"
        vendor, model = _read(dev / "vendor"), _read(dev / "model")
        real = os.path.realpath(dev)
        target = ""
        for part in real.split("/"):
            if part.startswith("session"):
                target = _read(Path("/sys/class/iscsi_session") / part / "targetname")
        blk = dev / "block"
        block = ",".join(sorted(p.name for p in blk.iterdir())) if blk.exists() else ""
        likely = any(h in f"{vendor} {model} {target}".lower() for h in VENDOR_HINTS)
        out.append(Candidate(f"/dev/{sg.name}", vendor, model, _read(dev / "rev"), target, block, likely))
    return out


def probe(path: str) -> str | None:
    """Returns the protocol version string if the device answers the Drobo vendor page."""
    try:
        t = DeviceTransport(path, timeout_ms=5000)
    except OSError:
        return None
    try:
        p = subpage(t, 0x06)
        return f"{p[0]}.{p[1]}"
    except Exception:  # noqa: BLE001
        return None
    finally:
        t.close()


def find_drobo() -> str | None:
    cands = candidates()
    # Likely Drobo devices first, then anything else attached over iSCSI.
    ordered = [c for c in cands if c.likely] + [c for c in cands if not c.likely and c.iscsi_target]
    for c in ordered:
        if probe(c.sg):
            return c.sg
    return None
