"""Changing the Drobo's iSCSI port settings (IP, netmask, gateway, MTU).

Drobo Dashboard does it with MODE SELECT(10) to vendor subpage 0x3A/0x31: it reads the network
page, changes one field, and writes the whole page back (512 bytes). drobo-web does the same,
starting from a fresh read of the page, and changes one port per write so the unit can still be
reached through the other port if something goes wrong.

Page layout (read and write share it; only the header differs):

    0..3    header: read 7A 31 FC 01, write 7A 31 01 FC (the length is byte-swapped in the read)
    4..10   zero
    11      flags (bit 0x10 set once an admin password exists)
    12..13  record size, 0x000E
    14..    one 14-byte record per port: u32 ip, u32 netmask, u32 gateway, u16 mtu; a zero ip ends it
    rest    zero, to 512 bytes
"""
from __future__ import annotations

import ipaddress
import json
import os
import struct
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .scsi import NET_HEADER, NET_LEN, NET_REC, NET_REC_OFF

MTU_MIN, MTU_MAX = 1500, 9000
MAX_PORTS = 4


class NetError(ValueError):
    def __init__(self, errors: dict[str, str]):
        super().__init__("; ".join(f"{k}: {v}" for k, v in errors.items()))
        self.errors = errors


@dataclass
class PortConfig:
    ip: str
    netmask: str
    gateway: str
    mtu: int

    def as_dict(self) -> dict:
        return {"ip": self.ip, "netmask": self.netmask, "gateway": self.gateway, "mtu": self.mtu}


def read_records(payload: bytes) -> list[PortConfig]:
    """Port records from a network page payload (the 508 bytes after the 4-byte header)."""
    out = []
    off = NET_REC_OFF - 4
    while len(out) < MAX_PORTS and off + NET_REC <= len(payload):
        ip, mask, gw, mtu = struct.unpack_from(">LLLH", payload, off)
        if ip == 0:
            break
        out.append(PortConfig(str(ipaddress.IPv4Address(ip)), str(ipaddress.IPv4Address(mask)),
                              str(ipaddress.IPv4Address(gw)), mtu))
        off += NET_REC
    return out


def validate(port: int, new: dict, current: list[PortConfig]) -> PortConfig:
    """Check one port's new settings against the unit's other ports. Raises NetError."""
    errors: dict[str, str] = {}
    if not 1 <= port <= len(current):
        raise NetError({"port": f"must be 1 to {len(current)}"})

    def addr(key: str) -> ipaddress.IPv4Address | None:
        v = new.get(key)
        try:
            return ipaddress.IPv4Address(str(v).strip())
        except (ipaddress.AddressValueError, ValueError):
            errors[key] = "isn't an IPv4 address"
            return None

    ip, mask, gw = addr("ip"), addr("netmask"), addr("gateway")
    raw = new.get("mtu")
    if isinstance(raw, int) and not isinstance(raw, bool):
        mtu = raw
    elif isinstance(raw, str) and raw.strip().isdigit():
        mtu = int(raw.strip())
    else:
        errors["mtu"] = "must be a whole number"
        mtu = 0
    if mtu and not MTU_MIN <= mtu <= MTU_MAX:
        errors["mtu"] = f"must be between {MTU_MIN} and {MTU_MAX}"

    net = None
    if mask is not None:
        try:
            net = ipaddress.IPv4Network(f"0.0.0.0/{mask}")
        except ValueError:
            errors["netmask"] = "isn't a valid subnet mask"
        else:
            if not 8 <= net.prefixlen <= 30:
                errors["netmask"] = "must be between /8 (255.0.0.0) and /30 (255.255.255.252)"
                net = None
    if ip is not None:
        if ip.is_multicast or ip.is_loopback or ip.is_unspecified or ip.is_reserved:
            errors["ip"] = "can't be used for a port"
        elif net is not None:
            subnet = ipaddress.IPv4Network(f"{ip}/{net.prefixlen}", strict=False)
            if ip in (subnet.network_address, subnet.broadcast_address):
                errors["ip"] = f"is the network or broadcast address of {subnet}"
            if gw is not None and gw != ipaddress.IPv4Address(0):
                if gw not in subnet:
                    errors["gateway"] = f"must be inside {subnet} (or 0.0.0.0 for none)"
                elif gw == ip:
                    errors["gateway"] = "can't be the port's own address"
        for i, other in enumerate(current, start=1):
            if i != port and other.ip == str(ip):
                errors["ip"] = f"is already used by port {i}"
    if errors:
        raise NetError(errors)
    return PortConfig(str(ip), str(mask), str(gw), mtu)


def build_write(payload: bytes, port: int, cfg: PortConfig) -> bytes:
    """The 512-byte MODE SELECT parameter list: the page as read, with one port's record replaced."""
    if len(payload) != NET_LEN - 4:
        raise ValueError(f"network page is {len(payload) + 4} bytes, expected {NET_LEN}")
    if payload[0:7] != bytes(7) or payload[8:10] != b"\x00\x0e":
        raise ValueError("network page doesn't have the layout drobo-web knows; not changing it")
    if not 1 <= port <= len(read_records(payload)):
        raise ValueError(f"the unit has no port {port}")
    buf = bytearray(NET_HEADER + payload)
    struct.pack_into(">LLLH", buf, NET_REC_OFF + (port - 1) * NET_REC,
                     int(ipaddress.IPv4Address(cfg.ip)), int(ipaddress.IPv4Address(cfg.netmask)),
                     int(ipaddress.IPv4Address(cfg.gateway)), cfg.mtu)
    return bytes(buf)


# ------------------------------------------------------------------ which ports this host uses

PS_CONNECTIONS = r"""
$ErrorActionPreference = 'SilentlyContinue'
$out = @()
foreach ($s in Get-IscsiSession) {
  foreach ($c in (Get-IscsiConnection -IscsiSession $s)) {
    $out += [pscustomobject]@{ target = $s.TargetNodeAddress; address = $c.TargetAddress }
  }
}
ConvertTo-Json -Compress -InputObject @($out)
"""


def _linux_connections(sysfs: Path = Path("/sys/class")) -> list[tuple[str, str]]:
    out = []
    sessions = sysfs / "iscsi_session"
    if not sessions.exists():
        raise FileNotFoundError(sessions)  # no iSCSI in this kernel or container: unknown
    for s in sessions.iterdir():
        try:
            target = (s / "targetname").read_text().strip()
        except OSError:
            continue
        sid = s.name.removeprefix("session")
        for c in (sysfs / "iscsi_connection").glob(f"connection{sid}:*"):
            for name in ("persistent_address", "address"):
                try:
                    addr = (c / name).read_text().strip()
                except OSError:
                    continue
                if addr:
                    out.append((target, addr))
                    break
    return out


def _windows_connections() -> list[tuple[str, str]]:
    r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                        "-Command", PS_CONNECTIONS], capture_output=True, text=True, timeout=60,
                       creationflags=0x08000000)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip()[:300] or "PowerShell failed")
    text = r.stdout.strip()
    rows = json.loads(text) if text else []
    if isinstance(rows, dict):
        rows = [rows]
    return [(str(x.get("target") or ""), str(x.get("address") or "")) for x in rows]


def ports_in_use(port_ips: list[str]) -> set[str] | None:
    """The unit's port addresses that this computer has iSCSI sessions to, or None if unknown."""
    try:
        conns = _windows_connections() if os.name == "nt" else _linux_connections()
    except Exception:  # noqa: BLE001
        return None
    ips = set(port_ips)
    return {addr for _target, addr in conns if addr in ips}
