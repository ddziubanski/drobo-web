from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from droboweb import netconfig
from droboweb.app import create_app
from droboweb.config import Config
from droboweb.demo import DemoTransport
from droboweb.monitor import Monitor, NetConflict
from droboweb.netconfig import NetError, PortConfig, build_write, read_records, validate
from droboweb.scsi import NET_CDB, NotAllowed, _check_write_allowed
from droboweb.settings import SettingsStore

# Drobo Dashboard's write when port 1's MTU was changed from 9000 to 8999 (B800i, fw 3.45).
# Only the first 42 bytes are non-zero; the rest of the 512 are zero.
DASHBOARD_WRITE = bytes.fromhex(
    "7a3101fc000000000000001f000ea9000003fffffff8a90000012327a9000002fffffff8a90000012328") + bytes(470)
P1 = {"ip": "169.0.0.3", "netmask": "255.255.255.248", "gateway": "169.0.0.1", "mtu": 9000}
P2 = {"ip": "169.0.0.2", "netmask": "255.255.255.248", "gateway": "169.0.0.1", "mtu": 9000}


def fixture_page(flags: int = 0x0F) -> bytes:
    p = bytearray(DemoTransport().responses["3a31"][4:])
    p[7] = flags
    return bytes(p)


def test_rebuilds_dashboards_write_byte_for_byte():
    page = fixture_page(flags=0x1F)  # that unit had an admin password set
    cur = read_records(page)
    assert [c.as_dict() for c in cur] == [P1, P2]
    new = validate(1, {**P1, "mtu": 8999}, cur)
    data = build_write(page, 1, new)
    assert data == DASHBOARD_WRITE
    _check_write_allowed(NET_CDB, data)


@pytest.mark.parametrize("change,field", [
    ({"ip": "169.0.0.300"}, "ip"),
    ({"ip": "169.0.0.2"}, "ip"),                 # port 2 has it
    ({"ip": "169.0.0.7"}, "ip"),                 # broadcast of 169.0.0.0/29
    ({"ip": "224.0.0.5"}, "ip"),
    ({"netmask": "255.0.255.0"}, "netmask"),     # not contiguous
    ({"netmask": "255.255.255.255"}, "netmask"),
    ({"gateway": "10.0.0.1"}, "gateway"),        # outside the subnet
    ({"gateway": "169.0.0.3"}, "gateway"),       # the port itself
    ({"mtu": 9001}, "mtu"),
    ({"mtu": 1200}, "mtu"),
    ({"mtu": "9k"}, "mtu"),
    ({"mtu": True}, "mtu"),
])
def test_validation(change, field):
    with pytest.raises(NetError) as e:
        validate(1, {**P1, **change}, read_records(fixture_page()))
    assert field in e.value.errors


def test_valid_changes():
    cur = read_records(fixture_page())
    assert validate(1, {**P1, "gateway": "0.0.0.0"}, cur).gateway == "0.0.0.0"
    assert validate(2, {**P2, "ip": "10.1.2.3", "netmask": "255.255.0.0", "gateway": "10.1.0.1",
                        "mtu": "1500"}, cur).mtu == 1500
    with pytest.raises(NetError):
        validate(3, P1, cur)


@pytest.mark.parametrize("mutate", [
    lambda b: b[:3] + b"\x00" + b[4:],          # read-side header
    lambda b: b[:5] + b"\x01" + b[6:],          # junk in the zero bytes
    lambda b: b[:13] + b"\x10" + b[14:],        # another record size
    lambda b: b[:100] + b"\x01" + b[101:],      # data past the last port
    lambda b: b[:26] + b"\x00\x64" + b[28:],    # MTU 100
    lambda b: b[:511],
])
def test_allowlist_rejects_malformed_pages(mutate):
    with pytest.raises(NotAllowed):
        _check_write_allowed(NET_CDB, mutate(DASHBOARD_WRITE))


def test_linux_connections(tmp_path):
    s = tmp_path / "iscsi_session" / "session3"
    s.mkdir(parents=True)
    (s / "targetname").write_text("iqn.2005-06.com.drobo:b800i.x.id1\n")
    c = tmp_path / "iscsi_connection" / "connection3:0"
    c.mkdir(parents=True)
    (c / "persistent_address").write_text("169.0.0.3\n")
    assert netconfig._linux_connections(tmp_path) == [("iqn.2005-06.com.drobo:b800i.x.id1", "169.0.0.3")]
    with pytest.raises(FileNotFoundError):
        netconfig._linux_connections(tmp_path / "nope")


# ------------------------------------------------------------------ monitor and API

@pytest.fixture
def no_sessions(monkeypatch):
    monkeypatch.setattr(netconfig, "ports_in_use", lambda ips: set())


def make(client=("127.0.0.1", 1), **kw):
    cfg = Config(demo="healthy", db_path=":memory:", **{"network_edit": True, **kw})
    mon = Monitor(cfg)
    mon._handle(mon.poll_once(), None)
    store = SettingsStore(cfg, None, env={})
    return TestClient(create_app(cfg, mon, start_poller=False, settings=store), client=client), mon


def test_monitor_changes_one_port_and_verifies(no_sessions):
    _, mon = make()
    r = mon.set_network(1, {**P1, "mtu": 8999}, [P1, P2])
    assert r["verified"] is True and r["changed"] == ["mtu"]
    cdb, data = mon.transport.writes[-1]
    assert cdb == NET_CDB and data[26:28] == b"\x23\x27"
    assert [p.as_dict() for p in mon.network_ports()] == [{**P1, "mtu": 8999}, P2]
    assert mon.snapshot.ports[0].mtu == 8999  # updated straight away
    mon._handle(mon.poll_once(), None)
    assert mon.snapshot.ports[0].mtu == 8999
    ev = mon.store.events()[0]
    assert ev["kind"] == "network_change" and ev["severity"] == "warning" and "9000 → 8999" in ev["message"]


def test_monitor_refuses_stale_page(no_sessions):
    _, mon = make()
    with pytest.raises(NetConflict, match="changed since"):
        mon.set_network(1, {**P1, "mtu": 8999}, [{**P1, "mtu": 1500}, P2])
    assert mon.transport.writes == []


def test_monitor_refuses_port_this_host_uses(monkeypatch):
    monkeypatch.setattr(netconfig, "ports_in_use", lambda ips: {"169.0.0.3"})
    _, mon = make()
    with pytest.raises(NetConflict, match="connected to the Drobo through port 1"):
        mon.set_network(1, {**P1, "mtu": 8999}, [P1, P2])
    assert mon.transport.writes == []
    assert mon.set_network(2, {**P2, "mtu": 8999}, [P1, P2])["verified"] is True


def test_nothing_to_change(no_sessions):
    _, mon = make()
    assert mon.set_network(1, P1, [P1, P2])["changed"] == []
    assert mon.transport.writes == []


def test_api_network(no_sessions, monkeypatch):
    monkeypatch.setattr("droboweb.app.ports_in_use", lambda ips: set())
    c, mon = make()
    assert c.get("/api/status").json()["actions"]["network"] is True
    g = c.get("/api/unit/network").json()
    assert g["ports"] == [P1, P2] and g["in_use"] == []
    body = {"port": 2, **P2, "mtu": 1500, "expected": g["ports"]}
    assert c.post("/api/unit/network", json=body).status_code == 400  # no confirm
    r = c.post("/api/unit/network", json={**body, "confirm": True})
    assert r.status_code == 200 and r.json()["verified"] is True
    r = c.post("/api/unit/network", json={**body, "confirm": True})
    assert r.status_code == 409  # the page changed since `expected`
    r = c.post("/api/unit/network", json={**body, "mtu": 20000, "confirm": True,
                                         "expected": c.get("/api/unit/network").json()["ports"]})
    assert r.status_code == 422 and "mtu" in r.json()["errors"]


def test_api_network_is_opt_in_and_guarded(no_sessions):
    c, mon = make(network_edit=False)
    assert c.get("/api/status").json()["actions"]["network"] is False
    assert c.get("/api/unit/network").status_code == 403
    body = {"port": 1, **P1, "mtu": 1500, "expected": [P1, P2], "confirm": True}
    assert c.post("/api/unit/network", json=body).status_code == 403
    c, mon = make(client=("192.168.1.9", 1))
    assert c.get("/api/status").json()["actions"]["network"] is False
    assert c.post("/api/unit/network", json=body).status_code == 403
    c, mon = make()
    assert c.post("/api/unit/network", json=body, headers={"Origin": "http://evil.example"}).status_code == 403
    assert mon.transport.writes == []


def test_refused_network_write_mentions_admin_password(no_sessions):
    _, mon = make()

    def refuse(cdb, data):
        raise RuntimeError("CHECK CONDITION")
    mon.transport.write = refuse
    with pytest.raises(RuntimeError, match="admin password"):
        mon.set_network(1, {**P1, "mtu": 8999}, [P1, P2])
