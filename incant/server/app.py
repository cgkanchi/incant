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
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from sqlalchemy import inspect, select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..db import claim_full_writer_role, engine, release_full_writer_role, session_scope
from ..registry import (
    adopt_content_tree,
    reconcile_drafts,
    reconcile_main_commits,
    recover_pending_promotions,
    sweep_expired_sessions,
)
from ..service import get_app
from ..targeting.observed import (
    flush_observations, load_suppressions, prune_and_census, record_suppressions,
)
from . import metrics
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
    """Serve replicas never create control-plane state — fail fast if the schema is
    absent. A missing content repo is recoverable: hydrate it as a mirror clone of an
    enabled backup remote (§13/§15 — the remotes the full node pushes to double as the
    content-distribution channel), and only fail if no remote can supply it."""
    if "environments" not in set(inspect(engine()).get_table_names()):
        raise RuntimeError(
            "serve mode: database schema is not initialized. A serve replica does not "
            "create it — run the `full` node (or `incant init`) against this database first."
        )
    if not ctx.git.exists():
        with session_scope() as s:
            hydrated = ctx.backup.hydrate(s)
        if not hydrated:
            raise RuntimeError(
                f"serve mode: content repo not found at {ctx.settings.repo_dir()} and no "
                "enabled backup remote could hydrate it. Start the `full` node with a "
                "shared volume, or register a reachable remote (POST /mgmt/remotes) first."
            )


def _warm_all(ctx) -> dict[str, bool]:
    """Warm every environment's content cache. Returns per-environment success.

    Each environment is warmed on its own short-lived session so one failure can't
    poison the others. Failures are logged (never swallowed silently) so readiness
    reflects reality.
    """
    with session_scope() as s:
        from .. import models
        env_ids = [e.id for e in s.execute(select(models.Environment)).scalars()]
    results: dict[str, bool] = {}
    for env_id in env_ids:
        try:
            with session_scope() as s:
                ctx.warm(s, env_id)
            results[env_id] = True
        except Exception:
            log.exception("warm failed for environment %s", env_id)
            results[env_id] = False
    return results


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


def _boot_prime(ctx) -> tuple[bool, dict[str, bool]]:
    """Everything readiness requires, evaluated every pass (no short-circuit) so every
    failure is logged and the retry loop drives it all toward green. Returns
    ``(ready, per_env_warm)``.

    Readiness is PER-ENVIRONMENT in spirit: the gate is the auth cache plus the
    DEFAULT environment (the one requests that name no env get). A broken scratch env
    must not hold new/restarted capacity out of rotation for a healthy prod — §10's
    per-request honesty already answers for a degraded env (fallback or 409), the
    retry loop keeps re-warming it, and /healthz names it (``degraded_environments``)
    so the operator sees exactly what a green node is NOT vouching for."""
    env_warm = _warm_all(ctx)
    primed = _prime_auth(ctx)
    default_env = ctx.settings.default_environment
    # A missing configured default environment is not servable and therefore cannot
    # be ready.  Treat absence as failure instead of allowing an empty database to
    # advertise green readiness.
    ready = primed and env_warm.get(default_env, False)
    return ready, env_warm


async def _warm_retry_loop(app: FastAPI, ctx) -> None:
    """Re-prime in the background until the readiness gate passes AND every
    environment is warm — a green node may still carry degraded environments, and
    this loop keeps driving them toward warm (surfaced via /healthz meanwhile)."""
    while True:
        env_warm = getattr(app.state, "env_warm", {})
        if getattr(app.state, "ready", False) and all(env_warm.values()):
            return
        await asyncio.sleep(_WARM_RETRY_SECONDS)
        try:
            ready, env_warm = _boot_prime(ctx)
            app.state.env_warm = env_warm
            if ready and not getattr(app.state, "ready", False):
                app.state.ready = True
                log.info("warm + auth priming complete; node is ready")
        except Exception:  # pragma: no cover - defensive; keep the loop alive
            log.exception("background readiness retry errored")


async def _session_sweep_loop(ctx) -> None:
    """Full mode, hourly: sweep expired browser sessions, and run the §7 observed-flags
    census (TTL prune + high-cardinality suppression), reloading the observer's
    suppressed set from DB truth. The boot sweep only runs once; this keeps a
    long-running node from accumulating dead rows. Logs only when it changes something."""
    while True:
        await asyncio.sleep(_SESSION_SWEEP_SECONDS)
        try:
            def _pass() -> None:
                with session_scope() as s:
                    sweep_expired_sessions(s)
                settings = get_settings()
                if settings.observe_flags:
                    with session_scope() as s:
                        res = prune_and_census(
                            s, ttl_days=settings.observed_flags_ttl_days,
                            value_cap=settings.observed_flags_value_cap)
                        ctx.observer.set_suppressed(load_suppressions(s))
                    metrics.observed_flags_suppressed.set(res.total_suppressed)
            await asyncio.to_thread(_pass)  # blocking DB work off the serving loop
        except Exception:  # pragma: no cover - defensive; keep the loop alive
            log.exception("periodic session sweep errored")


_observed_flush_failing = False


def _observed_flags_pass(ctx) -> int:
    """One writer pass (run via to_thread): drain the observer's queue into one upsert,
    persist any suppressions the observer tripped since the last pass, evict expired
    marks, publish the §14 counters. A failed flush un-marks the batch so the next
    request re-queues it; the failure is logged once until a pass succeeds again.
    Returns rows written."""
    global _observed_flush_failing
    obs = ctx.observer.drain()
    tripped = ctx.observer.take_new_suppressions()
    written = 0
    try:
        if obs or tripped:
            with session_scope() as s:
                written = flush_observations(s, obs)
                if tripped:
                    record_suppressions(s, tripped, get_settings().observed_flags_value_cap)
        if _observed_flush_failing:
            log.info("observed flags: flush recovered")
            _observed_flush_failing = False
    except Exception as exc:
        ctx.observer.unmark(obs)
        if not _observed_flush_failing:
            log.warning("observed flags: flush failed (%s: %s); will retry — "
                        "observations are re-queued on the next request",
                        type(exc).__name__, exc)
            _observed_flush_failing = True
        return 0
    ctx.observer.sweep()
    st = ctx.observer.stats
    metrics.observed_flags_total.labels("written").inc(written)
    metrics.observed_flags_total.labels("deduped").inc(st.deduped)
    metrics.observed_flags_total.labels("dropped").inc(st.dropped)
    metrics.observed_flags_total.labels("ignored").inc(st.ignored)
    st.deduped = st.dropped = st.ignored = st.queued = 0
    return written


async def _observed_flags_loop(ctx) -> None:
    """Full mode: the §7 observed-flags writer — every INCANT_OBSERVED_FLAGS_FLUSH_SECONDS,
    move what the request path queued into Postgres. The request path itself never
    writes; this loop is the only place observations touch the DB."""
    interval = get_settings().observed_flags_flush_seconds
    while True:
        await asyncio.sleep(interval)
        try:
            await asyncio.to_thread(_observed_flags_pass, ctx)
        except Exception:  # pragma: no cover - defensive; keep the loop alive
            log.exception("observed flags writer pass errored")


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
            def _pass() -> None:
                with session_scope() as s:
                    recover_pending_promotions(s, ctx.git)
                    ctx.record_reconcile(reconcile_main_commits(s, ctx.git))
            await asyncio.to_thread(_pass)  # DB + git subprocess work off the loop
        except Exception:  # pragma: no cover - defensive; keep the loop alive
            log.exception("periodic main reconcile errored")


def _backup_pass(ctx) -> None:
    """One synchronous pusher pass on its own session (run via to_thread — a remote
    push blocks on the network and must not stall the event loop)."""
    with session_scope() as s:
        ctx.backup.push_pending(s)


async def _backup_push_loop(ctx) -> None:
    """Full mode: drain the backup queue to every enabled remote every
    INCANT_BACKUP_POLL_SECONDS (§6). The interval bounds the content-durability
    exposure window; per-remote failures are logged inside the pusher and never
    raise, so a dead remote just leaves its queue growing (and its lag gauge
    rising) until it recovers."""
    interval = get_settings().backup_poll_seconds
    while True:
        await asyncio.sleep(interval)
        try:
            await asyncio.to_thread(_backup_pass, ctx)
        except Exception:  # pragma: no cover - defensive; keep the loop alive
            log.exception("backup push pass errored")


def _fetch_pass(ctx) -> bool:
    with session_scope() as s:
        return ctx.backup.fetch_once(s)


async def _content_fetch_loop(app: FastAPI, ctx) -> None:
    """Serve mode: follow an enabled backup remote by mirror-fetch every
    INCANT_CONTENT_FETCH_SECONDS (§13/§15). Rules reach replicas via the DB poll;
    THIS is how content does — without it a make-live can reference a commit the
    replica's repo copy has never seen. After a successful fetch, a not-yet-ready
    replica gets an immediate readiness retry (the missing SHAs may just have
    arrived) instead of waiting out the warm-retry backoff."""
    interval = get_settings().content_fetch_seconds
    while True:
        await asyncio.sleep(interval)
        try:
            fetched = await asyncio.to_thread(_fetch_pass, ctx)
            if fetched and not getattr(app.state, "ready", False):
                ready, env_warm = _boot_prime(ctx)
                app.state.env_warm = env_warm
                if ready:
                    app.state.ready = True
                    log.info("content fetch completed warm; node is ready")
        except Exception:  # pragma: no cover - defensive; keep the loop alive
            log.exception("content fetch pass errored")


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
            def _pass() -> None:
                with session_scope() as s:
                    ctx.refresh_control_plane(s)
            await asyncio.to_thread(_pass)  # snapshot rebuilds must not stall renders
        except Exception:  # pragma: no cover - defensive; keep the loop alive
            log.exception("control-plane poll errored")


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    ctx = get_app()

    if settings.mode == "serve":
        _verify_serve_prerequisites(ctx)  # fail fast; no writes in serve mode
    else:
        # Exactly one full writer per deployment: claim the role before touching
        # anything (a second full node fails fast here, not mid-double-reconcile).
        claim_full_writer_role()
        # First-boot content bootstrap (§6): with INCANT_BOOTSTRAP_REMOTE set and an
        # EMPTY repo volume, clone the remote — a populated Incant repo gets adopted
        # below; a blank one means "start fresh and push here" (initialize() seeds
        # main into the empty clone). Unreachable ⇒ fail the boot: a mistyped remote
        # must not silently mint a fresh lineage that later force-pushes over the
        # real one.
        if settings.bootstrap_remote and not ctx.git.exists():
            try:
                ctx.git.clone_mirror(
                    settings.bootstrap_remote,
                    auth_ref=settings.bootstrap_remote_key or None,
                    known_hosts_path=settings.known_hosts_path or None,
                    timeout=settings.backup_timeout_seconds,
                )
            except Exception as exc:
                raise RuntimeError(
                    f"INCANT_BOOTSTRAP_REMOTE is set but could not be cloned: {exc}. "
                    "Refusing to initialize a fresh repository while a bootstrap "
                    "remote is configured — fix the URL/credentials or unset it."
                ) from exc
            log.info("bootstrap: cloned content repo from %s",
                     settings.bootstrap_remote)
        ctx.initialize()  # git init + schema (Alembic migrations)
        with session_scope() as s:
            ensure_bootstrap_admin(s, settings.bootstrap_admin_key)
        # Reconcile git draft refs against DB draft rows before serving warms, sweep any
        # expired browser sessions, and detect (log, never repair) main-commit drift. The
        # main-reconcile result is recorded on the ctx so /healthz + the incant_reconcile_*
        # gauges reflect drift from the very first boot, not just after the first interval.
        with session_scope() as s:
            # The configured serving default must always exist — a fresh database
            # would otherwise boot into a UI that 404s on its own default env (and
            # delete_env already refuses to remove it). Idempotent by lookup.
            from .. import models
            if s.get(models.Environment, settings.default_environment) is None:
                s.add(models.Environment(id=settings.default_environment,
                                         name=settings.default_environment))
                s.flush()
                ctx.targeting(s, "system").ensure_baseline(settings.default_environment)
                log.info("boot: created default environment %r",
                         settings.default_environment)
            # Bootstrap-remote registration (idempotent) + content ADOPTION: a
            # populated repo meeting an empty database rebuilds the registry from
            # the tree + trailers (bootstrap-from-remote AND manual volume restore
            # both land here). Runs before the sweeps so adopted content is never
            # censused as drift. Targeting/users/RBAC still need the PG backup.
            if settings.bootstrap_remote:
                exists = s.execute(
                    select(models.Remote).where(
                        models.Remote.url == settings.bootstrap_remote)
                ).scalar_one_or_none()
                if exists is None:
                    s.add(models.Remote(url=settings.bootstrap_remote, enabled=True,
                                        auth_ref=settings.bootstrap_remote_key or None))
            adopt_content_tree(s, ctx.git)
            # Pending recovery FIRST among the sweeps: a staged publish whose DB
            # transaction committed must reach main before the draft sweep would
            # clean up its (now orphan) draft ref, and before the main sweep takes
            # its drift census.
            recover_pending_promotions(s, ctx.git)
            reconcile_drafts(s, ctx.git)
            sweep_expired_sessions(s)
            ctx.record_reconcile(reconcile_main_commits(s, ctx.git))
            # §7 observed flags: the observer must know which flags are suppressed
            # BEFORE the first request, or a suppressed user_id-class flag would
            # re-accumulate up to the cap after every restart.
            if settings.observe_flags:
                ctx.observer.set_suppressed(load_suppressions(s))

    # Readiness (both modes) requires warming EVERY environment (content + snapshot) AND
    # priming the auth cache — so "ready" honestly means "can serve + authenticate with
    # zero DB reads" (§8/§10). Any failure leaves the node not ready; in full mode a
    # background loop keeps retrying both, and in serve mode the same loop lets a replica
    # become ready once the full node has published its content.
    app.state.ready, app.state.env_warm = _boot_prime(ctx)
    retry_task = None
    if not app.state.ready or not all(app.state.env_warm.values()):
        if not app.state.ready:
            log.warning("warm/auth priming incomplete at boot — node not ready; "
                        "retrying in background")
        retry_task = asyncio.create_task(_warm_retry_loop(app, ctx))

    # Hourly expired-session sweep + periodic main-commit drift check + backup pushes
    # (full mode only — serve replicas have no sessions, never own the canonical main,
    # and never push). Serve replicas instead FOLLOW a backup remote for content.
    sweep_task = reconcile_task = backup_task = fetch_task = observed_task = None
    if settings.mode == "full":
        sweep_task = asyncio.create_task(_session_sweep_loop(ctx))
        reconcile_task = asyncio.create_task(_reconcile_loop(ctx))
        if settings.backup_poll_seconds > 0:
            backup_task = asyncio.create_task(_backup_push_loop(ctx))
        if settings.observe_flags:
            observed_task = asyncio.create_task(_observed_flags_loop(ctx))
    elif settings.content_fetch_seconds > 0:
        fetch_task = asyncio.create_task(_content_fetch_loop(app, ctx))

    # Control-plane poll (BOTH modes): the serving hot path never reads the DB itself;
    # this loop pulls targeting bumps + auth changes into memory (§7 poll fallback, §8/§10).
    poll_task = asyncio.create_task(_control_poll_loop(ctx))

    try:
        yield
    finally:
        # Graceful shutdown: cancel AND await each loop so an in-flight pass (a
        # backup push, a reconcile sweep) finishes its cancellation cleanly instead
        # of dying mid-await when the loop is torn down.
        tasks = [t for t in (retry_task, sweep_task, reconcile_task, backup_task,
                             fetch_task, observed_task, poll_task) if t is not None]
        for task in tasks:
            task.cancel()
        for task in tasks:
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001 - shutdown
                pass
        release_full_writer_role()


def _has_viewer_anywhere(ident) -> bool:
    """True iff the principal holds `viewer` (directly or by implication) at *any*
    scope — instance, project, or (project, env). /metrics is non-sensitive read-only
    telemetry, so scope doesn't matter; a renderer-only key (no viewer) is refused."""
    return any("viewer" in _IMPLIES.get(b.role, set()) for b in ident.bindings)


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="Incant",
        version="1.0.0",
        lifespan=lifespan,
        description=(
            "Prompt management: git for content, flag-based targeting for who sees "
            "what. Serving endpoints (`/prompt/*`, `/evaluate`) take a bearer API key "
            "with `renderer` scope; `/mgmt/*` is the authoring/targeting/admin "
            "surface. Every render reports the resolved version and commit SHA of "
            "the prompt and every included fragment — feed the `versions` map back "
            "as `pin` to reproduce a render exactly."
        ),
    )
    app.state.ready = False

    @app.exception_handler(RequestValidationError)
    async def _validation_hint(request: Request, exc: RequestValidationError):
        # FastAPI's default 422 for an unparseable body is famously cryptic when the
        # real problem is a missing Content-Type: curl defaults to form encoding, the
        # body never parses, and the error blames "input". Say the actual fix.
        body = {"detail": jsonable_encoder(exc.errors())}
        ct = request.headers.get("content-type", "")
        if request.method in ("POST", "PUT", "PATCH") and "application/json" not in ct:
            body["hint"] = (
                f"request Content-Type is {ct or 'not set'!r} — this endpoint expects a "
                "JSON body; send the header 'Content-Type: application/json'"
            )
        return JSONResponse(status_code=422, content=body)

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
        body: dict = {}
        if res is not None and (res.git_orphans or res.unvalidated_tips or res.missing_files):
            body["drift"] = {
                "git_orphans": res.git_orphans,
                "unvalidated_tips": res.unvalidated_tips,
                "missing_files": res.missing_files,
            }
        # Environments that failed their last warm pass. The node stays green — §10's
        # per-request honesty (fallback or 409) answers for them, and the retry loop
        # keeps re-warming — but a green node must SAY what it is not vouching for.
        degraded = sorted(e for e, ok in getattr(app.state, "env_warm", {}).items()
                          if not ok)
        if degraded:
            body["degraded_environments"] = degraded
        if body:
            return JSONResponse({"status": "ok", **body})
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
