from __future__ import annotations

import ctypes
from importlib import resources

import pytest

from droboweb import usage
from droboweb.config import Config
from droboweb.monitor import Monitor
from droboweb.protocol import read_snapshot
from droboweb.scsi import USAGE_CDB, NotAllowed, ReplayTransport, _check_write_allowed
from droboweb.winscsi import SCSI_PASS_THROUGH_DIRECT, SPTD_WITH_SENSE, build_sptd

FIXTURE = resources.files("droboweb") / "fixtures" / "b800i-fw3.45.json"

# Records region of the MODE SELECT Dashboard sent at t=28.4 s in the capture
# (bytes 4.. of the 86-byte parameter list; the first 4 bytes and the tail were junk).
CAPTURED_RECORDS = bytes.fromhex(
    "0105"
    "0103ea0000000000000000"
    "0203ed000002d9b9fba000"
    "0403f50000003c12df7000"
    "0503f20000000000000000"
    "0603f90000026f30adb000")


@pytest.fixture
def snap():
    return read_snapshot(ReplayTransport(FIXTURE))


def host_like_capture(snap):
    by_index = {v.index: v.iqn.lower() for v in snap.volumes}
    return {by_index[2]: 0x2D9B9FBA000, by_index[4]: 0x3C12DF7000, by_index[6]: 0x26F30ADB000}


def test_payload_matches_dashboard_byte_for_byte(snap):
    payload, summary = usage.build_payload(snap.volumes, host_like_capture(snap))
    assert len(payload) == 0x56
    assert payload[:4] == b"\0\0\0\0"
    assert payload[4:4 + len(CAPTURED_RECORDS)] == CAPTURED_RECORDS
    assert not any(payload[4 + len(CAPTURED_RECORDS):])
    assert [s["mounted_here"] for s in summary] == [False, True, True, False, True]
    _check_write_allowed(USAGE_CDB, payload)


def test_write_allowlist_rejects_everything_else(snap):
    payload, _ = usage.build_payload(snap.volumes, host_like_capture(snap))
    with pytest.raises(NotAllowed):
        _check_write_allowed(bytes([0x55, 0x01, 0x3A, 0x05, 0, 0, 0, 0, 0x56, 0]), payload)  # other subpage
    with pytest.raises(NotAllowed):
        _check_write_allowed(USAGE_CDB, payload[:-1])
    junk = bytearray(payload)
    junk[-1] = 1
    with pytest.raises(NotAllowed):
        _check_write_allowed(USAGE_CDB, bytes(junk))


def test_used_is_clamped_to_volume_size(snap):
    v = snap.volumes[1]
    payload, summary = usage.build_payload(snap.volumes, {v.iqn.lower(): v.size_bytes * 3})
    assert summary[1]["used_bytes"] == v.size_bytes


def test_parse_windows_usage_single_and_list():
    one = '{"target":"IQN.2005-06.com.drobo:b800i.x.id2","disk":3,"used":100,"mounted":true}'
    assert usage.parse_windows_usage(one) == {"iqn.2005-06.com.drobo:b800i.x.id2": 100}
    many = ('[{"target":"iqn.a.id1","disk":1,"used":5,"mounted":false},'
            '{"target":"iqn.a.id2","disk":2,"used":7,"mounted":true}]')
    assert usage.parse_windows_usage(many) == {"iqn.a.id2": 7}
    assert usage.parse_windows_usage("") == {}


@pytest.mark.skipif(__import__("os").name == "nt", reason="Linux paths")
def test_linux_usage(tmp_path):
    by_path = tmp_path / "by-path"
    by_path.mkdir()
    dev = tmp_path / "sdb1"
    dev.write_text("")
    (by_path / "ip-169.0.0.2:3260-iscsi-iqn.2005-06.com.drobo:b800i.x.id2-lun-0-part1").symlink_to(dev)
    mounts = tmp_path / "mounts"
    mounts.write_text(f"{dev} {tmp_path} ext4 rw 0 0\n/dev/sda1 / ext4 rw 0 0\n")
    got = usage.linux_usage(by_path, mounts)
    assert list(got) == ["iqn.2005-06.com.drobo:b800i.x.id2"]


def make_monitor(**kw):
    cfg = Config(demo="healthy", db_path=":memory:", report_usage=True, usage_seconds=0, **kw)
    m = Monitor(cfg)
    m.dashboard_check = lambda: False
    return m


def test_monitor_sends_report_when_enabled():
    m = make_monitor()
    snap = read_snapshot(m._open())
    m.usage_provider = lambda: host_like_capture(snap)
    m._handle(m.poll_once(), None)
    assert m.usage["status"] == "active"
    cdb, data = m.transport.writes[-1]
    assert cdb == USAGE_CDB and data[4:4 + len(CAPTURED_RECORDS)] == CAPTURED_RECORDS
    assert any(e["kind"] == "usage_report" for e in m.store.events())


def test_monitor_blocks_when_dashboard_service_runs():
    m = make_monitor()
    m.dashboard_check = lambda: True
    m.usage_provider = lambda: pytest.fail("should not read usage")
    m._handle(m.poll_once(), None)
    assert m.usage["status"] == "blocked" and "DDService" in m.usage["detail"]
    assert m.transport.writes == []


def test_monitor_blocks_when_nothing_mounted_here():
    m = make_monitor()
    m.usage_provider = lambda: {}
    m._handle(m.poll_once(), None)
    assert m.usage["status"] == "blocked"
    assert m.transport.writes == []  # would otherwise zero every volume


def test_monitor_off_by_default():
    m = Monitor(Config(demo="healthy", db_path=":memory:"))
    m._handle(m.poll_once(), None)
    assert m.usage["status"] == "off" and m.transport.writes == []


def test_sptd_layout_matches_windows_x64():
    if ctypes.sizeof(ctypes.c_void_p) != 8:
        pytest.skip("layout check is for 64-bit")
    assert ctypes.sizeof(SCSI_PASS_THROUGH_DIRECT) == 56
    assert SCSI_PASS_THROUGH_DIRECT.DataTransferLength.offset == 12
    assert SCSI_PASS_THROUGH_DIRECT.DataBuffer.offset == 24
    assert SCSI_PASS_THROUGH_DIRECT.SenseInfoOffset.offset == 32
    assert SCSI_PASS_THROUGH_DIRECT.Cdb.offset == 36
    buf = ctypes.create_string_buffer(16)
    s = build_sptd(bytes([0x5A, 0x08, 0x3A, 0x06, 0, 0, 0, 0, 6, 0]), buf, 6, True)
    assert s.sptd.Length == 56 and s.sptd.CdbLength == 10 and s.sptd.DataIn == 1
    assert s.sptd.SenseInfoOffset == SPTD_WITH_SENSE.Sense.offset == 60


def test_tray_helpers(tmp_path):
    from droboweb.tray import build_config, load_settings, make_icon, save_settings, summarize
    p = tmp_path / "settings.json"
    s = load_settings(p)
    s["report_usage"] = True
    s["port"] = 9999
    save_settings(p, s)
    cfg = build_config(load_settings(p), tmp_path)
    assert cfg.report_usage is True and cfg.port == 9999 and cfg.host == "127.0.0.1"
    assert build_config(s, tmp_path, demo="healthy").db_path == ":memory:"
    assert make_icon("critical", 32).size == (32, 32)
    m = Monitor(Config(demo="healthy", db_path=":memory:"))
    assert summarize(m)[0] == "starting"
    m._handle(m.poll_once(), None)
    assert summarize(m) == ("ok", "B800i-Demo: Healthy, 42.6% full")
