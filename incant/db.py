"""Database engine, session, and Base. Postgres in prod, SQLite for dev/single-node."""

from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import get_settings


class Base(DeclarativeBase):
    pass


_engine = None
_SessionLocal: sessionmaker | None = None


def _make_engine():
    url = get_settings().database_url
    kwargs: dict = {"future": True, "pool_pre_ping": True}
    if url.startswith("sqlite"):
        # SQLite is supported only for isolated single-process unit tests, never
        # for serving — its serialized writer masks the concurrency this app is
        # built for. FastAPI runs sync endpoints in a threadpool, so allow the
        # connection to cross threads.
        kwargs["connect_args"] = {"check_same_thread": False}
    else:
        # Real pool for the multi-user control plane. Sized for a threadpool plus
        # headroom; pre-ping survives Postgres restarts.
        kwargs.update(pool_size=10, max_overflow=20, pool_recycle=1800)
    return create_engine(url, **kwargs)


def engine():
    global _engine
    if _engine is None:
        _engine = _make_engine()
    return _engine


def session_factory() -> sessionmaker:
    global _SessionLocal
    if _SessionLocal is None:
        _SessionLocal = sessionmaker(bind=engine(), expire_on_commit=False, future=True)
    return _SessionLocal


@contextmanager
def session_scope() -> Iterator[Session]:
    s = session_factory()()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


def get_session() -> Iterator[Session]:
    """FastAPI dependency."""
    with session_scope() as s:
        yield s


def init_db() -> None:
    from . import models  # noqa: F401 — ensure models are registered

    Base.metadata.create_all(engine())


def reset_engine() -> None:
    """Drop cached engine/session (tests that switch databases)."""
    global _engine, _SessionLocal
    _engine = None
    _SessionLocal = None
