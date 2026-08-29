"""Database engine, session, and Base. The control plane is Postgres — everywhere.

There is deliberately no SQLite path: it enforced no foreign keys by default, took a
create_all shortcut past the Alembic migrations, and its serialized writer masked the
concurrency this app is built for — three ways for a local green run to lie about
production. Dev and tests run against the compose ``db`` service instead.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Hashable, Iterator

from sqlalchemy import create_engine, event, inspect
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import get_settings

log = logging.getLogger("incant.db")

_AFTER_COMMIT_CALLBACKS = "incant_after_commit_callbacks"


def after_commit(
    session: Session, key: Hashable, callback: Callable[[], None],
) -> None:
    """Run a deduplicated process-side effect only after this transaction commits."""
    callbacks = session.info.setdefault(_AFTER_COMMIT_CALLBACKS, {})
    callbacks[key] = callback


@event.listens_for(Session, "after_commit")
def _run_after_commit_callbacks(session: Session) -> None:
    callbacks = session.info.pop(_AFTER_COMMIT_CALLBACKS, {})
    for callback in callbacks.values():
        try:
            callback()
        except Exception:
            # The database is already committed. A cache callback must never turn a
            # successful mutation into a misleading 500 response.
            log.exception("post-commit callback failed")


@event.listens_for(Session, "after_rollback")
def _discard_after_commit_callbacks(session: Session) -> None:
    session.info.pop(_AFTER_COMMIT_CALLBACKS, None)

# Repo root holds alembic.ini + alembic/ (a sibling of the incant package). In the
# Docker image the whole tree is copied to /app, so this resolves there too.
_REPO_ROOT = Path(__file__).resolve().parent.parent


class Base(DeclarativeBase):
    pass


_engine = None
_SessionLocal: sessionmaker | None = None


def _make_engine():
    # Real pool for the multi-user control plane. Sized for a threadpool plus
    # headroom; pre-ping survives Postgres restarts.
    return create_engine(get_settings().database_url, future=True, pool_pre_ping=True,
                         pool_size=10, max_overflow=20, pool_recycle=1800)


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


def alembic_stamp(revision: str = "head") -> None:
    """Write ``revision`` into ``alembic_version`` without running any DDL.

    Used to *adopt* an already-existing schema: we assert "the database is at this
    revision" so a subsequent ``upgrade head`` runs only the migrations that come
    after it. Defaults to ``head`` for the fresh-adoption case where the live schema
    already matches the current models.
    """
    from alembic import command

    command.stamp(_alembic_config(), revision)


# Ordered oldest → newest. Each entry is the revision that INTRODUCED the schema
# object we probe for, so `_adoption_revision` can walk forward and stop at the last
# revision whose change is already materialised in the live database. Keep this in
# lockstep with alembic/versions/ when a migration adds a detectable object.
_ADOPTION_BASELINE = "da3e34b2b8fe"  # baseline schema; the floor for any populated DB


def _has_unique_columns(inspector, table: str, columns: list[str]) -> bool:
    """True if ``table`` enforces uniqueness over exactly ``columns``.

    Postgres can express "these columns are unique" as either a UNIQUE *constraint*
    (what our migrations create via ``create_unique_constraint``) or a UNIQUE *index*
    (what ``create_all`` may emit, and what a hand-built legacy schema might carry),
    and SQLAlchemy surfaces the two through different reflection calls. We accept
    either so adoption detection doesn't hinge on how the uniqueness was authored.
    Comparison is by column set, not object name, so a differently-named legacy
    object still counts.
    """
    wanted = set(columns)
    for uc in inspector.get_unique_constraints(table):
        if set(uc.get("column_names") or []) == wanted:
            return True
    for ix in inspector.get_indexes(table):
        if ix.get("unique") and set(ix.get("column_names") or []) == wanted:
            return True
    return False


def _adoption_revision(inspector) -> str:
    """Newest migration whose schema change is ALREADY present in the live database.

    This is the crux of adopting a non-Alembic Postgres schema *correctly*. Blindly
    stamping ``head`` is right only when the live schema already matches the current
    models (e.g. a ``create_all`` dev/test DB); a genuinely older install — say a
    pre-Alembic release, or one create_all'd before later migrations existed — would
    then be marked "at head" and permanently skip the migrations it actually still
    needs. So we probe the schema for the marker each post-baseline migration adds and
    return the last one we find; the caller stamps that revision and then runs
    ``upgrade head``, which applies exactly the migrations that are genuinely missing.

    Markers, in order (see alembic/versions/):
      * 67fb7465ee07 — the ``sessions`` table;
      * a3f1c8e29b41 — uniqueness on ``api_keys(prefix)`` (``uq_apikey_prefix``, which
        replaced the old non-unique ``ix_api_keys_prefix`` index);
      * b7d2e6f4a1c9 — uniqueness on ``reviews(draft_id, reviewer)`` (``uq_review``);
      * c4e8a17d5b23 — the ``rule_revisions.state`` column;
      * d7f3b92e6a41 — the ``users`` table.
      * e9a1c4f27b63 — immutable principal-id columns on drafts/reviews/comments.

    We never return anything older than the baseline: a populated schema is assumed to
    contain at least ``da3e34b2b8fe``'s tables (that is what "has tables but no
    alembic_version" means here).
    """
    tables = set(inspector.get_table_names())
    if "sessions" not in tables:
        return _ADOPTION_BASELINE  # 67fb7465ee07's table is absent → adopt at baseline
    if not _has_unique_columns(inspector, "api_keys", ["prefix"]):
        return "67fb7465ee07"
    # The newest migration replaces the old name-based review constraint. Its
    # principal-id column is therefore both a head marker and the escape hatch that
    # lets current create_all schemas skip the now-obsolete constraint probe below.
    if "reviewer_principal_id" in {
        c["name"] for c in inspector.get_columns("reviews")
    }:
        return "head"
    if not _has_unique_columns(inspector, "reviews", ["draft_id", "reviewer"]):
        return "a3f1c8e29b41"
    if "state" not in {c["name"] for c in inspector.get_columns("rule_revisions")}:
        return "b7d2e6f4a1c9"
    if "users" not in tables:
        return "c4e8a17d5b23"
    return "d7f3b92e6a41"


def ensure_schema() -> None:
    """Bring the database schema up to date for boot / `incant init`.

    The schema is driven through Alembic migrations everywhere, so every
    environment gets versioned, reviewable DDL:

      * fresh DB (no tables)                      → ``alembic upgrade head`` builds it;
      * already under Alembic (``alembic_version``) → ``alembic upgrade head`` applies
        any new migrations (idempotent when already at head);
      * tables present but no ``alembic_version`` (a test DB built by ``create_all``,
        or a pre-Alembic install) → ADOPT the existing schema. We must not assume such
        a schema is current: ``_adoption_revision`` inspects it to find the newest
        revision whose changes are already present, ``alembic stamp`` records exactly
        that, and ``alembic upgrade head`` then runs only the migrations still missing.
        A current schema resolves to ``head`` (stamp head, upgrade no-ops); an older
        one gets stamped lower and migrated the rest of the way — the case a blanket
        ``stamp head`` used to silently skip.
    """
    from . import models  # noqa: F401 — ensure models are registered

    inspector = inspect(engine())
    tables = set(inspector.get_table_names())
    if "alembic_version" in tables:
        alembic_upgrade_head()
    elif tables:
        # Schema exists but is not Alembic-managed — adopt it at the correct revision,
        # then migrate forward over whatever it was actually missing.
        alembic_stamp(_adoption_revision(inspector))
        alembic_upgrade_head()
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
