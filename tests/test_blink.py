from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from droboweb.app import create_app
from droboweb.config import Config
from droboweb.monitor import Monitor
from droboweb.scsi import NotAllowed, _check_write_allowed, blink_cdb
from droboweb.settings import SettingsStore

# The command Drobo Dashboard sent for Blink Lights (capture of a B800i, firmware 3.45)
CAPTURED = bytes.fromhex("ea100006 8b 00 21 00 01 00000000000000".replace(" ", ""))


def make(client=("127.0.0.1", 1), **kw):
    cfg = Config(demo="healthy", db_path=":memory:", **kw)
    mon = Monitor(cfg)
    mon._handle(mon.poll_once(), None)
    store = SettingsStore(cfg, None, env={})
    return TestClient(create_app(cfg, mon, start_poller=False, settings=store), client=client), mon, cfg


def test_cdb_matches_dashboard():
    assert blink_cdb(0x8B) == CAPTURED
    _check_write_allowed(CAPTURED, b"\x00")


@pytest.mark.parametrize("cdb,data", [
    (CAPTURED, b"\x4b"),                                  # only a zero data byte
    (CAPTURED, b"\x00\x00"),
    (CAPTURED[:3] + b"\x07" + CAPTURED[4:], b"\x00"),       # another 0xEA subcommand
    (CAPTURED[:8] + b"\x02" + CAPTURED[9:], b"\x00"),       # another length
    (CAPTURED[:15] + b"\x01", b"\x00"),                    # junk at the end
    (CAPTURED[:10], b"\x00"),
])
def test_allowlist_takes_only_the_exact_shape(cdb, data):
    with pytest.raises(NotAllowed):
        _check_write_allowed(cdb, data)


def test_monitor_blink_counts_up_and_logs():
    _, mon, _ = make()
    mon.blink()
    mon.blink()
    (c1, d1), (c2, d2) = mon.transport.writes[-2:]
    assert (c1[4], c2[4]) == (1, 2) and d1 == d2 == b"\x00"
    mon._handle(mon.poll_once(), None)
    assert mon.store.events()[0]["kind"] == "blink"
    mon._blink_seq = 0xFF
    mon.blink()
    assert mon.transport.writes[-1][0][4] == 1  # wraps, skipping 0


def test_refused_blink_mentions_admin_password():
    _, mon, _ = make()

    def refuse(cdb, data):
        raise RuntimeError("CHECK CONDITION")
    mon.transport.write = refuse
    with pytest.raises(RuntimeError, match="admin password"):
        mon.blink()


def test_api_blink():
    c, mon, _ = make()
    assert c.get("/api/status").json()["actions"]["blink"] is True
    assert c.post("/api/blink", json={}).json() == {"ok": True}
    assert mon.transport.writes[-1][0][:4] == CAPTURED[:4]


def test_api_blink_needs_edit_access_and_json():
    c, mon, _ = make(client=("192.168.1.9", 1))
    assert c.get("/api/status").json()["actions"]["blink"] is False
    assert c.post("/api/blink", json={}).status_code == 403
    c, mon, _ = make()
    assert c.post("/api/blink", content="{}", headers={"Content-Type": "text/plain"}).status_code == 415
    assert c.post("/api/blink", json={}, headers={"Origin": "http://evil.example"}).status_code == 403
    assert mon.transport.writes == []


def test_api_blink_can_be_turned_off():
    c, mon, cfg = make()
    assert c.put("/api/settings", json={"values": {"blink_button": False}}).status_code == 200
    assert c.get("/api/status").json()["actions"]["blink"] is False
    assert c.post("/api/blink", json={}).status_code == 403


def test_api_blink_reports_refusal():
    c, mon, _ = make()

    def refuse(cdb, data):
        raise RuntimeError("CHECK CONDITION")
    mon.transport.write = refuse
    r = c.post("/api/blink", json={})
    assert r.status_code == 502 and "admin password" in r.json()["detail"]


def test_tray_menu_has_blink(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("APPDATA", str(tmp_path))
    try:
        import pystray  # noqa: F401  (needs the tray extra, and a display on Linux)
    except Exception:  # noqa: BLE001
        pytest.skip("pystray not available")
    from droboweb.tray import TrayApp
    app = TrayApp(demo="healthy")
    texts = [str(i.text) for i in app.build_menu().items]
    assert "Blink lights" in texts
