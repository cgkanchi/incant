"""Admin: principals, keys, projects, environment create/settings."""

from __future__ import annotations

import datetime as dt
import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from ... import models
from ..auth import _IMPLIES, hash_key, key_prefix, Identity
from ..deps import app_context, get_session, identity
from ...service import AppContext
from ..schemas import (
    BindingRequest,
    EnvironmentRequest,
    EnvSettingsRequest,
    IssueKeyRequest,
    KeyRequest,
    ProjectRequest,
    ProjectSettingsRequest,
)
from ...targeting.audit import record_audit
from .helpers import ROLES, _principal_payload, _require

router = APIRouter()


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
    session: Session = Depends(get_session),
    ident: Identity = Depends(identity),
):
    _require(ident, "admin")
    reg = app.registry(session, ident.name)
    reg.ensure_project(req.id, review_policy=req.review_policy,
                       allow_self_review=req.allow_self_review)
    return {"ok": True, "id": req.id}


@router.patch("/projects/{project_id}")
def update_project(
    project_id: str, req: ProjectSettingsRequest,
    session: Session = Depends(get_session),
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
    session: Session = Depends(get_session),
    ident: Identity = Depends(identity),
):
    _require(ident, "admin")
    if session.get(models.Environment, req.id) is None:
        session.add(models.Environment(
            id=req.id, name=req.id, protected=req.protected, track_tip=req.track_tip,
        ))
    return {"ok": True, "id": req.id}


@router.patch("/envs/{env}")
def update_env(
    env: str, req: EnvSettingsRequest,
    app: AppContext = Depends(app_context),
    session: Session = Depends(get_session),
    ident: Identity = Depends(identity),
):
    _require(ident, "admin")
    e = session.get(models.Environment, env)
    if e is None:
        raise HTTPException(404, f"unknown environment {env}")
    if req.protected is not None:
        e.protected = req.protected
    if req.track_tip is not None:
        e.track_tip = req.track_tip
    session.flush()
    app.invalidate(env)
    return {"id": e.id, "protected": e.protected, "track_tip": e.track_tip}


@router.post("/keys")
def create_key(
    req: KeyRequest,
    app: AppContext = Depends(app_context),
    session: Session = Depends(get_session),
    ident: Identity = Depends(identity),
):
    _require(ident, "admin")
    if req.role not in _IMPLIES:
        raise HTTPException(400, f"unknown role {req.role!r}")
    raw = "incant_sk_" + uuid.uuid4().hex
    pid = "p_" + uuid.uuid4().hex[:8]
    expires_at = _expiry(req.expires_in_days)
    session.add(models.Principal(id=pid, kind="service", subject=req.principal_name,
                                 name=req.principal_name))
    session.flush()  # parent row before FK-bearing children
    session.add(models.ApiKey(principal_id=pid, prefix=key_prefix(raw), hash=hash_key(raw),
                              name=req.principal_name, expires_at=expires_at))
    session.add(models.RoleBinding(principal_id=pid, role=req.role,
                                   project_id=req.project_id, environment_id=req.environment_id))
    record_audit(session, ident.name, "principal.create", "principal", pid,
                 after={"name": req.principal_name, "role": req.role})
    app.invalidate_auth()  # reload the in-memory key table so the new key authenticates
    return _issued(raw, pid, expires_at, role=req.role)


# ── admin: users, roles, keys ────────────────────────────────────────

@router.get("/principals")
def list_principals(
    session: Session = Depends(get_session),
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
    session: Session = Depends(get_session),
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
    app.invalidate_auth()
    return {"ok": True}


@router.delete("/principals/{pid}/bindings/{binding_id}")
def remove_binding(
    pid: str, binding_id: int,
    app: AppContext = Depends(app_context),
    session: Session = Depends(get_session),
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
    app.invalidate_auth()
    return {"ok": True}


@router.post("/principals/{pid}/keys")
def issue_key(
    pid: str,
    req: IssueKeyRequest = IssueKeyRequest(),
    app: AppContext = Depends(app_context),
    session: Session = Depends(get_session),
    ident: Identity = Depends(identity),
):
    _require(ident, "admin")
    p = session.get(models.Principal, pid)
    if p is None:
        raise HTTPException(404, f"unknown principal {pid}")
    raw = "incant_sk_" + uuid.uuid4().hex
    expires_at = _expiry(req.expires_in_days)
    session.add(models.ApiKey(principal_id=pid, prefix=key_prefix(raw), hash=hash_key(raw),
                              name=p.name, expires_at=expires_at))
    record_audit(session, ident.name, "key.issue", "principal", pid)
    app.invalidate_auth()
    return _issued(raw, pid, expires_at)


@router.post("/keys/{key_id}/rotate")
def rotate_key(
    key_id: int,
    req: IssueKeyRequest = IssueKeyRequest(),
    app: AppContext = Depends(app_context),
    session: Session = Depends(get_session),
    ident: Identity = Depends(identity),
):
    """Atomically issue a replacement key for the old key's principal and revoke the
    old one, in a single committed transaction. Returns the new key once (issuance
    shape). The caller may set a fresh lifetime via `expires_in_days`."""
    _require(ident, "admin")
    old = session.get(models.ApiKey, key_id)
    if old is None:
        raise HTTPException(404, f"unknown key {key_id}")
    raw = "incant_sk_" + uuid.uuid4().hex
    expires_at = _expiry(req.expires_in_days)
    new_key = models.ApiKey(principal_id=old.principal_id, prefix=key_prefix(raw),
                            hash=hash_key(raw), name=old.name, expires_at=expires_at)
    session.add(new_key)
    old.revoked = True
    session.flush()  # materialize the new key's id for the response/audit
    record_audit(session, ident.name, "key.rotate", "principal", old.principal_id,
                 before={"key_id": key_id, "prefix": old.prefix},
                 after={"key_id": new_key.id, "prefix": new_key.prefix})
    app.invalidate_auth()
    return _issued(raw, old.principal_id, expires_at, key_id=new_key.id,
                   revoked_key_id=key_id)


@router.post("/keys/{key_id}/revoke")
def revoke_key(
    key_id: int,
    app: AppContext = Depends(app_context),
    session: Session = Depends(get_session),
    ident: Identity = Depends(identity),
):
    _require(ident, "admin")
    k = session.get(models.ApiKey, key_id)
    if k is None:
        raise HTTPException(404, f"unknown key {key_id}")
    k.revoked = True
    record_audit(session, ident.name, "key.revoke", "principal", k.principal_id,
                 after={"key_id": key_id, "prefix": k.prefix})
    app.invalidate_auth()
    return {"ok": True, "key_id": key_id}
