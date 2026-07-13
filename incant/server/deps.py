"""FastAPI dependencies: request-scoped DB session and authenticated identity."""

from __future__ import annotations

from typing import Iterator

from fastapi import Depends, Header, HTTPException, Request
from sqlalchemy.orm import Session

from ..db import session_factory
from ..service import AppContext, get_app
from .auth import AuthError, Identity


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
    """The caller's IP for throttling. Behind a proxy, trust only the first hop of
    X-Forwarded-For (the closest client the proxy saw) when the header is present."""
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",", 1)[0].strip()
    return request.client.host if request.client else "unknown"


def _presented_credential(authorization: str | None) -> bool:
    """True when the request actually carried a candidate token. Requests with no
    (or an empty) bearer credential are unauthenticated UI boots and probes, not
    brute-force guesses — counting them would let a signed-out browser throttle
    its own IP out of the sign-in screen."""
    if not authorization:
        return False
    scheme, _, token = authorization.partition(" ")
    return bool(token.strip()) if scheme else bool(authorization.strip())


def _authenticate(request: Request, session: Session, authorization: str | None) -> Identity:
    """Authenticate with per-IP throttling of failed attempts. A throttled IP is
    refused with 429 before auth is even attempted; only 401s from an actual
    presented credential (unknown/revoked/expired key) count as failures — a
    successful auth never does, a missing/empty credential never does, and a 503
    (DB down) is an outage, not a credential failure, so it doesn't count."""
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
    try:
        return app.authenticate(session, authorization)
    except AuthError as exc:
        if exc.status == 401 and limit > 0 and _presented_credential(authorization):
            app.throttle.record_failure(ip, window)
        raise HTTPException(status_code=exc.status, detail=exc.detail)


def identity(
    request: Request,
    authorization: str | None = Header(default=None),
    session: Session = Depends(get_session),
) -> Identity:
    return _authenticate(request, session, authorization)


def serving_identity(
    request: Request,
    authorization: str | None = Header(default=None),
    session: Session = Depends(get_readonly_session),
) -> Identity:
    """Identity for the serving hot path — auth from the in-memory cache over a
    read-only session (FastAPI shares this session with the route)."""
    return _authenticate(request, session, authorization)
