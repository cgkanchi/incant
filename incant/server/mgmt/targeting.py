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
    PublishRequest,
    RollbackRequest,
    RuleBatchRequest,
    RuleRequest,
    RuleStatusRequest,
    SegmentRequest,
)
from .helpers import _confirm_lock, _project_of, _references_segment, _require

router = APIRouter()


def _require_stored_scope(ident: Identity, existing: models.Rule, env: str) -> None:
    """Rehoming defense: require authority over where a rule lives NOW.

    Rule ids are globally unique, client-supplied strings that GET /rules surfaces, and
    ``TargetingService.upsert_rule`` loads any existing rule by id then freely overwrites its
    ``scope``/``prompt_id`` (it guards ONLY cross-ENVIRONMENT capture). So authorizing the
    REQUEST scope alone is not enough: a project-A operator could POST a GLOBAL rule's id
    re-scoped to A (neutering an env-wide rule with only project authority) or take a known
    project-B rule id and rehome it into A. scope/prompt are legitimately editable — the
    composer offers scope switching — so ownership is NOT immutable; the invariant is instead
    DUAL authorization: authority over BOTH the stored scope and the requested scope. Creating
    a rule (no existing row) needs only the requested-scope check; editing one needs both.
    Callers apply this only when the rule already lives in THIS env — the cross-env case is
    rejected by the service with its own clear message."""
    if existing.prompt_id:
        _require(ident, "operator", project=_project_of(existing.prompt_id), environment=env)
    else:
        _require(ident, "operator", environment=env)


@router.get("/envs")
def list_envs(
    app: AppContext = Depends(app_context),
    session: Session = Depends(get_session),
    ident: Identity = Depends(identity),
):
    # `default` marks the serving/registry default env (settings.default_environment); the
    # UI uses it to disable rename/delete on that env with an explanation.
    default_env = app.settings.default_environment
    return {"environments": [
        {"id": e.id, "protected": e.protected, "track_tip": e.track_tip,
         "rules_version": e.rules_version, "default": e.id == default_env}
        for e in session.execute(select(models.Environment)).scalars()
    ]}


@router.get("/envs/{env}/rules")
def get_rules(
    env: str,
    project: str | None = None,
    app: AppContext = Depends(app_context),
    session: Session = Depends(get_session),
    ident: Identity = Depends(identity),
):
    # Access model. The full env-wide rule list needs env-WIDE viewer. But the UI gates its
    # chrome by a principal's BEST role in ANY scope (util.js) — so a viewer scoped to a
    # single project reaches prompt screens that fetch this list and, with only the env-wide
    # door, sees a swallowed 403 as an empty (i.e. wrong) rule set. The optional `project`
    # param opens a narrower door: with it we require viewer on THAT project (in this env)
    # and return only the rules that govern the project's prompts — the project's own
    # prompt-scoped rules PLUS every *global* rule. Global rules target labels/segments that
    # apply across every project's prompts (including this one), so a project viewer
    # legitimately needs to see them. Without the param, behaviour is unchanged: env-wide
    # viewer, the full unfiltered list.
    if project is not None:
        _require(ident, "viewer", project=project, environment=env)
    else:
        _require(ident, "viewer", environment=env)
    tgt = app.targeting(session, ident.name)
    e = session.get(models.Environment, env)
    if e is None:
        raise HTTPException(404, f"unknown environment {env!r}")

    def _in_project(prompt_id: str | None) -> bool:
        # A prompt-scoped row belongs to `project` when its prompt id's leading segment
        # (the same split _project_of uses) is that project. None never matches.
        return bool(prompt_id) and _project_of(prompt_id) == project

    rules = [
        {"id": r.id, "scope": r.scope, "prompt_id": r.prompt_id, "priority": r.priority,
         "when": r.clauses, "serve": r.serve, "status": r.status, "comment": r.comment}
        for r in tgt.list_rules(env)
        # Scoped read keeps global rules (they govern this project's prompts too) plus the
        # project's own prompt-scoped rules; other projects' rules stay hidden.
        if project is None or r.scope == "global" or _in_project(r.prompt_id)
    ]
    # Kills and defaults are always prompt-scoped (never global), so the scoped read filters
    # them to the project alone — keeping the response's testing/kill/default facts internally
    # consistent with the (filtered) rule list the caller can see.
    kills = {k.prompt_id: k.engaged for k in session.execute(
        select(models.KillSwitch).where(models.KillSwitch.environment_id == env)
    ).scalars() if project is None or _in_project(k.prompt_id)}
    defaults = {d.prompt_id: d.version_number for d in session.execute(
        select(models.EnvDefault).where(models.EnvDefault.environment_id == env)
    ).scalars() if project is None or _in_project(d.prompt_id)}
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
    # Requested-scope authority: prompt-scoped rules need operator on that project+env;
    # a *global* rule governs every project, so it requires env-wide (or instance) operator.
    if req.prompt_id:
        _require(ident, "operator", project=_project_of(req.prompt_id), environment=env)
    else:
        _require(ident, "operator", environment=env)
    # Stored-scope authority (rehoming defense — see _require_stored_scope). Editing an
    # existing rule also requires authority over where it lives NOW, so its scope/prompt_id
    # can't be overwritten by a caller who only holds authority over the requested target.
    existing = session.get(models.Rule, req.id)
    if existing is not None and existing.environment_id == env:
        _require_stored_scope(ident, existing, env)
    tgt = app.targeting(session, ident.name)
    try:
        r = tgt.upsert_rule(env, req.model_dump())
    except TargetingError as exc:
        raise HTTPException(400, str(exc))
    app.invalidate(env)
    return {"id": r.id, "rules_version": session.get(models.Environment, env).rules_version}


@router.post("/envs/{env}/rules/batch")
def upsert_rules_batch(
    env: str, req: RuleBatchRequest,
    app: AppContext = Depends(app_context),
    session: Session = Depends(get_session),
    ident: Identity = Depends(identity),
):
    """Apply a set of rule upserts as ONE atomic act.

    The UI presents a composer priority-shift plan (renumber neighbours + write the new
    rule) and a two-rule reorder swap as a single user action, but historically fired N
    separate POSTs; a failure mid-sequence left rules at colliding/half-applied priorities
    while the UI toasted an error. A FastAPI request spans exactly one DB transaction
    (get_session commits on success, rolls back on any exception), so doing every upsert
    inside this one request makes the whole batch atomic — any failure returns 4xx and
    NOTHING persists.

    RBAC and TargetingError→400 mapping mirror the single upsert endpoint exactly, applied
    per rule: a prompt-scoped rule needs `operator` on that project+env; a *global* rule
    governs every project, so it needs env-wide `operator`. We check every rule's authz up
    front so a 403 anywhere in the batch persists nothing. There is deliberately NO
    type-to-confirm even on a locked env: rule edits are low-friction (DESIGN.md §7 —
    pointer-class changes are the governed acts; rule create/ramp/archive need only
    `operator`, no ceremony), so the single upsert has none and adding it here would break
    composer-save/reorder on a protected env.
    """
    for r in req.rules:
        # Requested-scope authority.
        if r.prompt_id:
            _require(ident, "operator", project=_project_of(r.prompt_id), environment=env)
        else:
            _require(ident, "operator", environment=env)
        # Stored-scope authority (rehoming defense) — same dual-authz invariant as the single
        # upsert. Checked here in the up-front pass so a hijack attempt ANYWHERE in the batch
        # 403s before any write lands, preserving atomicity (a 403 persists nothing).
        existing = session.get(models.Rule, r.id)
        if existing is not None and existing.environment_id == env:
            _require_stored_scope(ident, existing, env)
    tgt = app.targeting(session, ident.name)
    ids: list[str] = []
    try:
        for r in req.rules:
            ids.append(tgt.upsert_rule(env, r.model_dump()).id)
    except TargetingError as exc:
        raise HTTPException(400, str(exc))
    app.invalidate(env)
    return {"ids": ids, "count": len(ids),
            "rules_version": session.get(models.Environment, env).rules_version}


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
    env: str, limit: int = 100, project: str | None = None,
    app: AppContext = Depends(app_context),
    session: Session = Depends(get_session),
    ident: Identity = Depends(identity),
):
    # Access model mirrors get_rules. The full env-wide change log needs env-WIDE viewer,
    # but a project-scoped viewer reaching the targeting screen must not read a swallowed
    # 403 as an empty history. With `project`, require viewer on THAT project (in this env)
    # and filter the log to the revisions that touch the project's prompts.
    #
    # A revision is kept when its snapshot names a prompt in `project`. Every prompt-scoped
    # revision carries a `prompt_id` in its snapshot: rule edits (_rule_snapshot), pointer
    # moves, defaults, and kills all do. Revisions with NO prompt are env-wide facts —
    # GLOBAL-rule edits (prompt_id is None), segment edits, and rollbacks — so they are
    # EXCLUDED in project mode. This is narrower than get_rules on purpose: get_rules keeps
    # global RULES because they still govern the project's prompts, but a global-rule
    # *revision* is env-wide history a single project viewer has no scoped claim to. Best
    # effort: the DB `limit` is applied before this filter, so project mode may return fewer
    # than `limit` rows (same shape as get_rules' post-fetch filtering). Without the param,
    # behaviour is unchanged: env-wide viewer, the full log.
    if project is not None:
        _require(ident, "viewer", project=project, environment=env)
    else:
        _require(ident, "viewer", environment=env)
    tgt = app.targeting(session, ident.name)

    def _rev_project(r: models.RuleRevision) -> str | None:
        pid = (r.snapshot or {}).get("prompt_id")
        return _project_of(pid) if pid else None

    return {"environment": env, "revisions": [
        {"id": r.id, "rules_version": r.rules_version, "kind": r.kind,
         "rule_id": r.rule_id, "actor": r.actor, "comment": r.comment,
         "at": r.at.isoformat(), "snapshot": r.snapshot}
        for r in tgt.list_revisions(env, limit)
        if project is None or _rev_project(r) == project
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
    # This history is per-(prompt, version), so its natural scope is the prompt's project in
    # this env — requiring env-WIDE viewer was simply wrong (it 403'd a project-scoped viewer
    # off their own prompt's publish history, a navigable dead end). Authorize on the prompt's
    # project + env; an env-wide or instance viewer still satisfies it via role implication.
    _require(ident, "viewer", project=_project_of(prompt_id), environment=env)
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


@router.post("/envs/{env}/publish")
def publish(
    env: str, req: PublishRequest,
    app: AppContext = Depends(app_context),
    session: Session = Depends(get_session),
    ident: Identity = Depends(identity),
):
    """Advance the live pointer AND archive the now-redundant test rules in ONE atomic act.

    The UI's "Publish latest edits" and "Stop test & publish" present a single action but
    historically fired a POST pointer move followed by a LOOP of PATCH archives; a failure
    after the pointer moved (e.g. a bad rule id) left the pointer advanced while the
    archives never ran. Doing both inside this one request/transaction makes it atomic: any
    failure returns 4xx and the pointer move rolls back with it.

    RBAC mirrors the pieces exactly. The pointer move is releaser-gated on (project, env),
    identical to the `/pointers` endpoint, plus the same locked-env type-to-confirm. Each
    archive then rides the SAME requirement the single PATCH endpoint applies: look the rule
    up (404 if unknown in this env), then require `operator` (prompt-scoped → project+env;
    global → env). `releaser` implies `operator` (auth._IMPLIES), so a releaser on the
    rule's project+env already satisfies it — but we still check per rule so an archive of a
    rule in another project (or a global rule) isn't waved through. The pointer move runs
    first; a bad archive id then 404s and the whole transaction — pointer move included —
    rolls back.
    """
    _require(ident, "releaser", project=_project_of(req.prompt_id), environment=env)
    _confirm_lock(session, env, req.prompt_id, req.confirm)
    tgt = app.targeting(session, ident.name)
    try:
        outcome = tgt.make_live(
            env, req.prompt_id, req.version_number, req.to_sha, comment=req.comment,
        )
        archived = 0
        for rid in req.archive_rule_ids:
            r = session.get(models.Rule, rid)
            if r is None or r.environment_id != env:
                # Same 404 the single PATCH raises — but here it aborts the whole tx, so
                # the pointer move above never commits.
                raise HTTPException(404, f"unknown rule {rid!r} in {env!r}")
            if r.prompt_id:
                _require(ident, "operator", project=_project_of(r.prompt_id), environment=env)
            else:
                _require(ident, "operator", environment=env)
            tgt.set_rule_status(env, rid, "archived")
            archived += 1
    except TargetingError as exc:
        raise HTTPException(400, str(exc))
    app.invalidate(env)
    return {"status": outcome.status, "move_id": outcome.move_id,
            "archived": archived,
            "rules_version": session.get(models.Environment, env).rules_version}


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
