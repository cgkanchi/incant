"""Database engine, session, and Base. Postgres in prod, SQLite for dev/single-node."""

from __future__ import annotations

import logging
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import get_settings

log = logging.getLogger("incant.db")

# Repo root holds alembic.ini + alembic/ (a sibling of the incant package). In the
# Docker image the whole tree is copied to /app, so this resolves there too.
_REPO_ROOT = Path(__file__).resolve().parent.parent


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


def _alembic_config():
    """A programmatic Alembic config pointed at the current database + repo scripts."""
    from alembic.config import Config

    cfg = Config(str(_REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(_REPO_ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", get_settings().database_url)
    return cfg


def alembic_upgrade_head() -> None:
    from alembic import command

    command.upgrade(_alembic_config(), "head")


def alembic_stamp_head() -> None:
    from alembic import command

    command.stamp(_alembic_config(), "head")


def ensure_schema() -> None:
    """Bring the database schema up to date for boot / `incant init`.

    SQLite (tests/dev): plain ``create_all`` — no Alembic, no migration history to
    carry, and the test suite must stay green without invoking Alembic. Postgres
    (production control plane): drive the schema through Alembic migrations so real
    deployments get versioned, reviewable DDL:

      * fresh DB (no tables)                      → ``alembic upgrade head`` builds it;
      * already under Alembic (``alembic_version``) → ``alembic upgrade head`` applies
        any new migrations (idempotent when already at head);
      * tables present but no ``alembic_version`` (e.g. a test DB built by
        ``create_all``, or a pre-Alembic install) → ``alembic stamp head`` adopts the
        existing schema without recreating it.
    """
    from . import models  # noqa: F401 — ensure models are registered

    url = get_settings().database_url
    if url.startswith("sqlite"):
        Base.metadata.create_all(engine())
        return

    tables = set(inspect(engine()).get_table_names())
    if "alembic_version" in tables:
        alembic_upgrade_head()
    elif tables:
        # Schema exists but is not Alembic-managed — adopt it at head.
        alembic_stamp_head()
    else:
        alembic_upgrade_head()


def init_db() -> None:
    """Backwards-compatible schema bootstrap (create_all/Alembic per dialect)."""
    ensure_schema()


def reset_engine() -> None:
    """Drop cached engine/session (tests that switch databases)."""
    global _engine, _SessionLocal
    _engine = None
    _SessionLocal = None
