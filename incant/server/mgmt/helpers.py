"""Shared helpers for the mgmt route modules: RBAC guards, lock confirmation,
read helpers, and payload shapers. No routes here."""

from __future__ import annotations

import datetime as dt
from collections import defaultdict

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session, aliased

from ... import models
from ...core import extract
from ..auth import _IMPLIES, AuthError, Identity
from ...service import AppContext

ROLES = list(_IMPLIES)  # renderer → admin, canonical order

# Newest-K validated commits kept per (prompt, version) in the overview's bulk load. Only
# the tip and the live pointer's distance-from-tip are ever read, and the UI copy behind
# that distance ("N edits waiting") saturates long before 50 — a live pointer more than K
# validated commits behind the tip displays as the honest cap ("50+ edits waiting"
# territory), so there is no reason to materialise the full history. See
# `_tip_ahead_from_map`: with the window in force, an ancient live_sha absent from the
# (capped) list yields exactly K.
_OVERVIEW_TIP_CAP = 50


def _project_of(prompt_id: str) -> str:
    return prompt_id.split("/", 1)[0]


def _require(ident: Identity, role: str, *, project=None, environment=None) -> None:
    try:
        ident.require(role, project=project, environment=environment)
    except AuthError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.detail)


def _confirm_lock(session: Session, env: str, expected: str, provided: str | None) -> None:
    """Guard mutations to a *locked* (protected) environment: the caller must echo
    `expected` (the prompt id for prompt-scoped acts, the env name for env-scoped
    ones) in the request's `confirm` field — LaunchDarkly-style type-to-confirm.
    A no-op for unprotected environments."""
    e = session.get(models.Environment, env)
    if e and e.protected and (provided or "").strip() != expected:
        raise HTTPException(
            status_code=409,
            detail={"error": "confirmation_required", "environment": env,
                    "expected": expected,
                    "message": f"'{env}' is locked — retype '{expected}' to confirm"},
        )


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


# ── bulk read helpers (overview) ─────────────────────────────────────
#
# The library overview needs the same three facts — validated tip distance, current
# live pointer, open-draft count — for *every* prompt at once. Calling the per-prompt
# helpers above in a loop is one SELECT per prompt per fact; these bulk variants load
# each fact for the whole library in a single query, then the overview indexes into the
# resulting maps. Signatures of the per-call helpers stay untouched — get_versions and
# friends still use them for a single prompt's detail page.

def _validated_by_version(session) -> dict[tuple[str, int], list[str]]:
    """The newest ``_OVERVIEW_TIP_CAP`` valid commits for every (prompt, version),
    newest-first — the bulk analogue of ``_validated_newest_first``, WINDOWED.

    Same ordering as the per-call helper, so a slice keyed by (prompt, version) is
    byte-for-byte its result *within the window* and ``_tip_ahead_from_map`` yields the
    identical index for any live pointer within K validated commits of the tip. Beyond
    that the list is capped at K — an honest cap, not a scan: `_tip_ahead_from_map`
    returns K for a live_sha that has fallen off the window, which the UI shows as the
    "50+ edits waiting" territory.

    A window function (``row_number() OVER (PARTITION BY prompt,version ORDER BY newest)``,
    supported on SQLite ≥3.25 and Postgres) bounds the per-version work to K rows rather
    than scanning the entire validation history on every overview call."""
    rn = func.row_number().over(
        partition_by=(models.CommitValidation.prompt_id, models.CommitValidation.version_number),
        order_by=(models.CommitValidation.validated_at.desc(), models.CommitValidation.id.desc()),
    ).label("rn")
    ranked = (
        select(models.CommitValidation.prompt_id, models.CommitValidation.version_number,
               models.CommitValidation.sha, rn)
        .where(models.CommitValidation.status == "valid")
        .subquery()
    )
    rows = session.execute(
        select(ranked.c.prompt_id, ranked.c.version_number, ranked.c.sha)
        .where(ranked.c.rn <= _OVERVIEW_TIP_CAP)
        .order_by(ranked.c.prompt_id, ranked.c.version_number, ranked.c.rn)
    ).all()
    out: dict[tuple[str, int], list[str]] = defaultdict(list)
    for pid, ver, sha in rows:            # rn-ascending == newest-first within each key
        out[(pid, ver)].append(sha)
    return out


def _tip_ahead_from_map(shas: list[str], live_sha: str) -> int:
    """``_tip_ahead`` against a pre-fetched newest-first SHA list rather than a per-call
    query. Distance of the live SHA from the tip, or — when the live SHA isn't in the list
    — the list length, 0 if none exist. With ``_validated_by_version`` now windowed to
    ``_OVERVIEW_TIP_CAP``, that length branch is the deliberate cap: a live pointer more
    than K validated commits behind the tip reads as exactly K, never a larger true index."""
    if live_sha in shas:
        return shas.index(live_sha)
    return len(shas) if shas else 0


def _current_live_bulk(session, env_id) -> dict[tuple[str, int], models.PointerMove]:
    """The current (newest) live pointer for every (prompt, version) in one env, one
    query — the bulk analogue of ``_current_live``. A window function picks
    ``row_number() == 1`` (the newest move) per (prompt, version) in the database, so we
    materialise one row per key instead of scanning every historical move and reducing in
    Python."""
    rn = func.row_number().over(
        partition_by=(models.PointerMove.prompt_id, models.PointerMove.version_number),
        order_by=(models.PointerMove.moved_at.desc(), models.PointerMove.id.desc()),
    ).label("rn")
    ranked = (
        select(models.PointerMove, rn)
        .where(models.PointerMove.environment_id == env_id)
        .subquery()
    )
    live = aliased(models.PointerMove, ranked)
    rows = session.execute(select(live).where(ranked.c.rn == 1)).scalars().all()
    return {(r.prompt_id, r.version_number): r for r in rows}


def _open_draft_counts(session) -> dict[str, int]:
    """Open/approved draft count per prompt in one GROUP BY, replacing the per-prompt
    COUNT the overview used to run inside its loop."""
    rows = session.execute(
        select(models.Draft.prompt_id, func.count())
        .where(models.Draft.status.in_(["open", "approved"]))
        .group_by(models.Draft.prompt_id)
    ).all()
    return {pid: n for pid, n in rows}


def _drafts_needing_review(session) -> dict[str, int]:
    """Per-prompt count of drafts that ACTUALLY need review, in one GROUP BY — the truthful
    backing for the library's "Needs review" filter.

    "Needs review" only means something under a review policy: a draft awaits review only
    when its prompt's PROJECT requires approvals (Project.review_policy > 0; 0 = no review).
    And only *open* drafts are outstanding — an 'approved' draft has already collected its
    reviews, so it is deliberately excluded. Joining Draft → Prompt → Project lets us apply
    both conditions in the DB; prompts under a no-review project (or with no open drafts)
    simply never appear in the map, so ``.get(pid, 0)`` yields 0. This replaces the old UI
    heuristic (any open-or-approved draft, policy ignored), which over-counted."""
    rows = session.execute(
        select(models.Draft.prompt_id, func.count())
        .join(models.Prompt, models.Prompt.id == models.Draft.prompt_id)
        .join(models.Project, models.Project.id == models.Prompt.project_id)
        .where(models.Draft.status == "open", models.Project.review_policy > 0)
        .group_by(models.Draft.prompt_id)
    ).all()
    return {pid: n for pid, n in rows}


def _newest_version(session, prompt_id: str):
    v = session.execute(
        select(models.Version).where(models.Version.prompt_id == prompt_id)
        .order_by(models.Version.number.desc())
    ).scalars().first()
    return v.number if v else None


def _closure_variables(app, session, prompt_id: str, version: int):
    """Union the inferred variable sets over the whole static include closure (§4).

    Included fragments are followed at their newest version. Returns
    ``(names, required)`` where a name is required if any contributor requires it —
    so a fragment's required variable surfaces in the parent's effective schema.
    """
    names: set[str] = set()
    required: set[str] = set()
    seen: set[tuple[str, int]] = set()

    def walk(pid: str, ver) -> None:
        if ver is None or (pid, ver) in seen:
            return
        seen.add((pid, ver))
        source = app.git.read(f"{pid}/v{ver}.j2")
        if not source:
            return
        ev = extract(source)
        names.update(ev.names)
        required.update(ev.required)
        for inc in ev.includes:
            walk(inc, _newest_version(session, inc))

    walk(prompt_id, version)
    return names, required


def _effective_variables(app, session, prompt_id, version) -> list[dict]:
    names, required = _closure_variables(app, session, prompt_id, version)
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


def _includes_of(app: AppContext, prompt_id: str, version: int) -> list[str]:
    source = app.git.read(f"{prompt_id}/v{version}.j2")
    if not source:
        return []
    return list(extract(source).includes)


def _draft_payload(app, reg, d) -> dict:
    content = reg.draft_content(d.id)
    ev = extract(content)                       # empty content -> empty var set
    val = reg.validate(d.prompt_id, content)    # empty template is valid
    project = reg.s.get(models.Project, _project_of(d.prompt_id))
    return {
        "id": d.id, "prompt_id": d.prompt_id, "version_number": d.version_number,
        "base_sha": (d.base_sha[:7] if d.base_sha else None),
        "base_full_sha": d.base_sha,
        # Current draft revision — the client chains autosaves by echoing this back as
        # `base_revision` on the next PUT (Finding 2).
        "draft_sha": d.draft_sha,
        "title": d.title, "author": d.author, "status": d.status,
        "content": content,
        "variables": ev.as_dict(),
        "lint": {"status": val.status, "error": val.error},
        "project": _project_of(d.prompt_id),
        "review_policy": project.review_policy if project else 0,
        "allow_self_review": project.allow_self_review if project else True,
        # `reviewers` = *current* approvals only (backward-compat). `reviews` = every
        # principal's current verdict, incl. changes_requested; `current` marks whether
        # the verdict is for the draft's present content, `reviewed_sha` the revision it
        # judged (Finding 1).
        "reviewers": [r.reviewer for r in reg.approvals(d.id)],
        "reviews": [{"reviewer": r.reviewer, "state": r.state,
                     "reviewed_sha": r.reviewed_sha,
                     "current": r.reviewed_sha == d.draft_sha}
                    for r in reg.reviews(d.id)],
    }


def _comment_payload(c) -> dict:
    return {"id": c.id, "author": c.author, "anchor": c.anchor, "body": c.body,
            "created_at": c.created_at.isoformat()}


def _references_segment(clauses, name) -> bool:
    if isinstance(clauses, dict):
        if clauses.get("segment") == name:
            return True
        return any(_references_segment(v, name) for v in clauses.values())
    if isinstance(clauses, list):
        return any(_references_segment(v, name) for v in clauses)
    return False


def _principal_payload(session: Session, p: models.Principal) -> dict:
    bindings = session.execute(
        select(models.RoleBinding).where(models.RoleBinding.principal_id == p.id)
        .order_by(models.RoleBinding.id)
    ).scalars().all()
    keys = session.execute(
        select(models.ApiKey).where(models.ApiKey.principal_id == p.id)
        .order_by(models.ApiKey.id)
    ).scalars().all()
    # Active (unexpired) browser-session count — the admin UI shows it and offers
    # "revoke all" (DELETE /mgmt/principals/{id}/sessions).
    active_sessions = session.execute(
        select(func.count()).select_from(models.Session).where(
            models.Session.principal_id == p.id,
            models.Session.expires_at > dt.datetime.now(dt.timezone.utc),
        )
    ).scalar() or 0
    return {
        "id": p.id, "name": p.name, "kind": p.kind, "created_at": p.created_at.isoformat(),
        "bindings": [{"id": b.id, "role": b.role, "project_id": b.project_id,
                      "environment_id": b.environment_id} for b in bindings],
        "keys": [{"id": k.id, "prefix": k.prefix, "name": k.name, "revoked": k.revoked,
                  "expires_at": k.expires_at.isoformat() if k.expires_at else None,
                  "last_used_at": k.last_used_at.isoformat() if k.last_used_at else None}
                 for k in keys],
        "sessions": active_sessions,
    }
