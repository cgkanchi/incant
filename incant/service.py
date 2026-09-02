"""AppContext — wires the git store, content cache, DB, and serving hot path.

Holds the per-environment snapshot cache (RulesSync, poll-fallback form): a
snapshot is rebuilt when either of the environment's freshness keys advances —
``rules_version`` (targeting) or ``content_version`` (validated SHAs, versions,
variable defaults). If the DB is unreachable, serving continues on the
last-known-good snapshot with ``stale_rules: true`` — the design's "rules freeze"
availability posture.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field, replace
from typing import Any, Iterable

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from . import models
from .config import Settings, get_settings
from .core import (
    IncludeDepthExceeded,
    IncludeCycle,
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
from .db import after_commit, init_db
from .gitstore import BackupPusher, ContentStore, GitStore
from .registry import MainReconcileResult, RegistryService
from .targeting import TargetingService, build_snapshot, snapshot_from_state
from .targeting.snapshot import load_validated_index

log = logging.getLogger("incant.service")


def _record_snapshot_build_failure(env_id: str) -> None:
    """Best-effort §14 counter (lazy import: server.metrics depends on this module)."""
    try:
        from .server.metrics import snapshot_build_failures_total
        snapshot_build_failures_total.labels(env_id).inc()
    except Exception:  # pragma: no cover - telemetry must never affect serving
        pass


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
    # The two poll freshness keys the snapshot was built under (models.Environment):
    # targeting moves rules_version, content inputs move content_version. A rebuild is
    # due when EITHER differs from the DB's — one key alone missed every commit.
    rules_version: int
    content_version: int
    # The environment ROW's immutable identity (models.Environment.incarnation). Both
    # counters restart at 1 on a delete+recreate under the same id, so they cannot by
    # themselves distinguish "the row I cached" from "a new row wearing its id" (ABA);
    # a differing incarnation means the cached snapshot describes a DEAD environment
    # and must be dropped — replay memos included — never "confirmed" as fresh.
    incarnation: str
    snapshot: EnvSnapshot
    # Monotonic time of the last successful freshness check against the DB (build,
    # or a healthy poll confirming both keys unchanged). Feeds the §14
    # incant_rules_snapshot_age_seconds gauge: a rising age means the poll can't
    # reach Postgres and targeting changes are not propagating to this node.
    confirmed_at: float = field(default_factory=time.monotonic)


@dataclass
class _RefreshPlan:
    """The cache changes one control-plane read implies — computed apart from applying
    them, so a write can BUILD inside its own transaction and SWAP after its commit."""

    rebuilt: dict[str, _CachedSnapshot] = field(default_factory=dict)
    evicted: set[str] = field(default_factory=set)    # cached ids the DB no longer has
    confirmed: set[str] = field(default_factory=set)  # cached, keys unchanged, not forced
    # Ids whose DB row's incarnation differs from the cached one (a delete+recreate,
    # or a rename, happened between polls) → the STALE incarnation the cache held when
    # this plan was built. Applying such an entry also drops the env's replay memos,
    # and the apply-time value lets us tell "installing over the stale entry we
    # planned against" from "the cache reincarnated AGAIN after this plan" (skip).
    reincarnated: dict[str, str] = field(default_factory=dict)


_HISTORICAL_CACHE_MAX = 64  # replayed (env, rules_version) snapshots kept in memory


@dataclass
class AppContext:
    settings: Settings = field(default_factory=get_settings)
    _snapshots: dict[str, _CachedSnapshot] = field(default_factory=dict)
    # §9 pin.rules_version replay: reconstructed historical snapshots, LRU-bounded,
    # keyed by (env_id, incarnation, rules_version). The incarnation is in the key
    # because revision numbers restart when an id is deleted and recreated — an old
    # life's rules_version must never resolve to (or shadow) the new life's replay.
    # The cached object is the IMMUTABLE historical targeting state only; its two
    # CURRENT-state overlays (`servable`, `refinement_defaults`) are re-attached from
    # the live snapshot on every :meth:`snapshot_at` return — freezing them at first
    # build made a replay wrongly 409 a SHA validated later (see snapshot_at).
    _historical: "OrderedDict[tuple[str, str, int], EnvSnapshot]" = field(
        default_factory=OrderedDict)
    # Revisions whose recorded state can never be replayed (pre-1.1.0 shapes). A
    # revision's state is immutable, so the refusal is too — memoized (LRU-bounded)
    # so a renderer retrying a known-bad pin costs no control-plane queries. Same
    # key shape as _historical, incarnation included.
    _unreplayable: "OrderedDict[tuple[str, str, int], str]" = field(default_factory=OrderedDict)
    # Guards every mutation of the two OrderedDicts above. snapshot_at's LRU ops
    # (get + move_to_end + popitem) run on request threads (Starlette's threadpool)
    # while the poll thread evicts through _apply_refresh; unsynchronized, a
    # concurrent move_to_end/popitem pair can raise KeyError — a sporadic 500 on a
    # perfectly healthy replay. Critical sections are a few dict ops, never a build.
    _replay_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
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
        self.backup = BackupPusher(
            self.git,
            known_hosts_path=self.settings.known_hosts_path or None,
            timeout=self.settings.backup_timeout_seconds,
        )
        # Lazy import avoids an import cycle (server.auth -> ... -> service).
        from .server.auth import AuthCache
        from .server.throttle import AuthThrottler
        from .targeting.observed import FlagObserver
        self.auth = AuthCache(ttl=self.settings.auth_ttl)
        self.throttle = AuthThrottler()
        # §7 observed flags: request-path recorder (dict check + bounded queue, never a
        # DB write). Full mode only — serve replicas have no write path (v1).
        self.observer = FlagObserver(
            enabled=self.settings.observe_flags and self.settings.mode == "full",
            dedupe_seconds=self.settings.observed_flags_dedupe_seconds,
            max_pending=self.settings.observed_flags_max_pending,
            value_cap=self.settings.observed_flags_value_cap,
            exclude=self.settings.observed_flags_exclude_set(),
        )

    # ── auth (in-memory; survives DB outages) ─────────────────────────

    def authenticate(self, session: Session, authorization: str | None):
        return self.auth.identify(session, authorization)

    def invalidate_auth(self) -> None:
        self.auth.invalidate()

    def invalidate_after_commit(self, session: Session, env_id: str | None = None) -> None:
        """Bring THIS node's snapshots up to date the instant ``session``'s transaction
        commits — same-node freshness for a control-plane write.

        The affected snapshots are rebuilt NOW, inside the write's own transaction (which
        sees its uncommitted rows), and only SWAPPED IN after the commit; a rollback
        discards them (``db.after_commit``). Two properties hang on that shape:

        * The cache never empties. The old "clear the cache" made the next request a cold
          DB build — a 503 on a Postgres blip, the §10 rules-freeze posture lost in
          exactly that window. Here a rebuilt entry replaces the old one atomically.
        * The swap is instant. FastAPI's request-scoped session dependency commits AFTER
          the response is sent, so a client's very next request can arrive between the
          commit and whatever runs after it. A post-commit rebuild would widen that
          window by a whole build and the client would still see the old snapshot —
          breaking "commit, then render sees the new tip". Building before the commit
          keeps the window at the commit alone, the same as the old clear.

        ``env_id`` names an environment to rebuild even if its freshness keys did not
        move (an env-settings edit mints no revision); None means "whatever the keys say
        changed" — right for content writes, which bump every ``content_version``. Call
        it after the write's LAST mutation: the plan reflects the transaction as of this
        call (a later call with the same ``env_id`` supersedes an earlier one).
        """
        plan = self._plan_refresh(session, force=(env_id,) if env_id is not None else ())
        after_commit(
            session,
            ("snapshot", id(self), env_id),
            lambda: self._apply_refresh(plan),
        )

    def invalidate_auth_after_commit(self, session: Session) -> None:
        after_commit(
            session,
            ("auth", id(self)),
            self.invalidate_auth,
        )

    # ── lifecycle ────────────────────────────────────────────────────

    def initialize(self) -> None:
        self.git.init()
        init_db()

    def registry(self, session: Session, actor: str = "system") -> RegistryService:
        return RegistryService(session, self.git, self.content,
                               default_env=self.settings.default_environment, actor=actor)

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
            # Keys read BEFORE the build: a bump landing mid-build then leaves this entry
            # behind and the next poll rebuilds — never a snapshot recorded as current
            # that isn't.
            snap = build_snapshot(session, env_id)
            self._snapshots[env_id] = _CachedSnapshot(
                env.rules_version, env.content_version, env.incarnation, snap)
            return snap
        except SQLAlchemyError:
            raise ServingError(503, "node not ready: no cached targeting")

    def refresh_control_plane(self, session: Session) -> None:
        """Pull targeting + content + auth changes from the DB into memory. This is the
        ONLY place the control plane reaches the serving snapshots on a warm node from
        OUTSIDE a write, and it is driven by the background poll loop
        (``server.app._control_poll_loop``), never by a request. That is precisely what
        buys DESIGN.md §8's "No DB per request" and §10's "the DB is never on the
        per-request path": the periodic DB read moves off the hot path onto this poll —
        the fallback for §7's Postgres LISTEN/NOTIFY — so a targeting change (including
        "make live") or a publish lands on every replica in < 2 s. (A node's OWN writes
        take the same plan/apply pair through :meth:`invalidate_after_commit`.)

        One best-effort pass — :meth:`_plan_refresh` then :meth:`_apply_refresh`:

        * SELECT every environment's ``(id, rules_version, content_version)`` in a single
          query. For each environment already cached where EITHER key differs from the
          cached one (a targeting write, or a commit/refinement bumping every
          ``content_version`` — e.g. on the full node while this is a replica), rebuild
          its snapshot and atomically swap the cache entry — built fully *then* assigned,
          so a concurrent reader on the hot path never sees a half-built snapshot. Cold
          (uncached) environments are left alone; they build lazily on first request in
          :meth:`get_snapshot`.
        * Refresh the in-memory auth cache when its table has aged past its TTL.

        Availability (§10 "rules freeze"): on any ``SQLAlchemyError`` mark the node
        DB-unhealthy, roll back, and return WITHOUT raising. Serving keeps running on the
        last-known-good snapshots (now stale-flagged by :meth:`get_snapshot`) until a later
        poll succeeds and clears the flag — the loop must never die on a transient outage.
        """
        try:
            self._apply_refresh(self._plan_refresh(session))
            # The TTL-driven whole-table auth reload lives here now — off the hot path (§8).
            self.auth.refresh(session)
            self._db_healthy = True
        except SQLAlchemyError:
            self._db_healthy = False
            try:
                session.rollback()
            except SQLAlchemyError:
                pass
        self._publish_snapshot_ages()

    def _plan_refresh(self, session: Session, *, force: Iterable[str] = ()) -> _RefreshPlan:
        """Read every environment's freshness keys and BUILD the snapshots whose keys
        differ from the cache (or that are ``force``d) — without touching the cache.

        The validated-commit index (the ``servable`` predicate) is environment-
        independent, so a plan loads it at most once and every snapshot it builds shares
        that one set; a plan that builds nothing never loads it. Bad data in ONE
        environment (an unparseable rule) must not stall propagation for the others: it
        is logged and counted, that environment keeps its last good snapshot, and the
        pass carries on. DB errors propagate — the caller decides what an outage means.
        """
        forced = set(force)
        plan = _RefreshPlan()
        rows = session.execute(
            select(models.Environment.id, models.Environment.rules_version,
                   models.Environment.content_version, models.Environment.incarnation)
        ).all()
        live = {env_id for env_id, _, _, _ in rows}
        # list() copy: request threads write to the cache concurrently with the poll.
        plan.evicted = {e for e in list(self._snapshots) if e not in live}
        validated_index = None   # loaded on first build, then shared by the plan
        for env_id, rules_version, content_version, incarnation in rows:
            cached = self._snapshots.get(env_id)
            if cached is None:
                continue
            # "Unchanged" requires the same ROW, not just the same counters: a
            # delete+recreate under this id restarts both counters, so identical
            # values with a different incarnation are a brand-new environment that
            # must be rebuilt (and its replay memos dropped), never confirmed.
            if (cached.incarnation == incarnation
                    and cached.rules_version == rules_version
                    and cached.content_version == content_version
                    and env_id not in forced):
                plan.confirmed.add(env_id)
                continue
            if cached.incarnation != incarnation:
                plan.reincarnated[env_id] = cached.incarnation
            if validated_index is None:
                validated_index = load_validated_index(session)
            try:
                snap = build_snapshot(session, env_id, validated_index=validated_index)
            except SQLAlchemyError:
                raise
            except Exception:
                if env_id in plan.reincarnated:
                    # "Serve the last good snapshot" is only honest when that snapshot
                    # describes THIS environment. Here it describes a deleted row that
                    # happens to share the id — evict it (replay memos included) and
                    # let the cold-miss path (or the next poll) build the new one.
                    log.exception("snapshot rebuild failed for RECREATED environment "
                                  "%r — evicting the previous incarnation's snapshot; "
                                  "other environments continue", env_id)
                    _record_snapshot_build_failure(env_id)
                    plan.evicted.add(env_id)
                    continue
                log.exception("snapshot rebuild failed for environment %r — serving "
                              "its last good snapshot; other environments continue",
                              env_id)
                _record_snapshot_build_failure(env_id)
                continue
            plan.rebuilt[env_id] = _CachedSnapshot(
                rules_version, content_version, incarnation, snap)
        return plan

    def _apply_refresh(self, plan: _RefreshPlan) -> None:
        """Swap a plan into the cache. Each rebuilt entry replaces its predecessor whole
        (a hot-path reader sees the old or the new snapshot, never a half-built one); an
        environment deleted on another node — or by the write that planned this — stops
        serving here too, replay states included; unchanged entries are stamped as
        freshly confirmed against the DB (the §14 age gauge).

        The swap is generation-checked: a plan is a detached read of an OLDER DB state,
        and by the time it lands here a write may already have installed a NEWER
        snapshot through :meth:`invalidate_after_commit`'s in-transaction build. The
        cache must never move backward — a slow poll silently regressing a node to
        pre-write rules/content — so an entry only installs over a same-incarnation
        predecessor whose freshness keys it does not undercut. Equal keys still
        install: a forced rebuild (env-settings edit) moves no counter yet must land."""
        for env_id in plan.evicted:
            self._snapshots.pop(env_id, None)
            self._drop_replay_states(env_id)
            log.warning("environment %r no longer exists — evicted from the snapshot cache", env_id)
        now = time.monotonic()
        for env_id, entry in plan.rebuilt.items():
            current = self._snapshots.get(env_id)
            if current is not None:
                if current.incarnation != entry.incarnation:
                    # The cache reincarnated AFTER this plan was built (a request
                    # cold-built the recreated env mid-plan): unless the cached entry
                    # is exactly the stale incarnation this plan set out to replace,
                    # the plan's entry describes a row that no longer exists —
                    # installing it would resurrect a deleted environment.
                    if current.incarnation != plan.reincarnated.get(env_id):
                        continue
                elif (current.rules_version > entry.rules_version
                      or current.content_version > entry.content_version):
                    # A concurrent write already installed something strictly newer.
                    # Keep it; the plan's DB read still proves the env exists, so
                    # re-confirm for the §14 age gauge rather than letting it climb.
                    current.confirmed_at = now
                    continue
            if env_id in plan.reincarnated:
                # Same id, different row (delete+recreate, or a rename minting a new
                # identity): the §9 replay memos belong to the DEAD row — an old
                # life's rules_version must not resolve against the new one.
                self._drop_replay_states(env_id)
            self._snapshots[env_id] = entry
        for env_id in plan.confirmed:
            cached = self._snapshots.get(env_id)
            if cached is not None:
                cached.confirmed_at = now

    def _drop_replay_states(self, env_id: str) -> None:
        """Forget every §9 replay memo for an environment — the reconstructed
        snapshots AND the memoized refusals (a recreated env's revision numbers
        restart, so a stale refusal would 422 a perfectly replayable pin). Runs on
        the poll thread while request threads do LRU ops in :meth:`snapshot_at`;
        the shared lock keeps the OrderedDicts coherent."""
        with self._replay_lock:
            for key in [k for k in self._historical if k[0] == env_id]:
                del self._historical[key]
            for key in [k for k in self._unreplayable if k[0] == env_id]:
                del self._unreplayable[key]

    def _publish_snapshot_ages(self) -> None:
        """§14 incant_rules_snapshot_age_seconds — refreshed by every poll pass, on
        success AND failure, so an outage shows up as ages climbing in lockstep.
        Lazy import mirrors record_reconcile (server → service cycle)."""
        try:
            from .server.metrics import rules_snapshot_age_seconds
            now = time.monotonic()
            for env_id, cached in list(self._snapshots.items()):  # concurrent writers
                rules_snapshot_age_seconds.labels(env_id).set(now - cached.confirmed_at)
        except Exception:  # pragma: no cover - metrics are best-effort telemetry
            pass

    def snapshot_at(self, session: Session, env_id: str, rules_version: int) -> EnvSnapshot:
        """A historical targeting snapshot for §9 ``pin.rules_version`` replay.

        A response's ``rules_version`` always names an exact revision, so replay
        requires that revision to EXIST — anything else is a caller error, answered
        honestly (422 with the ``pin.versions`` alternative) rather than
        approximated. Checkpoint revisions resolve O(1); the rest reconstruct from
        the nearest older checkpoint (bounded by the checkpoint interval). The DB
        reads sit off the common path (replays only) and the *historical targeting
        state* is memoized — history never changes. The snapshot's two CURRENT-state
        overlays are NOT part of history and are re-attached on every call:
        ``servable`` (the validated-commit predicate) and ``refinement_defaults``
        grow over time, and freezing them at first build made a memoized replay
        wrongly 409 a ``pin.versions`` SHA validated after the memo (or serve a
        stale variable default). Both re-attachments are reference swaps from the
        in-memory current snapshot — the path stays DB-free on a hit."""
        current = self.get_snapshot(session, env_id)
        if rules_version == current.rules_version:
            return current
        # The memo key carries the environment's incarnation: revision numbers restart
        # when an id is deleted and recreated, so an old life's rules_version must
        # never resolve to (or refuse for) the new life. get_snapshot above ensured a
        # cache entry exists; in the narrow race where the poll evicts it right here
        # we just miss the memo for this one request (worst case a rebuild).
        entry = self._snapshots.get(env_id)
        incarnation = entry.incarnation if entry is not None else ""
        key = (env_id, incarnation, rules_version)
        with self._replay_lock:
            hit = self._historical.get(key)
            if hit is not None:
                self._historical.move_to_end(key)
            refused = self._unreplayable.get(key)
        if hit is not None:
            # Shallow copy with the overlays refreshed (see docstring). The memoized
            # object itself is never mutated: a request thread mid-render keeps a
            # consistent view, and `replace` costs a handful of reference copies.
            return replace(hit, servable=current.servable,
                           refinement_defaults=current.refinement_defaults)
        if refused is not None:
            raise ServingError(422, refused)
        try:
            state = self.targeting(session).state_at(env_id, rules_version)
        except SQLAlchemyError:
            raise ServingError(503, "targeting replay unavailable: control plane "
                                    "unreachable (pin.versions replay stays memory-only)")
        if state is None:
            raise ServingError(
                422,
                f"rules_version {rules_version} has no recorded targeting state for "
                f"{env_id!r} (it may predate state-tracked revisions); replay with "
                "pin.versions instead — the response's versions map is SHA-exact",
            )
        try:
            snap = snapshot_from_state(state, env_id, rules_version, current.servable)
        except ValueError as exc:
            msg = (f"rules_version {rules_version} of {env_id!r} cannot be replayed: {exc}; "
                   "replay with pin.versions instead — the response's versions map is SHA-exact")
            with self._replay_lock:
                self._unreplayable[key] = msg
                if len(self._unreplayable) > _HISTORICAL_CACHE_MAX:
                    self._unreplayable.popitem(last=False)
            raise ServingError(422, msg)
        # Variable defaults ride along from the live snapshot: refinements are
        # authoring metadata, not targeting state (documented replay semantics).
        # Like `servable`, this is a CURRENT-state overlay — the memoized copy's value
        # is superseded by the refresh above on every later hit.
        snap.refinement_defaults = current.refinement_defaults
        with self._replay_lock:
            self._historical[key] = snap
            self._historical.move_to_end(key)
            if len(self._historical) > _HISTORICAL_CACHE_MAX:
                self._historical.popitem(last=False)
        return snap

    def auto_advance_tips(self, session: Session, actor: str, prompt_id: str,
                          version: int, sha: str) -> list[str]:
        """§7 track_tip: in environments that track validated tips, advance an
        *existing* live pointer for (prompt, version) to the new tip. Returns the
        list of environments advanced.

        Same-node freshness is the CALLER's one :meth:`invalidate_after_commit` after
        its whole write (the commit route's): each advance bumped its environment's
        rules_version, so that keyed refresh rebuilds exactly these — registering one
        here as well would only build every snapshot twice."""
        advanced: list[str] = []
        # Deterministic id order: make_live now takes each environment's row lock, so
        # any transaction locking SEVERAL environments must do so in one global order
        # or two concurrent multi-env lockers can deadlock.
        envs = session.execute(
            select(models.Environment).where(models.Environment.track_tip.is_(True))
            .order_by(models.Environment.id)
        ).scalars().all()
        for env in envs:
            tgt = self.targeting(session, actor)
            if tgt.current_live(env.id, prompt_id, version) is None:
                continue  # nothing live to follow
            tgt.make_live(env.id, prompt_id, version, sha,
                          comment="track_tip auto-advance")
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

    def warm(self, session: Session, env_id: str, *,
             validated_index: set[tuple[str, int, str]] | None = None) -> None:
        """Eager-warm the content cache for everything reachable in an environment.

        Live pointers *and each version's previous-live* (the §10 fallback must be
        warm to be useful) and tips. A caller warming several environments in one pass
        may hand every call the same pre-loaded ``validated_index`` (see
        :func:`load_validated_index`) so the snapshots share one set.

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

        # Freshness keys read BEFORE the build (same reasoning as get_snapshot's cold
        # path): a bump landing mid-warm must leave this entry behind, not current.
        env = session.get(models.Environment, env_id)
        if env is None:
            raise KeyError(f"unknown environment {env_id!r}")
        snap = build_snapshot(session, env_id, validated_index=validated_index)
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
        # (:meth:`get_snapshot`), in its ``_CachedSnapshot`` shape.
        # Readiness must mean "can serve THIS env with zero DB reads" (§8 "No DB per
        # request"; §10 "the DB is never on the per-request path"): without this install
        # the FIRST render after /readyz went green would do a cold snapshot build (a DB
        # read), so a node that just reported ready would 503 if Postgres died the instant
        # after. With it, that first render is a pure memory hit off already-warm content.
        self._snapshots[env_id] = _CachedSnapshot(
            env.rules_version, env.content_version, env.incarnation, snap)

    # ── serving ──────────────────────────────────────────────────────

    def _unresolved_error(self, snap: EnvSnapshot, env_id: str, prompt_id: str,
                          root: str | None = None) -> ServingError:
        """The honest 404 for an unresolvable prompt. 'Unknown prompt' is only true
        when the prompt genuinely doesn't exist; a prompt that EXISTS but has no
        default/rules in this environment is the normal commits-change-nothing state
        right after publishing, and saying 'unknown' at that exact moment convinces
        an integrating dev their commit failed. Name the real situation and the fix."""
        via = (f" (included by {root!r})" if root is not None and root != prompt_id else "")
        if prompt_id in snap.versions:
            return ServingError(
                404,
                f"prompt {prompt_id!r}{via} exists but serves nothing in {env_id!r}: no rule "
                "targets it and it has no environment default. Set a default (POST "
                f"/mgmt/envs/{env_id}/defaults) or add a rule to start serving it.",
            )
        return ServingError(404, f"unknown prompt {prompt_id!r}{via} in {env_id!r}")

    @staticmethod
    def _unservable_detail(exc: Unservable, root: str) -> str:
        via = f" (included by {root!r})" if exc.prompt_id != root else ""
        if exc.reason == "archived":
            return f"{exc}{via}"
        return f"resolved content for {exc.prompt_id!r}{via} is unservable"

    def evaluate(self, session: Session, env_id: str, prompt_id: str, flags: dict) -> Resolution:
        snap = self.get_snapshot(session, env_id)
        try:
            return resolve(snap, prompt_id, flags)
        except UnresolvedPrompt:
            raise self._unresolved_error(snap, env_id, prompt_id)
        except Unservable as exc:
            raise ServingError(409, self._unservable_detail(exc, prompt_id))

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
        pin_rules_version: int | None = None,
    ) -> dict:
        # §9 pin.rules_version: evaluate against the recorded historical targeting
        # state instead of the live snapshot. pin.versions entries (if also given)
        # still override per prompt — they are SHA-exact and always win.
        if pin_rules_version is not None:
            snap = self.snapshot_at(session, env_id, pin_rules_version)
        else:
            snap = self.get_snapshot(session, env_id)

        # The kill switch beats reproducibility, exactly as validation does (§5/§9): a
        # pin naming a CURRENTLY killed prompt — or a rules_version replay of one — is
        # refused loudly instead of replaying content an operator just killed. The
        # normal (unpinned) path still degrades to the environment default.
        current_killed = (snap.killed if pin_rules_version is None
                          else self.get_snapshot(session, env_id).killed)
        killed_pins = sorted(set(pin or ()) & current_killed)
        if killed_pins:
            raise ServingError(
                409,
                f"pinned prompt {killed_pins[0]!r} is killed in {env_id!r} — the kill "
                "switch overrides replay; lift it to replay this pin",
                error="killed")
        if pin_rules_version is not None and prompt_id in current_killed:
            raise ServingError(
                409,
                f"prompt {prompt_id!r} is killed in {env_id!r} — a rules_version replay "
                "cannot serve it; lift the kill switch first",
                error="killed")

        # §5's invariant — "only validated SHAs can ever serve" — applies to pins too.
        # Every other door is already guarded at write time (make_live, rule pins) or
        # eval time (the snapshot's `servable` backstop); without this check a pin
        # could resolve a validation-FAILED commit (those land on main by design) or a
        # draft-ref commit (same object store), serving broken or unreviewed content.
        # The check is memory-only: `servable` closes over the snapshot's validated
        # (prompt, version, sha) index, so the hot path stays DB-free (§8).
        for pid, (pin_version, pin_sha) in (pin or {}).items():
            if not snap.servable(pid, pin_version, pin_sha):
                raise ServingError(
                    409,
                    f"pinned commit {pin_sha} is not a validated commit for {pid!r}; "
                    "only validated content can serve (§5)",
                )

        try:
            result = render(snap, prompt_id, flags, variables, self.content, pin=pin)
        except UnresolvedPrompt as exc:
            # Name the prompt that actually failed: an include with no default/pointer
            # here must not be reported as the ROOT serving nothing (it may well be).
            raise self._unresolved_error(snap, env_id, exc.prompt_id, root=prompt_id)
        except MissingVariable as exc:
            raise ServingError(422, str(exc), variable=exc.name)
        except RenderError as exc:
            raise ServingError(422, str(exc), lineno=exc.lineno)
        except Unservable as exc:
            raise ServingError(409, self._unservable_detail(exc, prompt_id))
        except (IncludeCycle, IncludeDepthExceeded) as exc:
            # A targeting-induced include cycle (static validation only sees the graph
            # at current defaults) is a content/config fault, not a caller error.
            raise ServingError(409, str(exc))
        except KeyError:
            raise ServingError(409, "resolved content missing from store")

        if pin_rules_version is not None:
            # Includes that are not pinned resolve INSIDE the historical snapshot, whose
            # ``killed`` is the recorded one — a fragment killed NOW would still have its
            # old rules fire there. The kill switch beats replay for every contributor
            # (§9), so check what actually rendered against the CURRENT kills; the
            # pre-render checks above only covered the root and the pinned prompts.
            killed_now = sorted(set(result.contributions) & current_killed)
            if killed_now:
                raise ServingError(
                    409,
                    f"included prompt {killed_now[0]!r} is killed in {env_id!r} — a "
                    "rules_version replay cannot serve it; lift the kill switch first",
                    error="killed")

        versions = {}
        for pid, res in result.contributions.items():
            # Full 40-char SHAs: the reproducibility tuple must be SHA-exact and is fed
            # back verbatim as a `pin` (which now accepts only full SHAs — §9, §4).
            entry = {"version": res.version, "commit": res.commit}
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
            # §7 eval-time backstop: rules that MATCHED but could not serve, skipped
            # and reported (the route also counts them — incant_rule_skips_total).
            "skipped_rules": [
                {"rule_id": sk.rule_id, "prompt_id": sk.prompt_id, "reason": sk.reason}
                for sk in result.skips
            ],
            # True iff at least one active rule was in play for this prompt — feeds
            # incant_flag_eval_fallthrough_total (matched_rule == "default" despite
            # rules existing: dead-rule telemetry, §14). Stripped by the route.
            "_rules_considered": bool(snap.prompt_rules(prompt_id)),
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
    # The servability fallback memo is module-global; commit SHAs are deterministic
    # in tests (INCANT_FIXED_GIT_DATE), so entries must not survive an app reset.
    from .targeting import clear_servable_memo
    clear_servable_memo()
