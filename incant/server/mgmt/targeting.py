"""Rules, revisions, rollback, pointers, defaults, kill switches, envs (read)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ... import models
from ...targeting import TargetingError
from ...targeting.audit import record_audit
from ...targeting.observed import (
    all_rules, collect_rule_flags, forget_flag, load_suppressions, typed_value,
)
from ..auth import ANY_ENVIRONMENT, ANY_PROJECT, Identity
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
)
from .helpers import _confirm_lock, _project_of, _require

router = APIRouter()


def _require_stored_scope(ident: Identity, existing: models.Rule, env: str) -> None:
    """Rehoming defense: require authority over where a rule lives NOW.

    Rule ids are globally unique, client-supplied strings that GET /rules surfaces, and
    ``TargetingService.upsert_rule`` loads any existing rule by id then overwrites its
    ``prompt_id`` (it guards ONLY cross-ENVIRONMENT capture). So authorizing the REQUEST
    prompt alone is not enough: a project-A operator could take a known project-B rule id
    and rehome it into A. The invariant is DUAL authorization — authority over BOTH the
    stored prompt's project and the requested one. Creating a rule (no existing row) needs
    only the requested check; editing one needs both. Callers apply this only when the rule
    already lives in THIS env — the cross-env case is rejected by the service."""
    _require(ident, "operator", project=_project_of(existing.prompt_id), environment=env)


@router.get("/envs")
def list_envs(
    app: AppContext = Depends(app_context),
    session: Session = Depends(get_session, scope="function"),
    ident: Identity = Depends(identity),
):
    # Viewer in ANY scope. Every other mgmt read is role-guarded; this one was open to a
    # renderer-only key, which could enumerate environments. The UI's env switcher lists
    # envs for project-scoped viewers too (their binding names a project and possibly ONE
    # env), so the check waives both scope dimensions: with one project per deployment,
    # "viewer of some project" is "viewer of this deployment" — while a renderer key (no
    # binding implies viewer) is still refused.
    _require(ident, "viewer", project=ANY_PROJECT, environment=ANY_ENVIRONMENT)
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
    session: Session = Depends(get_session, scope="function"),
    ident: Identity = Depends(identity),
):
    # Access model. The full env-wide rule list needs env-WIDE viewer. But the UI gates its
    # chrome by a principal's BEST role in ANY scope (util.js) — so a viewer scoped to a
    # single project reaches prompt screens that fetch this list and, with only the env-wide
    # door, sees a swallowed 403 as an empty (i.e. wrong) rule set. The optional `project`
    # param opens a narrower door: with it we require viewer on THAT project (in this env)
    # and return only the rules on the project's prompts. Without the param, behaviour is
    # unchanged: env-wide viewer, the full unfiltered list.
    if project is not None:
        _require(ident, "viewer", project=project, environment=env)
    else:
        _require(ident, "viewer", environment=env)
    tgt = app.targeting(session, ident.name)
    e = session.get(models.Environment, env)
    if e is None:
        raise HTTPException(404, f"unknown environment {env!r}")

    def _in_project(prompt_id: str) -> bool:
        # A row belongs to `project` when its prompt id's leading path segment (the same
        # split _project_of uses) is that project.
        return _project_of(prompt_id) == project

    # §7 "skipped, counted, surfaced in the UI": the eval-time skip only shows up in
    # metrics when a request actually hits the rule, so the console does the static
    # half — flag any ACTIVE rule whose serve target cannot currently resolve.
    snap = app.get_snapshot(session, env)

    rules = [
        {"id": r.id, "prompt_id": r.prompt_id, "priority": r.priority,
         "when": r.clauses, "serve": r.serve, "status": r.status, "comment": r.comment,
         "unservable_reason": _unservable_reason(snap, r)}
        for r in tgt.list_rules(env)
        if project is None or _in_project(r.prompt_id)
    ]
    # Kills and defaults are prompt-scoped too, so the scoped read filters them to the
    # project alone — keeping the response's facts consistent with the visible rule list.
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


def _unservable_reason(snap, r: models.Rule) -> str | None:
    """§7 "skipped, counted, surfaced": the static half of the eval-time skip — why an
    ACTIVE rule's serve target cannot currently resolve (None when it can). Used by the
    rules listing AND echoed as a warning when a rule is created/updated, so an author
    learns "this can never serve" at save time, not after 30 baffling requests."""
    if r.status != "active":
        return None
    serve = r.serve if isinstance(r.serve, dict) else {}
    if "version" not in serve:
        return "serve target is not a version"
    pid = r.prompt_id
    version, at, sha = int(serve["version"]), serve.get("at"), serve.get("sha")
    vinfo = snap.version_info(pid, version)
    if vinfo is None:
        return f"v{version} does not exist"
    if vinfo.status == "archived":
        return f"v{version} is archived — unarchive it to serve"
    if at == "tip":
        if vinfo.tip_sha is None:
            return f"v{version} has no validated tip"
    elif at == "sha":
        if not sha or not snap.servable(pid, version, sha):
            return f"pinned SHA for v{version} is not servable"
    elif vinfo.live_sha is None or not snap.servable(pid, version, vinfo.live_sha):
        if not any(snap.servable(pid, version, s) for s in vinfo.previous_live):
            return f"v{version} has no servable live content"
    return None


@router.post("/envs/{env}/rules")
def upsert_rule(
    env: str, req: RuleRequest,
    app: AppContext = Depends(app_context),
    session: Session = Depends(get_session, scope="function"),
    ident: Identity = Depends(identity),
):
    # Requested authority: operator on the prompt's project + env.
    _require(ident, "operator", project=_project_of(req.prompt_id), environment=env)
    # Stored authority (rehoming defense — see _require_stored_scope). Editing an existing
    # rule also requires authority over where it lives NOW, so its prompt_id can't be
    # overwritten by a caller who only holds authority over the requested target.
    existing = session.get(models.Rule, req.id)
    if existing is not None and existing.environment_id == env:
        _require_stored_scope(ident, existing, env)
    tgt = app.targeting(session, ident.name)
    try:
        r = tgt.upsert_rule(env, req.model_dump())
    except TargetingError as exc:
        raise HTTPException(400, str(exc))
    app.invalidate_after_commit(session, env)
    out = {"id": r.id, "rules_version": session.get(models.Environment, env).rules_version}
    # Save-time honesty: a rule whose serve target can't resolve (e.g. it names a
    # version that's never been published here) is accepted — publishing later makes it
    # real — but the author hears about it NOW, not after baffling default-only traffic.
    warning = _unservable_reason(app.get_snapshot(session, env), r)
    if warning:
        out["warning"] = f"this rule can't serve yet — {warning}"
    return out


@router.post("/envs/{env}/rules/batch")
def upsert_rules_batch(
    env: str, req: RuleBatchRequest,
    app: AppContext = Depends(app_context),
    session: Session = Depends(get_session, scope="function"),
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
    per rule: `operator` on the prompt's project+env. We check every rule's authz up
    front so a 403 anywhere in the batch persists nothing. There is deliberately NO
    type-to-confirm even on a locked env: rule edits are low-friction (DESIGN.md §7 —
    pointer-class changes are the governed acts; rule create/ramp/archive need only
    `operator`, no ceremony), so the single upsert has none and adding it here would break
    composer-save/reorder on a protected env.
    """
    for r in req.rules:
        _require(ident, "operator", project=_project_of(r.prompt_id), environment=env)
        # Stored authority (rehoming defense) — same dual-authz invariant as the single
        # upsert. Checked here in the up-front pass so a hijack attempt ANYWHERE in the batch
        # 403s before any write lands, preserving atomicity (a 403 persists nothing).
        existing = session.get(models.Rule, r.id)
        if existing is not None and existing.environment_id == env:
            _require_stored_scope(ident, existing, env)
    tgt = app.targeting(session, ident.name)
    ids: list[str] = []
    upserted: list[models.Rule] = []
    try:
        for r in req.rules:
            row = tgt.upsert_rule(env, r.model_dump())
            ids.append(row.id)
            upserted.append(row)
    except TargetingError as exc:
        raise HTTPException(400, str(exc))
    app.invalidate_after_commit(session, env)
    out = {"ids": ids, "count": len(ids),
           "rules_version": session.get(models.Environment, env).rules_version}
    # Same save-time honesty as the single upsert: any rule in the batch whose serve
    # target can't currently resolve is reported NOW (the composer toasts these).
    snap = app.get_snapshot(session, env)
    warnings = []
    for row in upserted:
        reason = _unservable_reason(snap, row)
        if reason:
            warnings.append(f"rule {row.id!r} can't serve yet — {reason}")
    if warnings:
        out["warnings"] = warnings
    return out


@router.patch("/envs/{env}/rules/{rule_id}")
def patch_rule(
    env: str, rule_id: str, req: RuleStatusRequest,
    app: AppContext = Depends(app_context),
    session: Session = Depends(get_session, scope="function"),
    ident: Identity = Depends(identity),
):
    r = session.get(models.Rule, rule_id)
    if r is None or r.environment_id != env:
        raise HTTPException(404, f"unknown rule {rule_id!r} in {env!r}")
    _require(ident, "operator", project=_project_of(r.prompt_id), environment=env)
    tgt = app.targeting(session, ident.name)
    try:
        tgt.set_rule_status(env, rule_id, req.status)
    except TargetingError as exc:
        raise HTTPException(404, str(exc))
    app.invalidate_after_commit(session, env)
    return {"id": rule_id, "status": req.status}


@router.get("/envs/{env}/revisions")
def get_revisions(
    env: str, limit: int = 100, project: str | None = None,
    app: AppContext = Depends(app_context),
    session: Session = Depends(get_session, scope="function"),
    ident: Identity = Depends(identity),
):
    # Access model mirrors get_rules. The full env-wide change log needs env-WIDE viewer,
    # but a project-scoped viewer reaching the targeting screen must not read a swallowed
    # 403 as an empty history. With `project`, require viewer on THAT project (in this env)
    # and filter the log to the revisions that touch the project's prompts.
    #
    # A revision is kept when its snapshot names a prompt in `project`. Every prompt-scoped
    # revision carries a `prompt_id` in its snapshot: rule edits (_rule_snapshot), pointer
    # moves, defaults, and kills all do. Revisions with NO prompt are env-wide facts
    # (rollbacks, baselines) and are EXCLUDED in project mode. Best effort: the DB `limit`
    # is applied before this filter, so project mode may return fewer than `limit` rows.
    # Without the param, behaviour is unchanged: env-wide viewer, the full log.
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
    session: Session = Depends(get_session, scope="function"),
    ident: Identity = Depends(identity),
):
    # Rollback touches every prompt's targeting at once, so it needs env-wide operator.
    _require(ident, "operator", environment=env)
    _confirm_lock(session, env, env, req.confirm)
    tgt = app.targeting(session, ident.name)
    try:
        result = tgt.rollback(env, req.to_rules_version)
    except TargetingError as exc:
        raise HTTPException(400, str(exc))
    app.invalidate_after_commit(session, env)
    return result


@router.get("/envs/{env}/pointers")
def pointer_timeline(
    env: str, prompt_id: str, version: int,
    app: AppContext = Depends(app_context),
    session: Session = Depends(get_session, scope="function"),
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
        {"sha": (m.to_sha[:7] if m.to_sha else None), "full_sha": m.to_sha,
         "from_sha": (m.from_sha[:7] if m.from_sha else None),
         "by": m.moved_by, "at": m.moved_at.isoformat(), "comment": m.comment,
         "current": m.to_sha == current}
        for m in hist
    ]}


def _render_check_warning(session: Session, prompt_id: str, sha: str) -> str | None:
    """§5: making a sha live whose configured test-context render never ran deserves a
    loud warning — the stored verdict was static-only (snapshot unavailable at commit
    time), and the commit response that said so is long gone."""
    row = session.execute(select(models.CommitValidation).where(
        models.CommitValidation.sha == sha,
        models.CommitValidation.prompt_id == prompt_id)).scalars().first()
    if row is None or row.render_checked:
        return None
    if session.execute(select(models.TestContext.id).where(
            models.TestContext.prompt_id == prompt_id).limit(1)).first() is None:
        return None  # nothing was configured — static-only IS the full check
    reason = f" ({row.render_skipped_reason})" if row.render_skipped_reason else ""
    return (f"validation of {sha[:12]} skipped the test-context render{reason} — the "
            "verdict is static-only; re-commit once the default environment builds to "
            "run the configured contexts")


@router.post("/envs/{env}/pointers")
def make_live(
    env: str, req: PointerRequest,
    app: AppContext = Depends(app_context),
    session: Session = Depends(get_session, scope="function"),
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
    app.invalidate_after_commit(session, env)
    out = {"status": outcome.status, "move_id": outcome.move_id,
           "rules_version": outcome.rules_version}
    warn = _render_check_warning(session, req.prompt_id, req.to_sha)
    if warn:
        out["warnings"] = [warn]
    return out


@router.post("/envs/{env}/publish")
def publish(
    env: str, req: PublishRequest,
    app: AppContext = Depends(app_context),
    session: Session = Depends(get_session, scope="function"),
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
    up (404 if unknown in this env), then require `operator` on its project+env. `releaser`
    implies `operator` (auth._IMPLIES), so a releaser on the rule's project+env already
    satisfies it — but we still check per rule so an archive of a rule in another project
    isn't waved through. The pointer move runs first; a bad archive id then 404s and the
    whole transaction — pointer move included — rolls back.
    """
    _require(ident, "releaser", project=_project_of(req.prompt_id), environment=env)
    _confirm_lock(session, env, req.prompt_id, req.confirm)
    tgt = app.targeting(session, ident.name)
    try:
        outcome = tgt.make_live(
            env, req.prompt_id, req.version_number, req.to_sha, comment=req.comment,
        )
        if req.make_default:
            # Same transaction: the pointer advance and the default switch land (or
            # roll back) together — "publish for everyone" is never half-true.
            tgt.set_default(env, req.prompt_id, req.version_number)
        archived = 0
        for rid in req.archive_rule_ids:
            r = session.get(models.Rule, rid)
            if r is None or r.environment_id != env:
                # Same 404 the single PATCH raises — but here it aborts the whole tx, so
                # the pointer move above never commits.
                raise HTTPException(404, f"unknown rule {rid!r} in {env!r}")
            _require(ident, "operator", project=_project_of(r.prompt_id), environment=env)
            tgt.set_rule_status(env, rid, "archived")
            archived += 1
    except TargetingError as exc:
        raise HTTPException(400, str(exc))
    app.invalidate_after_commit(session, env)
    out = {"status": outcome.status, "move_id": outcome.move_id,
           "archived": archived,
           "rules_version": session.get(models.Environment, env).rules_version}
    warn = _render_check_warning(session, req.prompt_id, req.to_sha)
    if warn:
        out["warnings"] = [warn]
    return out


@router.post("/envs/{env}/defaults")
def set_default(
    env: str, req: DefaultRequest,
    app: AppContext = Depends(app_context),
    session: Session = Depends(get_session, scope="function"),
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
    app.invalidate_after_commit(session, env)
    return {"ok": True}


@router.post("/envs/{env}/kill")
def kill_switch(
    env: str, prompt_id: str, req: KillRequest,
    app: AppContext = Depends(app_context),
    session: Session = Depends(get_session, scope="function"),
    ident: Identity = Depends(identity),
):
    _require(ident, "operator", project=_project_of(prompt_id), environment=env)
    tgt = app.targeting(session, ident.name)
    try:
        tgt.set_kill(env, prompt_id, req.engaged)
    except TargetingError as exc:
        # Killing a prompt that doesn't exist is a typo, not a 500 — refuse with 404.
        raise HTTPException(404, str(exc))
    app.invalidate_after_commit(session, env)
    return {"ok": True, "engaged": req.engaged}


# ── §7 observed flags: the composer's typeahead ──────────────────────

def _env_or_404(session: Session, env: str) -> models.Environment:
    e = session.get(models.Environment, env)
    if e is None:
        raise HTTPException(404, f"unknown environment {env!r}")
    return e


def _ilike_escape(q: str) -> str:
    return q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


@router.get("/envs/{env}/flags")
def list_observed_flags(
    env: str,
    app: AppContext = Depends(app_context),
    session: Session = Depends(get_session, scope="function"),
    ident: Identity = Depends(identity),
):
    """Every flag known in this environment: names seen on the serving API (with distinct
    value counts and recency), names the active rules consult (`in_rules`), and flags
    suppressed as high-cardinality (`suppressed`, never suggested). Viewer-gated."""
    _require(ident, "viewer", environment=env)
    _env_or_404(session, env)
    observed = {
        flag: (int(n), last)
        for flag, n, last in session.execute(
            select(models.ObservedFlag.flag, func.count(), func.max(models.ObservedFlag.last_seen))
            .where(models.ObservedFlag.environment_id == env)
            .group_by(models.ObservedFlag.flag)
        ).all()
    }
    suppressed = {
        r.flag: r for r in session.execute(
            select(models.ObservedFlagSuppression)
            .where(models.ObservedFlagSuppression.environment_id == env)
        ).scalars()
    }
    try:
        snap = app.get_snapshot(session, env)
        in_rules = set(collect_rule_flags(all_rules(snap)))
    except Exception:  # a degraded environment still lists what traffic saw
        in_rules = set()
    names = sorted(set(observed) | set(suppressed) | in_rules)
    out = []
    for name in names:
        n, last = observed.get(name, (0, None))
        sup = suppressed.get(name)
        out.append({
            "name": name,
            "values_seen": sup.values_seen if sup else n,
            "last_seen": last.isoformat() if last else None,
            "in_rules": name in in_rules,
            "suppressed": sup is not None,
        })
    return {"environment": env, "flags": out}


@router.get("/envs/{env}/flags/{flag}/values")
def observed_flag_values(
    env: str, flag: str,
    q: str = Query("", max_length=128),
    limit: int = Query(25, ge=1, le=100),
    app: AppContext = Depends(app_context),
    session: Session = Depends(get_session, scope="function"),
    ident: Identity = Depends(identity),
):
    """Typeahead: values seen for `flag` matching `q` (infix, case-insensitive), prefix
    matches first, then trigram similarity, then recency; empty `q` = most recent.
    Values the active rules already name are merged in (`sources` says which). Values
    come back typed (`value_type`) so a clause can carry them faithfully."""
    _require(ident, "viewer", environment=env)
    _env_or_404(session, env)
    sup = session.get(models.ObservedFlagSuppression, (env, flag))
    if sup is not None:
        return {"environment": env, "flag": flag, "q": q, "values": [],
                "suppressed": True, "values_seen": sup.values_seen}
    col = models.ObservedFlag.value
    stmt = select(col, models.ObservedFlag.value_type, models.ObservedFlag.last_seen).where(
        models.ObservedFlag.environment_id == env, models.ObservedFlag.flag == flag)
    q = q.strip()
    if q:
        esc = _ilike_escape(q)
        stmt = stmt.where(col.ilike(f"%{esc}%", escape="\\")).order_by(
            col.ilike(f"{esc}%", escape="\\").desc(),
            func.similarity(col, q).desc(),
            models.ObservedFlag.last_seen.desc(),
        )
    else:
        stmt = stmt.order_by(models.ObservedFlag.last_seen.desc())
    rows = session.execute(stmt.limit(limit)).all()
    values = [
        {"value": typed_value(v, t), "value_type": t, "last_seen": last.isoformat(),
         "sources": ["traffic"]}
        for v, t, last in rows
    ]
    # Rule-named values for this flag (the zero-traffic baseline), merged by value.
    try:
        snap = app.get_snapshot(session, env)
        rule_vals = collect_rule_flags(all_rules(snap)).get(flag, set())
    except Exception:
        rule_vals = set()
    by_value = {(r["value_type"], str(r["value"])): r for r in values}
    for rv in rule_vals:
        if q and q.lower() not in str(rv).lower():
            continue
        vt = ("bool" if isinstance(rv, bool) else "int" if isinstance(rv, int)
              else "float" if isinstance(rv, float) else "str")
        key = (vt, "true" if rv is True else "false" if rv is False else str(rv))
        if key in by_value:
            by_value[key]["sources"].append("rules")
        else:
            row = {"value": rv, "value_type": vt, "last_seen": None, "sources": ["rules"]}
            values.append(row)
            by_value[key] = row
    return {"environment": env, "flag": flag, "q": q, "values": values[:limit],
            "suppressed": False}


@router.delete("/envs/{env}/flags/{flag}")
def forget_observed_flag(
    env: str, flag: str,
    app: AppContext = Depends(app_context),
    session: Session = Depends(get_session, scope="function"),
    ident: Identity = Depends(identity),
):
    """Operator reset: drop everything observed for `flag` here, including a
    high-cardinality suppression, so it can be suggested again. Audited."""
    _require(ident, "operator", environment=env)
    _env_or_404(session, env)
    removed = forget_flag(session, env, flag)
    record_audit(session, ident.name, "observed_flag.forget", "observed_flag", f"{env}/{flag}",
                 before={"values_removed": removed})
    session.flush()
    app.observer.set_suppressed(load_suppressions(session))
    return {"ok": True, "environment": env, "flag": flag, "values_removed": removed}
