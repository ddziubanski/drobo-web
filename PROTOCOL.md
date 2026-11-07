# Drobo B800i management protocol

How Drobo Dashboard 3.5.0 manages a Drobo B800i (firmware 3.45.10935, ArmMarvell,
protocol 0.11), worked out from a packet capture. The capture is in
[`droboweb/fixtures/b800i-fw3.45.json`](droboweb/fixtures/b800i-fw3.45.json), redacted
(serial, IQN token, unit name and volume names replaced). drobo-utils by Peter Silva et al.
documented the same vendor page for earlier models and was the starting point.

Status: **reads are confirmed against live data.** Two setting writes are mapped from a second
capture (network config and admin password, below); the rest aren't yet (see "Open questions").

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
| `55 01 3A 31 00 00 00 02 00 00` | MODE SELECT(10): **write**, network config (512 bytes) | (Dashboard → unit) |
| `55 01 3A 12 00 00 00 02 08 00` | MODE SELECT(10): **write**, set admin password (520 bytes) | (Dashboard → unit) |
| `55 01 3A 14 00 00 00 02 08 00` | MODE SELECT(10): **write**, authenticate with the admin password | (Dashboard → unit) |
| `55 01 3A 05 00 00 00 00 2A 00` | MODE SELECT(10): **write**, clock, timezone and unit name (42 bytes) | (Dashboard → unit) |
| `EA 10 00 06 <seq> 00 21 00 01` + 7×`00` | vendor: **Blink Lights** (identify the unit), 1 data byte | (Dashboard → unit) |

Allocation lengths Dashboard uses for each subpage: `01`=0x2C, `02`=0x34, `03`=0x370, `05`=0x3A,
`06`=0x06, `08`=0x14A, `09`=0x20, `15`=0x31, `30`=0x1D, `31`=0x210.

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
Payload bytes 0–9 are `00×7 <flags> 00 0E`: 0x0E is the record size. The flags byte was 0x0F
on a unit without an admin password and became **0x1F the moment an admin password was set**,
so bit 0x10 means "admin password set" or "this session has authenticated" (one capture can't
tell which). The other bits aren't mapped.

The reply header's length is little-endian here (`7A 31 FC 01`), unlike the other pages.

### MODE SELECT 3A/31: network config (write)
Dashboard writes the whole page back with the changed field. Header `7A 31 01 FC` (length
big-endian, 0x1FC = 508), then the same payload as the read: flags byte, record size 0x0E, and
one 14-byte record per port, zero-filled to 512 bytes. Confirmed by changing port 1's MTU
from 9000 to 8999: the write carried `… 23 27` in port 1's MTU field, and the next read
returned 8999. Changing IP, netmask or gateway is presumed to work the same way, but isn't captured.

### MODE SELECT 3A/12 and 3A/14: admin password (write)
Changing the admin password in Dashboard sent **the same 520-byte payload to 3A/12 and then
3A/14**, 70 ms apart (only the subpage byte in the header differs). The likely reading is
that 3A/12 sets the password and 3A/14 authenticates with it. A third capture confirms the
second part: with the password already set, Dashboard sent only 3A/14 (same payload) before
using Blink Lights. The payload is `7A <sub> 02 08`, then
`01 27 A7 03 E8`, then 158 bytes that aren't plain text: an 82-byte pattern repeats inside it,
which suggests a fixed-key encoding of a short padded secret. The rest of the payload is zero.
Decoding it needs captures of known passwords (for example `a`, then `aa`), and the real
captured payload isn't published here because it's derived from a real password.

### MODE SELECT 3A/05: unit name and clock (write)
Header `7A 05 26 00` (length little-endian), then `u32 clock`, `s16 tz`, `char[32] name`.
Unlike the read, the written `clock` is **UTC** seconds and `tz` is the offset in **minutes**,
big-endian signed: `FE 20` = −480 = UTC−8. The unit then reads back local time and `08 00`, as
before.

Captured once, while the Rename Drobo option was being used: the write carried the current time
and the **old** name, and the unit kept the old name. The rename to a new name isn't in the
capture, so this is very likely the rename command (Dashboard writes the whole record, setting
the clock as a side effect), but a capture with the new name in the payload is still needed to
confirm it.

### Vendor 0xEA 06: Blink Lights
Dashboard's **Blink Lights** button (flashes the front panel to identify the unit) sends
`EA 10 00 06 <seq> 00 21 00 01` followed by 7 zero bytes, with **one data byte** to the unit.
`<seq>` goes up by one per command (`8B`, `8C`, `8D`, `8E` in the capture), so it's probably a
transaction counter. Four presses of the same button carried `4B`, `32`, `32`, `00` (75, 50, 50, 0),
so the byte doesn't select anything: it's either unfilled memory, like the junk bytes in the usage
report, or a blink duration in seconds. None of the pages drobo-web reads change. The capture had
an admin password set, and Dashboard had authenticated with 3A/14 earlier in the session; whether
Blink Lights needs that isn't known.

drobo-web sends exactly this command, with its own counter starting at 1 and a zero data byte
(the value Dashboard's last press sent), only when someone presses its Blink Lights button.

### 3A/15: identity
Read by Dashboard while it detects units (`5A 08 3A 15 … 31`). Payload `01 01 03 EA` and then
the unit serial as ASCII, zero-padded. 0x03EA may be a model id.

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

Looking across all five reports in the capture, bytes 0–3 and everything after the last record change
from one report to the next and contain fragments of other replies (drive model strings). They're
uninitialised memory in Dashboard and the unit ignores them. The meaningful layout is:

| Off | Type | Field |
|---|---|---|
| 4 | u8 | 0x01 |
| 5 | u8 | record count |
| 6 + 11·i | u8, u16, u64 | volume index, target id, used bytes (0 = not mounted here → unit shows unknown) |

drobo-web sends this only when `report_usage` is turned on, with zeros in the junk bytes, and
only if Dashboard's service isn't running and at least one Drobo volume is mounted on the host.
It's the only write in a normal Dashboard session.

## Open questions

1. **Which target does Dashboard's management session log in to?** The capture began after the login.
   Output of `iscsiadm -m discovery -t sendtargets -p <unit-ip>` would answer it.
2. **Setting writes**: rename (to confirm, see 3A/05), drive spin-down, email alerts, and volume
   create/delete/resize.
   Capture Dashboard while making one change at a time. (Network config, admin password, clock
   and Blink Lights are mapped above.)
3. **The admin password**: how the 3A/12 payload encodes it, and whether other writes are
   refused once a password is set but the session hasn't authenticated with 3A/14.
4. Bytes 68 (bay record) and 0x107/0x119 (volume record), and status bits 0x8000/0x10000.
5. Which other subcommands the 0xEA command takes (0x81 volume list and 0x06 Blink Lights are known),
   and what Blink Lights' data byte does.

Captures from other models (B810i, B1200i, DroboPro, DroboElite) are very welcome. Use
`tools/pcap_to_fixture.py`, or `droboweb dump` on Linux. Both redact by default.
