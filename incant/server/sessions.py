"""Browser session endpoints (full mode only): exchange an API key for an HttpOnly
session cookie, whoami over that cookie, and sign-out. Service/API callers keep using
opaque bearer keys against every other endpoint — this router is purely the UI's door.

Mounted next to the mgmt router in ``full`` mode; never in ``serve`` mode (serve
replicas have no sessions and the render path stays memory-only).
"""

from __future__ import annotations

import datetime as dt
import hmac

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy.orm import Session

from .. import models
from ..config import get_settings
from .auth import (
    CSRF_HEADER,
    SESSION_COOKIE,
    SESSION_TTL_DEFAULT,
    SESSION_TTL_REMEMBER,
    Identity,
    hash_key,
    identity_for_principal,
    lookup_session,
    new_csrf_token,
    new_session_id,
    new_session_token,
    touch_last_seen,
)
from .deps import _authenticate, get_session
from .schemas import SessionLoginRequest

router = APIRouter(prefix="/auth", tags=["auth"])


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


@router.post("/session")
def create_session(
    req: SessionLoginRequest,
    request: Request,
    response: Response,
    session: Session = Depends(get_session),
):
    """Verify the presented key through the same machinery as bearer auth (throttle
    included — a bad key here is a presented credential and counts), then mint a
    server-side session and set the HttpOnly cookie."""
    ident = _authenticate(request, session, f"Bearer {req.key}")

    token = new_session_token()
    csrf = new_csrf_token()
    now = dt.datetime.now(dt.timezone.utc)
    ttl = SESSION_TTL_REMEMBER if req.remember else SESSION_TTL_DEFAULT
    session.add(models.Session(
        id=new_session_id(), token_hash=hash_key(token), principal_id=ident.principal_id,
        created_at=now, expires_at=now + ttl, last_seen_at=now,
        csrf_token=csrf, remember=req.remember,
    ))
    response.set_cookie(
        SESSION_COOKIE, token, httponly=True, samesite="strict", path="/",
        secure=_cookie_secure(request),
        # Persistent cookie only for "remember me"; otherwise a session cookie that
        # dies with the browser (absolute server-side expiry still applies).
        max_age=int(ttl.total_seconds()) if req.remember else None,
    )
    return _whoami(ident, csrf)


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
