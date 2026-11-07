"""Windows transport: SCSI pass-through to \\\\.\\PhysicalDriveN.

This is the same mechanism Drobo Dashboard uses (IOCTL_SCSI_PASS_THROUGH_DIRECT). Opening a
physical drive for pass-through needs Administrator rights; the packaged .exe asks for them.
The structure layout is defined with fixed-width types so it can be unit-tested anywhere.
"""
from __future__ import annotations

import ctypes
from ctypes import c_ubyte, c_uint32, c_ushort, c_void_p

from .scsi import ScsiError, Transport, _check_allowed, _check_write_allowed, cdb_key

IOCTL_SCSI_PASS_THROUGH_DIRECT = 0x4D014
SCSI_IOCTL_DATA_OUT = 0
SCSI_IOCTL_DATA_IN = 1
GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
FILE_SHARE_READ = 0x1
FILE_SHARE_WRITE = 0x2
OPEN_EXISTING = 3
INVALID_HANDLE_VALUE = c_void_p(-1).value


class SCSI_PASS_THROUGH_DIRECT(ctypes.Structure):
    _fields_ = [
        ("Length", c_ushort),
        ("ScsiStatus", c_ubyte),
        ("PathId", c_ubyte),
        ("TargetId", c_ubyte),
        ("Lun", c_ubyte),
        ("CdbLength", c_ubyte),
        ("SenseInfoLength", c_ubyte),
        ("DataIn", c_ubyte),
        ("DataTransferLength", c_uint32),
        ("TimeOutValue", c_uint32),
        ("DataBuffer", c_void_p),
        ("SenseInfoOffset", c_uint32),
        ("Cdb", c_ubyte * 16),
    ]


class SPTD_WITH_SENSE(ctypes.Structure):
    _fields_ = [
        ("sptd", SCSI_PASS_THROUGH_DIRECT),
        ("Filler", c_uint32),
        ("Sense", c_ubyte * 32),
    ]


def build_sptd(cdb: bytes, buf, length: int, data_in: bool, timeout_s: int = 10) -> SPTD_WITH_SENSE:
    if len(cdb) > 16:
        raise ValueError("CDB longer than 16 bytes")
    s = SPTD_WITH_SENSE()
    s.sptd.Length = ctypes.sizeof(SCSI_PASS_THROUGH_DIRECT)
    s.sptd.CdbLength = len(cdb)
    s.sptd.SenseInfoLength = len(s.Sense)
    s.sptd.DataIn = SCSI_IOCTL_DATA_IN if data_in else SCSI_IOCTL_DATA_OUT
    s.sptd.DataTransferLength = length
    s.sptd.TimeOutValue = timeout_s
    s.sptd.DataBuffer = ctypes.cast(buf, c_void_p)
    s.sptd.SenseInfoOffset = SPTD_WITH_SENSE.Sense.offset
    for i, b in enumerate(cdb):
        s.sptd.Cdb[i] = b
    return s


class WinDeviceTransport(Transport):
    def __init__(self, path: str, timeout_s: int = 10):
        from ctypes import wintypes
        self.k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self.k32.CreateFileW.restype = wintypes.HANDLE
        self.k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, c_void_p,
                                         wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        self.k32.DeviceIoControl.argtypes = [wintypes.HANDLE, wintypes.DWORD, c_void_p, wintypes.DWORD,
                                             c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), c_void_p]
        self.k32.CloseHandle.argtypes = [wintypes.HANDLE]
        if not path.startswith("\\\\.\\"):
            path = "\\\\.\\" + path  # accept "PhysicalDrive3"
        self.path = path
        self.name = path
        self.timeout_s = timeout_s
        h = self.k32.CreateFileW(path, GENERIC_READ | GENERIC_WRITE, FILE_SHARE_READ | FILE_SHARE_WRITE,
                                 None, OPEN_EXISTING, 0, None)
        if h in (None, INVALID_HANDLE_VALUE):
            err = ctypes.get_last_error()
            hint = " (run as Administrator)" if err == 5 else ""
            raise OSError(err, f"cannot open {path}: Windows error {err}{hint}")
        self.handle = h

    def _io(self, cdb: bytes, buf, length: int, data_in: bool) -> int:
        from ctypes import wintypes
        s = build_sptd(cdb, buf, length, data_in, self.timeout_s)
        returned = wintypes.DWORD(0)
        ok = self.k32.DeviceIoControl(self.handle, IOCTL_SCSI_PASS_THROUGH_DIRECT,
                                      ctypes.byref(s), ctypes.sizeof(s), ctypes.byref(s), ctypes.sizeof(s),
                                      ctypes.byref(returned), None)
        if not ok:
            raise ScsiError(f"{cdb_key(cdb)}: DeviceIoControl failed, Windows error {ctypes.get_last_error()}")
        if s.sptd.ScsiStatus:
            raise ScsiError(f"{cdb_key(cdb)}: SCSI status {s.sptd.ScsiStatus:#x} sense={bytes(s.Sense).hex()}")
        return s.sptd.DataTransferLength

    def read(self, cdb: bytes, alloc: int) -> bytes:
        _check_allowed(cdb)
        buf = ctypes.create_string_buffer(alloc)
        n = self._io(cdb, buf, alloc, True)
        return buf.raw[:min(n, alloc)]

    def write(self, cdb: bytes, data: bytes) -> None:
        _check_write_allowed(cdb, data)
        buf = ctypes.create_string_buffer(bytes(data), len(data))
        self._io(cdb, buf, len(data), False)

    def close(self) -> None:
        if getattr(self, "handle", None):
            self.k32.CloseHandle(self.handle)
            self.handle = None


INQUIRY_CDB = bytes([0x12, 0, 0, 0, 0x60, 0])


def candidates(max_drives: int = 64) -> list[dict]:
    """Every \\\\.\\PhysicalDriveN that opens, with its INQUIRY vendor/product."""
    out = []
    for n in range(max_drives):
        path = f"\\\\.\\PhysicalDrive{n}"
        try:
            t = WinDeviceTransport(path, timeout_s=5)
        except OSError as e:
            if e.errno == 5:
                out.append({"path": path, "vendor": "", "product": "", "error": "access denied"})
            continue
        try:
            d = t.read(INQUIRY_CDB, 0x60)
            out.append({"path": path, "vendor": d[8:16].decode("ascii", "replace").strip(),
                        "product": d[16:32].decode("ascii", "replace").strip(), "error": ""})
        except Exception as e:  # noqa: BLE001
            out.append({"path": path, "vendor": "", "product": "", "error": str(e)})
        finally:
            t.close()
    return out


def find_drobo() -> str | None:
    from .discovery import VENDOR_HINTS
    from .protocol import subpage
    cands = candidates()
    likely = [c for c in cands if any(h in f"{c['vendor']} {c['product']}".lower() for h in VENDOR_HINTS)]
    for c in likely + [c for c in cands if c not in likely and not c["error"]]:
        try:
            t = WinDeviceTransport(c["path"], timeout_s=5)
        except OSError:
            continue
        try:
            subpage(t, 0x06)
            return c["path"]
        except Exception:  # noqa: BLE001
            pass
        finally:
            t.close()
    return None
