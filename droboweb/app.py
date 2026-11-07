"""FastAPI application."""
from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import secrets
import time
from importlib import resources

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles

from . import __version__, metrics
from .config import Config
from .monitor import Monitor
from .redact import redact
from .scsi import RecordingTransport


def create_app(cfg: Config | None = None, monitor: Monitor | None = None, start_poller: bool = True) -> FastAPI:
    cfg = cfg or Config.from_env()
    mon = monitor or Monitor(cfg)

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

    @app.get("/api/status", dependencies=guard)
    def status():
        return {"version": __version__, "monitor": mon.state(),
                "snapshot": mon.snapshot.to_dict() if mon.snapshot else None}

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

    @app.get("/metrics", dependencies=guard)
    def prom():
        return PlainTextResponse(metrics.render(mon), media_type="text/plain; version=0.0.4")

    @app.exception_handler(Exception)
    async def err(_req, exc):  # pragma: no cover
        return JSONResponse({"error": str(exc)}, status_code=500)

    static = resources.files("droboweb") / "static"
    app.mount("/", StaticFiles(directory=str(static), html=True), name="static")
    return app
