"""Management API — authoring, targeting, admin, and the read endpoints for the UI.

Devs and agents get the same flow as the UI: create draft -> put content ->
commit, same validation/review/audit. No side door.
"""

from __future__ import annotations

import datetime as dt
import difflib
import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models
from ..core import extract
from ..registry import ConcurrencyError, RegistryError, ReviewRequired
from ..service import AppContext, ServingError
from ..targeting import TargetingError, build_snapshot
from . import metrics
from .auth import AuthError, Identity, hash_key, key_prefix
from .deps import app_context, get_session, identity
from .schemas import (
    CommitRequest,
    CreateDraftRequest,
    CreatePromptRequest,
    DefaultRequest,
    DraftContentRequest,
    DraftRenderRequest,
    EnvironmentRequest,
    KeyRequest,
    KillRequest,
    PointerRequest,
    ProjectRequest,
    RefinementRequest,
    ReviewRequest,
    RuleRequest,
    RuleStatusRequest,
    SegmentRequest,
    TestContextRequest,
)

router = APIRouter(prefix="/mgmt", tags=["mgmt"])


def _project_of(prompt_id: str) -> str:
    return prompt_id.split("/", 1)[0]


def _guard(fn):
    try:
        return fn()
    except AuthError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.detail)


def _require(ident: Identity, role: str, *, project=None, environment=None) -> None:
    try:
        ident.require(role, project=project, environment=environment)
    except AuthError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.detail)


# ── read helpers ─────────────────────────────────────────────────────

def _validated_newest_first(session: Session, prompt_id: str, version: int) -> list[models.CommitValidation]:
    return list(session.execute(
        select(models.CommitValidation).where(
            models.CommitValidation.prompt_id == prompt_id,
            models.CommitValidation.version_number == version,
            models.CommitValidation.status == "valid",
        ).order_by(models.CommitValidation.validated_at.desc(), models.CommitValidation.id.desc())
    ).scalars())


def _tip_ahead(session, env_id, prompt_id, version, live_sha) -> int:
    validated = _validated_newest_first(session, prompt_id, version)
    shas = [v.sha for v in validated]
    if live_sha in shas:
        return shas.index(live_sha)
    return len(shas) if shas else 0


def _current_live(session, env_id, prompt_id, version) -> models.PointerMove | None:
    return session.execute(
        select(models.PointerMove).where(
            models.PointerMove.environment_id == env_id,
            models.PointerMove.prompt_id == prompt_id,
            models.PointerMove.version_number == version,
        ).order_by(models.PointerMove.moved_at.desc(), models.PointerMove.id.desc())
    ).scalars().first()


def _effective_variables(session, prompt_id, version) -> list[dict]:
    validated = _validated_newest_first(session, prompt_id, version)
    ev = validated[0].extracted_variables if validated else {"names": [], "required": [], "optional": []}
    required = set(ev.get("required", []))
    names = set(ev.get("names", []))
    refinements = {
        r.name: r for r in session.execute(
            select(models.VariableRefinement).where(
                models.VariableRefinement.prompt_id == prompt_id,
                models.VariableRefinement.version_number == version,
            )
        ).scalars()
    }
    out = []
    for name in sorted(names):
        r = refinements.get(name)
        is_required = name in required
        if r is not None and r.required is not None:
            is_required = r.required
        out.append({
            "name": name,
            "required": is_required,
            "inferred_required": name in required,
            "type": (r.type if r else None) or "string",
            "default": r.default if r else None,
            "description": (r.description if r else "") or "",
            "overridden": bool(r and r.required is not None and r.required != (name in required)),
        })
    return out


# ── overview / prompts list ──────────────────────────────────────────

@router.get("/overview")
def overview(
    environment: str = "prod",
    app: AppContext = Depends(app_context),
    session: Session = Depends(get_session),
    ident: Identity = Depends(identity),
):
    snap = build_snapshot(session, environment)
    projects: dict[str, list] = {}
    for prompt in session.execute(select(models.Prompt).order_by(models.Prompt.id)).scalars():
        pid = prompt.id
        if not ident.has("viewer", project=prompt.project_id, environment=environment):
            continue
        vers = snap.versions.get(pid, {})
        live_v = None
        live_sha = None
        for vnum, vinfo in vers.items():
            if vinfo.live_sha:
                # env default determines the "live" version shown
                pass
        default_v = snap.defaults.get(pid)
        vinfo = vers.get(default_v) if default_v else None
        tip_ahead = 0
        if vinfo and vinfo.live_sha:
            tip_ahead = _tip_ahead(session, environment, pid, default_v, vinfo.live_sha)
        hist = app.git.history(f"{pid}/v{default_v}.j2") if default_v else []
        updated = hist[0] if hist else None
        projects.setdefault(prompt.project_id, []).append({
            "prompt_id": pid,
            "versions": len(vers),
            "live_version": default_v,
            "live": bool(vinfo and vinfo.live_sha),
            "tip_ahead": tip_ahead,
            "updated": {"when": updated.date, "who": updated.author} if updated else None,
        })
    return {
        "environment": environment,
        "rules_version": snap.rules_version,
        "projects": [{"project": k, "prompts": v} for k, v in projects.items()],
    }


@router.get("/prompts/{prompt_id:path}/versions")
def get_versions(
    prompt_id: str,
    environment: str = "prod",
    app: AppContext = Depends(app_context),
    session: Session = Depends(get_session),
    ident: Identity = Depends(identity),
):
    _require(ident, "viewer", project=_project_of(prompt_id))
    reg = app.registry(session)
    if not reg.prompt_exists(prompt_id):
        raise HTTPException(404, f"unknown prompt {prompt_id!r}")
    snap = build_snapshot(session, environment)
    vers = snap.versions.get(prompt_id, {})
    default_v = snap.defaults.get(prompt_id)
    version_rows = reg.get_versions(prompt_id)
    # Show effective variables/includes for the default version, or the newest
    # committed version when no default is set yet (e.g. a brand-new prompt).
    display_v = default_v or (version_rows[0].number if version_rows else None)
    out = []
    for v in version_rows:
        vinfo = vers.get(v.number)
        hist = app.git.history(f"{prompt_id}/v{v.number}.j2")
        tip = hist[0] if hist else None
        live = _current_live(session, environment, prompt_id, v.number)
        out.append({
            "version": v.number,
            "label": v.label,
            "status": v.status,
            "notes": v.notes,
            "is_default": v.number == default_v,
            "live_sha": (live.to_sha[:7] if live else None),
            "live_full_sha": (live.to_sha if live else None),
            "live_at": (live.moved_at.isoformat() if live else None),
            "tip_sha": (tip.sha[:7] if tip else None),
            "tip_full_sha": (tip.sha if tip else None),
            "tip_author": (tip.author if tip else None),
            "tip_when": (tip.date if tip else None),
            "tip_ahead": _tip_ahead(session, environment, prompt_id, v.number, live.to_sha) if live else 0,
            "history": [
                {"sha": c.sha[:7], "full_sha": c.sha, "author": c.author,
                 "when": c.date, "subject": c.subject}
                for c in hist
            ],
        })
    return {
        "prompt_id": prompt_id,
        "environment": environment,
        "versions": out,
        "variables": _effective_variables(session, prompt_id, display_v) if display_v else [],
        "includes": _includes_of(app, prompt_id, display_v) if display_v else [],
        "display_version": display_v,
    }


def _includes_of(app: AppContext, prompt_id: str, version: int) -> list[str]:
    source = app.git.read(f"{prompt_id}/v{version}.j2")
    if not source:
        return []
    return list(extract(source).includes)


# ── authoring ────────────────────────────────────────────────────────

@router.post("/prompts")
def create_prompt(
    req: CreatePromptRequest,
    app: AppContext = Depends(app_context),
    session: Session = Depends(get_session),
    ident: Identity = Depends(identity),
):
    _require(ident, "editor", project=_project_of(req.prompt_id))
    reg = app.registry(session, ident.name)
    try:
        p = reg.create_prompt(req.prompt_id, req.description)
    except RegistryError as exc:
        raise HTTPException(409, str(exc))
    return {"prompt_id": p.id, "project_id": p.project_id}


@router.post("/prompts/{prompt_id:path}/drafts")
def create_draft(
    prompt_id: str, req: CreateDraftRequest,
    app: AppContext = Depends(app_context),
    session: Session = Depends(get_session),
    ident: Identity = Depends(identity),
):
    _require(ident, "editor", project=_project_of(prompt_id))
    reg = app.registry(session, ident.name)
    try:
        d = reg.create_draft(
            prompt_id, version_number=req.version_number,
            seed_from_version=req.seed_from_version,
            author=ident.name, title=req.title, content=req.content,
        )
    except RegistryError as exc:
        raise HTTPException(400, str(exc))
    return _draft_payload(app, reg, d)


def _draft_payload(app, reg, d) -> dict:
    content = reg.draft_content(d.id)
    ev = extract(content)                       # empty content -> empty var set
    val = reg.validate(d.prompt_id, content)    # empty template is valid
    return {
        "id": d.id, "prompt_id": d.prompt_id, "version_number": d.version_number,
        "base_sha": (d.base_sha[:7] if d.base_sha else None),
        "title": d.title, "author": d.author, "status": d.status,
        "content": content,
        "variables": ev.as_dict(),
        "lint": {"status": val.status, "error": val.error},
    }


@router.get("/drafts/{draft_id}")
def get_draft(
    draft_id: str,
    app: AppContext = Depends(app_context),
    session: Session = Depends(get_session),
    ident: Identity = Depends(identity),
):
    reg = app.registry(session, ident.name)
    try:
        d = reg.get_draft(draft_id)
    except RegistryError as exc:
        raise HTTPException(404, str(exc))
    _require(ident, "viewer", project=_project_of(d.prompt_id))
    return _draft_payload(app, reg, d)


@router.put("/drafts/{draft_id}/content")
def put_draft_content(
    draft_id: str, req: DraftContentRequest,
    app: AppContext = Depends(app_context),
    session: Session = Depends(get_session),
    ident: Identity = Depends(identity),
):
    reg = app.registry(session, ident.name)
    try:
        d = reg.get_draft(draft_id)
    except RegistryError as exc:
        raise HTTPException(404, str(exc))
    _require(ident, "editor", project=_project_of(d.prompt_id))
    reg.put_draft_content(draft_id, req.content, author=ident.name)
    return _draft_payload(app, reg, d)


@router.post("/drafts/{draft_id}/render")
def render_draft(
    draft_id: str, req: DraftRenderRequest,
    app: AppContext = Depends(app_context),
    session: Session = Depends(get_session),
    ident: Identity = Depends(identity),
):
    reg = app.registry(session, ident.name)
    try:
        d = reg.get_draft(draft_id)
    except RegistryError as exc:
        raise HTTPException(404, str(exc))
    _require(ident, "viewer", project=_project_of(d.prompt_id))
    source = reg.draft_content(draft_id)
    flags, variables = req.flags, req.variables
    if req.test_context:
        for tc in reg.get_test_contexts(d.prompt_id):
            if tc.name == req.test_context:
                flags, variables = tc.flags, tc.variables
                break
    try:
        text = app.render_draft_source(session, req.environment, d.prompt_id, source, flags, variables)
    except ServingError as exc:
        raise HTTPException(status_code=exc.status, detail={"detail": exc.detail, **exc.extra})
    except Exception as exc:  # core render errors
        raise HTTPException(422, str(exc))
    return {"rendered": text, "flags": flags, "variables": variables}


@router.post("/drafts/{draft_id}/review")
def review_draft(
    draft_id: str, req: ReviewRequest,
    app: AppContext = Depends(app_context),
    session: Session = Depends(get_session),
    ident: Identity = Depends(identity),
):
    reg = app.registry(session, ident.name)
    try:
        d = reg.get_draft(draft_id)
    except RegistryError as exc:
        raise HTTPException(404, str(exc))
    # The reviewer is the authenticated principal — never a body-supplied string —
    # so self-approval (author == reviewer) can't be spoofed.
    _require(ident, "editor", project=_project_of(d.prompt_id))
    reg.add_review(draft_id, reviewer=ident.name, state=req.state)
    return {"draft_id": draft_id, "status": reg.get_draft(draft_id).status,
            "approvals": [r.reviewer for r in reg.approvals(draft_id)]}


@router.post("/drafts/{draft_id}/commit")
def commit_draft(
    draft_id: str, req: CommitRequest,
    app: AppContext = Depends(app_context),
    session: Session = Depends(get_session),
    ident: Identity = Depends(identity),
):
    reg = app.registry(session, ident.name)
    try:
        d = reg.get_draft(draft_id)
    except RegistryError as exc:
        raise HTTPException(404, str(exc))
    _require(ident, "editor", project=_project_of(d.prompt_id))
    try:
        outcome = reg.commit_draft(
            draft_id, author=ident.name, email=req.email,
            message=req.message, force=req.force,
        )
    except ReviewRequired as exc:
        raise HTTPException(412, str(exc))
    except ConcurrencyError as exc:
        raise HTTPException(409, str(exc))
    metrics.commits_total.labels(_project_of(d.prompt_id)).inc()
    if outcome.validation["status"] != "valid":
        metrics.validation_failures_total.inc()
    app.invalidate()
    return {
        "sha": outcome.sha[:7], "full_sha": outcome.sha,
        "version_number": outcome.version_number, "validation": outcome.validation,
    }


@router.get("/prompts/{prompt_id:path}/drafts")
def list_drafts(
    prompt_id: str,
    app: AppContext = Depends(app_context),
    session: Session = Depends(get_session),
    ident: Identity = Depends(identity),
):
    _require(ident, "viewer", project=_project_of(prompt_id))
    reg = app.registry(session, ident.name)
    drafts = session.execute(
        select(models.Draft).where(
            models.Draft.prompt_id == prompt_id, models.Draft.status.in_(["open", "approved"])
        ).order_by(models.Draft.updated_at.desc())
    ).scalars()
    return {"prompt_id": prompt_id, "drafts": [
        {"id": d.id, "title": d.title, "author": d.author, "status": d.status,
         "version_number": d.version_number,
         "approvals": [r.reviewer for r in reg.approvals(d.id)]}
        for d in drafts
    ]}


@router.get("/prompts/{prompt_id:path}/variables")
def get_variables(
    prompt_id: str, version: int,
    app: AppContext = Depends(app_context),
    session: Session = Depends(get_session),
    ident: Identity = Depends(identity),
):
    _require(ident, "viewer", project=_project_of(prompt_id))
    return {"prompt_id": prompt_id, "version": version,
            "variables": _effective_variables(session, prompt_id, version)}


@router.put("/prompts/{prompt_id:path}/variables")
def put_variable(
    prompt_id: str, version: int, req: RefinementRequest,
    app: AppContext = Depends(app_context),
    session: Session = Depends(get_session),
    ident: Identity = Depends(identity),
):
    _require(ident, "editor", project=_project_of(prompt_id))
    reg = app.registry(session, ident.name)
    reg.set_refinement(prompt_id, version, req.name, type=req.type,
                       required=req.required, default=req.default, description=req.description)
    app.invalidate()  # optional-var defaults are folded into snapshots
    return {"ok": True, "variables": _effective_variables(session, prompt_id, version)}


@router.get("/prompts/{prompt_id:path}/test-contexts")
def get_test_contexts(
    prompt_id: str,
    app: AppContext = Depends(app_context),
    session: Session = Depends(get_session),
    ident: Identity = Depends(identity),
):
    _require(ident, "viewer", project=_project_of(prompt_id))
    reg = app.registry(session, ident.name)
    return {"prompt_id": prompt_id, "test_contexts": [
        {"name": t.name, "flags": t.flags, "variables": t.variables}
        for t in reg.get_test_contexts(prompt_id)
    ]}


@router.put("/prompts/{prompt_id:path}/test-contexts")
def put_test_context(
    prompt_id: str, req: TestContextRequest,
    app: AppContext = Depends(app_context),
    session: Session = Depends(get_session),
    ident: Identity = Depends(identity),
):
    _require(ident, "editor", project=_project_of(prompt_id))
    reg = app.registry(session, ident.name)
    reg.set_test_context(prompt_id, req.name, req.flags, req.variables)
    return {"ok": True}


@router.get("/prompts/{prompt_id:path}/diff")
def diff_versions(
    prompt_id: str, a_version: int, a_sha: str, b_version: int, b_sha: str,
    mode: str = "source", environment: str = "prod", test_context: str | None = None,
    app: AppContext = Depends(app_context),
    session: Session = Depends(get_session),
    ident: Identity = Depends(identity),
):
    _require(ident, "viewer", project=_project_of(prompt_id))
    if mode == "rendered":
        reg = app.registry(session)
        tcs = reg.get_test_contexts(prompt_id)
        tc = next((t for t in tcs if t.name == test_context), tcs[0] if tcs else None)
        flags = tc.flags if tc else {}
        variables = tc.variables if tc else {}
        try:
            left = app.render_at(session, environment, prompt_id, a_version, a_sha, flags, variables)
            right = app.render_at(session, environment, prompt_id, b_version, b_sha, flags, variables)
        except Exception as exc:
            return {"mode": "rendered", "diff": "", "context": (tc.name if tc else None),
                    "error": f"render failed — {exc}. Add a test context that supplies required variables."}
        diff = list(difflib.unified_diff(
            left.splitlines(), right.splitlines(), lineterm="", n=3,
            fromfile=f"v{a_version}@{a_sha[:7]}", tofile=f"v{b_version}@{b_sha[:7]}",
        ))
        return {"mode": "rendered", "diff": "\n".join(diff), "context": (tc.name if tc else None)}
    left = app.git.read(f"{prompt_id}/v{a_version}.j2", ref=a_sha) or ""
    right = app.git.read(f"{prompt_id}/v{b_version}.j2", ref=b_sha) or ""
    diff = list(difflib.unified_diff(
        left.splitlines(), right.splitlines(), lineterm="", n=3,
        fromfile=f"v{a_version}@{a_sha[:7]}", tofile=f"v{b_version}@{b_sha[:7]}",
    ))
    return {"mode": "source", "diff": "\n".join(diff)}


# ── targeting ────────────────────────────────────────────────────────

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


def _references_segment(clauses, name) -> bool:
    if isinstance(clauses, dict):
        if clauses.get("segment") == name:
            return True
        return any(_references_segment(v, name) for v in clauses.values())
    if isinstance(clauses, list):
        return any(_references_segment(v, name) for v in clauses)
    return False


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
    # An operator may propose (or directly release in an unprotected env). `force`
    # is a break-glass direct release even in a protected env — gated to releaser.
    _require(ident, "operator", project=_project_of(req.prompt_id), environment=env)
    if req.force:
        _require(ident, "releaser", environment=env)
    tgt = app.targeting(session, ident.name)
    try:
        outcome = tgt.make_live(
            env, req.prompt_id, req.version_number, req.to_sha,
            comment=req.comment, force=req.force,
        )
    except TargetingError as exc:
        raise HTTPException(400, str(exc))
    app.invalidate(env)
    return {"status": outcome.status, "move_id": outcome.move_id,
            "rules_version": outcome.rules_version}


@router.get("/envs/{env}/approvals")
def list_approvals(
    env: str,
    app: AppContext = Depends(app_context),
    session: Session = Depends(get_session),
    ident: Identity = Depends(identity),
):
    _require(ident, "viewer", environment=env)
    tgt = app.targeting(session, ident.name)
    return {"environment": env, "approvals": [
        {"id": a.id, "change": a.change, "proposed_by": a.proposed_by,
         "status": a.status, "created_at": a.created_at.isoformat()}
        for a in tgt.list_pending_approvals(env)
    ]}


@router.post("/envs/{env}/approvals/{approval_id}/approve")
def approve_change(
    env: str, approval_id: int,
    app: AppContext = Depends(app_context),
    session: Session = Depends(get_session),
    ident: Identity = Depends(identity),
):
    _require(ident, "releaser", environment=env)
    tgt = app.targeting(session, ident.name)
    try:
        appr = tgt.approve(approval_id)
    except TargetingError as exc:
        raise HTTPException(400, str(exc))
    app.invalidate(env)
    return {"id": appr.id, "status": appr.status, "approved_by": appr.approved_by}


@router.post("/envs/{env}/approvals/{approval_id}/reject")
def reject_change(
    env: str, approval_id: int,
    app: AppContext = Depends(app_context),
    session: Session = Depends(get_session),
    ident: Identity = Depends(identity),
):
    _require(ident, "releaser", environment=env)
    tgt = app.targeting(session, ident.name)
    try:
        appr = tgt.reject(approval_id)
    except TargetingError as exc:
        raise HTTPException(400, str(exc))
    return {"id": appr.id, "status": appr.status}


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
    tgt = app.targeting(session, ident.name)
    tgt.set_default(env, req.prompt_id, req.version_number)
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


# ── admin ────────────────────────────────────────────────────────────

@router.get("/audit")
def get_audit(
    limit: int = 100,
    session: Session = Depends(get_session),
    ident: Identity = Depends(identity),
):
    _require(ident, "viewer")
    rows = session.execute(
        select(models.AuditLog).order_by(models.AuditLog.at.desc()).limit(limit)
    ).scalars()
    return {"audit": [
        {"actor": a.actor, "action": a.action, "object_type": a.object_type,
         "object_id": a.object_id, "before": a.before, "after": a.after,
         "at": a.at.isoformat()}
        for a in rows
    ]}


@router.post("/projects")
def create_project(
    req: ProjectRequest,
    app: AppContext = Depends(app_context),
    session: Session = Depends(get_session),
    ident: Identity = Depends(identity),
):
    _require(ident, "admin")
    reg = app.registry(session, ident.name)
    reg.ensure_project(req.id, review_policy=req.review_policy)
    return {"ok": True, "id": req.id}


@router.post("/envs")
def create_env(
    req: EnvironmentRequest,
    session: Session = Depends(get_session),
    ident: Identity = Depends(identity),
):
    _require(ident, "admin")
    if session.get(models.Environment, req.id) is None:
        session.add(models.Environment(
            id=req.id, name=req.id, protected=req.protected, track_tip=req.track_tip
        ))
    return {"ok": True, "id": req.id}


@router.post("/keys")
def create_key(
    req: KeyRequest,
    app: AppContext = Depends(app_context),
    session: Session = Depends(get_session),
    ident: Identity = Depends(identity),
):
    _require(ident, "admin")
    raw = "incant_sk_" + uuid.uuid4().hex
    pid = "p_" + uuid.uuid4().hex[:8]
    session.add(models.Principal(id=pid, kind="service", subject=req.principal_name,
                                 name=req.principal_name))
    session.flush()  # parent row before FK-bearing children
    session.add(models.ApiKey(principal_id=pid, prefix=key_prefix(raw), hash=hash_key(raw),
                              name=req.principal_name))
    session.add(models.RoleBinding(principal_id=pid, role=req.role,
                                   project_id=req.project_id, environment_id=req.environment_id))
    app.invalidate_auth()  # reload the in-memory key table so the new key authenticates
    return {"key": raw, "principal_id": pid, "role": req.role,
            "note": "store this key now; it is not recoverable"}
