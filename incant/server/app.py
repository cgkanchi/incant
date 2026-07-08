"""FastAPI application: serving API + mgmt API + UI, RBAC-guarded."""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from ..config import get_settings
from ..db import session_scope
from ..service import get_app
from .auth import ensure_bootstrap_admin
from .mgmt import router as mgmt_router
from .serving import router as serving_router

_UI_DIR = Path(__file__).resolve().parent.parent / "ui"


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    ctx = get_app()
    ctx.initialize()
    with session_scope() as s:
        ensure_bootstrap_admin(s, settings.bootstrap_admin_key)
    # Eager-warm content caches for every environment before readiness.
    with session_scope() as s:
        from .. import models
        from sqlalchemy import select
        for env in s.execute(select(models.Environment)).scalars():
            try:
                ctx.warm(s, env.id)
            except Exception:
                pass
    app.state.ready = True
    yield


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title="Incant", version="0.1.0", lifespan=lifespan)
    app.state.ready = False

    app.include_router(serving_router)
    if settings.mode == "full":
        app.include_router(mgmt_router)

    @app.get("/healthz", response_class=PlainTextResponse)
    def healthz() -> str:
        return "ok"

    @app.get("/readyz", response_class=PlainTextResponse)
    def readyz():
        if not getattr(app.state, "ready", False):
            return PlainTextResponse("warming", status_code=503)
        return PlainTextResponse("ready")

    @app.get("/metrics")
    def metrics_endpoint():
        return PlainTextResponse(generate_latest().decode(), media_type=CONTENT_TYPE_LATEST)

    # UI (built assets served by the server).
    if _UI_DIR.exists():
        @app.get("/", response_class=HTMLResponse)
        def index():
            index_file = _UI_DIR / "index.html"
            headers = {"Cache-Control": "no-store"}
            if index_file.exists():
                return HTMLResponse(index_file.read_text(), headers=headers)
            return HTMLResponse("<h1>Incant</h1><p>UI not built.</p>", headers=headers)

        app.mount("/ui", StaticFiles(directory=str(_UI_DIR), html=True), name="ui")

    return app


app = create_app()
