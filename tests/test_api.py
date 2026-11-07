from __future__ import annotations

import base64

from fastapi.testclient import TestClient

from droboweb.app import create_app
from droboweb.config import Config
from droboweb.monitor import Monitor


def make(**kw):
    cfg = Config(demo="healthy", db_path=":memory:", **kw)
    mon = Monitor(cfg)
    snap = mon.poll_once()
    mon._handle(snap, None)
    return TestClient(create_app(cfg, mon, start_poller=False)), mon


def test_status_and_history():
    c, _ = make()
    s = c.get("/api/status").json()
    assert s["monitor"]["mode"] == "demo" and s["monitor"]["reachable"]
    assert s["snapshot"]["health"] == "ok"
    h = c.get("/api/history?hours=168").json()
    assert len(h["points"]) > 100  # demo backfill
    ev = c.get("/api/events").json()["events"]
    assert ev and ev[0]["kind"] == "connected"


def test_metrics():
    c, _ = make()
    body = c.get("/metrics").text
    assert "drobo_up 1" in body
    assert 'drobo_drive_temperature_celsius{unit="B800i-Demo"' in body
    assert "drobo_capacity_total_bytes" in body


def test_dump_is_redacted():
    c, _ = make()
    r = c.get("/api/dump")
    assert r.status_code == 200
    j = r.json()
    assert j["redacted"] is True and "3a03" in j["responses"]


def test_static_ui():
    c, _ = make()
    r = c.get("/")
    assert r.status_code == 200 and "Front panel" in r.text


def test_basic_auth():
    c, _ = make(auth_user="admin", auth_password="secret")
    assert c.get("/api/status").status_code == 401
    tok = base64.b64encode(b"admin:secret").decode()
    assert c.get("/api/status", headers={"Authorization": f"Basic {tok}"}).status_code == 200
    assert c.get("/healthz").status_code == 200  # stays open for container health checks


def test_unreachable_then_recovered():
    _, mon = make()
    mon._handle(None, RuntimeError("iSCSI session dropped"))
    assert not mon.reachable
    kinds = [e["kind"] for e in mon.store.events()]
    assert "unreachable" in kinds
    mon._handle(mon.poll_once(), None)
    assert mon.reachable
    assert mon.store.events()[0]["kind"] == "reconnected"
