"""SCSI transports.

DeviceTransport sends commands to a Linux SCSI generic device (/dev/sgN) using the
SG_IO ioctl. It is read-only by construction: every CDB is checked against an
allowlist of query commands before it goes to the kernel, and data only ever flows
from the device to the host.
"""
from __future__ import annotations

import ctypes
import fcntl
import json
import os
from pathlib import Path

SG_IO = 0x2285
SG_DXFER_FROM_DEV = -3
SG_INFO_OK_MASK = 0x1


class ScsiError(Exception):
    pass


class NotAllowed(ScsiError):
    pass


def _check_allowed(cdb: bytes) -> None:
    op = cdb[0]
    if op == 0x12:  # INQUIRY
        return
    if op in (0x1A, 0x5A):  # MODE SENSE(6), MODE SENSE(10)
        return
    if op == 0xEA and cdb[1:4] == b"\x10\x80\x81":  # Drobo vendor read: volume list
        return
    raise NotAllowed(f"refusing to send CDB {cdb.hex()} (not on the read-only allowlist)")


def cdb_key(cdb: bytes) -> str:
    """Stable name for a command, used to store and replay responses."""
    op = cdb[0]
    if op == 0x5A and cdb[2] == 0x3A:
        return f"3a{cdb[3]:02x}"
    if op == 0xEA:
        return f"ea{cdb[4]:02x}"
    if op == 0x12 and cdb[1] & 1:
        return f"vpd{cdb[2]:02x}"
    if op == 0x12:
        return "inquiry"
    if op == 0x1A:
        return f"ms6_{cdb[2]:02x}"
    return cdb.hex()


class Transport:
    name = "transport"

    def read(self, cdb: bytes, alloc: int) -> bytes:  # pragma: no cover - interface
        raise NotImplementedError

    def close(self) -> None:
        pass


class _SgIoHdr(ctypes.Structure):
    _fields_ = [
        ("interface_id", ctypes.c_int),
        ("dxfer_direction", ctypes.c_int),
        ("cmd_len", ctypes.c_ubyte),
        ("mx_sb_len", ctypes.c_ubyte),
        ("iovec_count", ctypes.c_ushort),
        ("dxfer_len", ctypes.c_uint),
        ("dxferp", ctypes.c_void_p),
        ("cmdp", ctypes.c_void_p),
        ("sbp", ctypes.c_void_p),
        ("timeout", ctypes.c_uint),
        ("flags", ctypes.c_uint),
        ("pack_id", ctypes.c_int),
        ("usr_ptr", ctypes.c_void_p),
        ("status", ctypes.c_ubyte),
        ("masked_status", ctypes.c_ubyte),
        ("msg_status", ctypes.c_ubyte),
        ("sb_len_wr", ctypes.c_ubyte),
        ("host_status", ctypes.c_ushort),
        ("driver_status", ctypes.c_ushort),
        ("resid", ctypes.c_int),
        ("duration", ctypes.c_uint),
        ("info", ctypes.c_uint),
    ]


class DeviceTransport(Transport):
    def __init__(self, path: str, timeout_ms: int = 10000):
        self.path = path
        self.name = path
        self.timeout_ms = timeout_ms
        self.fd = os.open(path, os.O_RDWR | os.O_NONBLOCK)

    def read(self, cdb: bytes, alloc: int) -> bytes:
        _check_allowed(cdb)
        cmd = ctypes.create_string_buffer(bytes(cdb), len(cdb))
        buf = ctypes.create_string_buffer(alloc)
        sense = ctypes.create_string_buffer(64)
        hdr = _SgIoHdr()
        hdr.interface_id = ord("S")
        hdr.dxfer_direction = SG_DXFER_FROM_DEV
        hdr.cmd_len = len(cdb)
        hdr.mx_sb_len = len(sense)
        hdr.dxfer_len = alloc
        hdr.dxferp = ctypes.cast(buf, ctypes.c_void_p)
        hdr.cmdp = ctypes.cast(cmd, ctypes.c_void_p)
        hdr.sbp = ctypes.cast(sense, ctypes.c_void_p)
        hdr.timeout = self.timeout_ms
        fcntl.ioctl(self.fd, SG_IO, hdr)
        if (hdr.info & SG_INFO_OK_MASK) != 0 or hdr.status or hdr.host_status or hdr.driver_status:
            raise ScsiError(
                f"{cdb_key(cdb)}: status={hdr.status:#x} host={hdr.host_status:#x} "
                f"driver={hdr.driver_status:#x} sense={sense.raw[:hdr.sb_len_wr].hex()}"
            )
        return buf.raw[: alloc - max(hdr.resid, 0)]

    def close(self) -> None:
        try:
            os.close(self.fd)
        except OSError:
            pass


class ReplayTransport(Transport):
    """Answers commands from a saved fixture (captured from a real unit)."""

    def __init__(self, fixture: str | Path):
        data = json.loads(Path(fixture).read_text())
        self.meta = {k: v for k, v in data.items() if k != "responses"}
        self.responses = {k: bytes.fromhex(v) for k, v in data["responses"].items()}
        self.name = f"replay:{Path(fixture).name}"

    def patch(self, key: str, data: bytes) -> bytes:
        return data

    def read(self, cdb: bytes, alloc: int) -> bytes:
        _check_allowed(cdb)
        key = cdb_key(cdb)
        if key not in self.responses:
            raise ScsiError(f"{key}: not in fixture (check condition)")
        return self.patch(key, self.responses[key])[:alloc]


class RecordingTransport(Transport):
    """Wraps another transport and keeps every response, for `droboweb dump`."""

    def __init__(self, inner: Transport):
        self.inner = inner
        self.name = inner.name
        self.responses: dict[str, bytes] = {}

    def read(self, cdb: bytes, alloc: int) -> bytes:
        data = self.inner.read(cdb, alloc)
        self.responses[cdb_key(cdb)] = data
        return data

    def close(self) -> None:
        self.inner.close()
