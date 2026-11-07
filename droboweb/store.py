"""SQLite history of capacity, temperatures and events."""
from __future__ import annotations

import json
import sqlite3
import threading
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS samples (
  ts REAL PRIMARY KEY, used INTEGER, free INTEGER, total INTEGER,
  status_bits INTEGER, health TEXT, temps TEXT
);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, severity TEXT, kind TEXT, message TEXT
);
CREATE INDEX IF NOT EXISTS events_ts ON events(ts);
"""


class Store:
    def __init__(self, path: str):
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.lock = threading.Lock()
        with self.lock:
            self.db.executescript(SCHEMA)

    def add_sample(self, snap) -> None:
        temps = {str(b.slot): b.temperature_c for b in snap.bays if b.temperature_c is not None}
        with self.lock:
            self.db.execute(
                "INSERT OR REPLACE INTO samples VALUES (?,?,?,?,?,?,?)",
                (snap.taken_at, snap.used_bytes, snap.free_bytes, snap.total_bytes,
                 snap.status_bits, snap.health, json.dumps(temps)))
            self.db.commit()

    def add_event(self, ev) -> None:
        with self.lock:
            self.db.execute("INSERT INTO events (ts, severity, kind, message) VALUES (?,?,?,?)",
                            (ev.ts, ev.severity, ev.kind, ev.message))
            self.db.commit()

    def history(self, hours: float, points: int = 300) -> list[dict]:
        since = time.time() - hours * 3600
        bucket = max(1.0, hours * 3600 / points)
        with self.lock:
            rows = self.db.execute(
                "SELECT ts, used, free, total, temps FROM samples WHERE ts >= ? ORDER BY ts",
                (since,)).fetchall()
        out: dict[int, dict] = {}
        for ts, used, free, total, temps in rows:
            k = int(ts // bucket)
            cur = out.setdefault(k, {"ts": ts, "used": used, "free": free, "total": total, "temps": {}, "_n": {}})
            cur.update(ts=ts, used=used, free=free, total=total)
            for slot, t in json.loads(temps or "{}").items():
                n = cur["_n"].get(slot, 0)
                cur["temps"][slot] = (cur["temps"].get(slot, 0) * n + t) / (n + 1)
                cur["_n"][slot] = n + 1
        res = []
        for v in out.values():
            v.pop("_n")
            v["temps"] = {k: round(t, 1) for k, t in v["temps"].items()}
            res.append(v)
        return res

    def events(self, limit: int = 100) -> list[dict]:
        with self.lock:
            rows = self.db.execute(
                "SELECT ts, severity, kind, message FROM events ORDER BY id DESC LIMIT ?",
                (limit,)).fetchall()
        return [dict(ts=r[0], severity=r[1], kind=r[2], message=r[3]) for r in rows]

    def prune(self, days: int) -> None:
        cutoff = time.time() - days * 86400
        with self.lock:
            self.db.execute("DELETE FROM samples WHERE ts < ?", (cutoff,))
            self.db.execute("DELETE FROM events WHERE ts < ?", (cutoff,))
            self.db.commit()
