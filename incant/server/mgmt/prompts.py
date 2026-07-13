"""Prompts, versions, variables, test contexts, compare-diff, overview, whoami."""

from __future__ import annotations

import difflib

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ... import models
from ...registry import RegistryError
from ...targeting import build_snapshot
from ..auth import Identity
from ..deps import app_context, get_session, identity
from ...service import AppContext, ServingError
from ..schemas import (
    CreatePromptRequest,
    RefinementRequest,
    TestContextRequest,
)
from .helpers import (
    _current_live,
    _effective_variables,
    _includes_of,
    _project_of,
    _require,
    _tip_ahead,
)

router = APIRouter()


# ── identity ─────────────────────────────────────────────────────────

@router.get("/whoami")
def whoami(ident: Identity = Depends(identity)):
    return {
        "principal_id": ident.principal_id,
        "name": ident.name,
        "roles": [
            {"role": b.role, "project_id": b.project_id, "environment_id": b.environment_id}
            for b in ident.bindings
        ],
    }


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
        default_v = snap.defaults.get(pid)
        vinfo = vers.get(default_v) if default_v else None
        tip_ahead = 0
        if vinfo and vinfo.live_sha:
            tip_ahead = _tip_ahead(session, environment, pid, default_v, vinfo.live_sha)
        # Who/when published the default version's current live pointer.
        live = _current_live(session, environment, pid, default_v) if default_v else None
        # Newest minted version and whether it has ever been published in this env —
        # drives the "vN draft, not live" badge when a new version exists but is unpublished.
        newest_version = max(vers) if vers else None
        newest_vinfo = vers.get(newest_version) if newest_version is not None else None
        hist = app.git.history(f"{pid}/v{default_v}.j2") if default_v else []
        updated = hist[0] if hist else None
        # Drafts still in flight (not committed/discarded) — drives the library's
        # "N open drafts" affordance and filter.
        open_drafts = session.execute(
            select(func.count()).select_from(models.Draft).where(
                models.Draft.prompt_id == pid,
                models.Draft.status.in_(["open", "approved"]),
            )
        ).scalar_one()
        projects.setdefault(prompt.project_id, []).append({
            "prompt_id": pid,
            "description": prompt.description or "",
            "open_drafts": open_drafts,
            "versions": len(vers),
            "live_version": default_v,
            "live": bool(vinfo and vinfo.live_sha),
            "live_by": (live.moved_by or None) if live else None,
            "live_at": (live.moved_at.isoformat() if live else None),
            "tip_ahead": tip_ahead,
            "newest_version": newest_version,
            "newest_version_live": bool(newest_vinfo and newest_vinfo.live_sha),
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
            "live_by": (live.moved_by or None) if live else None,
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
        "variables": _effective_variables(app, session, prompt_id, display_v) if display_v else [],
        "includes": _includes_of(app, prompt_id, display_v) if display_v else [],
        "display_version": display_v,
    }


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


@router.get("/prompts/{prompt_id:path}/variables")
def get_variables(
    prompt_id: str, version: int,
    app: AppContext = Depends(app_context),
    session: Session = Depends(get_session),
    ident: Identity = Depends(identity),
):
    _require(ident, "viewer", project=_project_of(prompt_id))
    return {"prompt_id": prompt_id, "version": version,
            "variables": _effective_variables(app, session, prompt_id, version)}


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
    return {"ok": True, "variables": _effective_variables(app, session, prompt_id, version)}


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
        except ServingError as exc:
            return {"mode": "rendered", "diff": "", "context": (tc.name if tc else None),
                    "error": str(exc.detail), "error_kind": "serving"}
        except Exception as exc:
            return {"mode": "rendered", "diff": "", "context": (tc.name if tc else None), "error_kind": "template",
                    "error": f"render failed — {exc}. Supply the variables this prompt needs."}
        diff = list(difflib.unified_diff(
            left.splitlines(), right.splitlines(), lineterm="", n=3,
            fromfile=f"v{a_version}@{a_sha[:7]}", tofile=f"v{b_version}@{b_sha[:7]}",
        ))
        return {"mode": "rendered", "diff": "\n".join(diff), "left": left, "right": right,
                "context": (tc.name if tc else None)}
    left = app.git.read(f"{prompt_id}/v{a_version}.j2", ref=a_sha) or ""
    right = app.git.read(f"{prompt_id}/v{b_version}.j2", ref=b_sha) or ""
    diff = list(difflib.unified_diff(
        left.splitlines(), right.splitlines(), lineterm="", n=3,
        fromfile=f"v{a_version}@{a_sha[:7]}", tofile=f"v{b_version}@{b_sha[:7]}",
    ))
    return {"mode": "source", "diff": "\n".join(diff), "left": left, "right": right}
