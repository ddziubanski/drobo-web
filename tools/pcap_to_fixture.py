#!/usr/bin/env python3
"""Turn a Wireshark capture of Drobo Dashboard into a droboweb fixture.

    python3 tools/pcap_to_fixture.py capture.pcapng -o fixtures/my-unit.json [--no-redact]

Needs `tshark` on PATH. Finds the iSCSI session(s) that carry Drobo vendor commands
(mode page 0x3A / opcode 0xEA), reassembles the PDUs, and keeps the last response to
each distinct command. Redacts serial, IQNs, unit and volume names by default.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from droboweb.redact import redact  # noqa: E402
from droboweb.scsi import cdb_key  # noqa: E402


def tshark(pcap: str, *args: str) -> str:
    return subprocess.run(["tshark", "-r", pcap, *args], check=True, capture_output=True,
                          text=True).stdout


def assemble(segments: dict[int, bytes]) -> bytes:
    buf = bytearray()
    exp = None
    for seq in sorted(segments):
        p = segments[seq]
        if exp is None:
            exp = seq
        if seq + len(p) <= exp:
            continue
        if seq < exp:
            p = p[exp - seq:]
        buf += p
        exp = max(exp, seq) + len(p)
    return bytes(buf)


def pdus(buf: bytes):
    i = 0
    while i + 48 <= len(buf):
        bhs = buf[i:i + 48]
        ahs = bhs[4] * 4
        dl = int.from_bytes(bhs[5:8], "big")
        yield bhs, buf[i + 48 + ahs:i + 48 + ahs + dl]
        i += 48 + ahs + dl + ((-dl) % 4)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("pcap")
    ap.add_argument("-o", "--out", required=True)
    ap.add_argument("--no-redact", action="store_true")
    a = ap.parse_args()

    streams = set()
    for line in tshark(a.pcap, "-Y", "iscsi.opcode == 0x01", "-T", "fields", "-e", "tcp.stream",
                       "-e", "_ws.col.Info").splitlines():
        sid, _, info = line.partition("\t")
        if "Mode Sense(10)" in info or "0xea" in info:
            streams.add(sid)
    if not streams:
        sys.exit("no Drobo management traffic found")

    responses: dict[str, bytes] = {}
    for sid in sorted(streams):
        rows = tshark(a.pcap, "-Y", f"tcp.stream=={sid} && tcp.len>0", "-T", "fields",
                      "-e", "tcp.srcport", "-e", "tcp.seq", "-e", "tcp.payload").splitlines()
        by_dir: dict[str, dict[int, bytes]] = {}
        for r in rows:
            port, seq, payload = r.split("\t")
            if payload:
                by_dir.setdefault(port, {})[int(seq)] = bytes.fromhex(payload.replace(":", ""))
        cmds: dict[bytes, list] = {}
        order = []
        for port, segs in by_dir.items():
            for bhs, data in pdus(assemble(segs)):
                op, itt = bhs[0] & 0x3F, bhs[16:20]
                if op == 0x01:
                    cmds.setdefault(itt, [None, bytearray()])[0] = bhs[32:48]
                    order.append(itt)
                elif op == 0x25:
                    cmds.setdefault(itt, [None, bytearray()])[1] += data
        for itt in order:
            cdb, data = cmds[itt]
            if cdb and cdb[0] in (0x12, 0x1A, 0x5A, 0xEA) and data:
                responses[cdb_key(cdb)] = bytes(data)

    if not a.no_redact:
        responses = redact(responses)
    Path(a.out).write_text(json.dumps({
        "source": f"pcap:{Path(a.pcap).name}", "redacted": not a.no_redact,
        "responses": {k: v.hex() for k, v in sorted(responses.items())},
    }, indent=1))
    print(f"wrote {a.out}: {len(responses)} responses ({', '.join(sorted(responses))})")


if __name__ == "__main__":
    main()
