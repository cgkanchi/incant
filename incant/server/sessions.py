"""Browser session endpoints (full mode only): exchange an API key for an HttpOnly
session cookie, whoami over that cookie, and sign-out. Service/API callers keep using
opaque bearer keys against every other endpoint — this router is purely the UI's door.

Mounted next to the mgmt router in ``full`` mode; never in ``serve`` mode (serve
replicas have no sessions and the render path stays memory-only).
"""

from __future__ import annotations

import datetime as dt
import hmac

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from .. import models
from ..config import get_settings
from ..service import get_app
from ..targeting.audit import record_audit
from . import passwords
from .accounts import (
    begin_initial_setup,
    create_user,
    redeem_invite,
    user_by_email,
    user_count,
    valid_email,
)
from .auth import (
    CSRF_HEADER,
    SESSION_COOKIE,
    SESSION_TTL_DEFAULT,
    SESSION_TTL_REMEMBER,
    Identity,
    _expired,
    hash_key,
    identity_for_principal,
    lookup_session,
    new_csrf_token,
    new_session_id,
    new_session_token,
    touch_last_seen,
)
from .deps import _authenticate, _presented_credential, client_ip, get_session
from .schemas import (
    AcceptInviteRequest,
    PasswordChangeRequest,
    SessionLoginRequest,
    SetupRequest,
)

router = APIRouter(prefix="/auth", tags=["auth"])

# Verified when an email doesn't exist, so "unknown email" and "wrong password"
# take the same time — the response text alone never confirms an account.
_DUMMY_HASH = passwords.hash_password("incant-timing-equalizer")


def _roles(ident: Identity) -> list[dict]:
    return [
        {"role": b.role, "project_id": b.project_id, "environment_id": b.environment_id}
        for b in ident.bindings
    ]


def _whoami(ident: Identity, csrf: str) -> dict:
    return {"principal_id": ident.principal_id, "name": ident.name,
            "roles": _roles(ident), "csrf": csrf}


def _cookie_secure(request: Request) -> bool:
    """Mark the cookie Secure when TLS is enforced or the request itself is https."""
    return get_settings().enforce_tls or request.url.scheme == "https"


def _mint_session(
    request: Request, response: Response, session: Session,
    ident: Identity, remember: bool,
) -> dict:
    """Mint a server-side session for an already-authenticated identity and set the
    HttpOnly cookie. Shared by key sign-in, password sign-in, setup, and invites."""
    token = new_session_token()
    csrf = new_csrf_token()
    now = dt.datetime.now(dt.timezone.utc)
    ttl = SESSION_TTL_REMEMBER if remember else SESSION_TTL_DEFAULT
    session.add(models.Session(
        id=new_session_id(), token_hash=hash_key(token), principal_id=ident.principal_id,
        created_at=now, expires_at=now + ttl, last_seen_at=now,
        csrf_token=csrf, remember=remember,
    ))
    response.set_cookie(
        SESSION_COOKIE, token, httponly=True, samesite="strict", path="/",
        secure=_cookie_secure(request),
        # Persistent cookie only for "remember me"; otherwise a session cookie that
        # dies with the browser (absolute server-side expiry still applies).
        max_age=int(ttl.total_seconds()) if remember else None,
    )
    return _whoami(ident, csrf)


def _throttle_gate(request: Request) -> None:
    """The same per-IP failed-auth throttle bearer auth gets, for the password and
    invite doors (both accept low-entropy guesses, so they need it MOST)."""
    app = get_app()
    retry = app.throttle.retry_after(client_ip(request),
                                     app.settings.auth_throttle_limit,
                                     app.settings.auth_throttle_window)
    if retry is not None:
        raise HTTPException(status_code=429, detail="too many failed attempts",
                            headers={"Retry-After": str(int(retry))})


def _throttle_failure(request: Request) -> None:
    app = get_app()
    if app.settings.auth_throttle_limit > 0:
        app.throttle.record_failure(client_ip(request), app.settings.auth_throttle_window)


def _ident_for_user(session: Session, user: models.User) -> Identity:
    ident = identity_for_principal(session, user.principal_id)
    if ident is None:  # pragma: no cover - the FK guarantees the principal exists
        raise HTTPException(status_code=401, detail="invalid email or password")
    return ident


@router.post("/session")
def create_session(
    req: SessionLoginRequest,
    request: Request,
    response: Response,
    session: Session = Depends(get_session),
):
    """Sign in. Humans present email + password; machines/recovery may present an
    API key. Every failure path is throttled per IP, and the error text never
    reveals whether an email exists."""
    if req.key and (req.email or req.password):
        raise HTTPException(422, "present either email+password or an API key, not both")

    if req.key:
        # Verified through the same machinery as bearer auth (throttle included —
        # a bad key here is a presented credential and counts).
        ident = _authenticate(request, session, f"Bearer {req.key}")
        return _mint_session(request, response, session, ident, req.remember)

    if not (req.email and req.password):
        raise HTTPException(422, "email and password are required")

    _throttle_gate(request)
    user = user_by_email(session, req.email)
    # Always burn one verification so unknown-email and wrong-password take the
    # same time; only an active account with a matching password gets through.
    ok = passwords.verify_password(req.password, (user.password_hash if user else None)
                                   or _DUMMY_HASH)
    if user is None or user.status != "active" or not user.password_hash or not ok:
        _throttle_failure(request)
        raise HTTPException(status_code=401, detail="invalid email or password")

    if passwords.needs_rehash(user.password_hash):
        user.password_hash = passwords.hash_password(req.password)  # transparent upgrade
    user.last_login_at = dt.datetime.now(dt.timezone.utc)
    return _mint_session(request, response, session,
                         _ident_for_user(session, user), req.remember)


@router.get("/setup")
def setup_status(session: Session = Depends(get_session)):
    """Public: does this instance still need its first admin account? Reveals only
    whether setup has happened — the UI uses it to pick the first-run screen."""
    return {"needs_setup": user_count(session) == 0}


@router.post("/setup")
def initial_setup(
    req: SetupRequest,
    request: Request,
    response: Response,
    session: Session = Depends(get_session),
):
    """First boot: create the initial admin ACCOUNT (no API key involved) and sign
    them in. Works exactly once — refused the moment any user exists. Run it right
    after bringing an instance up; machine access stays on API keys (§11)."""
    if not req.name.strip():
        raise HTTPException(422, "name is required")
    if not valid_email(req.email):
        raise HTTPException(422, "a valid email address is required")
    problem = passwords.validate_password(req.password)
    if problem:
        raise HTTPException(422, problem)
    if not begin_initial_setup(session):
        raise HTTPException(409, "setup already completed — sign in instead")

    user = create_user(session, email=req.email, name=req.name.strip())
    user.password_hash = passwords.hash_password(req.password)
    user.status = "active"
    user.last_login_at = dt.datetime.now(dt.timezone.utc)
    session.add(models.RoleBinding(principal_id=user.principal_id, role="admin"))
    record_audit(session, user.email, "auth.setup", "user", user.id,
                 after={"email": user.email, "role": "admin"})
    get_app().invalidate_auth_after_commit(session)
    return _mint_session(request, response, session,
                         _ident_for_user(session, user), remember=False)


@router.post("/accept-invite")
def accept_invite(
    req: AcceptInviteRequest,
    request: Request,
    response: Response,
    session: Session = Depends(get_session),
):
    """Redeem an invite (or password-reset) link: set a password, activate the
    account, sign in. The token is single-use — redemption clears it."""
    _throttle_gate(request)
    user = redeem_invite(session, req.token)
    if user is None:
        _throttle_failure(request)
        raise HTTPException(status_code=401, detail="invalid or expired invite link — "
                                                    "ask an admin for a fresh one")
    problem = passwords.validate_password(req.password)
    if problem:
        raise HTTPException(422, problem)

    user.password_hash = passwords.hash_password(req.password)
    user.status = "active"
    user.invite_token_hash = None
    user.invite_expires_at = None
    if req.name and req.name.strip():
        user.name = req.name.strip()
        principal = session.get(models.Principal, user.principal_id)
        if principal is not None:
            principal.name = user.name
    user.last_login_at = dt.datetime.now(dt.timezone.utc)
    record_audit(session, user.email, "user.accept_invite", "user", user.id,
                 after={"email": user.email})
    get_app().invalidate_auth_after_commit(session)
    return _mint_session(request, response, session,
                         _ident_for_user(session, user), remember=False)


@router.post("/password", status_code=204)
def change_password(
    req: PasswordChangeRequest,
    request: Request,
    session: Session = Depends(get_session),
):
    """Change the signed-in user's own password (cookie + CSRF). Requires the
    current password, and signs out every OTHER session — a stolen session must not
    survive the owner rotating their credential."""
    ident = _authenticate(request, session, None, allow_cookie=True)
    row = lookup_session(session, request.cookies.get(SESSION_COOKIE) or "")
    if row is None:
        raise HTTPException(status_code=401, detail="password change requires a "
                                                    "signed-in browser session")
    user = session.execute(
        select(models.User).where(models.User.principal_id == ident.principal_id)
    ).scalar_one_or_none()
    if user is None or user.status != "active" or not user.password_hash:
        raise HTTPException(status_code=400, detail="this principal has no password account")
    if not passwords.verify_password(req.current_password, user.password_hash):
        _throttle_failure(request)
        raise HTTPException(status_code=403, detail="current password is incorrect")
    problem = passwords.validate_password(req.new_password)
    if problem:
        raise HTTPException(422, problem)
    user.password_hash = passwords.hash_password(req.new_password)
    session.execute(delete(models.Session).where(
        models.Session.principal_id == ident.principal_id,
        models.Session.id != row.id,
    ))
    record_audit(session, user.email, "user.password_change", "user", user.id)
    return Response(status_code=204)


@router.get("/session")
def read_session(
    request: Request,
    session: Session = Depends(get_session),
):
    """Cookie-authenticated whoami. 401 when the cookie is absent/expired/unknown."""
    row = lookup_session(session, request.cookies.get(SESSION_COOKIE) or "")
    if row is None:
        raise HTTPException(status_code=401, detail="invalid or expired session")
    ident = identity_for_principal(session, row.principal_id)
    if ident is None:
        raise HTTPException(status_code=401, detail="invalid or expired session")
    touch_last_seen(row)  # bounded to once / 5 min inside the helper
    return _whoami(ident, row.csrf_token)


@router.delete("/session", status_code=204)
def delete_session(
    request: Request,
    session: Session = Depends(get_session),
):
    """Sign out: requires a valid session + matching CSRF header, deletes the row and
    clears the cookie."""
    row = lookup_session(session, request.cookies.get(SESSION_COOKIE) or "")
    if row is None:
        raise HTTPException(status_code=401, detail="invalid or expired session")
    provided = request.headers.get(CSRF_HEADER)
    if not provided or not hmac.compare_digest(provided, row.csrf_token):
        raise HTTPException(status_code=403, detail="csrf_required")
    session.delete(row)
    resp = Response(status_code=204)
    resp.delete_cookie(SESSION_COOKIE, path="/", samesite="strict",
                       secure=_cookie_secure(request))
    return resp


def _session_row(row: "models.Session", *, current: bool) -> dict:
    def _iso(x):
        return x.isoformat() if x else None
    return {"id": row.id, "created_at": _iso(row.created_at),
            "last_seen_at": _iso(row.last_seen_at), "expires_at": _iso(row.expires_at),
            "remember": row.remember, "current": current}


@router.get("/sessions")
def list_sessions(
    request: Request,
    authorization: str | None = Header(default=None),
    session: Session = Depends(get_session),
):
    """The caller's own active sessions. Cookie OR bearer auth: a cookie caller sees its
    sessions with ``current: true`` on the one making the request; a bearer caller sees
    the principal's sessions with ``current`` always false (no session made the call)."""
    ident = _authenticate(request, session, authorization, allow_cookie=True)
    current_id = None
    if not _presented_credential(authorization):  # cookie caller — mark the current one
        row = lookup_session(session, request.cookies.get(SESSION_COOKIE) or "")
        if row is not None:
            current_id = row.id
    now = dt.datetime.now(dt.timezone.utc)
    rows = session.execute(
        select(models.Session).where(models.Session.principal_id == ident.principal_id)
        .order_by(models.Session.created_at.desc())
    ).scalars().all()
    return {"sessions": [
        _session_row(r, current=(r.id == current_id))
        for r in rows if not _expired(r.expires_at, now)
    ]}


@router.delete("/sessions", status_code=204)
def delete_all_sessions(
    request: Request,
    authorization: str | None = Header(default=None),
    session: Session = Depends(get_session),
):
    """Sign out everywhere: delete every session for the caller's principal, including
    the current one. Cookie OR bearer auth; in cookie mode ``_authenticate`` enforces
    CSRF (this is a non-GET). Returns 204 with an ``X-Incant-Sessions-Deleted`` count and
    clears the caller's own cookie."""
    ident = _authenticate(request, session, authorization, allow_cookie=True)
    count = session.execute(
        delete(models.Session).where(models.Session.principal_id == ident.principal_id)
    ).rowcount or 0
    resp = Response(status_code=204)
    resp.headers["X-Incant-Sessions-Deleted"] = str(count)
    resp.delete_cookie(SESSION_COOKIE, path="/", samesite="strict",
                       secure=_cookie_secure(request))
    return resp
