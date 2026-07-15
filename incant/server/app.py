"""FastAPI application: serving API + mgmt API + UI, RBAC-guarded.

Two run modes (INCANT_MODE):

* ``full``  — owns the canonical repo + control-plane schema. Boot initializes the
  repo/schema, ensures a bootstrap admin, runs the git↔DB reconciliation sweep, then
  warms. Warm failures are logged and leave the node *not ready* (a background loop
  re-warms until it succeeds — the simplest honest readiness).
* ``serve`` — read-only replica: no schema create, no repo init, no bootstrap-admin
  write, no mgmt router, no UI. Boot verifies the repo + schema already exist (fail
  fast otherwise) and requires a successful warm before readiness.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from sqlalchemy import inspect, select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..db import engine, session_scope
from ..registry import reconcile_drafts, reconcile_main_commits, sweep_expired_sessions
from ..service import get_app
from .auth import AuthError, _IMPLIES, ensure_bootstrap_admin
from .deps import get_session
from .mgmt import router as mgmt_router
from .serving import router as serving_router
from .sessions import router as session_router

log = logging.getLogger("incant.server")

_UI_DIR = Path(__file__).resolve().parent.parent / "ui"
_WARM_RETRY_SECONDS = 5.0
_SESSION_SWEEP_SECONDS = 3600.0  # hourly expired-session sweep (full mode)

# Self-hosted UI CSP. The app serves its own fonts and assets, so everything is
# 'self'. Two deliberate loosenings: `img-src` allows `data:` (inline SVG/data URIs
# the UI embeds) and `style-src` allows 'unsafe-inline' — the UI uses inline `style`
# attributes pervasively, so this is required until they move to classes; scripts are
# NOT loosened ('self' only), which is the part that actually stops injected JS.
_CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data:; font-src 'self'; connect-src 'self'; "
    "frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
)


def _verify_serve_prerequisites(ctx) -> None:
    """Serve replicas never create state — fail fast if the repo or schema is absent."""
    if not ctx.git.exists():
        raise RuntimeError(
            f"serve mode: content repo not found at {ctx.settings.repo_dir()}. "
            "A serve replica does not create it — start the `full` node (or hydrate the "
            "repo from a backup remote) first."
        )
    if "environments" not in set(inspect(engine()).get_table_names()):
        raise RuntimeError(
            "serve mode: database schema is not initialized. A serve replica does not "
            "create it — run the `full` node (or `incant init`) against this database first."
        )


def _warm_all(ctx) -> bool:
    """Warm every environment's content cache. Returns True iff all succeeded.

    Each environment is warmed on its own short-lived session so one failure can't
    poison the others. Failures are logged (never swallowed silently) so readiness
    reflects reality.
    """
    with session_scope() as s:
        from .. import models
        env_ids = [e.id for e in s.execute(select(models.Environment)).scalars()]
    ok = True
    for env_id in env_ids:
        try:
            with session_scope() as s:
                ctx.warm(s, env_id)
        except Exception:
            log.exception("warm failed for environment %s", env_id)
            ok = False
    return ok


def _prime_auth(ctx) -> bool:
    """Prime the in-memory auth cache from the DB so readiness also means the node can
    AUTHENTICATE with zero DB reads (§8 "No DB per request"; §10 "the DB is never on the
    per-request path"). Without this, a node could report ready with a cold auth cache
    and 503 the first authenticated request if Postgres died right after readiness — the
    exact mirror of the cold-snapshot hazard warming closes. Its own short-lived session;
    priming is a serving concern, so this runs in BOTH modes. Failure is LOGGED (never
    swallowed) and blocks readiness exactly like a warm failure."""
    try:
        with session_scope() as s:
            ctx.auth.refresh(s)  # AuthCache.refresh — force a cold load past its TTL guard
        return True
    except Exception:
        log.exception("auth-cache priming failed")
        return False


def _boot_prime(ctx) -> bool:
    """Everything readiness requires: every environment warmed (content + snapshot) AND
    the auth cache primed. Both are evaluated every pass (no short-circuit) so a single
    failure is always logged and the retry loop drives BOTH to green; readiness is their
    AND. Per-concern isolation is preserved — `_warm_all` already isolates each env on its
    own session so one failure can't poison the others, and auth priming has its own."""
    warmed = _warm_all(ctx)
    primed = _prime_auth(ctx)
    return warmed and primed


async def _warm_retry_loop(app: FastAPI, ctx) -> None:
    """Re-prime in the background until warming AND auth priming both succeed, then flip
    readiness green. Retries the whole readiness gate, not just warming."""
    while not getattr(app.state, "ready", False):
        await asyncio.sleep(_WARM_RETRY_SECONDS)
        try:
            if _boot_prime(ctx):
                app.state.ready = True
                log.info("warm + auth priming complete; node is ready")
        except Exception:  # pragma: no cover - defensive; keep the loop alive
            log.exception("background readiness retry errored")


async def _session_sweep_loop() -> None:
    """Full mode: sweep expired browser sessions hourly so a long-running node doesn't
    accumulate dead rows (the boot sweep only runs once). Logs only when it deletes."""
    while True:
        await asyncio.sleep(_SESSION_SWEEP_SECONDS)
        try:
            with session_scope() as s:
                sweep_expired_sessions(s)
        except Exception:  # pragma: no cover - defensive; keep the loop alive
            log.exception("periodic session sweep errored")


async def _reconcile_loop(ctx) -> None:
    """Full mode: re-run the git↔DB main-commit drift check on an interval (the boot sweep
    only runs once). Drift can appear AFTER boot — a publish whose outer DB transaction
    rolled back after `commit_version` moved `main` leaves an unvalidated tip (see
    RegistryService.commit_draft) — so a boot-only check would never notice it. Each pass
    records the result on the ctx (feeding /healthz + the incant_reconcile_* gauges).
    Detect-and-log only: it NEVER repairs and NEVER flips readiness (§3, §5)."""
    interval = get_settings().reconcile_interval_seconds
    while True:
        await asyncio.sleep(interval)
        try:
            with session_scope() as s:
                ctx.record_reconcile(reconcile_main_commits(s, ctx.git))
        except Exception:  # pragma: no cover - defensive; keep the loop alive
            log.exception("periodic main reconcile errored")


async def _control_poll_loop(ctx) -> None:
    """Background control-plane poll — the piece that keeps the serving hot path DB-free.

    Every INCANT_CONTROL_POLL_SECONDS it opens a session and calls
    ``ctx.refresh_control_plane(s)``, pulling targeting bumps and the TTL-driven auth
    reload into memory so requests never read the DB (§8 "No DB per request"; §10 "the DB
    is never on the per-request path") and cross-replica changes land within the interval
    — the poll fallback for §7's Postgres LISTEN/NOTIFY. Runs in BOTH full and serve modes
    because it feeds the serving hot path itself. Never raises out: refresh_control_plane
    absorbs a DB outage (flipping the stale flag), and anything else is logged so the loop
    stays alive."""
    interval = get_settings().control_poll_seconds
    while True:
        await asyncio.sleep(interval)
        try:
            with session_scope() as s:
                ctx.refresh_control_plane(s)
        except Exception:  # pragma: no cover - defensive; keep the loop alive
            log.exception("control-plane poll errored")


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    ctx = get_app()

    if settings.mode == "serve":
        _verify_serve_prerequisites(ctx)  # fail fast; no writes in serve mode
    else:
        ctx.initialize()  # git init + schema (create_all on SQLite, Alembic on Postgres)
        with session_scope() as s:
            ensure_bootstrap_admin(s, settings.bootstrap_admin_key)
        # Reconcile git draft refs against DB draft rows before serving warms, sweep any
        # expired browser sessions, and detect (log, never repair) main-commit drift. The
        # main-reconcile result is recorded on the ctx so /healthz + the incant_reconcile_*
        # gauges reflect drift from the very first boot, not just after the first interval.
        with session_scope() as s:
            reconcile_drafts(s, ctx.git)
            sweep_expired_sessions(s)
            ctx.record_reconcile(reconcile_main_commits(s, ctx.git))

    # Readiness (both modes) requires warming EVERY environment (content + snapshot) AND
    # priming the auth cache — so "ready" honestly means "can serve + authenticate with
    # zero DB reads" (§8/§10). Any failure leaves the node not ready; in full mode a
    # background loop keeps retrying both, and in serve mode the same loop lets a replica
    # become ready once the full node has published its content.
    app.state.ready = _boot_prime(ctx)
    retry_task = None
    if not app.state.ready:
        log.warning("warm/auth priming incomplete at boot — node not ready; retrying in "
                    "background")
        retry_task = asyncio.create_task(_warm_retry_loop(app, ctx))

    # Hourly expired-session sweep + periodic main-commit drift check (full mode only —
    # serve replicas have no sessions and never own the canonical main to reconcile).
    sweep_task = reconcile_task = None
    if settings.mode == "full":
        sweep_task = asyncio.create_task(_session_sweep_loop())
        reconcile_task = asyncio.create_task(_reconcile_loop(ctx))

    # Control-plane poll (BOTH modes): the serving hot path never reads the DB itself;
    # this loop pulls targeting bumps + auth changes into memory (§7 poll fallback, §8/§10).
    poll_task = asyncio.create_task(_control_poll_loop(ctx))

    try:
        yield
    finally:
        for task in (retry_task, sweep_task, reconcile_task, poll_task):
            if task is not None:
                task.cancel()


def _has_viewer_anywhere(ident) -> bool:
    """True iff the principal holds `viewer` (directly or by implication) at *any*
    scope — instance, project, or (project, env). /metrics is non-sensitive read-only
    telemetry, so scope doesn't matter; a renderer-only key (no viewer) is refused."""
    return any("viewer" in _IMPLIES.get(b.role, set()) for b in ident.bindings)


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title="Incant", version="0.1.0", lifespan=lifespan)
    app.state.ready = False

    @app.middleware("http")
    async def _security_headers(request: Request, call_next):
        response = await call_next(request)
        h = response.headers
        h.setdefault("Content-Security-Policy", _CSP)
        h.setdefault("X-Content-Type-Options", "nosniff")
        h.setdefault("X-Frame-Options", "DENY")
        h.setdefault("Referrer-Policy", "no-referrer")
        h.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        # HSTS only when TLS terminates in front of us (a proxy); Incant speaks plain
        # HTTP, so emitting it unconditionally could wedge a plain-HTTP deployment.
        if get_settings().enforce_tls:
            h.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
        return response

    app.include_router(serving_router)
    if settings.mode == "full":
        app.include_router(mgmt_router)
        app.include_router(session_router)

    # /healthz and /readyz stay public and unauthenticated on purpose: they are
    # load-balancer / orchestrator probes and return no sensitive data (a literal
    # "ok"/"ready"/"warming"), so they must answer before any credential is presented.
    @app.get("/healthz", response_class=PlainTextResponse)
    def healthz():
        # Liveness/health probe — public + unauthenticated (LB/orchestrator poll). We fold
        # the latest git↔DB drift counts into the body WHEN there is drift, but deliberately
        # do NOT flip health on it: a drifted node still serves correct content from the
        # last VALIDATED SHAs (§5), so returning non-200 (pulling it from rotation) would
        # convert a governance ALARM into an outage. Continuous numeric monitoring lives in
        # the incant_reconcile_* gauges; this body just makes drift glanceable. A clean (or
        # not-yet-reconciled, e.g. serve replica) node stays the literal "ok".
        res = get_app().last_reconcile
        if res is not None and (res.git_orphans or res.unvalidated_tips or res.missing_files):
            return JSONResponse({
                "status": "ok",  # still serving correctly — drift is NOT unhealthy
                "drift": {
                    "git_orphans": res.git_orphans,
                    "unvalidated_tips": res.unvalidated_tips,
                    "missing_files": res.missing_files,
                },
            })
        return PlainTextResponse("ok")

    @app.get("/readyz", response_class=PlainTextResponse)
    def readyz():
        if not getattr(app.state, "ready", False):
            return PlainTextResponse("warming", status_code=503)
        return PlainTextResponse("ready")

    @app.get("/metrics")
    def metrics_endpoint(
        authorization: str | None = Header(default=None),
        session: Session = Depends(get_session),
    ):
        # Two ways in: a Prometheus scraper with no principal presents the shared
        # INCANT_METRICS_TOKEN, or any authenticated principal holding `viewer`.
        token = get_settings().metrics_token
        if not (token and authorization == f"Bearer {token}"):
            try:
                ident = get_app().authenticate(session, authorization)
            except AuthError:
                raise HTTPException(status_code=401, detail="metrics requires authentication")
            if not _has_viewer_anywhere(ident):
                raise HTTPException(status_code=401, detail="metrics requires a viewer credential")
        return PlainTextResponse(generate_latest().decode(), media_type=CONTENT_TYPE_LATEST)

    # UI (built assets) — full mode only; serve replicas expose no mgmt/UI surface.
    if settings.mode == "full" and _UI_DIR.exists():
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
