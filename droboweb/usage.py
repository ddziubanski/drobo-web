"""Host-reported volume usage: the one write Drobo Dashboard makes during normal running.

The B800i is thin-provisioned, so it can't see how full each volume's filesystem is. The host
that has the volumes mounted tells it, every few seconds, with MODE SELECT(10) to vendor
subpage 0x3A/0x0C. Layout (from a Dashboard 3.5.0 capture, 86-byte parameter list):

    0..3   ignored by the unit (Dashboard leaves uninitialised memory here; we send zeros)
    4      0x01
    5      number of records (max 7)
    6..    records of 11 bytes: u8 volume index, u16 target id, u64 used bytes
    rest   zero padding (Dashboard leaves junk; we send zeros)

Dashboard sends 0 for volumes it doesn't have mounted, which the unit then reports as
"unknown". We do the same.
"""
from __future__ import annotations

import json
import os
import struct
import subprocess
from pathlib import Path

from .scsi import USAGE_CDB, USAGE_LEN, USAGE_MAX_RECORDS

CREATE_NO_WINDOW = 0x08000000

PS_SCRIPT = r"""
$ErrorActionPreference = 'SilentlyContinue'
$out = @()
foreach ($s in Get-IscsiSession) {
  foreach ($d in (Get-Disk -IscsiSession $s)) {
    $used = [int64]0; $mounted = $false
    foreach ($v in (Get-Partition -DiskNumber $d.Number | Get-Volume)) {
      if ($v.Size -gt 0) { $used += [int64]($v.Size - $v.SizeRemaining); $mounted = $true }
    }
    $out += [pscustomobject]@{ target = $s.TargetNodeAddress; disk = $d.Number; used = $used; mounted = $mounted }
  }
}
ConvertTo-Json -Compress -InputObject @($out)
"""


def windows_usage() -> dict[str, int]:
    r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                        "-Command", PS_SCRIPT], capture_output=True, text=True, timeout=60,
                       creationflags=CREATE_NO_WINDOW)
    if r.returncode != 0:
        raise RuntimeError(f"PowerShell failed: {r.stderr.strip()[:300]}")
    return parse_windows_usage(r.stdout)


def parse_windows_usage(text: str) -> dict[str, int]:
    text = text.strip()
    if not text:
        return {}
    data = json.loads(text)
    if isinstance(data, dict):
        data = [data]
    out: dict[str, int] = {}
    for row in data:
        if row.get("mounted") and row.get("target"):
            iqn = row["target"].lower()
            out[iqn] = out.get(iqn, 0) + int(row.get("used") or 0)
    return out


def linux_usage(by_path: Path = Path("/dev/disk/by-path"), mounts: Path = Path("/proc/self/mounts")) -> dict[str, int]:
    """iSCSI disks are linked as ip-<addr>:3260-iscsi-<iqn>-lun-<n>[-partN]."""
    if not by_path.exists():
        return {}
    dev_to_iqn: dict[str, str] = {}
    for link in by_path.iterdir():
        name = link.name
        if "-iscsi-" not in name:
            continue
        iqn = name.split("-iscsi-", 1)[1].rsplit("-lun-", 1)[0]
        dev_to_iqn[os.path.realpath(link)] = iqn.lower()
    out: dict[str, int] = {}
    seen = set()
    for line in mounts.read_text().splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        dev, mnt = os.path.realpath(parts[0]), parts[1].replace("\\040", " ")
        iqn = dev_to_iqn.get(dev)
        if not iqn or dev in seen:
            continue
        seen.add(dev)
        try:
            st = os.statvfs(mnt)
        except OSError:
            continue
        out[iqn] = out.get(iqn, 0) + (st.f_blocks - st.f_bfree) * st.f_frsize
    return out


def host_usage() -> dict[str, int]:
    return windows_usage() if os.name == "nt" else linux_usage()


def dashboard_service_running() -> bool:
    """True if Drobo Dashboard's own service is running (it sends the same report)."""
    if os.name != "nt":
        return False
    try:
        r = subprocess.run(["tasklist", "/FI", "IMAGENAME eq DDService.exe", "/NH"], capture_output=True,
                           text=True, timeout=15, creationflags=CREATE_NO_WINDOW)
        return "ddservice.exe" in r.stdout.lower()
    except Exception:  # noqa: BLE001
        return False


def build_payload(volumes, usage_by_iqn: dict[str, int]) -> tuple[bytes, list[dict]]:
    """Returns (86-byte parameter list, per-volume summary)."""
    vols = sorted(volumes, key=lambda v: v.index)
    if not vols:
        raise ValueError("no volumes reported by the unit")
    if len(vols) > USAGE_MAX_RECORDS:
        raise ValueError(f"{len(vols)} volumes; the report format seen so far holds {USAGE_MAX_RECORDS}")
    buf = bytearray(USAGE_LEN)
    buf[4] = 0x01
    buf[5] = len(vols)
    summary = []
    for i, v in enumerate(vols):
        if not 0 < v.index < 256 or not 0 < v.target_id < 65536:
            raise ValueError(f"volume {v.index}: unexpected index/target id {v.target_id}")
        used = usage_by_iqn.get(v.iqn.lower())
        val = 0 if used is None else max(0, min(int(used), v.size_bytes))
        struct.pack_into(">BHQ", buf, 6 + i * 11, v.index, v.target_id, val)
        summary.append({"index": v.index, "name": v.name, "mounted_here": used is not None, "used_bytes": val})
    return bytes(buf), summary


def send(transport, volumes, usage_by_iqn: dict[str, int]) -> list[dict]:
    payload, summary = build_payload(volumes, usage_by_iqn)
    transport.write(USAGE_CDB, payload)
    return summary
