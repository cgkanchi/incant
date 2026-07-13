"""FastAPI dependencies: request-scoped DB session and authenticated identity."""

from __future__ import annotations

import hmac
from typing import Iterator

from fastapi import Depends, Header, HTTPException, Request
from sqlalchemy.orm import Session

from ..db import session_factory
from ..service import AppContext, get_app
from .auth import (
    CSRF_HEADER,
    SESSION_COOKIE,
    AuthError,
    Identity,
    identity_for_principal,
    lookup_session,
    touch_last_seen,
)


def get_session() -> Iterator[Session]:
    s = session_factory()()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


def get_readonly_session() -> Iterator[Session]:
    """Serving-path session: never commits (read-only replicas must not write,
    §15) and swallows teardown errors so a DB outage can't 500 a served request."""
    s = session_factory()()
    try:
        yield s
    finally:
        try:
            s.rollback()
        except Exception:
            pass
        try:
            s.close()
        except Exception:
            pass


def app_context() -> AppContext:
    return get_app()


def client_ip(request: Request) -> str:
    """The caller's IP for throttling. X-Forwarded-For is honored (its first hop — the
    closest client the proxy saw) ONLY when the direct peer is a trusted proxy
    (INCANT_TRUSTED_PROXIES). Otherwise the direct peer is used, so a client behind an
    untrusted hop can't spoof its IP by sending its own XFF. Default trusts nothing."""
    peer = request.client.host if request.client else "unknown"
    xff = request.headers.get("x-forwarded-for")
    if xff and peer in get_app().settings.trusted_proxy_set():
        return xff.split(",", 1)[0].strip()
    return peer


def _presented_credential(authorization: str | None) -> bool:
    """True when the request actually carried a candidate token. Requests with no
    (or an empty) bearer credential are unauthenticated UI boots and probes, not
    brute-force guesses — counting them would let a signed-out browser throttle
    its own IP out of the sign-in screen."""
    if not authorization:
        return False
    scheme, _, token = authorization.partition(" ")
    return bool(token.strip()) if scheme else bool(authorization.strip())


def _enforce_csrf(request: Request, csrf_token: str) -> None:
    """Double-submit CSRF guard for cookie-authenticated mutations. Safe methods
    (GET/HEAD/OPTIONS) never require it; anything else must carry an X-Incant-CSRF
    header equal to the session's CSRF token. Bearer (header) auth is CSRF-immune and
    never reaches here."""
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return
    provided = request.headers.get(CSRF_HEADER)
    if not provided or not hmac.compare_digest(provided, csrf_token):
        raise HTTPException(status_code=403, detail="csrf_required")


def _authenticate(
    request: Request, session: Session, authorization: str | None, *, allow_cookie: bool = False,
) -> Identity:
    """Authenticate with per-IP throttling of failed attempts. A throttled IP is
    refused with 429 before auth is even attempted; only 401s from an actual
    presented credential (unknown/revoked/expired key) count as failures — a
    successful auth never does, a missing/empty credential never does, and a 503
    (DB down) is an outage, not a credential failure, so it doesn't count.

    Bearer Authorization takes precedence (unchanged semantics; header auth is
    CSRF-immune). When ``allow_cookie`` is set and no bearer is presented, an
    ``incant_session`` cookie is accepted: the session is resolved to the same
    Identity the bearer path builds, and mutations require the CSRF header. A
    stale/unknown cookie is a plain 401 and does NOT count toward the throttle."""
    app = get_app()
    ip = client_ip(request)
    limit = app.settings.auth_throttle_limit
    window = app.settings.auth_throttle_window
    retry = app.throttle.retry_after(ip, limit, window)
    if retry is not None:
        raise HTTPException(
            status_code=429, detail="too many failed authentication attempts",
            headers={"Retry-After": str(int(retry))},
        )

    # Bearer wins whenever a candidate token is presented (unchanged path).
    if _presented_credential(authorization):
        try:
            return app.authenticate(session, authorization)
        except AuthError as exc:
            if exc.status == 401 and limit > 0:
                app.throttle.record_failure(ip, window)
            raise HTTPException(status_code=exc.status, detail=exc.detail)

    # No bearer credential — a UI request may instead carry a session cookie.
    if allow_cookie:
        token = request.cookies.get(SESSION_COOKIE)
        if token:
            row = lookup_session(session, token)
            if row is None:
                # Stale/unknown cookie — not a brute-force guess, so never throttled.
                raise HTTPException(status_code=401, detail="invalid or expired session")
            ident = identity_for_principal(session, row.principal_id)
            if ident is None:
                raise HTTPException(status_code=401, detail="invalid or expired session")
            _enforce_csrf(request, row.csrf_token)
            touch_last_seen(row)
            return ident

    # Nothing presented — surface the standard 401 (never throttled).
    try:
        return app.authenticate(session, authorization)
    except AuthError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.detail)


def identity(
    request: Request,
    authorization: str | None = Header(default=None),
    session: Session = Depends(get_session),
) -> Identity:
    return _authenticate(request, session, authorization, allow_cookie=True)


def serving_identity(
    request: Request,
    authorization: str | None = Header(default=None),
    session: Session = Depends(get_readonly_session),
) -> Identity:
    """Identity for the serving hot path — auth from the in-memory cache over a
    read-only session (FastAPI shares this session with the route)."""
    return _authenticate(request, session, authorization)
