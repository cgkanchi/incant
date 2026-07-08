"""Shared test helpers: a dict-backed ContentProvider and snapshot builders."""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from incant.core import ContentBlob, EnvSnapshot, VersionInfo

# DB-touching tests run against whatever INCANT_TEST_DATABASE_URL points at (a real
# Postgres in CI/Docker), falling back to a throwaway SQLite file only for quick
# local unit runs. Serving always uses Postgres.
#
# Tests DROP + recreate all tables, so they must never touch the app's database.
# For Postgres we always redirect to a dedicated '<db>_test' database on the same
# server (creating it on demand) — even if the env var points at the app DB — so a
# test run can never wipe live/demo data.
TEST_DATABASE_URL = os.environ.get("INCANT_TEST_DATABASE_URL")


def _is_pg(url: str) -> bool:
    return url.startswith("postgres")


def _test_db_url(raw: str) -> str:
    """Map a Postgres URL onto its dedicated '<db>_test' sibling database."""
    u = make_url(raw)
    if u.database and not u.database.endswith("_test"):
        u = u.set(database=u.database + "_test")
    return u.render_as_string(hide_password=False)


# The URL every DB-touching test actually uses.
EFFECTIVE_TEST_URL = _test_db_url(TEST_DATABASE_URL) if TEST_DATABASE_URL else None


def db_url_for(tmp_path) -> str:
    return EFFECTIVE_TEST_URL or f"sqlite:///{tmp_path/'incant.db'}"


def _ensure_pg_database(url: str) -> None:
    """CREATE DATABASE <name> if it doesn't exist, via the maintenance 'postgres' db."""
    u = make_url(url)
    admin = u.set(database="postgres")
    eng = create_engine(admin, isolation_level="AUTOCOMMIT", future=True)
    try:
        with eng.connect() as c:
            exists = c.execute(
                text("SELECT 1 FROM pg_database WHERE datname = :n"), {"n": u.database}
            ).scalar()
            if not exists:
                c.execute(text(f'CREATE DATABASE "{u.database}"'))
    finally:
        eng.dispose()


@pytest.fixture(scope="session", autouse=True)
def _prepare_test_database():
    """Once per session: make sure the isolated Postgres test DB exists."""
    if EFFECTIVE_TEST_URL and _is_pg(EFFECTIVE_TEST_URL):
        _ensure_pg_database(EFFECTIVE_TEST_URL)
    yield


def reset_schema() -> None:
    """Drop + recreate all tables so a shared Postgres is isolated per test."""
    from incant import models  # noqa: F401 — register tables
    from incant.db import Base, engine

    url = str(engine().url)
    # Safety rail: never drop_all against a non-test Postgres database.
    if _is_pg(url) and not make_url(url).database.endswith("_test"):
        raise RuntimeError(
            f"Refusing to reset schema on Postgres database {make_url(url).database!r}: "
            "it is not a '_test' database. Point INCANT_TEST_DATABASE_URL at a Postgres "
            "server and tests will use the isolated '<db>_test' sibling automatically."
        )

    Base.metadata.drop_all(engine())
    Base.metadata.create_all(engine())


def blob_sha(source: str) -> str:
    return "b" + hashlib.sha256(source.encode()).hexdigest()[:12]


@dataclass
class DictContent:
    """Maps (prompt_id, commit_sha) -> source. Commit SHAs are arbitrary labels."""

    sources: dict[tuple[str, str], str]

    def get(self, prompt_id: str, version: int, commit_sha: str) -> ContentBlob:
        source = self.sources[(prompt_id, commit_sha)]
        return ContentBlob(blob_sha=blob_sha(source), source=source)


def vinfo(version, live=None, tip=None, label=None, previous=(), status="active"):
    return VersionInfo(
        version=version, live_sha=live, tip_sha=tip, label=label,
        status=status, previous_live=tuple(previous),
    )


def snapshot(environment="prod", rules_version=1, **kw) -> EnvSnapshot:
    return EnvSnapshot(environment=environment, rules_version=rules_version, **kw)
