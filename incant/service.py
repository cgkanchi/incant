"""AppContext — wires the git store, content cache, DB, and serving hot path.

Holds the per-environment snapshot cache (RulesSync, poll-fallback form): a
snapshot is rebuilt when the environment's ``rules_version`` advances. If the DB
is unreachable, serving continues on the last-known-good snapshot with
``stale_rules: true`` — the design's "rules freeze" availability posture.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field, replace
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from . import models
from .config import Settings, get_settings
from .core import (
    EnvSnapshot,
    MissingVariable,
    RenderError,
    Resolution,
    Unservable,
    UnresolvedPrompt,
    render,
    render_source,
    resolve,
)
from .db import init_db, session_scope
from .gitstore import ContentStore, GitStore
from .registry import MainReconcileResult, RegistryService
from .targeting import TargetingService, build_snapshot

log = logging.getLogger("incant.service")


class ServingError(Exception):
    def __init__(self, status: int, detail: str, **extra: Any) -> None:
        self.status = status
        self.detail = detail
        self.extra = extra
        super().__init__(detail)


class WarmError(Exception):
    """Warming an environment failed: a version that a *live pointer* references has no
    servable content at all — its live SHA and every previous-live fallback SHA are
    unfetchable. Tips and other history are best-effort and never raise this. The boot
    warm treats it as "not ready" and the background loop keeps retrying (§10)."""

    def __init__(self, env_id: str, prompt_id: str, version: int, sha: str | None) -> None:
        self.env_id = env_id
        self.prompt_id = prompt_id
        self.version = version
        self.sha = sha
        super().__init__(
            f"environment {env_id!r}: live pointer for {prompt_id!r} v{version} "
            f"(sha {sha}) has no servable content — its live SHA and every previous-live "
            "fallback SHA are unfetchable"
        )


@dataclass
class _CachedSnapshot:
    rules_version: int
    snapshot: EnvSnapshot


@dataclass
class AppContext:
    settings: Settings = field(default_factory=get_settings)
    _snapshots: dict[str, _CachedSnapshot] = field(default_factory=dict)
    # DB health as last observed by the background poll (refresh_control_plane), NOT by
    # any request — the serving hot path never touches the DB to learn this. False means
    # the poller last saw an outage, so warm snapshots are served frozen (§10 "rules
    # freeze") with ``stale_rules: true`` until a healthy poll clears it.
    _db_healthy: bool = True
    # Latest git↔DB main-commit drift result (``reconcile_main_commits``), set by the boot
    # sweep and the periodic reconcile loop (server.app). Read by /healthz to SURFACE drift
    # without flipping readiness (§3, §5) — see :meth:`record_reconcile`. None until the
    # first sweep runs (e.g. serve replicas, which never sweep main).
    last_reconcile: MainReconcileResult | None = None

    def __post_init__(self) -> None:
        self.git = GitStore(self.settings.repo_dir())
        self.content = ContentStore(self.git)
        # Lazy import avoids an import cycle (server.auth -> ... -> service).
        from .server.auth import AuthCache
        from .server.throttle import AuthThrottler
        self.auth = AuthCache(ttl=self.settings.auth_ttl)
        self.throttle = AuthThrottler()

    # ── auth (in-memory; survives DB outages) ─────────────────────────

    def authenticate(self, session: Session, authorization: str | None):
        return self.auth.identify(session, authorization)

    def invalidate_auth(self) -> None:
        self.auth.invalidate()

    # ── lifecycle ────────────────────────────────────────────────────

    def initialize(self) -> None:
        self.git.init()
        init_db()

    def registry(self, session: Session, actor: str = "system") -> RegistryService:
        return RegistryService(session, self.git, self.content,
                               default_env=self.settings.default_environment)

    def targeting(self, session: Session, actor: str = "system") -> TargetingService:
        return TargetingService(session, actor)

    # ── snapshots ────────────────────────────────────────────────────

    def get_snapshot(self, session: Session, env_id: str) -> EnvSnapshot:
        """Return an environment's targeting snapshot for the render hot path.

        DESIGN.md §8 ("No DB per request") and §10 ("Postgres … sits on the refresh/write
        paths only, never per-request") require this to be memory-only when the node is
        warm. It is: a cached environment is served straight from the in-memory snapshot
        with **no** DB read. Freshness comes from off the request path — the background
        poll (:meth:`refresh_control_plane`, §7's LISTEN/NOTIFY 2s-poll fallback) rebuilds
        cached snapshots when ``rules_version`` advances, and same-process control-plane
        writes call :meth:`invalidate` for immediate same-node freshness.

        Two departures from the pure memory read, both preserving the §10 posture:

        * **DB frozen** — the poller last saw an outage (``_db_healthy`` False): serve the
          last-known-good snapshot but flagged ``stale``. Return a *copy* via ``replace``;
          never mutate the cached snapshot, so the flag clears the instant a healthy poll
          flips ``_db_healthy`` back, with no sticky mutation to unwind.
        * **Cold miss** — an environment this process has never warmed: build it from the
          DB now. This is the one permitted per-request read (a cache miss, mirroring the
          §8 content-cache-miss exception); a DB failure here is a genuine 503 because
          there is nothing cached to freeze on.
        """
        cached = self._snapshots.get(env_id)
        if cached is not None:
            if not self._db_healthy:
                return replace(cached.snapshot, stale=True)
            return cached.snapshot
        # Cold: never warmed in-process. Build from the DB (the permitted cache-miss read).
        try:
            env = session.get(models.Environment, env_id)
            if env is None:
                raise ServingError(404, f"unknown environment {env_id!r}")
            snap = build_snapshot(session, env_id)
            self._snapshots[env_id] = _CachedSnapshot(env.rules_version, snap)
            return snap
        except SQLAlchemyError:
            raise ServingError(503, "node not ready: no cached targeting")

    def refresh_control_plane(self, session: Session) -> None:
        """Pull targeting + auth changes from the DB into memory. This is the ONLY place
        the control plane reaches the serving snapshots on a warm node, and it is driven
        by the background poll loop (``server.app._control_poll_loop``), never by a
        request. That is precisely what buys DESIGN.md §8's "No DB per request" and §10's
        "the DB is never on the per-request path": the periodic DB read moves off the hot
        path onto this poll — the fallback for §7's Postgres LISTEN/NOTIFY — so a targeting
        change (including "make live") lands on every replica in < 2 s.

        One best-effort pass:

        * SELECT every environment's ``(id, rules_version)`` in a single query. For each
          environment already cached whose ``rules_version`` advanced (e.g. a write on
          another replica), rebuild its snapshot and atomically swap the cache entry —
          built fully *then* assigned, so a concurrent reader on the hot path never sees a
          half-built snapshot. Cold (uncached) environments are left alone; they build
          lazily on first request in :meth:`get_snapshot`.
        * Refresh the in-memory auth cache when its table has aged past its TTL.

        Availability (§10 "rules freeze"): on any ``SQLAlchemyError`` mark the node
        DB-unhealthy, roll back, and return WITHOUT raising. Serving keeps running on the
        last-known-good snapshots (now stale-flagged by :meth:`get_snapshot`) until a later
        poll succeeds and clears the flag — the loop must never die on a transient outage.
        """
        try:
            rows = session.execute(
                select(models.Environment.id, models.Environment.rules_version)
            ).all()
            for env_id, rules_version in rows:
                cached = self._snapshots.get(env_id)
                if cached is not None and cached.rules_version != rules_version:
                    snap = build_snapshot(session, env_id)              # build fully…
                    self._snapshots[env_id] = _CachedSnapshot(rules_version, snap)  # …then swap
            # The TTL-driven whole-table auth reload lives here now — off the hot path (§8).
            self.auth.refresh(session)
            self._db_healthy = True
        except SQLAlchemyError:
            self._db_healthy = False
            try:
                session.rollback()
            except SQLAlchemyError:
                pass

    def auto_advance_tips(self, session: Session, actor: str, prompt_id: str,
                          version: int, sha: str) -> list[str]:
        """§7 track_tip: in environments that track validated tips, advance an
        *existing* live pointer for (prompt, version) to the new tip. Returns the
        list of environments advanced."""
        advanced: list[str] = []
        envs = session.execute(
            select(models.Environment).where(models.Environment.track_tip.is_(True))
        ).scalars().all()
        for env in envs:
            tgt = self.targeting(session, actor)
            if tgt.current_live(env.id, prompt_id, version) is None:
                continue  # nothing live to follow
            tgt.make_live(env.id, prompt_id, version, sha,
                          comment="track_tip auto-advance")
            self.invalidate(env.id)
            advanced.append(env.id)
        return advanced

    def invalidate(self, env_id: str | None = None) -> None:
        if env_id is None:
            self._snapshots.clear()
        else:
            self._snapshots.pop(env_id, None)

    # ── governance drift (observability, never gates serving) ─────────

    def record_reconcile(self, result: MainReconcileResult) -> None:
        """Record the latest git↔DB main-commit drift result and publish it to the
        Prometheus gauges. Called by the boot sweep and the periodic reconcile loop
        (server.app). The stored value is read by /healthz to surface drift WITHOUT
        flipping readiness — a drifted node still serves correctly from the last VALIDATED
        SHAs (§5), so taking it out of rotation would turn a governance alarm into an
        outage. Lazy import mirrors the __post_init__ idiom (server → service cycle)."""
        self.last_reconcile = result
        from .server.metrics import update_reconcile_metrics
        update_reconcile_metrics(result)

    # ── warming ──────────────────────────────────────────────────────

    def _warmable(self, prompt_id: str, version: int, sha: str) -> bool:
        """Try to warm one SHA; True iff its content was fetchable (and now cached)."""
        try:
            self.content.warm(prompt_id, version, sha)
            return True
        except KeyError:
            return False

    def warm(self, session: Session, env_id: str) -> None:
        """Eager-warm the content cache for everything reachable in an environment.

        Live pointers *and each version's previous-live* (the §10 fallback must be
        warm to be useful) and tips.

        Failure criterion (§10): warming FAILS — raising :class:`WarmError` — only when a
        version referenced by a *live pointer* has no servable content at all: its live
        SHA and every previous-live fallback SHA are unfetchable. That is the one state a
        node cannot honestly serve from. Everything else is tolerated:

        * live SHA unfetchable but a previous-live fallback warms → WARNING, still
          succeeds (serving will step back within the version's own history, §10);
        * a missing tip, or a version with no live pointer at all → skipped silently
          (best-effort; the design requires tolerating missing content when a fallback,
          or simply no live obligation, exists).
        """

        snap = build_snapshot(session, env_id)
        for prompt_id, vers in snap.versions.items():
            for vnum, vinfo in vers.items():
                # Tips and prior history are best-effort — warm what we can, skip misses.
                for sha in filter(None, (vinfo.tip_sha, *vinfo.previous_live)):
                    self._warmable(prompt_id, vnum, sha)

                if vinfo.live_sha is None:
                    continue  # no live pointer → no serving obligation for this version
                if self._warmable(prompt_id, vnum, vinfo.live_sha):
                    continue  # fully healthy: the live SHA itself is warm
                # Live SHA is unfetchable — a §10 previous-live fallback may still serve.
                if any(self._warmable(prompt_id, vnum, s) for s in vinfo.previous_live):
                    log.warning(
                        "warm: live SHA %s for %s v%d in env %r is unfetchable; serving "
                        "will fall back to a previous-live SHA (degraded but available)",
                        vinfo.live_sha, prompt_id, vnum, env_id,
                    )
                    continue
                raise WarmError(env_id, prompt_id, vnum, vinfo.live_sha)

        # Reaching here means the §10 criterion is satisfied for every live pointer: each
        # either warmed or has a warm previous-live fallback (the one unservable state
        # raised WarmError above). Only NOW — after content warming for this env has
        # succeeded — do we install the snapshot into the same cache the hot path reads
        # (:meth:`get_snapshot`), in its ``_CachedSnapshot(rules_version, snap)`` shape.
        # Readiness must mean "can serve THIS env with zero DB reads" (§8 "No DB per
        # request"; §10 "the DB is never on the per-request path"): without this install
        # the FIRST render after /readyz went green would do a cold snapshot build (a DB
        # read), so a node that just reported ready would 503 if Postgres died the instant
        # after. With it, that first render is a pure memory hit off already-warm content.
        self._snapshots[env_id] = _CachedSnapshot(snap.rules_version, snap)

    # ── serving ──────────────────────────────────────────────────────

    def evaluate(self, session: Session, env_id: str, prompt_id: str, flags: dict) -> Resolution:
        snap = self.get_snapshot(session, env_id)
        try:
            return resolve(snap, prompt_id, flags)
        except UnresolvedPrompt:
            raise ServingError(404, f"no resolution for {prompt_id!r} in {env_id!r}")
        except Unservable:
            raise ServingError(409, f"resolved content for {prompt_id!r} is unservable")

    def evaluate_all(self, session: Session, env_id: str, flags: dict) -> dict[str, Resolution]:
        snap = self.get_snapshot(session, env_id)
        out: dict[str, Resolution] = {}
        for pid in snap.all_prompt_ids():
            try:
                out[pid] = resolve(snap, pid, flags)
            except (UnresolvedPrompt, Unservable):
                continue
        return out

    def render_draft_source(
        self, session: Session, env_id: str, prompt_id: str, source: str,
        flags: dict, variables: dict,
    ) -> str:
        """Render an explicit draft/source top-level, resolving includes live."""
        snap = self.get_snapshot(session, env_id)
        result = render_source(snap, prompt_id, source, flags, variables, self.content)
        return result.text

    def render_at(
        self, session: Session, env_id: str, prompt_id: str, version: int, sha: str,
        flags: dict, variables: dict,
    ) -> str:
        """Render a specific committed SHA as the top-level (for rendered diffs)."""
        blob = self.content.get(prompt_id, version, sha)
        return self.render_draft_source(session, env_id, prompt_id, blob.source, flags, variables)

    def serve(
        self, session: Session, env_id: str, prompt_id: str,
        flags: dict, variables: dict, pin: dict | None = None,
    ) -> dict:
        snap = self.get_snapshot(session, env_id)

        # Determine the root version — for defaults lookup — honouring a pin (§9).
        pinned = (pin or {}).get(prompt_id)
        if pinned is not None:
            root_version = pinned[0]
        else:
            try:
                root_version = resolve(snap, prompt_id, flags).version
            except UnresolvedPrompt:
                raise ServingError(404, f"unknown prompt {prompt_id!r} in {env_id!r}")
            except Unservable:
                raise ServingError(409, f"resolved content for {prompt_id!r} is unservable")

        # Optional-variable defaults come from the snapshot (no per-request DB read).
        defaults = snap.refinement_defaults.get((prompt_id, root_version), {})

        try:
            result = render(snap, prompt_id, flags, variables, self.content,
                            defaults=defaults, pin=pin)
        except MissingVariable as exc:
            raise ServingError(422, str(exc), variable=exc.name)
        except RenderError as exc:
            raise ServingError(422, str(exc), lineno=exc.lineno)
        except Unservable:
            raise ServingError(409, f"resolved content for {prompt_id!r} is unservable")
        except KeyError:
            raise ServingError(409, "resolved content missing from store")

        versions = {}
        for pid, res in result.contributions.items():
            # Full 40-char SHAs: the reproducibility tuple must be SHA-exact and is fed
            # back verbatim as a `pin` (which now accepts only full SHAs — §9, §4).
            entry = {"version": res.version, "commit": res.commit, "label": res.label}
            if res.content_fallback:
                entry["fallback"] = True
            versions[pid] = entry

        matched = (
            "default" if result.root.match_scope == "default"
            else {"scope": result.root.match_scope, "id": result.root.rule_id}
        )
        return {
            "prompt": result.text,
            "prompt_id": prompt_id,
            "matched_rule": matched,
            "versions": versions,
            "environment": env_id,
            "rules_version": snap.rules_version,
            "stale_rules": snap.stale,
            "content_fallback": result.content_fallback,
        }


_app: AppContext | None = None


def get_app() -> AppContext:
    global _app
    if _app is None:
        _app = AppContext()
    return _app


def reset_app() -> None:
    global _app
    _app = None
