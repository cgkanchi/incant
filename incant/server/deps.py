"""FastAPI dependencies: request-scoped DB session and authenticated identity."""

from __future__ import annotations

from typing import Iterator

from fastapi import Depends, Header, HTTPException
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


def identity(
    authorization: str | None = Header(default=None),
    session: Session = Depends(get_session),
) -> Identity:
    try:
        return get_app().authenticate(session, authorization)
    except AuthError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.detail)


def serving_identity(
    authorization: str | None = Header(default=None),
    session: Session = Depends(get_readonly_session),
) -> Identity:
    """Identity for the serving hot path — auth from the in-memory cache over a
    read-only session (FastAPI shares this session with the route)."""
    try:
        return get_app().authenticate(session, authorization)
    except AuthError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.detail)
