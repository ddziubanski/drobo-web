"""Drobo management protocol (vendor mode page 0x3A + vendor opcode 0xEA).

Layouts were worked out from a packet capture of Drobo Dashboard 3.5.0 talking to a
B800i (firmware 3.45) and cross-checked against drobo-utils (GPL, Peter Silva et al.).
See PROTOCOL.md for the field-by-field notes and what is still unconfirmed.
"""
from __future__ import annotations

import struct
import time
from dataclasses import asdict, dataclass, field

from .scsi import ScsiError, Transport

PAGE = 0x3A

LED_COLOURS = ["off", "red", "yellow", "green", "red-green", "red-yellow", "red-off"]

STATUS_BITS = {
    0x0002: "Red alert",
    0x0004: "Yellow alert",
    0x0008: "No disks",
    0x0010: "Bad disk",
    0x0020: "Too many missing disks",
    0x0040: "No redundancy",
    0x0080: "No hot spare",
    0x0100: "No space left",
    0x0200: "Data protection in progress",
    0x0400: "Format in progress",
    0x0800: "Mismatched disks",
    0x1000: "Unknown version",
    0x2000: "New firmware installed",
    0x4000: "New volume available after reboot",
}
# Bits seen set on a healthy B800i; meaning not yet known, so they are not alarms.
BENIGN_UNKNOWN_BITS = 0x8000 | 0x10000
ALARM_BITS = 0x0002 | 0x0004 | 0x0008 | 0x0010 | 0x0020 | 0x0040 | 0x0100 | 0x0800

FEATURES = [
    (0x0001, "NO_AUTO_REBOOT"), (0x0002, "NO_FAT32_FORMAT"), (0x0004, "USED_CAPACITY_FROM_HOST"),
    (0x0008, "DISKPACKSTATUS"), (0x0010, "ENCRYPT_NOHEADER"), (0x0020, "CMD_STATUS_QUERIABLE"),
    (0x0040, "VARIABLE_LUN_SIZE_1_16"), (0x0080, "PARTITION_LUN_GPT_MBR"),
    (0x0100, "FAT32_FORMAT_VOLNAME"), (0x0200, "SUPPORTS_DROBOSHARE"),
    (0x0400, "SUPPORTS_NEW_LUNINFO2"), (0x1000, "LUN_MANAGEMENT"), (0x4000, "SUPPORTS_OPTIONS2"),
    (0x8000, "SUPPORTS_SHUTDOWN"), (0x20000, "SUPPORTS_ISCSI"),
    (0x40000000, "SUPPORTS_VOLUME_RENAME"), (0x80000000, "SUPPORTS_SINGLE_LUN_FORMAT"),
]

UNKNOWN_U64 = 0xFFFFFFFFFFFFFFFF


def _s(b: bytes) -> str:
    return b.split(b"\0", 1)[0].decode("ascii", "replace").strip()


def _ip(n: int) -> str:
    return ".".join(str((n >> s) & 0xFF) for s in (24, 16, 8, 0))


def _prefix(mask: int) -> int:
    return bin(mask).count("1")


# ---------------------------------------------------------------- commands

def mode_sense_cdb(sub: int, alloc: int) -> bytes:
    return bytes([0x5A, 0x08, PAGE, sub, 0, 0, 0, (alloc >> 8) & 0xFF, alloc & 0xFF, 0])


def volume_list_cdb(start: int, alloc: int = 0x217C) -> bytes:
    return bytes([0xEA, 0x10, 0x80, 0x81, start, 0, (alloc >> 8) & 0xFF, alloc & 0xFF]) + bytes(8)


SERIAL_CDB = bytes([0x12, 0x01, 0x80, 0x00, 0x1C, 0x00])

# allocation lengths Dashboard uses for each subpage
ALLOC = {0x01: 0x2C, 0x02: 0x34, 0x03: 0x370, 0x05: 0x3A, 0x06: 0x06, 0x08: 0x14A,
         0x09: 0x20, 0x30: 0x1D, 0x31: 0x210}


def subpage(t: Transport, sub: int) -> bytes:
    data = t.read(mode_sense_cdb(sub, ALLOC.get(sub, 0x200)), ALLOC.get(sub, 0x200))
    if len(data) < 4 or data[0] & 0x3F != PAGE or data[1] != sub:
        raise ScsiError(f"subpage {sub:#04x}: unexpected header {data[:4].hex()}")
    return data[4:]


# ---------------------------------------------------------------- data model

@dataclass
class Bay:
    slot: int
    present: bool
    led: str
    model: str
    physical_bytes: int
    managed_bytes: int
    temperature_c: int | None
    raw_tail: str
    led_code: int = 0           # the raw light byte from the unit


@dataclass
class Volume:
    index: int
    target_id: int
    iqn: str
    name: str
    size_bytes: int
    used_bytes: int | None
    flags: str


@dataclass
class Port:
    port: int
    ip: str
    netmask: str
    prefix: int
    gateway: str
    mtu: int


@dataclass
class Snapshot:
    taken_at: float
    source: str
    serial: str = ""
    name: str = ""
    clock_utc: int = 0
    tz_offset: int = 0
    protocol: str = ""
    firmware: str = ""
    firmware_build_date: str = ""
    arch: str = ""
    features: list[str] = field(default_factory=list)
    slot_count: int = 0
    max_volume_bytes: int = 0
    free_bytes: int = 0
    used_bytes: int = 0
    total_bytes: int = 0
    unprotected_bytes: int = 0
    status_bits: int = 0
    status: list[str] = field(default_factory=list)
    relayout_remaining: int = 0
    options_raw: str = ""
    bays: list[Bay] = field(default_factory=list)
    volumes: list[Volume] = field(default_factory=list)
    ports: list[Port] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    identifying: bool = False   # Blink Lights is running: the lights don't show drive state

    @property
    def health(self) -> str:
        if self.status_bits & 0x0002 or any(b.led.startswith("red") for b in self.bays):
            return "critical"
        if self.status_bits & ALARM_BITS or any(b.led == "yellow" for b in self.bays):
            return "warning"
        if self.status_bits & 0x0200:
            return "rebuilding"
        return "ok"

    def to_dict(self) -> dict:
        d = asdict(self)
        d["health"] = self.health
        d["used_pct"] = round(100 * self.used_bytes / self.total_bytes, 1) if self.total_bytes else None
        return d


# ---------------------------------------------------------------- parsers

def parse_config(p: bytes) -> tuple[int, int]:
    slots = p[0]
    max_lun_sectors = int.from_bytes(p[4:11], "big")  # 0x07ffffffff on B800i = 16 TiB
    return slots, max_lun_sectors * 512


def parse_capacity(p: bytes) -> tuple[int, int, int, int]:
    return struct.unpack_from(">QQQQ", p, 0)


SLOT_REC = struct.Struct(">HBQQB32s16s4s")  # 72 bytes


def parse_slots(p: bytes) -> list[Bay]:
    count = p[0]
    bays = []
    for i in range(count):
        off = 1 + i * SLOT_REC.size
        if off + SLOT_REC.size > len(p):
            break
        _, slot, phys, managed, led, model, _short, tail = SLOT_REC.unpack_from(p, off)
        empty = bool(led & 0x80)  # 0x80 empty; during Blink Lights an empty bay reads 0x85
        present = not empty and phys > 0
        temp = tail[2] if present and 5 <= tail[2] <= 90 else None
        bays.append(Bay(
            slot=slot, present=present,
            led="empty" if empty else LED_COLOURS[led & 0x0F] if (led & 0x0F) < len(LED_COLOURS) else f"0x{led:02x}",
            model=_s(model), physical_bytes=phys, managed_bytes=managed,
            temperature_c=temp, raw_tail=tail.hex(), led_code=led,
        ))
    return bays


IDENTIFY_LED = 0x05
ALERT_BITS = 0x0002 | 0x0004 | 0x0010 | 0x0020  # red alert, yellow alert, bad disk, too many missing


def is_identifying(bays: list[Bay], status_bits: int) -> bool:
    """True while Blink Lights runs. The unit then reports every bay, empty ones included, as
    flashing red/yellow (0x05, or 0x85 for an empty bay), while its status flags stay clear.
    A real fault never lights an empty bay or every bay at once without raising an alert bit."""
    return (len(bays) >= 2 and all((b.led_code & 0x0F) == IDENTIFY_LED for b in bays)
            and not status_bits & ALERT_BITS)


def parse_settings(p: bytes) -> tuple[int, int, str]:
    utc, off = struct.unpack_from(">LH", p, 0)
    name = _s(p[6:38])
    # offset is stored byte-swapped (0x0800 == 8 hours west of UTC)
    tz = (off >> 8) if off & 0xFF == 0 else off
    return utc, tz, name


def parse_firmware(p: bytes) -> dict:
    major, minor, build, _um, _un = struct.unpack_from(">BBHBB", p, 0)
    date, arch, ver = _s(p[6:38]), _s(p[38:54]), _s(p[54:70])
    feats = 0
    if len(p) >= 70 + 116:
        feats = struct.unpack_from(">L", p, 70 + 112)[0]
    names = [n for bit, n in FEATURES if feats & bit]
    return dict(firmware=f"{major}.{minor}.{build}", firmware_build_date=date,
                arch=f"{arch} {ver}".strip(), features=names, feature_bits=feats)


def parse_status(p: bytes) -> tuple[int, int, list[str]]:
    bits, relayout = struct.unpack_from(">LL", p, 0)
    names = [n for b, n in STATUS_BITS.items() if bits & b]
    leftovers = bits & ~sum(STATUS_BITS) & ~BENIGN_UNKNOWN_BITS
    if leftovers:
        names.append(f"Unknown flags 0x{leftovers:x}")
    return bits, relayout, names


def parse_network(p: bytes, max_ports: int = 4) -> list[Port]:
    ports = []
    off = 10  # payload offset of the first port record (14 bytes each)
    for i in range(max_ports):
        if off + 14 > len(p):
            break
        ip, mask, gw, mtu = struct.unpack_from(">LLLH", p, off)
        if ip == 0:
            break
        ports.append(Port(port=i + 1, ip=_ip(ip), netmask=_ip(mask), prefix=_prefix(mask),
                          gateway=_ip(gw), mtu=mtu))
        off += 14
    return ports


VOL_REC = 0x11D


def parse_volume_page(p: bytes) -> tuple[list[Volume], bool]:
    """Returns (volumes, is_last_page)."""
    if len(p) < 3:
        return [], True
    count, last = p[1], bool(p[2])
    vols = []
    for i in range(count):
        r = 3 + i * VOL_REC
        if r + VOL_REC > len(p):
            break
        rec = p[r:r + VOL_REC]
        idx = rec[0]
        target_id = struct.unpack_from(">L", rec, 1)[0]
        iqn = _s(rec[5:0x85])
        name = rec[0x85:0x105].decode("utf-16-be", "replace").split("\0", 1)[0].strip()
        size, used = struct.unpack_from(">QQ", rec, 0x109)
        vols.append(Volume(index=idx, target_id=target_id, iqn=iqn, name=name, size_bytes=size,
                           used_bytes=None if used == UNKNOWN_U64 else used,
                           flags=rec[0x107:0x109].hex()))
    return vols, last


def read_volumes(t: Transport, max_pages: int = 16) -> list[Volume]:
    vols: list[Volume] = []
    start = 1
    for _ in range(max_pages):
        page, last = parse_volume_page(t.read(volume_list_cdb(start), 0x217C))
        if not page:
            break
        vols.extend(page)
        if last:
            break
        start = page[-1].index + 1
    return vols


def read_serial(t: Transport) -> str:
    d = t.read(SERIAL_CDB, 0x1C)
    return _s(d[4:4 + d[3]]) if len(d) > 4 else ""


def read_snapshot(t: Transport) -> Snapshot:
    """Polls everything Dashboard polls. Individual failures are recorded, not fatal,
    except when the unit doesn't answer the vendor page at all."""
    s = Snapshot(taken_at=time.time(), source=t.name)
    proto = subpage(t, 0x06)  # raises if this isn't a Drobo
    s.protocol = f"{proto[0]}.{proto[1]}"

    def attempt(label, fn):
        try:
            fn()
        except Exception as e:  # noqa: BLE001 - keep polling the rest
            s.errors.append(f"{label}: {e}")

    def cfg():
        s.slot_count, s.max_volume_bytes = parse_config(subpage(t, 0x01))

    def cap():
        s.free_bytes, s.used_bytes, s.total_bytes, s.unprotected_bytes = parse_capacity(subpage(t, 0x02))

    def slots():
        s.bays = parse_slots(subpage(t, 0x03))

    def settings():
        s.clock_utc, s.tz_offset, s.name = parse_settings(subpage(t, 0x05))

    def fw():
        f = parse_firmware(subpage(t, 0x08))
        s.firmware, s.firmware_build_date, s.arch, s.features = (
            f["firmware"], f["firmware_build_date"], f["arch"], f["features"])

    def status():
        s.status_bits, s.relayout_remaining, s.status = parse_status(subpage(t, 0x09))

    def options():
        s.options_raw = subpage(t, 0x30).hex()

    def net():
        s.ports = parse_network(subpage(t, 0x31))

    def vols():
        s.volumes = read_volumes(t)

    def serial():
        s.serial = read_serial(t)

    for label, fn in [("serial", serial), ("config", cfg), ("capacity", cap), ("slots", slots),
                      ("settings", settings), ("firmware", fw), ("status", status),
                      ("options", options), ("network", net), ("volumes", vols)]:
        attempt(label, fn)
    if is_identifying(s.bays, s.status_bits):
        s.identifying = True
        for b in s.bays:
            if b.present:
                b.led = "identify"  # the real light is hidden; the monitor fills in the last one seen
    return s
