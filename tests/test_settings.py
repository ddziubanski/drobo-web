from __future__ import annotations

import base64
import json
import os

import pytest
from fastapi.testclient import TestClient

from droboweb.app import create_app
from droboweb.config import Config
from droboweb.monitor import Monitor
from droboweb.settings import SettingsError, SettingsStore, default_path

LOCAL = ("127.0.0.1", 50000)
REMOTE = ("192.168.1.50", 50000)
JSON = {"Content-Type": "application/json"}


def make(tmp_path, client=LOCAL, env=None, **kw):
    cfg = Config(demo="healthy", db_path=":memory:", settings_path=str(tmp_path / "s.json"), **kw)
    store = SettingsStore(cfg, default_path(cfg), env=env or {})
    mon = Monitor(cfg)
    mon._handle(mon.poll_once(), None)
    return TestClient(create_app(cfg, mon, start_poller=False, settings=store), client=client), cfg, store


def field(body, key):
    return next(f for g in body["groups"] for f in g["fields"] if f["key"] == key)


# ------------------------------------------------------------------ store

def test_saved_settings_apply_on_next_start(tmp_path):
    cfg = Config(db_path=str(tmp_path / "h.sqlite3"))
    path = default_path(cfg)
    assert path == tmp_path / "droboweb-settings.json"
    SettingsStore(cfg, path, env={}).update({"temp_warn_c": 45, "mail_to": "a@b.c", "report_usage": True})
    if os.name != "nt":
        assert oct(path.stat().st_mode & 0o777) == "0o600"  # it can hold passwords
    fresh = Config(db_path=cfg.db_path)
    SettingsStore(fresh, path, env={})
    assert (fresh.temp_warn_c, fresh.mail_to, fresh.report_usage) == (45, "a@b.c", True)


def test_validation_is_all_or_nothing(tmp_path):
    cfg = Config(demo="healthy")
    s = SettingsStore(cfg, tmp_path / "s.json", env={})
    with pytest.raises(SettingsError) as e:
        s.update({"temp_warn_c": 45, "poll_seconds": 0, "mail_to": "nope", "webhook_url": "ftp://x",
                  "webhook_format": "irc", "device": "/etc/passwd; rm", "bogus": 1, "smtp_port": 1.5})
    assert set(e.value.errors) == {"poll_seconds", "mail_to", "webhook_url", "webhook_format", "device",
                                   "bogus", "smtp_port"}
    assert cfg.temp_warn_c == 50 and not (tmp_path / "s.json").exists()


def test_coercion_and_unknown_keys_survive(tmp_path):
    p = tmp_path / "s.json"
    p.write_text(json.dumps({"window_pos": [1, 2], "temp_warn_c": "999", "poll_seconds": "15"}))
    cfg = Config()
    s = SettingsStore(cfg, p, env={})
    assert cfg.temp_warn_c == 50      # out-of-range value in the file is ignored
    assert cfg.poll_seconds == 15.0
    s.update({"device": "", "mail_to": "a@b.c, c@d.e"})
    assert cfg.device == "auto"
    assert json.loads(p.read_text())["window_pos"] == [1, 2]


def test_env_and_cli_lock_settings(tmp_path):
    p = tmp_path / "s.json"
    p.write_text(json.dumps({"temp_warn_c": 40, "device": "/dev/sg9"}))
    cfg = Config(temp_warn_c=55, device="/dev/sg2")
    cfg.extra["locked"] = {"device": "command line (--device)"}
    s = SettingsStore(cfg, p, env={"DROBOWEB_TEMP_WARN_C": "55"})
    assert (cfg.temp_warn_c, cfg.device) == (55, "/dev/sg2")  # file doesn't override env or CLI
    with pytest.raises(SettingsError) as e:
        s.update({"temp_warn_c": 41})
    assert "DROBOWEB_TEMP_WARN_C" in e.value.errors["temp_warn_c"]


def test_login_needs_a_password(tmp_path):
    s = SettingsStore(Config(), None, env={})
    with pytest.raises(SettingsError) as e:
        s.update({"auth_user": "admin"})
    assert "auth_password" in e.value.errors
    assert s.update({"auth_user": "admin", "auth_password": "pw"}) == {"auth_user", "auth_password"}


def test_device_change_reconnects():
    cfg = Config(demo="healthy", db_path=":memory:")
    mon = Monitor(cfg)
    mon.poll_once()
    mon.apply_settings({"device"})
    assert mon._reopen
    mon.poll_once()
    assert not mon._reopen and mon.transport is not None


# ------------------------------------------------------------------ API

def test_get_hides_passwords(tmp_path):
    c, cfg, _ = make(tmp_path, smtp_password="hunter2")
    r = c.get("/api/settings")
    assert r.status_code == 200 and "hunter2" not in r.text
    b = r.json()
    assert b["editable"] and b["mode"] == "demo"
    pw = field(b, "smtp_password")
    assert pw["value"] == "" and pw["is_set"] is True
    assert field(b, "temp_warn_c")["value"] == 50 and field(b, "temp_warn_c")["default"] == 50
    assert field(b, "temp_warn_c")["min"] == 30


def test_put_applies_live(tmp_path):
    c, cfg, _ = make(tmp_path)
    r = c.put("/api/settings", json={"values": {"capacity_warn_pct": 70, "notify_min_severity": "critical"}})
    assert r.status_code == 200, r.text
    assert r.json()["changed"] == ["capacity_warn_pct", "notify_min_severity"]
    assert cfg.capacity_warn_pct == 70 and cfg.notify_min_severity == "critical"
    assert c.get("/api/status").json()["monitor"]["thresholds"]["capacity_warn_pct"] == 70
    assert json.loads((tmp_path / "s.json").read_text())["capacity_warn_pct"] == 70


def test_put_reports_field_errors(tmp_path):
    c, cfg, _ = make(tmp_path)
    r = c.put("/api/settings", json={"values": {"temp_warn_c": 5}})
    assert r.status_code == 422 and "temp_warn_c" in r.json()["errors"]
    assert c.put("/api/settings", json={"oops": 1}).status_code == 400


def test_empty_password_field_keeps_it(tmp_path):
    c, cfg, _ = make(tmp_path, smtp_password="hunter2")
    c.put("/api/settings", json={"values": {"smtp_host": "mail.example.com"}})
    assert cfg.smtp_password == "hunter2"
    c.put("/api/settings", json={"values": {"smtp_password": ""}})  # explicit "Remove"
    assert cfg.smtp_password == ""


def test_remote_needs_a_login_to_edit(tmp_path):
    c, cfg, _ = make(tmp_path, client=REMOTE)
    b = c.get("/api/settings").json()
    assert b["editable"] is False and "login" in b["reason"]
    assert c.put("/api/settings", json={"values": {"temp_warn_c": 45}}).status_code == 403
    assert cfg.temp_warn_c == 50

    c, cfg, _ = make(tmp_path, client=REMOTE, auth_user="admin", auth_password="pw")
    tok = {"Authorization": "Basic " + base64.b64encode(b"admin:pw").decode()}
    assert c.put("/api/settings", json={"values": {"temp_warn_c": 45}}).status_code == 401
    assert c.put("/api/settings", json={"values": {"temp_warn_c": 45}}, headers=tok).status_code == 200


def test_settings_ui_off(tmp_path):
    c, _, _ = make(tmp_path, settings_ui="off")
    assert c.get("/api/settings").json()["editable"] is False
    assert c.put("/api/settings", json={"values": {"temp_warn_c": 45}}).status_code == 403


def test_cross_site_writes_are_refused(tmp_path):
    c, cfg, _ = make(tmp_path)
    body = json.dumps({"values": {"report_usage": True}})
    # a plain HTML form or no-cors fetch can't send application/json
    assert c.put("/api/settings", content=body, headers={"Content-Type": "text/plain"}).status_code == 415
    assert c.put("/api/settings", content=body, headers={**JSON, "Origin": "http://evil.example"}).status_code == 403
    assert c.put("/api/settings", content=body, headers={**JSON, "Origin": "http://testserver"}).status_code == 200
    assert cfg.report_usage is True


def test_test_alert(tmp_path, monkeypatch):
    c, cfg, _ = make(tmp_path)
    assert c.post("/api/settings/test-alert", json={}).status_code == 400  # nothing configured
    sent = []
    monkeypatch.setattr("urllib.request.urlopen", lambda req, timeout: sent.append(req) or _Closer())
    c.put("/api/settings", json={"values": {"webhook_url": "https://hooks.example/x", "webhook_format": "slack"}})
    r = c.post("/api/settings/test-alert?channel=webhook", json={})
    assert r.json() == {"results": {"webhook": "ok"}}
    assert b"Test alert" in sent[0].data and json.loads(sent[0].data)["text"]


def test_settings_page_is_served(tmp_path):
    c, _, _ = make(tmp_path)
    assert "settings.js" in c.get("/settings.html").text
    assert 'href="settings.html"' in c.get("/").text


class _Closer:
    def close(self):
        pass


@pytest.mark.skipif(os.name == "nt", reason="tray fields differ on Windows")
def test_tray_fields_share_the_settings_file(tmp_path):
    from droboweb.settings import FIELDS
    from droboweb.tray import build_config, load_settings, tray_fields
    p = tmp_path / "settings.json"
    cfg = build_config(load_settings(p), tmp_path)
    s = SettingsStore(cfg, p, FIELDS + tray_fields(), env={})
    assert s.get("notifications") is True and cfg.port == 8780
    s.update({"notifications": False, "port": 8790})
    saved = json.loads(p.read_text())
    assert saved["notifications"] is False and saved["port"] == 8790
    again = SettingsStore(build_config(load_settings(p), tmp_path), p, FIELDS + tray_fields(), env={})
    assert again.get("notifications") is False and again.cfg.port == 8790
