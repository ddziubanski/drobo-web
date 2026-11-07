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
    kinds = [e["kind"] for e in mon.store.events()]
    assert kinds[0] == "blink" and "identify" not in kinds  # one event per press
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


# ------------------------------------------------------------------ the unit while it blinks

def _blinking_bays(fixture_bytes: bytes) -> bytes:
    """The fixture's 3A/03 reply as the unit sent it during Blink Lights in a capture:
    every bay 0x05, empty bays 0x85."""
    b = bytearray(fixture_bytes)
    for i in range(b[4]):
        off = 4 + 1 + i * 72
        b[off + 19] = (b[off + 19] & 0x80) | 0x05
    return bytes(b)


def test_parser_recognises_blink_lights():
    from droboweb.demo import DemoTransport
    from droboweb.protocol import parse_slots, is_identifying
    t = DemoTransport()
    bays = parse_slots(_blinking_bays(t.responses["3a03"])[4:])
    assert [b.present for b in bays] == [True] * 6 + [False] * 2  # 0x85 is still an empty bay
    assert is_identifying(bays, 0x00018000)
    assert not is_identifying(bays, 0x00018000 | 0x0002)  # with a red alert it's a real fault
    real = parse_slots(t.responses["3a03"][4:])
    assert not is_identifying(real, 0x00018000)
    one_bad = parse_slots(t.responses["3a03"][4:])
    one_bad[0].led_code = 0x05
    assert not is_identifying(one_bad, 0x00018000)  # one bay flashing isn't Blink Lights


def test_blink_is_not_a_drive_failure():
    _, mon, _ = make()
    events_before = len(mon.store.events())
    mon.blink()
    snap = mon.poll_once()
    assert snap.identifying
    mon._handle(snap, None)
    assert mon.snapshot.health == "ok"
    assert all(b.led == "green" for b in mon.snapshot.bays if b.present)  # last real light kept
    new = mon.store.events()[: len(mon.store.events()) - events_before]
    assert [e["kind"] for e in new] == ["blink"]
    assert all(e["severity"] == "info" for e in new)
    mon.transport.blink_until = 0  # the blink ends
    mon._handle(mon.poll_once(), None)
    assert not mon.snapshot.identifying and mon.snapshot.health == "ok"
    assert all(e["severity"] == "info" for e in mon.store.events())


def test_real_fault_still_shows_during_blink():
    _, mon, _ = make()
    mon.snapshot.bays[2].led = "red"  # bay 3 really failed before the blink
    mon.blink()
    mon._handle(mon.poll_once(), None)
    assert mon.snapshot.bays[2].led == "red" and mon.snapshot.health == "critical"


def test_first_poll_during_blink():
    from droboweb.monitor import Monitor
    cfg = Config(demo="healthy", db_path=":memory:")
    mon = Monitor(cfg)
    mon.transport = mon._open()
    mon.transport.write(blink_cdb(1), b"\x00")  # as if Drobo Dashboard pressed Blink Lights
    mon._handle(mon.poll_once(), None)
    assert mon.snapshot.health == "ok"
    assert "identify" in [e["kind"] for e in mon.store.events()]
    assert {b.led for b in mon.snapshot.bays if b.present} == {"identify"}
    assert not [e for e in mon.store.events() if e["severity"] != "info"]
