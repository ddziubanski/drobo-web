"""Command line: droboweb serve | status | list | dump"""
from __future__ import annotations

import argparse
import json
import logging
import sys

from .config import Config


def _human(n: int | None) -> str:
    if n is None:
        return "?"
    for unit in ("B", "KB", "MB", "GB", "TB", "PB"):
        if abs(n) < 1000 or unit == "PB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} B"
        n /= 1000
    return str(n)


def _transport(args, cfg):
    from .demo import DemoTransport
    from .discovery import find_drobo
    from .scsi import ReplayTransport, open_device
    if args.demo:
        return DemoTransport(args.fixture or None, args.demo)
    if args.fixture:
        return ReplayTransport(args.fixture)
    dev = args.device or cfg.device
    if dev == "auto":
        dev = find_drobo()
        if not dev:
            sys.exit("No Drobo found. Log in to the B800i over iSCSI first, or pass --device /dev/sgN.\n"
                     "Run `droboweb list` to see the SCSI devices this host can see.")
    return open_device(dev)


def cmd_serve(args, cfg):
    import uvicorn

    from .app import create_app
    for k in ("device", "demo", "fixture", "host", "port", "db_path"):
        v = getattr(args, k, None)
        if v:
            setattr(cfg, k, v)
            cfg.extra.setdefault("locked", {})[k] = f"command line (--{k.replace('_path', '')})"
    uvicorn.run(create_app(cfg), host=cfg.host, port=cfg.port, log_level="info")


def cmd_tray(args, cfg):
    from .tray import main as tray_main
    tray_main(demo=args.demo or "")


def cmd_status(args, cfg):
    from .protocol import read_snapshot
    s = read_snapshot(_transport(args, cfg))
    if args.json:
        print(json.dumps(s.to_dict(), indent=2))
        return
    print(f"{s.name}  serial {s.serial}  firmware {s.firmware}  protocol {s.protocol}")
    print(f"health: {s.health}   status: {', '.join(s.status) or 'normal'}")
    print(f"capacity: {_human(s.used_bytes)} used of {_human(s.total_bytes)} "
          f"({s.to_dict()['used_pct']}%), {_human(s.free_bytes)} free")
    for b in s.bays:
        t = f"{b.temperature_c}°C" if b.temperature_c is not None else ""
        print(f"  bay {b.slot + 1}: {b.led:9} {_human(b.physical_bytes) if b.present else '-':>9} {t:>5}  {b.model}")
    for v in s.volumes:
        print(f"  volume {v.index}: {v.name or '(unnamed)':20} {_human(v.size_bytes):>9}  used {_human(v.used_bytes)}")
    for p in s.ports:
        print(f"  port {p.port}: {p.ip}/{p.prefix} gw {p.gateway} mtu {p.mtu}")
    for e in s.errors:
        print(f"  ! {e}")


def cmd_list(args, cfg):
    import os
    if os.name == "nt":
        from .winscsi import candidates as wc
        from .discovery import probe
        for c in wc():
            ok = probe(c["path"]) if not c["error"] else None
            print(f"{c['path']:22} {c['vendor']:8} {c['product']:16} "
                  f"{'DROBO (protocol ' + ok + ')' if ok else c['error'] or 'not a Drobo'}")
        return
    from .discovery import candidates, probe
    cands = candidates()
    if not cands:
        print("No SCSI generic devices found (is the sg module loaded? `sudo modprobe sg`).")
        return
    for c in cands:
        ok = probe(c.sg) if (c.likely or c.iscsi_target or args.all) else None
        tag = f"DROBO (protocol {ok})" if ok else ("not a Drobo" if ok is None and (c.likely or c.iscsi_target or args.all) else "skipped")
        print(f"{c.sg:10} {c.vendor:8} {c.model:16} {c.block:6} {tag}")
        if c.iscsi_target:
            print(f"{'':10} target {c.iscsi_target}")


def cmd_dump(args, cfg):
    import time

    from .protocol import read_snapshot
    from .redact import redact
    from .scsi import RecordingTransport
    rec = RecordingTransport(_transport(args, cfg))
    read_snapshot(rec)
    resp = rec.responses if args.no_redact else redact(rec.responses)
    out = {"source": rec.name, "redacted": not args.no_redact, "taken_at": time.time(),
           "responses": {k: v.hex() for k, v in sorted(resp.items())}}
    with open(args.output, "w") as f:
        json.dump(out, f, indent=1)
    print(f"wrote {args.output} ({len(resp)} responses{', redacted' if not args.no_redact else ''})")


def main(argv=None):
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = Config.from_env()
    ap = argparse.ArgumentParser(prog="droboweb", description="Web dashboard for Drobo B800i (and friends)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p):
        p.add_argument("--device", help="SCSI generic device, e.g. /dev/sg3 (default: auto-detect)")
        p.add_argument("--demo", nargs="?", const="healthy", choices=["healthy", "failing"],
                       help="simulate a unit from a real capture")
        p.add_argument("--fixture", help="replay a fixture file (from `droboweb dump`)")

    p = sub.add_parser("serve", help="run the web dashboard")
    common(p)
    p.add_argument("--host")
    p.add_argument("--port", type=int)
    p.add_argument("--db", dest="db_path")
    p.set_defaults(fn=cmd_serve)

    p = sub.add_parser("tray", help="system-tray app with notifications (replaces Drobo's DDAssist)")
    p.add_argument("--demo", nargs="?", const="failing", choices=["healthy", "failing"])
    p.set_defaults(fn=cmd_tray)

    p = sub.add_parser("status", help="print the unit's status once")
    common(p)
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_status)

    p = sub.add_parser("list", help="list SCSI devices and which ones are Drobos")
    p.add_argument("--all", action="store_true", help="probe every device, not just likely ones")
    p.set_defaults(fn=cmd_list)

    p = sub.add_parser("dump", help="save raw responses for a bug report / new fixture")
    common(p)
    p.add_argument("-o", "--output", default="drobo-dump.json")
    p.add_argument("--no-redact", action="store_true", help="keep serial, IQNs and names")
    p.set_defaults(fn=cmd_dump)

    args = ap.parse_args(argv)
    args.fn(args, cfg)


if __name__ == "__main__":
    main()
