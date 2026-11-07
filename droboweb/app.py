"""FastAPI application."""
from __future__ import annotations

import asyncio
import base64
import contextlib
import ipaddress
import json
import secrets
import time
from importlib import resources
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles

from . import __version__, metrics
from .config import Config
from .monitor import Monitor
from .redact import redact
from .scsi import RecordingTransport
from .settings import SettingsError, SettingsStore, default_path



def _is_loopback(host: str | None) -> bool:
    try:
        ip = ipaddress.ip_address(host or "")
    except ValueError:
        return False
    return ip.is_loopback or bool(getattr(ip, "ipv4_mapped", None) and ip.ipv4_mapped.is_loopback)


def create_app(cfg: Config | None = None, monitor: Monitor | None = None, start_poller: bool = True,
               settings: SettingsStore | None = None) -> FastAPI:
    cfg = cfg or Config.from_env()
    store = settings or SettingsStore(cfg, default_path(cfg))  # applies the saved settings to cfg
    mon = monitor or Monitor(cfg)
    store.listeners.append(mon.apply_settings)

    @contextlib.asynccontextmanager
    async def lifespan(app):
        task = asyncio.create_task(mon.run()) if start_poller else None
        yield
        if task:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    app = FastAPI(title="Drobo Web", version=__version__, lifespan=lifespan,
                  docs_url="/api/docs", redoc_url=None)
    app.state.monitor = mon
    app.state.settings = store

    def auth(request: Request):
        if not cfg.auth_user:
            return
        h = request.headers.get("authorization", "")
        if h.startswith("Basic "):
            try:
                user, _, pw = base64.b64decode(h[6:]).decode().partition(":")
            except Exception:  # noqa: BLE001
                user = pw = ""
            if secrets.compare_digest(user, cfg.auth_user) and secrets.compare_digest(pw, cfg.auth_password):
                return
        raise HTTPException(401, headers={"WWW-Authenticate": 'Basic realm="drobo-web"'})

    guard = [Depends(auth)]

    @app.get("/healthz")
    def healthz():
        return {"ok": True, "reachable": mon.reachable}


    @app.get("/api/history", dependencies=guard)
    def history(hours: float = 24, points: int = 300):
        hours = max(0.05, min(hours, 24 * 365))
        return {"hours": hours, "points": mon.store.history(hours, max(10, min(points, 2000)))}

    @app.get("/api/events", dependencies=guard)
    def events(limit: int = 100):
        return {"events": mon.store.events(max(1, min(limit, 1000)))}

    @app.get("/api/dump", dependencies=guard)
    def dump(redacted: bool = True):
        """Raw responses from the unit, in fixture format. Attach this to bug reports."""
        if mon.transport is None:
            raise HTTPException(503, "not connected")
        from .protocol import read_snapshot
        rec = RecordingTransport(mon.transport)
        read_snapshot(rec)
        resp = redact(rec.responses) if redacted else rec.responses
        body = {"source": mon.transport.name, "redacted": redacted, "taken_at": time.time(),
                "responses": {k: v.hex() for k, v in sorted(resp.items())}}
        return Response(json.dumps(body, indent=1), media_type="application/json",
                        headers={"Content-Disposition": 'attachment; filename="drobo-dump.json"'})

    # ------------------------------------------------------------ settings
    def can_edit(request: Request) -> tuple[bool, str]:
        mode = (cfg.settings_ui or "auto").lower()
        if mode == "off":
            return False, "Editing is turned off (DROBOWEB_SETTINGS_UI=off)."
        if mode == "on" or cfg.auth_user:
            return True, ""
        if request.client and _is_loopback(request.client.host):
            return True, ""
        return False, ("Settings can only be changed from the computer drobo-web runs on, or after a login "
                       "is set (DROBOWEB_AUTH_USER and DROBOWEB_AUTH_PASSWORD). They include alert passwords "
                       "and the volume-usage write, so they aren't left open to the network.")

    def check_write(request: Request) -> None:
        ok, why = can_edit(request)
        if not ok:
            raise HTTPException(403, why)
        # A JSON body forces a CORS preflight, which this server never answers, so another site
        # can't post here from a visitor's browser. The Origin check covers DNS rebinding.
        if not request.headers.get("content-type", "").startswith("application/json"):
            raise HTTPException(415, "send JSON")
        origin = request.headers.get("origin")
        if origin and urlsplit(origin).netloc != request.headers.get("host"):
            raise HTTPException(403, "cross-origin request refused")

    def settings_body(request: Request) -> dict:
        ok, why = can_edit(request)
        return {"editable": ok, "reason": why, "mode": mon.mode, **store.describe()}

    @app.get("/api/status", dependencies=guard)
    def status(request: Request):
        return {"version": __version__, "monitor": mon.state(),
                "snapshot": mon.snapshot.to_dict() if mon.snapshot else None,
                "actions": {"blink": bool(cfg.blink_button and can_edit(request)[0])}}

    @app.post("/api/blink", dependencies=guard)
    async def blink(request: Request):
        """Flash the Drobo's front-panel lights (Drobo Dashboard's Blink Lights)."""
        check_write(request)
        if not cfg.blink_button:
            raise HTTPException(403, "The Blink Lights button is turned off in Settings.")
        try:
            await asyncio.to_thread(mon.blink)
        except Exception as e:  # noqa: BLE001
            raise HTTPException(502, str(e)) from None
        return {"ok": True}

    @app.get("/api/settings", dependencies=guard)
    def get_settings(request: Request):
        return settings_body(request)

    @app.put("/api/settings", dependencies=guard)
    async def put_settings(request: Request):
        check_write(request)
        try:
            body = await request.json()
        except ValueError:
            raise HTTPException(400, "body isn't valid JSON") from None
        values = body.get("values") if isinstance(body, dict) else None
        if not isinstance(values, dict):
            raise HTTPException(400, 'expected {"values": {...}}')
        try:
            changed = await asyncio.to_thread(store.update, values)
        except SettingsError as e:
            return JSONResponse({"errors": e.errors}, status_code=422)
        restart = sorted(k for k in changed if store.fields[k].restart)
        return {"changed": sorted(changed), "restart": restart, **settings_body(request)}

    @app.post("/api/settings/test-alert", dependencies=guard)
    async def test_alert(request: Request, channel: str | None = None):
        check_write(request)
        if not mon.notifier.channels():
            raise HTTPException(400, "No alert channel is set up. Fill in email or a webhook and save first.")
        unit = (mon.snapshot.name if mon.snapshot else "") or "Drobo"
        return {"results": await asyncio.to_thread(mon.notifier.test, unit, channel)}

    @app.get("/metrics", dependencies=guard)
    def prom():
        return PlainTextResponse(metrics.render(mon), media_type="text/plain; version=0.0.4")

    @app.exception_handler(Exception)
    async def err(_req, exc):  # pragma: no cover
        return JSONResponse({"error": str(exc)}, status_code=500)

    static = resources.files("droboweb") / "static"
    app.mount("/", StaticFiles(directory=str(static), html=True), name="static")
    return app
