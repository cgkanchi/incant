"""Prompts, versions, variables, test contexts, compare-diff, overview, whoami."""

from __future__ import annotations

import difflib

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from ... import models
from ...registry import RegistryError
from ...targeting import build_snapshot
from ..auth import ANY_ENVIRONMENT, Identity
from ..deps import app_context, get_session, identity
from ...service import AppContext, ServingError
from ..schemas import (
    CreatePromptRequest,
    RefinementRequest,
    TestContextRequest,
)
from .helpers import (
    _current_live,
    _current_live_bulk,
    _drafts_needing_review,
    _effective_variables,
    _includes_of,
    _open_draft_counts,
    _project_of,
    _require,
    _tip_ahead,
    _tip_ahead_from_map,
    _validated_by_version,
)

router = APIRouter()

# The version-detail response's `history` array is a UI list — the panel shows recent
# commits, nobody scrolls thousands. Bound the per-version `git log` walk to the newest K
# rather than letting it grow with a version's edit history. `history()` already caps at 50
# by default; we pass it explicitly so the bound is visible at the call site (§ scalability).
_VERSION_HISTORY_LIMIT = 50


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
    # Bulk-load everything the per-prompt loop needs BEFORE the loop, so the landing
    # screen stays flat as the library grows. It used to fan out — per prompt — into a
    # validation SELECT (_tip_ahead), a pointer SELECT (_current_live), a draft-count
    # SELECT, and a `git log` subprocess (app.git.history) — the queries degrading
    # linearly and the subprocess painfully. Each of these loads the same fact for the
    # whole library in one shot: three constant-count queries + one git-log walk.
    validated_by_version = _validated_by_version(session)
    live_by_version = _current_live_bulk(session, environment)
    open_draft_counts = _open_draft_counts(session)
    # Drafts genuinely awaiting review (open drafts under a review-policy project) — the
    # honest count behind the "Needs review" filter, one GROUP BY, same bulk pattern.
    review_needed_counts = _drafts_needing_review(session)
    latest = app.git.latest_commits()  # {path -> newest CommitInfo}, one git log walk
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
            tip_ahead = _tip_ahead_from_map(validated_by_version.get((pid, default_v), []), vinfo.live_sha)
        # Who/when published the default version's current live pointer.
        live = live_by_version.get((pid, default_v)) if default_v else None
        # Newest minted version and whether it has ever been published in this env —
        # drives the "vN draft, not live" badge when a new version exists but is unpublished.
        newest_version = max(vers) if vers else None
        newest_vinfo = vers.get(newest_version) if newest_version is not None else None
        # `updated` = newest commit touching the default version's file; the bulk map's
        # entry is exactly what `app.git.history(...)[0]` returned per prompt before.
        updated = latest.get(f"{pid}/v{default_v}.j2") if default_v else None
        # Drafts still in flight (not committed/discarded) — drives the library's
        # "N open drafts" affordance and filter.
        open_drafts = open_draft_counts.get(pid, 0)
        projects.setdefault(prompt.project_id, []).append({
            "prompt_id": pid,
            "description": prompt.description or "",
            "open_drafts": open_drafts,
            # Open drafts that need a review under this project's policy (0 if no policy) —
            # distinct from open_drafts, which counts drafts-in-flight regardless of policy.
            "drafts_needing_review": review_needed_counts.get(pid, 0),
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
    # Versions ARE environment-divisible — the response carries this env's live pointer,
    # live_sha and tip-ahead per version — so authorize on the concrete environment. A
    # viewer scoped to (this project, this env) then passes exactly the screen the overview
    # already showed them, instead of hitting the old project-only door that a (project, env)
    # binding could not satisfy.
    _require(ident, "viewer", project=_project_of(prompt_id), environment=environment)
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
        hist = app.git.history(f"{prompt_id}/v{v.number}.j2", limit=_VERSION_HISTORY_LIMIT)
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
    # A prompt's variable schema describes the template itself, not any one environment, so
    # an env-scoped project viewer should read it — ANY_ENVIRONMENT waives the env dimension
    # while keeping the project check (see auth.ANY_ENVIRONMENT).
    _require(ident, "viewer", project=_project_of(prompt_id), environment=ANY_ENVIRONMENT)
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
    # Test contexts (named flag/variable sets) belong to the prompt, not an environment —
    # env-scoped project viewer reads them too (ANY_ENVIRONMENT waives only the env dimension).
    _require(ident, "viewer", project=_project_of(prompt_id), environment=ANY_ENVIRONMENT)
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
    # Comparing two versions of a prompt is a read of prompt content, not an env-divisible
    # fact (rendered mode names an env only to resolve includes for the preview). Authorize
    # env-agnostically so an env-scoped project viewer can diff — ANY_ENVIRONMENT keeps the
    # project check and waives just the env dimension.
    _require(ident, "viewer", project=_project_of(prompt_id), environment=ANY_ENVIRONMENT)
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
