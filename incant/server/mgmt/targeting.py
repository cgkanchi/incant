"""Rules, segments, revisions, rollback, pointers, defaults, kill switches, envs (read)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from ... import models
from ...targeting import TargetingError
from ..auth import Identity
from ..deps import app_context, get_session, identity
from ...service import AppContext
from ..schemas import (
    DefaultRequest,
    KillRequest,
    PointerRequest,
    RollbackRequest,
    RuleRequest,
    RuleStatusRequest,
    SegmentRequest,
)
from .helpers import _confirm_lock, _project_of, _references_segment, _require

router = APIRouter()


@router.get("/envs")
def list_envs(
    session: Session = Depends(get_session),
    ident: Identity = Depends(identity),
):
    return {"environments": [
        {"id": e.id, "protected": e.protected, "track_tip": e.track_tip,
         "rules_version": e.rules_version}
        for e in session.execute(select(models.Environment)).scalars()
    ]}


@router.get("/envs/{env}/rules")
def get_rules(
    env: str,
    app: AppContext = Depends(app_context),
    session: Session = Depends(get_session),
    ident: Identity = Depends(identity),
):
    _require(ident, "viewer", environment=env)
    tgt = app.targeting(session, ident.name)
    e = session.get(models.Environment, env)
    if e is None:
        raise HTTPException(404, f"unknown environment {env!r}")
    rules = [
        {"id": r.id, "scope": r.scope, "prompt_id": r.prompt_id, "priority": r.priority,
         "when": r.clauses, "serve": r.serve, "status": r.status, "comment": r.comment}
        for r in tgt.list_rules(env)
    ]
    kills = {k.prompt_id: k.engaged for k in session.execute(
        select(models.KillSwitch).where(models.KillSwitch.environment_id == env)
    ).scalars()}
    defaults = {d.prompt_id: d.version_number for d in session.execute(
        select(models.EnvDefault).where(models.EnvDefault.environment_id == env)
    ).scalars()}
    return {
        "environment": env, "protected": e.protected, "track_tip": e.track_tip,
        "rules_version": e.rules_version, "rules": rules,
        "kills": kills, "defaults": defaults,
    }


@router.post("/envs/{env}/rules")
def upsert_rule(
    env: str, req: RuleRequest,
    app: AppContext = Depends(app_context),
    session: Session = Depends(get_session),
    ident: Identity = Depends(identity),
):
    # Prompt-scoped rules need operator on that project+env; a *global* rule
    # governs every project, so it requires env-wide (or instance) operator.
    if req.prompt_id:
        _require(ident, "operator", project=_project_of(req.prompt_id), environment=env)
    else:
        _require(ident, "operator", environment=env)
    tgt = app.targeting(session, ident.name)
    try:
        r = tgt.upsert_rule(env, req.model_dump())
    except TargetingError as exc:
        raise HTTPException(400, str(exc))
    app.invalidate(env)
    return {"id": r.id, "rules_version": session.get(models.Environment, env).rules_version}


@router.patch("/envs/{env}/rules/{rule_id}")
def patch_rule(
    env: str, rule_id: str, req: RuleStatusRequest,
    app: AppContext = Depends(app_context),
    session: Session = Depends(get_session),
    ident: Identity = Depends(identity),
):
    r = session.get(models.Rule, rule_id)
    if r is None or r.environment_id != env:
        raise HTTPException(404, f"unknown rule {rule_id!r} in {env!r}")
    if r.prompt_id:
        _require(ident, "operator", project=_project_of(r.prompt_id), environment=env)
    else:
        _require(ident, "operator", environment=env)
    tgt = app.targeting(session, ident.name)
    try:
        tgt.set_rule_status(env, rule_id, req.status)
    except TargetingError as exc:
        raise HTTPException(404, str(exc))
    app.invalidate(env)
    return {"id": rule_id, "status": req.status}


@router.get("/envs/{env}/revisions")
def get_revisions(
    env: str, limit: int = 100,
    app: AppContext = Depends(app_context),
    session: Session = Depends(get_session),
    ident: Identity = Depends(identity),
):
    _require(ident, "viewer", environment=env)
    tgt = app.targeting(session, ident.name)
    return {"environment": env, "revisions": [
        {"id": r.id, "rules_version": r.rules_version, "kind": r.kind,
         "rule_id": r.rule_id, "actor": r.actor, "comment": r.comment,
         "at": r.at.isoformat(), "snapshot": r.snapshot}
        for r in tgt.list_revisions(env, limit)
    ]}


@router.post("/envs/{env}/rollback")
def rollback_targeting(
    env: str, req: RollbackRequest,
    app: AppContext = Depends(app_context),
    session: Session = Depends(get_session),
    ident: Identity = Depends(identity),
):
    # Rollback can touch global rules, so it needs env-wide operator.
    _require(ident, "operator", environment=env)
    _confirm_lock(session, env, env, req.confirm)
    tgt = app.targeting(session, ident.name)
    try:
        result = tgt.rollback(env, req.to_rules_version)
    except TargetingError as exc:
        raise HTTPException(400, str(exc))
    app.invalidate(env)
    return result


@router.get("/envs/{env}/segments")
def get_segments(
    env: str,
    app: AppContext = Depends(app_context),
    session: Session = Depends(get_session),
    ident: Identity = Depends(identity),
):
    _require(ident, "viewer", environment=env)
    tgt = app.targeting(session, ident.name)
    # count references from rules
    rules = tgt.list_rules(env)

    def refs(name: str) -> int:
        return sum(1 for r in rules if _references_segment(r.clauses, name))

    return {"environment": env, "segments": [
        {"name": s.name, "when": s.clauses, "version": s.version, "referenced_by": refs(s.name)}
        for s in tgt.list_segments(env)
    ]}


@router.post("/envs/{env}/segments")
def upsert_segment(
    env: str, req: SegmentRequest,
    app: AppContext = Depends(app_context),
    session: Session = Depends(get_session),
    ident: Identity = Depends(identity),
):
    _require(ident, "operator", environment=env)
    tgt = app.targeting(session, ident.name)
    tgt.upsert_segment(env, req.name, req.when)
    app.invalidate(env)
    return {"ok": True}


@router.get("/envs/{env}/pointers")
def pointer_timeline(
    env: str, prompt_id: str, version: int,
    app: AppContext = Depends(app_context),
    session: Session = Depends(get_session),
    ident: Identity = Depends(identity),
):
    _require(ident, "viewer", environment=env)
    tgt = app.targeting(session, ident.name)
    hist = tgt.pointer_history(env, prompt_id, version)
    current = hist[0].to_sha if hist else None
    return {"environment": env, "prompt_id": prompt_id, "version": version, "moves": [
        {"sha": m.to_sha[:7], "full_sha": m.to_sha, "from_sha": (m.from_sha[:7] if m.from_sha else None),
         "by": m.moved_by, "at": m.moved_at.isoformat(), "comment": m.comment,
         "current": m.to_sha == current}
        for m in hist
    ]}


@router.post("/envs/{env}/pointers")
def make_live(
    env: str, req: PointerRequest,
    app: AppContext = Depends(app_context),
    session: Session = Depends(get_session),
    ident: Identity = Depends(identity),
):
    # Pointer moves are unilateral and releaser-gated — no propose→approve ceremony.
    _require(ident, "releaser", project=_project_of(req.prompt_id), environment=env)
    _confirm_lock(session, env, req.prompt_id, req.confirm)
    tgt = app.targeting(session, ident.name)
    try:
        outcome = tgt.make_live(
            env, req.prompt_id, req.version_number, req.to_sha, comment=req.comment,
        )
    except TargetingError as exc:
        raise HTTPException(400, str(exc))
    app.invalidate(env)
    return {"status": outcome.status, "move_id": outcome.move_id,
            "rules_version": outcome.rules_version}


@router.post("/envs/{env}/defaults")
def set_default(
    env: str, req: DefaultRequest,
    app: AppContext = Depends(app_context),
    session: Session = Depends(get_session),
    ident: Identity = Depends(identity),
):
    _require(ident, "operator", project=_project_of(req.prompt_id), environment=env)
    e = session.get(models.Environment, env)
    if e and e.protected:
        _require(ident, "releaser", environment=env)
    _confirm_lock(session, env, req.prompt_id, req.confirm)
    tgt = app.targeting(session, ident.name)
    try:
        tgt.set_default(env, req.prompt_id, req.version_number)
    except TargetingError as exc:
        raise HTTPException(400, str(exc))
    app.invalidate(env)
    return {"ok": True}


@router.post("/envs/{env}/kill")
def kill_switch(
    env: str, prompt_id: str, req: KillRequest,
    app: AppContext = Depends(app_context),
    session: Session = Depends(get_session),
    ident: Identity = Depends(identity),
):
    _require(ident, "operator", project=_project_of(prompt_id), environment=env)
    tgt = app.targeting(session, ident.name)
    tgt.set_kill(env, prompt_id, req.engaged)
    app.invalidate(env)
    return {"ok": True, "engaged": req.engaged}
