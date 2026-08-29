"""Human-account machinery shared by the /auth endpoints (setup, sign-in, invite
redemption) and the /mgmt/users admin surface. No FastAPI here — routes stay thin.

Invite/reset tokens are high-entropy random strings stored only as hashes (the same
pepper-aware ``hash_key`` the API keys use — note the same property: rotating
INCANT_KEY_PEPPER invalidates outstanding invites, which simply get re-issued).
Passwords go through server/passwords.py (scrypt)."""

from __future__ import annotations

import datetime as dt
import secrets
import uuid

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import models
from .auth import hash_key

INVITE_TTL = dt.timedelta(days=7)

# A stable, application-owned bigint key for the singleton initial-setup operation.
# Transaction-scoped advisory locks release automatically on commit or rollback.
_INITIAL_SETUP_LOCK_ID = 0x496E63616E74  # ASCII "Incant"


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def normalize_email(email: str) -> str:
    return (email or "").strip().lower()


def valid_email(email: str) -> bool:
    """Light-touch shape check — the invite link is the real verification loop."""
    e = normalize_email(email)
    return 3 <= len(e) <= 254 and "@" in e[1:-1] and " " not in e


def user_by_email(session: Session, email: str) -> models.User | None:
    return session.execute(
        select(models.User).where(models.User.email == normalize_email(email))
    ).scalar_one_or_none()


def user_count(session: Session) -> int:
    return len(session.execute(select(models.User.id)).scalars().all())


def begin_initial_setup(session: Session) -> bool:
    """Serialize first-user creation and report whether setup is still available.

    The caller must create the initial user in this same transaction.  The lock is
    held until that transaction commits or rolls back, so a competing setup waits
    and then observes the user created by the winner.
    """
    session.execute(select(func.pg_advisory_xact_lock(_INITIAL_SETUP_LOCK_ID)))
    return user_count(session) == 0


def create_user(session: Session, *, email: str, name: str) -> models.User:
    """A user + their principal (kind ``user``; RBAC lives on the principal, same as
    services). Starts ``invited`` with no password — ``issue_invite`` mints the way in."""
    principal = models.Principal(id="p_" + uuid.uuid4().hex[:8], kind="user",
                                 subject=normalize_email(email), name=name)
    session.add(principal)
    session.flush()
    user = models.User(id="u_" + uuid.uuid4().hex[:8], principal_id=principal.id,
                       email=normalize_email(email), name=name, status="invited")
    session.add(user)
    session.flush()
    return user


def issue_invite(user: models.User) -> str:
    """Mint a fresh invite/reset token (returned ONCE, stored hashed; any previous
    token stops working). Valid for INVITE_TTL; redeeming sets the password."""
    token = "incant_inv_" + secrets.token_urlsafe(24)
    user.invite_token_hash = hash_key(token)
    user.invite_expires_at = _now() + INVITE_TTL
    return token


def redeem_invite(session: Session, token: str) -> models.User | None:
    """The user a live (unexpired, un-superseded) invite token belongs to, or None.
    Disabled accounts can hold a token but never redeem it.

    The matching row is locked until the caller's transaction ends.  Callers that
    accept the invite must clear its hash in that same transaction; a competing
    redemption then wakes, re-checks the predicate, and finds no matching row.
    """
    if not token:
        return None
    user = session.execute(
        select(models.User)
        .where(models.User.invite_token_hash == hash_key(token))
        .with_for_update()
    ).scalar_one_or_none()
    if user is None or user.status == "disabled":
        return None
    if user.invite_expires_at is None:
        return None
    exp = user.invite_expires_at
    if exp.tzinfo is None:  # defensive: treat naive as UTC
        exp = exp.replace(tzinfo=dt.timezone.utc)
    if exp <= _now():
        return None
    return user


def user_payload(user: models.User) -> dict:
    """The admin-facing row (never token or hash material)."""
    return {
        "id": user.id, "principal_id": user.principal_id, "email": user.email,
        "name": user.name, "status": user.status,
        "invite_pending": bool(user.invite_token_hash),
        "created_at": user.created_at.isoformat() if user.created_at else None,
        "last_login_at": user.last_login_at.isoformat() if user.last_login_at else None,
    }
