"""Admin: principals, keys, projects, environment create/settings."""

from __future__ import annotations

import datetime as dt
import re
import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session

from ... import models
from ..auth import _IMPLIES, Identity, issue_api_key
from ..deps import app_context, get_session, identity
from ...registry import RegistryError
from ...service import AppContext
from ..schemas import (
    BindingRequest,
    EnvironmentRequest,
    EnvSettingsRequest,
    IssueKeyRequest,
    KeyRequest,
    ProjectRequest,
    ProjectSettingsRequest,
    RenameEnvRequest,
)
from ...targeting.audit import record_audit
from .helpers import ROLES, _confirm_lock, _principal_payload, _require

router = APIRouter()

# Environment ids live in URL paths (``/mgmt/envs/{env}/...``) and in every child row's
# ``environment_id``, so they must be URL-safe slugs: 1–32 lowercase letters/digits, with
# ``-`` or ``_`` allowed only *inside* (a slug can't start or end with a separator).
_ENV_ID_MAX = 32
_ENV_ID_RE = re.compile(r"^[a-z0-9]([a-z0-9_-]*[a-z0-9])?$")


def _validate_env_id(env_id: str) -> None:
    if not env_id or len(env_id) > _ENV_ID_MAX or not _ENV_ID_RE.match(env_id):
        raise HTTPException(
            400,
            f"invalid environment id {env_id!r}: use 1–{_ENV_ID_MAX} lowercase letters, "
            "digits, '-' or '_' (must start and end with a letter or digit)",
        )


# Every table whose rows are scoped to one environment by ``environment_id``. Rule,
# EnvDefault, KillSwitch, PointerMove and the observed-flags tables carry a real FK to
# ``environments.id`` (the observed-flags ones with ON DELETE CASCADE); RuleRevision and
# RoleBinding hold it as a plain string. Delete/rename fan out across all nine — rename
# MUST repoint the cascading tables too, or deleting the old parent row would take the
# observed flags and suppressions with it.
_ENV_SCOPED_MODELS = (
    models.Rule, models.EnvDefault, models.KillSwitch,
    models.PointerMove, models.RuleRevision, models.RoleBinding,
    models.ObservedFlag, models.ObservedFlagSuppression,
)


def _expiry(expires_in_days: int | None) -> dt.datetime | None:
    """Translate an optional key lifetime (days) into an absolute UTC expiry."""
    if expires_in_days is None:
        return None
    return dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=expires_in_days)


def _issued(raw: str, principal_id: str, expires_at: dt.datetime | None, **extra) -> dict:
    """Standard one-time issuance response shape (create/issue/rotate share it)."""
    return {"key": raw, "principal_id": principal_id,
            "expires_at": expires_at.isoformat() if expires_at else None,
            "note": "store this key now; it is not recoverable", **extra}


@router.post("/projects")
def create_project(
    req: ProjectRequest,
    app: AppContext = Depends(app_context),
    session: Session = Depends(get_session, scope="function"),
    ident: Identity = Depends(identity),
):
    _require(ident, "admin")
    reg = app.registry(session, ident.name)
    try:
        reg.ensure_project(req.id, review_policy=req.review_policy,
                           allow_self_review=req.allow_self_review)
    except RegistryError as exc:
        raise HTTPException(409, str(exc))
    return {"ok": True, "id": req.id}


@router.patch("/projects/{project_id}")
def update_project(
    project_id: str, req: ProjectSettingsRequest,
    session: Session = Depends(get_session, scope="function"),
    ident: Identity = Depends(identity),
):
    _require(ident, "admin")
    p = session.get(models.Project, project_id)
    if p is None:
        raise HTTPException(404, f"unknown project {project_id}")
    if req.review_policy is not None:
        p.review_policy = req.review_policy
    if req.allow_self_review is not None:
        p.allow_self_review = req.allow_self_review
    session.flush()
    return {"id": p.id, "review_policy": p.review_policy,
            "allow_self_review": p.allow_self_review}


@router.post("/envs")
def create_env(
    req: EnvironmentRequest,
    app: AppContext = Depends(app_context),
    session: Session = Depends(get_session, scope="function"),
    ident: Identity = Depends(identity),
):
    _require(ident, "admin")
    _validate_env_id(req.id)
    if session.get(models.Environment, req.id) is not None:
        raise HTTPException(409, f"environment {req.id!r} already exists")
    session.add(models.Environment(
        id=req.id, name=req.id, protected=req.protected, track_tip=req.track_tip,
    ))
    session.flush()
    app.targeting(session, ident.name).ensure_baseline(req.id)
    record_audit(session, ident.name, "env.create", "environment", req.id,
                 after={"protected": req.protected, "track_tip": req.track_tip})
    app.invalidate_after_commit(session, req.id)
    return {"ok": True, "id": req.id}


@router.patch("/envs/{env}")
def update_env(
    env: str, req: EnvSettingsRequest,
    app: AppContext = Depends(app_context),
    session: Session = Depends(get_session, scope="function"),
    ident: Identity = Depends(identity),
):
    _require(ident, "admin")
    e = session.get(models.Environment, env)
    if e is None:
        raise HTTPException(404, f"unknown environment {env}")
    before = {"protected": e.protected, "track_tip": e.track_tip}
    if req.protected is not None:
        e.protected = req.protected
    if req.track_tip is not None:
        e.track_tip = req.track_tip
    session.flush()
    after = {"protected": e.protected, "track_tip": e.track_tip}
    # Toggling protection is governance-relevant; record any change (lock/unlock, track_tip).
    if after != before:
        record_audit(session, ident.name, "env.update", "environment", env,
                     before=before, after=after)
    app.invalidate_after_commit(session, env)
    return {"id": e.id, "protected": e.protected, "track_tip": e.track_tip}


@router.delete("/envs/{env}")
def delete_env(
    env: str,
    confirm: str | None = None,
    app: AppContext = Depends(app_context),
    session: Session = Depends(get_session, scope="function"),
    ident: Identity = Depends(identity),
):
    """Delete an environment and EVERYTHING scoped to it — rules, defaults, kill
    switches, the whole live-pointer history, rule revisions, and any env-scoped role
    bindings — in one request/transaction.

    The pointer history (who published what, when) dies with the environment and cannot be
    recovered, so the type-to-confirm ceremony here is UNCONDITIONAL: unlike per-mutation
    ``_confirm_lock`` (which only guards *protected* envs), every delete must echo the env
    id in ``confirm``, protected or not. We refuse outright to delete the configured serving
    default (nothing could serve requests that don't name an env) and any protected env (the
    lock must be lifted first — a deliberate two-step for an irreversible act). Env-scoped
    role bindings are removed too: leaving them would dangle at a now-nonexistent env.
    """
    _require(ident, "admin")
    e = session.get(models.Environment, env)
    if e is None:
        raise HTTPException(404, f"unknown environment {env}")
    default_env = app.settings.default_environment
    if env == default_env:
        raise HTTPException(
            409, f"{env!r} is the configured default environment (INCANT_DEFAULT_ENVIRONMENT) "
                 "and cannot be deleted — point the serving default elsewhere first")
    if e.protected:
        raise HTTPException(409, f"{env!r} is protected — unprotect it first, then delete")
    if (confirm or "").strip() != env:
        # Same shape as _confirm_lock's confirmation_required, so the UI modal machinery
        # works — but unconditional here (not gated on `protected`): deletion is destructive.
        raise HTTPException(
            status_code=409,
            detail={"error": "confirmation_required", "environment": env, "expected": env,
                    "message": f"deleting '{env}' permanently removes its targeting history — "
                               f"retype '{env}' to confirm"},
        )
    before = {"protected": e.protected, "track_tip": e.track_tip}
    # FK-bearing children (Rule/EnvDefault/KillSwitch/PointerMove) must go before the
    # parent env row; RuleRevision/RoleBinding hold the id as a plain string (order-free).
    counts = {
        m.__tablename__: (session.execute(
            delete(m).where(m.environment_id == env)
        ).rowcount or 0)
        for m in _ENV_SCOPED_MODELS
    }
    session.delete(e)
    record_audit(session, ident.name, "env.delete", "environment", env,
                 before=before, after={"deleted": counts})
    app.invalidate_after_commit(session, env)
    app.invalidate_auth_after_commit(session)  # env-scoped role bindings changed
    return {"ok": True, "id": env, "deleted": counts}


@router.post("/envs/{env}/rename")
def rename_env(
    env: str, req: RenameEnvRequest,
    app: AppContext = Depends(app_context),
    session: Session = Depends(get_session, scope="function"),
    ident: Identity = Depends(identity),
):
    """Rename an environment: give it a new id and move EVERYTHING scoped to it across.

    ``Environment.id`` is the primary key AND the value stored in every child's
    ``environment_id``, so a rename is insert-new → repoint-children → delete-old, all in one
    transaction. Statement ORDER satisfies the FKs at every step: the new parent row must
    exist BEFORE we repoint the FK-bearing children at it, and the old parent can only be
    deleted AFTER nothing references it. ``protected``/``track_tip``/``rules_version``/
    ``content_version`` carry over unchanged; ``incarnation`` does not — the new row is
    a new identity to every node's snapshot cache. A locked env requires ``confirm`` to
    echo the CURRENT id.
    """
    _require(ident, "admin")
    e = session.get(models.Environment, env)
    if e is None:
        raise HTTPException(404, f"unknown environment {env}")
    default_env = app.settings.default_environment
    if env == default_env:
        raise HTTPException(
            409, f"{env!r} is the configured default environment and cannot be renamed — "
                 "it would orphan the serving default; point the default elsewhere first")
    new_id = req.new_id
    _validate_env_id(new_id)
    if new_id == env:
        raise HTTPException(409, "the new id is the same as the current id")
    if session.get(models.Environment, new_id) is not None:
        raise HTTPException(409, f"environment {new_id!r} already exists")
    _confirm_lock(session, env, env, req.confirm)  # locked env: echo the CURRENT id
    protected, track_tip, rules_version, content_version = (
        e.protected, e.track_tip, e.rules_version, e.content_version)
    # 1. New parent first (copies every setting; name := new id, matching create_env).
    #    content_version MUST carry over: it is a poll freshness key, and letting the
    #    model default reset it to 1 silently rewound content freshness — every node
    #    kept (or rebuilt to) stale content until the next deployment-wide bump.
    #    `incarnation` deliberately does NOT carry over: to the snapshot caches a
    #    rename IS a new identity (the old id leaves the live set, the new id must be
    #    built — replay memos included — from scratch), so the model default mints a
    #    fresh one for the new row.
    session.add(models.Environment(id=new_id, name=new_id, protected=protected,
                                   track_tip=track_tip, rules_version=rules_version,
                                   content_version=content_version))
    session.flush()  # materialize the new parent before repointing FKs at it
    # 2. Repoint every env-scoped child (FK-bearing + the two plain-string columns).
    for m in _ENV_SCOPED_MODELS:
        session.execute(
            update(m).where(m.environment_id == env).values(environment_id=new_id)
        )
    # 3. Old parent last — nothing references it now.
    session.delete(e)
    record_audit(session, ident.name, "env.rename", "environment", new_id,
                 before={"id": env},
                 after={"id": new_id, "protected": protected, "track_tip": track_tip,
                        "rules_version": rules_version,
                        "content_version": content_version})
    app.invalidate_after_commit(session, env)
    app.invalidate_after_commit(session, new_id)
    app.invalidate_auth_after_commit(session)  # env-scoped role bindings moved to the new id
    return {"id": new_id, "protected": protected, "track_tip": track_tip,
            "rules_version": rules_version, "content_version": content_version}


@router.get("/setup-status")
def setup_status(
    session: Session = Depends(get_session, scope="function"),
    ident: Identity = Depends(identity),
):
    """The first-run checklist's live state (admin): what this deployment still
    lacks. Cheap counts only — the UI's 'Get set up' card renders from this."""
    _require(ident, "admin")
    def _count(q) -> int:
        return session.execute(select(func.count()).select_from(q)).scalar() or 0
    return {
        "prompts": _count(models.Prompt),
        "people": _count(models.User),
        "remotes": _count(models.Remote),
        # The checklist item is "issue a key your app can RENDER with" — so count
        # active keys on renderer-bound principals only. Counting every ApiKey marked
        # the item done from minute one: the auto-generated bootstrap ADMIN key
        # (auth._insert_bootstrap_admin) always exists and is not a serving credential.
        "renderer_keys": session.execute(
            select(func.count(func.distinct(models.ApiKey.id)))
            .select_from(models.ApiKey)
            .join(models.RoleBinding,
                  models.RoleBinding.principal_id == models.ApiKey.principal_id)
            .where(models.ApiKey.revoked.is_(False),
                   models.RoleBinding.role == "renderer")
        ).scalar() or 0,
    }


@router.post("/seed-example")
def seed_example(
    app: AppContext = Depends(app_context),
    session: Session = Depends(get_session, scope="function"),
    ident: Identity = Depends(identity),
):
    """Load the design's example dataset into an EMPTY library — the fastest way
    to see the product. One-shot and gated: refused the moment any prompt exists,
    so it can never pollute a real instance."""
    _require(ident, "admin")
    if session.execute(select(models.Prompt.id).limit(1)).first() is not None:
        raise HTTPException(409, "the library already has prompts — the example "
                                 "dataset only loads into an empty deployment")
    from ...seed import seed as run_seed
    renderer_key = run_seed(app)
    # The seed lands in the deployment's bound project (or names it "support").
    project = session.execute(select(models.Project.id)).scalars().first()
    record_audit(session, ident.name, "seed.example", "project", project or "support")
    app.invalidate()
    app.invalidate_auth_after_commit(session)
    return {"ok": True, "renderer_key": renderer_key, "project": project,
            "note": "store the renderer key now; it is not shown again"}


@router.post("/keys")
def create_key(
    req: KeyRequest,
    app: AppContext = Depends(app_context),
    session: Session = Depends(get_session, scope="function"),
    ident: Identity = Depends(identity),
):
    _require(ident, "admin")
    if req.role not in _IMPLIES:
        raise HTTPException(400, f"unknown role {req.role!r}")
    pid = "p_" + uuid.uuid4().hex[:8]
    expires_at = _expiry(req.expires_in_days)
    session.add(models.Principal(id=pid, kind="service", subject=req.principal_name,
                                 name=req.principal_name))
    session.flush()  # parent row before FK-bearing children
    raw, _ = issue_api_key(session, principal_id=pid, name=req.principal_name,
                           expires_at=expires_at)
    session.add(models.RoleBinding(principal_id=pid, role=req.role,
                                   project_id=req.project_id, environment_id=req.environment_id))
    record_audit(session, ident.name, "principal.create", "principal", pid,
                 after={"name": req.principal_name, "role": req.role})
    app.invalidate_auth_after_commit(session)  # reload after the new key commits
    return _issued(raw, pid, expires_at, role=req.role)


# ── admin: users, roles, keys ────────────────────────────────────────

@router.get("/principals")
def list_principals(
    session: Session = Depends(get_session, scope="function"),
    ident: Identity = Depends(identity),
):
    _require(ident, "admin")
    principals = session.execute(
        select(models.Principal).order_by(models.Principal.created_at)
    ).scalars().all()
    projects = [p.id for p in session.execute(
        select(models.Project).order_by(models.Project.id)).scalars()]
    envs = [e.id for e in session.execute(
        select(models.Environment).order_by(models.Environment.id)).scalars()]
    return {"roles": ROLES, "projects": projects, "environments": envs,
            "principals": [_principal_payload(session, p) for p in principals]}


@router.post("/principals/{pid}/bindings")
def add_binding(
    pid: str, req: BindingRequest,
    app: AppContext = Depends(app_context),
    session: Session = Depends(get_session, scope="function"),
    ident: Identity = Depends(identity),
):
    _require(ident, "admin")
    if session.get(models.Principal, pid) is None:
        raise HTTPException(404, f"unknown principal {pid}")
    if req.role not in _IMPLIES:
        raise HTTPException(400, f"unknown role {req.role!r}")
    session.add(models.RoleBinding(principal_id=pid, role=req.role,
                                   project_id=req.project_id, environment_id=req.environment_id))
    record_audit(session, ident.name, "binding.add", "principal", pid,
                 after={"role": req.role, "project_id": req.project_id,
                        "environment_id": req.environment_id})
    app.invalidate_auth_after_commit(session)
    return {"ok": True}


@router.delete("/principals/{pid}/bindings/{binding_id}")
def remove_binding(
    pid: str, binding_id: int,
    app: AppContext = Depends(app_context),
    session: Session = Depends(get_session, scope="function"),
    ident: Identity = Depends(identity),
):
    _require(ident, "admin")
    b = session.get(models.RoleBinding, binding_id)
    if b is None or b.principal_id != pid:
        raise HTTPException(404, "unknown binding")
    record_audit(session, ident.name, "binding.remove", "principal", pid,
                 before={"role": b.role, "project_id": b.project_id,
                         "environment_id": b.environment_id})
    session.delete(b)
    app.invalidate_auth_after_commit(session)
    return {"ok": True}


@router.delete("/principals/{pid}/sessions")
def revoke_principal_sessions(
    pid: str,
    session: Session = Depends(get_session, scope="function"),
    ident: Identity = Depends(identity),
):
    """Admin: revoke every browser session for a principal (sign them out everywhere).
    Audited as ``session.revoke_all``. Returns the number of sessions deleted."""
    _require(ident, "admin")
    if session.get(models.Principal, pid) is None:
        raise HTTPException(404, f"unknown principal {pid}")
    count = session.execute(
        delete(models.Session).where(models.Session.principal_id == pid)
    ).rowcount or 0
    record_audit(session, ident.name, "session.revoke_all", "principal", pid,
                 after={"revoked": count})
    return {"ok": True, "revoked": count}


@router.post("/principals/{pid}/keys")
def issue_key(
    pid: str,
    req: IssueKeyRequest = IssueKeyRequest(),
    app: AppContext = Depends(app_context),
    session: Session = Depends(get_session, scope="function"),
    ident: Identity = Depends(identity),
):
    _require(ident, "admin")
    p = session.get(models.Principal, pid)
    if p is None:
        raise HTTPException(404, f"unknown principal {pid}")
    expires_at = _expiry(req.expires_in_days)
    raw, _ = issue_api_key(session, principal_id=pid, name=p.name, expires_at=expires_at)
    record_audit(session, ident.name, "key.issue", "principal", pid)
    app.invalidate_auth_after_commit(session)
    return _issued(raw, pid, expires_at)


@router.post("/keys/{key_id}/rotate")
def rotate_key(
    key_id: int,
    req: IssueKeyRequest = IssueKeyRequest(),
    app: AppContext = Depends(app_context),
    session: Session = Depends(get_session, scope="function"),
    ident: Identity = Depends(identity),
):
    """Atomically issue a replacement key for the old key's principal and revoke the
    old one, in a single committed transaction. Returns the new key once (issuance
    shape). The caller may set a fresh lifetime via `expires_in_days`."""
    _require(ident, "admin")
    old = session.get(models.ApiKey, key_id)
    if old is None:
        raise HTTPException(404, f"unknown key {key_id}")
    expires_at = _expiry(req.expires_in_days)
    raw, new_key = issue_api_key(session, principal_id=old.principal_id, name=old.name,
                                 expires_at=expires_at)
    old.revoked = True
    session.flush()  # materialize the new key's id for the response/audit
    record_audit(session, ident.name, "key.rotate", "principal", old.principal_id,
                 before={"key_id": key_id, "prefix": old.prefix},
                 after={"key_id": new_key.id, "prefix": new_key.prefix})
    app.invalidate_auth_after_commit(session)
    return _issued(raw, old.principal_id, expires_at, key_id=new_key.id,
                   revoked_key_id=key_id)


@router.post("/keys/{key_id}/revoke")
def revoke_key(
    key_id: int,
    app: AppContext = Depends(app_context),
    session: Session = Depends(get_session, scope="function"),
    ident: Identity = Depends(identity),
):
    _require(ident, "admin")
    k = session.get(models.ApiKey, key_id)
    if k is None:
        raise HTTPException(404, f"unknown key {key_id}")
    k.revoked = True
    record_audit(session, ident.name, "key.revoke", "principal", k.principal_id,
                 after={"key_id": key_id, "prefix": k.prefix})
    app.invalidate_auth_after_commit(session)
    return {"ok": True, "key_id": key_id}
