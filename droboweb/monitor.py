"""Background poller: keeps the latest snapshot, history and events."""
from __future__ import annotations

import asyncio
import logging
import threading
import time

from .config import Config
from .demo import DemoTransport, backfill
from .discovery import find_drobo
from . import usage as usage_mod
from .events import Event, diff
from .notify import Notifier
from .protocol import Snapshot, read_snapshot
from .scsi import BLINK_DATA, ReplayTransport, Transport, blink_cdb, open_device
from .store import Store

log = logging.getLogger("droboweb.monitor")


class Monitor:
    def __init__(self, cfg: Config, store: Store | None = None):
        self.cfg = cfg
        if cfg.demo:
            # demo runs keep their made-up history in memory, and sample faster
            if cfg.db_path == Config.db_path:
                cfg.db_path = ":memory:"
            cfg.sample_seconds = min(cfg.sample_seconds, cfg.poll_seconds)
        self.store = store or Store(cfg.db_path)
        self.notifier = Notifier(cfg)
        self.transport: Transport | None = None
        self.snapshot: Snapshot | None = None
        self.last_ok: float = 0
        self.last_error: str = ""
        self.reachable = False
        self.polls = 0
        self.failures = 0
        self._last_sample = 0.0
        self._last_prune = 0.0
        self.listeners: list = []          # callables(event, unit) — e.g. the tray's notifications
        self._pending: list[Event] = []    # events raised from the poll thread
        self._reopen = False               # set when the device setting changes
        self._io = threading.Lock()        # one command at a time on the device
        self._blink_seq = 0
        # host-reported usage (the one write); injectable for tests
        self.usage_provider = usage_mod.host_usage
        self.dashboard_check = usage_mod.dashboard_service_running
        self._last_usage = 0.0
        self.usage = {"enabled": cfg.report_usage, "status": "off", "detail": "", "last_sent": None,
                      "volumes": []}

    @property
    def mode(self) -> str:
        if self.cfg.demo:
            return "demo"
        if self.cfg.fixture:
            return "replay"
        return "live"

    def _open(self) -> Transport:
        if self.cfg.demo:
            return DemoTransport(self.cfg.fixture or None, self.cfg.demo, self.cfg.demo_speed)
        if self.cfg.fixture:
            return ReplayTransport(self.cfg.fixture)
        path = self.cfg.device
        if path == "auto":
            path = find_drobo()
            if not path:
                raise RuntimeError("no Drobo found: log in to the B800i over iSCSI first "
                                   "(see README), or set DROBOWEB_DEVICE=/dev/sgN")
        return open_device(path)

    def _emit(self, events: list[Event]) -> None:
        unit = (self.snapshot.name if self.snapshot else "") or "Drobo"
        for ev in events:
            log.info("%s %s: %s", ev.severity, ev.kind, ev.message)
            self.store.add_event(ev)
            self.notifier.send(ev, unit)
            for fn in list(self.listeners):
                try:
                    fn(ev, unit)
                except Exception:  # noqa: BLE001
                    log.exception("listener failed")

    def apply_settings(self, changed: set[str]) -> None:
        """Called when the Settings page changes something. Most settings are read live from
        the config; a new device means reconnecting, which happens before the next poll."""
        if "device" in changed:
            self._reopen = True
        if "report_usage" in changed:
            self._last_usage = 0.0
        if "retention_days" in changed:
            self._last_prune = 0.0

    def poll_once(self) -> Snapshot:
        with self._io:
            return self._poll_locked()

    def blink(self) -> None:
        """Flash the front-panel lights, like Drobo Dashboard's Blink Lights. Raises on failure."""
        with self._io:
            if self.transport is None:
                self.transport = self._open()
            self._blink_seq = self._blink_seq % 0xFF + 1
            try:
                self.transport.write(blink_cdb(self._blink_seq), BLINK_DATA)
            except Exception as e:  # noqa: BLE001
                raise RuntimeError(f"The Drobo refused Blink Lights ({e}). If an admin password is set on "
                                   "the Drobo, turn it off in Drobo Dashboard: drobo-web can't send the "
                                   "password yet.") from e
        self._pending.append(Event("info", "blink", "Blink Lights sent from drobo-web"))

    def _poll_locked(self) -> Snapshot:
        if self._reopen:
            self._reopen = False
            if self.transport is not None and self.mode == "live":
                self.transport.close()
                self.transport = None
        if self.transport is None:
            self.transport = self._open()
        snap = read_snapshot(self.transport)
        self._report_usage(snap)
        return snap

    def _set_usage(self, status: str, detail: str = "") -> None:
        prev = self.usage["status"]
        self.usage["status"], self.usage["detail"] = status, detail
        if status != prev and status in ("blocked", "error"):
            self._pending.append(Event("warning", "usage_report", f"Not reporting volume usage: {detail}"))
        elif status != prev and status == "active" and prev in ("blocked", "error", "off", "waiting"):
            self._pending.append(Event("info", "usage_report", "Reporting volume usage to the Drobo from this host"))

    def _report_usage(self, snap: Snapshot) -> None:
        """Send Dashboard's usage report, if enabled and safe. Runs in the poll thread."""
        self.usage["enabled"] = self.cfg.report_usage
        if not self.cfg.report_usage:
            self.usage.update(status="off", detail="", volumes=[])
            return
        if time.time() - self._last_usage < self.cfg.usage_seconds:
            return
        self._last_usage = time.time()
        if any(e.startswith("volumes") for e in snap.errors) or not snap.volumes:
            self._set_usage("waiting", "The unit's volume list isn't available yet")
            return
        if self.dashboard_check():
            self._set_usage("blocked", "Drobo Dashboard's service (DDService.exe) is running and already sends "
                                       "this report. Stop the 'Drobo Dashboard Service' to let drobo-web take over.")
            return
        try:
            host = self.usage_provider()
        except Exception as e:  # noqa: BLE001
            self._set_usage("error", f"Couldn't read this host's volume usage ({e})")
            return
        if not any(v.iqn.lower() in host for v in snap.volumes):
            self._set_usage("blocked", "None of the Drobo's volumes are mounted on this host, so it isn't the "
                                       "host that should report usage (that would mark every volume as unknown).")
            return
        try:
            self.usage["volumes"] = usage_mod.send(self.transport, snap.volumes, host)
        except Exception as e:  # noqa: BLE001
            self._set_usage("error", f"The unit rejected the usage report ({e}). If an admin password is "
                                     "set on the Drobo, turn it off in Drobo Dashboard: drobo-web can't "
                                     "send the password yet. Reading status works either way.")
            return
        self.usage["last_sent"] = time.time()
        self._set_usage("active")

    def _handle(self, snap: Snapshot | None, err: Exception | None) -> None:
        self.polls += 1
        if err is not None:
            self.failures += 1
            self.last_error = str(err)
            if self.transport:
                self.transport.close()
                self.transport = None
            if self.reachable:
                self.reachable = False
                self._emit([Event("critical", "unreachable", f"Lost contact with the Drobo: {err}")])
            return
        pending, self._pending = self._pending, []
        if snap.identifying:
            # Blink Lights is running (from drobo-web or Drobo Dashboard). The unit reports every
            # bay as flashing, so keep each drive's last real light instead of raising alarms.
            prev = {b.slot: b for b in self.snapshot.bays} if self.snapshot else {}
            for b in snap.bays:
                o = prev.get(b.slot)
                if b.present and o is not None and o.present:
                    b.led = o.led
            started_here = any(e.kind == "blink" for e in pending)
            if not (self.snapshot and self.snapshot.identifying) and not started_here:
                pending.append(Event("info", "identify", "Front-panel lights are blinking (Blink Lights)"))
        if self.cfg.demo and self.snapshot is None and not self.store.history(24 * 8, 10):
            backfill(self.store, snap)
        events = diff(self.snapshot, snap, self.cfg.temp_warn_c, self.cfg.capacity_warn_pct)
        if not self.reachable and self.snapshot is not None:
            events.insert(0, Event("info", "reconnected", "Contact with the Drobo restored"))
        self.reachable = True
        self.last_error = ""
        self.last_ok = snap.taken_at
        self.snapshot = snap
        self._emit(events + pending)
        now = time.time()
        if now - self._last_sample >= self.cfg.sample_seconds:
            self.store.add_sample(snap)
            self._last_sample = now
        if now - self._last_prune > 3600:
            self.store.prune(self.cfg.retention_days)
            self._last_prune = now

    async def run(self) -> None:
        while True:
            try:
                snap = await asyncio.to_thread(self.poll_once)
                self._handle(snap, None)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                log.warning("poll failed: %s", e)
                self._handle(None, e)
            await asyncio.sleep(self.cfg.poll_seconds)

    def state(self) -> dict:
        return {
            "mode": self.mode,
            "source": self.transport.name if self.transport else None,
            "reachable": self.reachable,
            "last_ok": self.last_ok or None,
            "last_error": self.last_error or None,
            "poll_seconds": self.cfg.poll_seconds,
            "polls": self.polls,
            "failures": self.failures,
            "usage_report": self.usage,
            "thresholds": {"temp_warn_c": self.cfg.temp_warn_c, "capacity_warn_pct": self.cfg.capacity_warn_pct},
        }
