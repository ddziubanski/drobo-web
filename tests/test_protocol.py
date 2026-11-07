"""Tests against the redacted fixture captured from a real B800i (firmware 3.45)."""
from __future__ import annotations

import json
import struct
from importlib import resources

import pytest

from droboweb.demo import DemoTransport
from droboweb.events import diff
from droboweb.protocol import mode_sense_cdb, parse_volume_page, read_snapshot
from droboweb.redact import redact
from droboweb.scsi import NotAllowed, ReplayTransport, _check_allowed

FIXTURE = resources.files("droboweb") / "fixtures" / "b800i-fw3.45.json"


@pytest.fixture
def snap():
    return read_snapshot(ReplayTransport(FIXTURE))


def test_unit_info(snap):
    assert snap.errors == []
    assert snap.protocol == "0.11"
    assert snap.firmware == "3.45.10935"
    assert snap.firmware_build_date.startswith("Sep 10 2015")
    assert snap.serial == "DEMO00000001"
    assert snap.name == "B800i-Demo"
    assert snap.tz_offset == 8
    assert "SUPPORTS_ISCSI" in snap.features


def test_capacity(snap):
    assert snap.total_bytes == 33_801_728_163_840
    assert snap.used_bytes == 14_407_406_227_456
    assert snap.free_bytes + snap.used_bytes == snap.total_bytes
    assert snap.to_dict()["used_pct"] == 42.6


def test_bays(snap):
    assert snap.slot_count == 8 and len(snap.bays) == 8
    present = [b for b in snap.bays if b.present]
    assert len(present) == 6
    assert all(b.led == "green" for b in present)
    assert present[0].model == "WDC WD102KFBX-68M95N0"
    assert present[0].physical_bytes == 10_000_831_348_736
    assert present[5].model == "WDC WD4000FYYZ-01UL1B0"
    assert [b.temperature_c for b in present] == [38, 39, 39, 39, 36, 33]
    assert [b.led for b in snap.bays[6:]] == ["empty", "empty"]


def test_volumes(snap):
    v = snap.volumes
    assert [x.index for x in v] == [1, 2, 4, 5, 6]
    assert [x.name for x in v] == ["Media", "Data", "Apps", "Backups", "Archive"]
    assert v[0].size_bytes == 16 * 2**40
    assert v[0].used_bytes is None  # unit reports 0xffff... when the host hasn't said
    assert v[1].used_bytes == 3_134_151_434_240
    assert all(x.iqn.startswith("iqn.2005-06.com.drobo:b800i.demo00000001.id") for x in v)


def test_network(snap):
    assert [(p.ip, p.prefix, p.gateway, p.mtu) for p in snap.ports] == [
        ("169.0.0.3", 29, "169.0.0.1", 9000), ("169.0.0.2", 29, "169.0.0.1", 9000)]


def test_health_ok(snap):
    assert snap.status == []  # bits 0x8000|0x10000 are known-benign
    assert snap.health == "ok"


def test_allowlist_blocks_writes():
    for cdb in [bytes([0x55, 0x01, 0x3A, 0x0C, 0, 0, 0, 0, 0x56, 0]),  # MODE SELECT
                bytes([0x2A] + [0] * 9),  # WRITE(10)
                bytes([0x8A] + [0] * 15),  # WRITE(16)
                bytes([0xEA, 0x10, 0x80, 0x82, 1, 0, 0, 0])]:  # other vendor variants
        with pytest.raises(NotAllowed):
            _check_allowed(cdb)
    _check_allowed(mode_sense_cdb(1, 0x2C))


def test_redaction_is_idempotent_and_complete():
    data = json.loads(FIXTURE.read_text())
    resp = {k: bytes.fromhex(v) for k, v in data["responses"].items()}
    again = redact(resp)
    assert again == resp  # fixture is already redacted, so nothing changes
    # inject a fake serial into the IQNs and check it is removed
    dirty = {k: v.replace(b"demo00000001", b"tdd9999x0042") for k, v in resp.items()}
    assert b"tdd9999x0042" in b"".join(dirty.values())
    assert b"tdd9999x0042" not in b"".join(redact(dirty).values())


def test_volume_page_last_flag():
    data = json.loads(FIXTURE.read_text())
    vols, last = parse_volume_page(bytes.fromhex(data["responses"]["ea01"]))
    assert len(vols) == 3 and last is False
    vols, last = parse_volume_page(bytes.fromhex(data["responses"]["ea05"]))
    assert len(vols) == 2 and last is True


def test_failing_scenario_events():
    t = DemoTransport(scenario="failing")
    before = read_snapshot(t)
    t.t0 -= 200  # jump past the simulated failure
    after = read_snapshot(t)
    assert before.health == "ok"
    assert after.health == "critical"
    assert after.relayout_remaining > 0
    kinds = {e.kind for e in diff(before, after, 50, 85)}
    assert {"health", "drive_led", "status"} <= kinds


def test_temperature_event(snap):
    import copy
    hot = copy.deepcopy(snap)
    hot.bays[0].temperature_c = 55
    evs = diff(snap, hot, 50, 85)
    assert any(e.kind == "temperature" and "Bay 1" in e.message for e in evs)


def test_slot_record_layout_is_72_bytes():
    data = bytes.fromhex(json.loads(FIXTURE.read_text())["responses"]["3a03"])
    assert struct.unpack_from(">H", data, 5)[0] == 0x46  # record length field
