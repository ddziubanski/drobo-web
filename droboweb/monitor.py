"""Background poller: keeps the latest snapshot, history and events."""
from __future__ import annotations

import asyncio
import logging
import time

from .config import Config
from .demo import DemoTransport, backfill
from .discovery import find_drobo
from .events import Event, diff
from .notify import Notifier
from .protocol import Snapshot, read_snapshot
from .scsi import DeviceTransport, ReplayTransport, Transport
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
        return DeviceTransport(path)

    def _emit(self, events: list[Event]) -> None:
        unit = (self.snapshot.name if self.snapshot else "") or "Drobo"
        for ev in events:
            log.info("%s %s: %s", ev.severity, ev.kind, ev.message)
            self.store.add_event(ev)
            self.notifier.send(ev, unit)

    def poll_once(self) -> Snapshot:
        if self.transport is None:
            self.transport = self._open()
        snap = read_snapshot(self.transport)
        return snap

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
        if self.cfg.demo and self.snapshot is None and not self.store.history(24 * 8, 10):
            backfill(self.store, snap)
        events = diff(self.snapshot, snap, self.cfg.temp_warn_c, self.cfg.capacity_warn_pct)
        if not self.reachable and self.snapshot is not None:
            events.insert(0, Event("info", "reconnected", "Contact with the Drobo restored"))
        self.reachable = True
        self.last_error = ""
        self.last_ok = snap.taken_at
        self.snapshot = snap
        self._emit(events)
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
            "thresholds": {"temp_warn_c": self.cfg.temp_warn_c, "capacity_warn_pct": self.cfg.capacity_warn_pct},
        }
