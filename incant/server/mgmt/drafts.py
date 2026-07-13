"""Drafts, comments, review, render, and diff."""

from __future__ import annotations

import difflib
import json

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from ... import models
from ...registry import ConcurrencyError, RegistryError, ReviewRequired, StaleDraftWrite
from ..auth import Identity
from ..deps import app_context, get_session, identity
from ...service import AppContext, ServingError
from ..schemas import (
    CommentRequest,
    CommitRequest,
    CreateDraftRequest,
    DraftContentRequest,
    DraftRenderRequest,
    ReviewRequest,
)
from .. import metrics
from .helpers import _comment_payload, _draft_payload, _project_of, _require

router = APIRouter()


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
    try:
        reg.put_draft_content(draft_id, req.content, author=ident.name,
                              base_revision=req.base_revision)
    except StaleDraftWrite as exc:
        # 409: the client's editor state is behind a newer autosave — hand back the
        # current tip + content so it can recover instead of clobbering (Finding 2).
        raise HTTPException(409, {"error": "stale_write", "current_sha": exc.current_sha,
                                  "current_content": exc.current_content})
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


@router.get("/drafts/{draft_id}/diff")
def diff_draft(
    draft_id: str,
    against_version: int | None = None, against_sha: str | None = None,
    mode: str = "source", environment: str = "prod", test_context: str | None = None,
    flags: str | None = None, variables: str | None = None,
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
    # Default target is the draft's own version at its base — "what did I change".
    v = against_version if against_version is not None else d.version_number
    sha = against_sha if against_sha is not None else d.base_sha
    left = (app.git.read(f"{d.prompt_id}/v{v}.j2", ref=sha) if sha else "") or ""
    right = reg.draft_content(draft_id)
    fromfile = f"v{v}@{sha[:7]}" if sha else f"v{v} (new)"
    tofile = f"draft:{draft_id[:7]}"
    if mode == "rendered":
        # An inline ad-hoc context (JSON query params) takes precedence over a
        # named test context — the UI's set-values-right-here panel uses this.
        if flags is not None or variables is not None:
            try:
                fl = json.loads(flags) if flags else {}
                vb = json.loads(variables) if variables else {}
            except ValueError:
                raise HTTPException(422, "flags/variables must be valid JSON")
            tc, ctx_name = None, "custom"
        else:
            tcs = reg.get_test_contexts(d.prompt_id)
            tc = next((t for t in tcs if t.name == test_context), tcs[0] if tcs else None)
            fl = tc.flags if tc else {}
            vb = tc.variables if tc else {}
            ctx_name = tc.name if tc else None
        try:
            left_txt = app.render_at(session, environment, d.prompt_id, v, sha,
                                     fl, vb) if sha else ""
            right_txt = app.render_draft_source(session, environment, d.prompt_id,
                                                right, fl, vb)
        except ServingError as exc:
            # Content-resolution failure (e.g. an included prompt with nothing
            # published in this environment) — variables/test contexts won't fix it.
            return {"mode": "rendered", "diff": "", "context": ctx_name,
                    "error": str(exc.detail), "error_kind": "serving"}
        except Exception as exc:
            return {"mode": "rendered", "diff": "", "context": ctx_name, "error_kind": "template",
                    "error": f"render failed — {exc}. Supply the variables this draft needs."}
        diff = list(difflib.unified_diff(
            left_txt.splitlines(), right_txt.splitlines(), lineterm="", n=3,
            fromfile=fromfile, tofile=tofile,
        ))
        return {"mode": "rendered", "diff": "\n".join(diff), "context": ctx_name,
                "left": left_txt, "right": right_txt}
    diff = list(difflib.unified_diff(
        left.splitlines(), right.splitlines(), lineterm="", n=3,
        fromfile=fromfile, tofile=tofile,
    ))
    return {"mode": "source", "diff": "\n".join(diff), "left": left, "right": right}


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
    # The reviewer is the authenticated principal — never a body-supplied string.
    # Self-review is allowed by default (per-project opt-out); either way the
    # recorded reviewer is the real identity, not a spoofable name.
    _require(ident, "editor", project=_project_of(d.prompt_id))
    # "approved" counts toward the review policy; "changes_requested" is recorded and
    # visible but does not, and it clears this principal's earlier approval (and v.v.).
    reg.add_review(draft_id, reviewer=ident.name, state=req.state)
    d = reg.get_draft(draft_id)
    return {"draft_id": draft_id, "status": d.status,
            "approvals": [r.reviewer for r in reg.approvals(draft_id)],
            "reviews": [{"reviewer": r.reviewer, "state": r.state,
                         "reviewed_sha": r.reviewed_sha,
                         "current": r.reviewed_sha == d.draft_sha}
                        for r in reg.reviews(draft_id)]}


@router.get("/drafts/{draft_id}/comments")
def get_comments(
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
    # Viewer is the loosest sensible role: reviewers who can see a draft may comment,
    # even without editor on the project.
    _require(ident, "viewer", project=_project_of(d.prompt_id))
    return {"comments": [_comment_payload(c) for c in reg.list_comments(draft_id)]}


@router.post("/drafts/{draft_id}/comments")
def create_comment(
    draft_id: str, req: CommentRequest,
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
    # A committed/discarded draft is settled — no further review conversation on it.
    if d.status in ("committed", "discarded", "abandoned"):
        raise HTTPException(409, f"cannot comment on a {d.status} draft")
    # Author is always the authenticated principal, never body-supplied.
    c = reg.add_comment(draft_id, author=ident.name, body=req.body, anchor=req.anchor)
    return _comment_payload(c)


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
        if exc.base_sha is None:  # plain conflict without shas — surface the message
            raise HTTPException(409, str(exc))
        path = f"{d.prompt_id}/v{d.version_number}.j2"
        left = app.git.read(path, ref=exc.base_sha) or ""
        right = app.git.read(path) or ""
        diff = "\n".join(difflib.unified_diff(
            left.splitlines(), right.splitlines(), lineterm="", n=3,
            fromfile=f"v{d.version_number}@{exc.base_sha[:7]}",
            tofile=f"v{d.version_number}@{exc.current_sha[:7]}",
        ))
        raise HTTPException(409, {"detail": str(exc), "base_sha": exc.base_sha[:7],
                                  "current_sha": exc.current_sha[:7], "diff": diff})
    metrics.commits_total.labels(_project_of(d.prompt_id)).inc()
    if outcome.validation["status"] != "valid":
        metrics.validation_failures_total.inc()
    else:
        # §7 track_tip: environments that follow tips auto-advance their live pointer.
        app.auto_advance_tips(session, ident.name, d.prompt_id, d.version_number, outcome.sha)
    app.invalidate()
    return {
        "sha": outcome.sha[:7], "full_sha": outcome.sha,
        "version_number": outcome.version_number, "validation": outcome.validation,
    }


@router.post("/drafts/{draft_id}/discard")
def discard_draft(
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
    _require(ident, "editor", project=_project_of(d.prompt_id))
    try:
        reg.discard_draft(draft_id)
    except RegistryError as exc:
        raise HTTPException(400, str(exc))
    return {"ok": True, "draft_id": draft_id, "status": "discarded"}


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
         "base_sha": (d.base_sha[:7] if d.base_sha else None),
         "updated_at": (d.updated_at.isoformat() if d.updated_at else None),
         "approvals": [r.reviewer for r in reg.approvals(d.id)]}
        for d in drafts
    ]}
