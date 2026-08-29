"""Admin: human accounts (§11 Users). Invite people, hand them the link, reset,
disable. API keys stay the machine/developer door (admin.py) — this surface never
touches them except to revoke a disabled person's keys."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from ... import models
from ...service import AppContext
from ...targeting.audit import record_audit
from ..accounts import (
    create_user,
    issue_invite,
    user_by_email,
    user_payload,
    valid_email,
)
from ..auth import _IMPLIES, Identity
from ..deps import app_context, get_session, identity
from ..schemas import InviteUserRequest, UserStatusRequest
from .helpers import _require

router = APIRouter()


def _get_user(session: Session, user_id: str) -> models.User:
    u = session.get(models.User, user_id)
    if u is None:
        raise HTTPException(404, f"unknown user {user_id}")
    return u


def _invite_response(user: models.User, token: str) -> dict:
    """The one-time invite payload. The token appears HERE and never again — the
    admin copies the link to the invitee; only its hash is stored."""
    return {
        "user": user_payload(user),
        "invite_token": token,
        "invite_path": f"/#/welcome/{token}",
        "note": "share this link with them now; it is not recoverable and expires in 7 days",
    }


@router.get("/users")
def list_users(
    session: Session = Depends(get_session),
    ident: Identity = Depends(identity),
):
    _require(ident, "admin")
    rows = session.execute(
        select(models.User).order_by(models.User.created_at)
    ).scalars().all()
    return {"users": [user_payload(u) for u in rows]}


@router.post("/users")
def invite_user(
    req: InviteUserRequest,
    app: AppContext = Depends(app_context),
    session: Session = Depends(get_session),
    ident: Identity = Depends(identity),
):
    """Invite a person: create their account (status ``invited``, no password) with
    an optional initial role, and return the single-use invite link ONCE."""
    _require(ident, "admin")
    if not valid_email(req.email):
        raise HTTPException(422, "a valid email address is required")
    if user_by_email(session, req.email) is not None:
        raise HTTPException(409, f"a user with email {req.email!r} already exists")
    if req.role is not None and req.role not in _IMPLIES:
        raise HTTPException(400, f"unknown role {req.role!r}")

    user = create_user(session, email=req.email, name=req.name)
    if req.role is not None:
        session.add(models.RoleBinding(
            principal_id=user.principal_id, role=req.role,
            project_id=req.project_id, environment_id=req.environment_id))
    token = issue_invite(user)
    record_audit(session, ident.name, "user.invite", "user", user.id,
                 after={"email": user.email, "role": req.role})
    app.invalidate_auth()
    return _invite_response(user, token)


@router.post("/users/{user_id}/reset")
def reset_user(
    user_id: str,
    session: Session = Depends(get_session),
    ident: Identity = Depends(identity),
):
    """A fresh invite/reset link: re-invites someone who lost theirs, or resets an
    active user's password (their current password keeps working until they redeem
    it — redemption replaces it). Any previous link stops working immediately."""
    _require(ident, "admin")
    user = _get_user(session, user_id)
    if user.status == "disabled":
        raise HTTPException(409, "this account is disabled — enable it first")
    token = issue_invite(user)
    record_audit(session, ident.name, "user.reset_link", "user", user.id,
                 after={"email": user.email})
    return _invite_response(user, token)


@router.patch("/users/{user_id}")
def set_user_status(
    user_id: str, req: UserStatusRequest,
    app: AppContext = Depends(app_context),
    session: Session = Depends(get_session),
    ident: Identity = Depends(identity),
):
    """Disable (or re-enable) an account. Disabling is immediate and total: every
    browser session is deleted, every API key on their principal is revoked, and
    outstanding invite links stop redeeming. Re-enabling restores nothing — issue a
    reset link and/or new keys deliberately."""
    _require(ident, "admin")
    user = _get_user(session, user_id)
    me = session.execute(
        select(models.User).where(models.User.principal_id == ident.principal_id)
    ).scalar_one_or_none()
    if req.disabled and me is not None and me.id == user.id:
        raise HTTPException(409, "you can't disable your own account")

    before = user.status
    if req.disabled:
        user.status = "disabled"
        sessions_deleted = session.execute(
            delete(models.Session).where(models.Session.principal_id == user.principal_id)
        ).rowcount or 0
        keys_revoked = 0
        for k in session.execute(
            select(models.ApiKey).where(models.ApiKey.principal_id == user.principal_id,
                                        models.ApiKey.revoked.is_(False))
        ).scalars():
            k.revoked = True
            keys_revoked += 1
        record_audit(session, ident.name, "user.disable", "user", user.id,
                     before={"status": before},
                     after={"status": "disabled", "sessions_deleted": sessions_deleted,
                            "keys_revoked": keys_revoked})
    else:
        # Back to the state their password implies: active if they have one,
        # otherwise invited (they'll need a fresh link either way if it expired).
        user.status = "active" if user.password_hash else "invited"
        record_audit(session, ident.name, "user.enable", "user", user.id,
                     before={"status": before}, after={"status": user.status})
    app.invalidate_auth()
    return {"user": user_payload(user)}
