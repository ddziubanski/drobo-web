# Drobo B800i management protocol

How Drobo Dashboard 3.5.0 manages a Drobo B800i (firmware 3.45.10935, ArmMarvell,
protocol 0.11), worked out from a packet capture. The capture is in
[`droboweb/fixtures/b800i-fw3.45.json`](droboweb/fixtures/b800i-fw3.45.json), redacted
(serial, IQN token, unit name and volume names replaced). drobo-utils by Peter Silva et al.
documented the same vendor page for earlier models and was the starting point.

Status: **reads are confirmed against live data. Writes are not mapped yet** (see "Open questions").

## Transport

- **There is no management IP and no web service.** All management runs inside iSCSI on TCP 3260.
- Dashboard opens its own iSCSI session that carries no disk I/O, only the commands below.
  NOP-In/NOP-Out keepalives run about every 10 s.
- Every command goes to LUN 0 of that session.
- Dashboard repeats the full poll set about every **5 s**.
- Big-endian throughout.

## Commands

| CDB | Meaning | Reply |
|---|---|---|
| `1A 00 3F 00 C0 00` | MODE SENSE(6), all pages: liveness | 4 bytes |
| `12 01 80 00 1C 00` | INQUIRY, VPD 0x80: unit serial | 16 bytes |
| `5A 08 3A <sub> 00 00 00 <len16> 00` | MODE SENSE(10), DBD, vendor page 0x3A, subpage `<sub>` | see below |
| `EA 10 80 81 <start> 00 21 7C` + 8×`00` | vendor: volume / target list, paged | see below |
| `55 01 3A 0C 00 00 00 00 56 00` | MODE SELECT(10): **write**, host-reported usage | (Dashboard → unit) |

Allocation lengths Dashboard uses for each subpage: `01`=0x2C, `02`=0x34, `03`=0x370, `05`=0x3A,
`06`=0x06, `08`=0x14A, `09`=0x20, `30`=0x1D, `31`=0x210.

Each vendor-page reply starts with a 4-byte header: `7A <sub> <len_hi> <len_lo>` (0x7A is
page 0x3A with the SPF bit set). Offsets below are **relative to the payload**, i.e. after
that header.

### 3A/01: configuration
| Off | Type | Field |
|---|---|---|
| 0 | u8 | number of bays (8) |
| 4 | u56 | maximum volume size, in 512-byte sectors (`07FFFFFFFF` = 16 TiB) |

### 3A/02: capacity
`u64 free, u64 used, u64 total_protected, u64 unprotected` (bytes).

### 3A/03: bays
`u8 count`, then `count` records of **72 bytes**:

| Off | Type | Field |
|---|---|---|
| 0 | u16 | record length (0x46) |
| 2 | u8 | bay index (0-based) |
| 3 | u64 | physical size, bytes |
| 11 | u64 | managed size (0 on B800i) |
| 19 | u8 | light: `0x80` empty, else low nibble indexes `off, red, yellow, green, red/green flash, red/yellow flash, red/off flash` |
| 20 | char[32] | drive model |
| 52 | char[16] | drive model, short |
| 68 | u8 | ? (0 for the WD Red Pro drives, 2 for the WD RE drive) |
| 70 | u8 | **drive temperature, °C** (inferred: 33–39 on a running unit, 0 when empty) |

### 3A/05: settings
`u32 clock, u16 tz, char[32] unit_name`.
`clock` is **local time** as seconds since the epoch, so UTC = `clock + tz_hours·3600`.
`tz` is stored byte-swapped: `0x0800` means 8 hours west of UTC.

### 3A/06: protocol version
`u8 major, u8 minor`: 0.11.

### 3A/08: firmware
`u8 major, u8 minor, u16 build, u8 upg_major, u8 upg_minor, char[32] build_date, char[16] arch, char[16] version, …`.
Feature bits are the `u32` at payload offset 182 (0x0056FDFF on this unit). Bit names follow drobo-utils:
`SUPPORTS_ISCSI` 0x20000, `SUPPORTS_SHUTDOWN` 0x8000, `LUN_MANAGEMENT` 0x1000, and so on.

### 3A/09: status
`u32 flags, u32 relayout_blocks_remaining`.

| Bit | Meaning |
|---|---|
| 0x0002 | red alert |
| 0x0004 | yellow alert |
| 0x0008 | no disks |
| 0x0010 | bad disk |
| 0x0020 | too many missing disks |
| 0x0040 | no redundancy |
| 0x0080 | no hot spare |
| 0x0100 | no space left |
| 0x0200 | relayout (data protection) in progress |
| 0x0400 | format in progress |
| 0x0800 | mismatched disks |
| 0x2000 | new firmware installed |
| 0x4000 | new volume available after reboot |
| 0x8000, 0x10000 | **set on a healthy B800i; meaning unknown** |

### 3A/30: options
9 bytes, `55 5F 00 …` on this unit. Flag meanings are not yet mapped.

### 3A/31: network
Port records start at payload offset 10. Each is 14 bytes: `u32 ip, u32 netmask, u32 gateway, u16 mtu`.
A zero IP ends the list. This unit: `169.0.0.3/29` and `169.0.0.2/29`, gateway `169.0.0.1`, MTU 9000.
Payload bytes 0–9 (`00×7 0F 00 0E`) aren't mapped yet; 0x0E is likely the record size.

### Vendor 0xEA: volumes / iSCSI targets
Request `EA 10 80 81 <start> 00 <alloc16>`. Dashboard asks with `start=1`, then with
`start = last index + 1` until the "last page" flag is set.

Reply: `u8 ?(01), u8 count, u8 last_page, ` then `count` records of **0x11D bytes**:

| Off | Type | Field |
|---|---|---|
| 0x000 | u8 | volume index (1-based; can have gaps) |
| 0x001 | u32 | target id |
| 0x005 | char[128] | target IQN: `iqn.2005-06.com.drobo:b800i.<serial token>.id<N>` |
| 0x085 | UTF-16BE[64] | volume name |
| 0x107 | u8, u8 | ? (`02 02` / `01 02`; likely partition scheme / format) |
| 0x109 | u64 | volume size, bytes (sizes are 2/4/8/16 TiB) |
| 0x111 | u64 | used bytes **as reported by the host**, `FFFF…` = unknown |
| 0x119 | u32 | ? (2) |

### MODE SELECT 3A/0C: host-reported usage (write)
Dashboard sends this every cycle, telling the thin-provisioned unit how full each
volume's filesystem is. The values add up to the 3A/02 "used" figure. Payload:
`2C 00 00 00 01 <count>`, then per volume `u8 index, u16 target_id, …, u64 used`.

**drobo-web never sends this.** A monitor on a different host would overwrite the real host's numbers.
It is the only write in a normal Dashboard session.

## Open questions

1. **Which target does Dashboard's management session log in to?** The capture began after the login.
   Output of `iscsiadm -m discovery -t sendtargets -p <unit-ip>` would answer it.
2. **Setting writes**: rename, light dimming, network config, email alerts, and volume create/delete/resize.
   Capture Dashboard while making one change at a time.
3. Bytes 68 (bay record) and 0x107/0x119 (volume record), and status bits 0x8000/0x10000.
4. Whether the 0xEA command takes other subcommands (byte 3 ≠ 0x81).

Captures from other models (B810i, B1200i, DroboPro, DroboElite) are very welcome. Use
`tools/pcap_to_fixture.py`, or `droboweb dump` on Linux. Both redact by default.
