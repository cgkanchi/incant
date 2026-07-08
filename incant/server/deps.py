"""FastAPI dependencies: request-scoped DB session and authenticated identity."""

from __future__ import annotations

from typing import Iterator

from fastapi import Depends, Header, HTTPException
from sqlalchemy.orm import Session

from ..db import session_factory
from ..service import AppContext, get_app
from .auth import AuthError, Identity, authenticate


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


def app_context() -> AppContext:
    return get_app()


def identity(
    authorization: str | None = Header(default=None),
    session: Session = Depends(get_session),
) -> Identity:
    try:
        return authenticate(session, authorization)
    except AuthError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.detail)
