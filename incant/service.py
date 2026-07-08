"""AppContext — wires the git store, content cache, DB, and serving hot path.

Holds the per-environment snapshot cache (RulesSync, poll-fallback form): a
snapshot is rebuilt when the environment's ``rules_version`` advances. If the DB
is unreachable, serving continues on the last-known-good snapshot with
``stale_rules: true`` — the design's "rules freeze" availability posture.
"""

from __future__ import annotations

from dataclasses import dataclass, field
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
from .registry import RegistryService
from .targeting import TargetingService, build_snapshot


class ServingError(Exception):
    def __init__(self, status: int, detail: str, **extra: Any) -> None:
        self.status = status
        self.detail = detail
        self.extra = extra
        super().__init__(detail)


@dataclass
class _CachedSnapshot:
    rules_version: int
    snapshot: EnvSnapshot


@dataclass
class AppContext:
    settings: Settings = field(default_factory=get_settings)
    _snapshots: dict[str, _CachedSnapshot] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.git = GitStore(self.settings.repo_dir())
        self.content = ContentStore(self.git)

    # ── lifecycle ────────────────────────────────────────────────────

    def initialize(self) -> None:
        self.git.init()
        init_db()

    def registry(self, session: Session, actor: str = "system") -> RegistryService:
        return RegistryService(session, self.git, self.content)

    def targeting(self, session: Session, actor: str = "system") -> TargetingService:
        return TargetingService(session, actor)

    # ── snapshots ────────────────────────────────────────────────────

    def get_snapshot(self, session: Session, env_id: str) -> EnvSnapshot:
        try:
            env = session.get(models.Environment, env_id)
            if env is None:
                raise ServingError(404, f"unknown environment {env_id!r}")
            cached = self._snapshots.get(env_id)
            if cached is None or cached.rules_version != env.rules_version:
                snap = build_snapshot(session, env_id)
                self._snapshots[env_id] = _CachedSnapshot(env.rules_version, snap)
                return snap
            return cached.snapshot
        except SQLAlchemyError:
            # DB unreachable — freeze on last-known-good targeting (stale_rules).
            cached = self._snapshots.get(env_id)
            if cached is None:
                raise ServingError(503, "node not ready: no cached targeting")
            frozen = cached.snapshot
            frozen.stale = True
            return frozen

    def invalidate(self, env_id: str | None = None) -> None:
        if env_id is None:
            self._snapshots.clear()
        else:
            self._snapshots.pop(env_id, None)

    # ── warming ──────────────────────────────────────────────────────

    def warm(self, session: Session, env_id: str) -> None:
        """Eager-warm the content cache for everything reachable in an environment.

        Live pointers *and each version's previous-live* (the §10 fallback must be
        warm to be useful), tips, and defaults.
        """

        snap = build_snapshot(session, env_id)
        for prompt_id, vers in snap.versions.items():
            for vnum, vinfo in vers.items():
                for sha in filter(None, (vinfo.live_sha, vinfo.tip_sha, *vinfo.previous_live)):
                    try:
                        self.content.warm(prompt_id, vnum, sha)
                    except KeyError:
                        pass

    # ── defaults for optional variables ──────────────────────────────

    def _refinement_defaults(self, session: Session, prompt_id: str, version: int) -> dict:
        out: dict[str, Any] = {}
        for r in session.execute(
            select(models.VariableRefinement).where(
                models.VariableRefinement.prompt_id == prompt_id,
                models.VariableRefinement.version_number == version,
            )
        ).scalars():
            if r.default is not None:
                out[r.name] = r.default
        return out

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
        flags: dict, variables: dict,
    ) -> dict:
        snap = self.get_snapshot(session, env_id)

        # Resolve the root first to gather DB-held defaults for its optional vars.
        try:
            root = resolve(snap, prompt_id, flags)
        except UnresolvedPrompt:
            raise ServingError(404, f"unknown prompt {prompt_id!r} in {env_id!r}")
        except Unservable:
            raise ServingError(409, f"resolved content for {prompt_id!r} is unservable")

        defaults = self._refinement_defaults(session, prompt_id, root.version)

        try:
            result = render(snap, prompt_id, flags, variables, self.content, defaults=defaults)
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
            entry = {"version": res.version, "commit": res.commit[:7], "label": res.label}
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
