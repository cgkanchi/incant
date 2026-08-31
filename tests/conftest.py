"""Shared test helpers: a dict-backed ContentProvider and snapshot builders."""

from __future__ import annotations

import hashlib
import os
from contextlib import contextmanager
from dataclasses import dataclass

# Pin git commit dates so seeded test repos are byte-identical across runs.
# Production uses real wall-clock (this env var is never set there).
os.environ.setdefault("INCANT_FIXED_GIT_DATE", "1700000000 +0000")

# The suite exercises the well-known dev admin key (`incant_sk_dev_admin`); opt into
# the escape hatch so ensure_bootstrap_admin accepts it instead of refusing to boot.
os.environ.setdefault("INCANT_ALLOW_DEV_KEY", "1")

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from incant.core import ContentBlob, EnvSnapshot, VersionInfo

# DB-touching tests run against Postgres — always. INCANT_TEST_DATABASE_URL points at
# your server; unset, it defaults to the compose `db` service (`docker compose up -d
# db`). There is deliberately no SQLite fallback: it enforced no FKs, skipped the
# Alembic path, and serialized writes — three ways a green local run could lie.
#
# Tests DROP + recreate all tables, so they must never touch the app's database.
# We always redirect to a dedicated '<db>_test' database on the same server
# (creating it on demand) — even if the env var points at the app DB — so a test
# run can never wipe live/demo data.
TEST_DATABASE_URL = os.environ.get(
    "INCANT_TEST_DATABASE_URL",
    "postgresql+psycopg://incant:incant@localhost:5432/incant",
)


def _is_pg(url: str) -> bool:
    return url.startswith("postgres")


def _test_db_url(raw: str) -> str:
    """Map a Postgres URL onto its dedicated '<db>_test' sibling database."""
    u = make_url(raw)
    if u.database and not u.database.endswith("_test"):
        u = u.set(database=u.database + "_test")
    return u.render_as_string(hide_password=False)


# The URL every DB-touching test actually uses.
EFFECTIVE_TEST_URL = _test_db_url(TEST_DATABASE_URL)


def db_url_for(tmp_path) -> str:  # tmp_path kept for call-site compatibility
    return EFFECTIVE_TEST_URL


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
    """Once per session: make sure the isolated Postgres test DB exists — and fail
    with the actual fix if the server isn't there, instead of 200 cryptic errors."""
    try:
        _ensure_pg_database(EFFECTIVE_TEST_URL)
    except Exception as exc:
        pytest.exit(
            f"\nPostgres is required for the test suite and {make_url(EFFECTIVE_TEST_URL).host}:"
            f"{make_url(EFFECTIVE_TEST_URL).port or 5432} did not answer ({type(exc).__name__}).\n"
            "Start the bundled server:  docker compose up -d db\n"
            "or point INCANT_TEST_DATABASE_URL at a Postgres you manage.",
            returncode=2,
        )
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
    # `alembic_version` is created by Alembic, not Base.metadata, so drop_all leaves it
    # behind. A stale stamp (< head) would make ctx.initialize()'s ensure_schema try to
    # re-run migrations against the tables create_all just built (DuplicateTable). Drop it
    # so ensure_schema instead stamps head over the create_all'd schema, as intended for
    # test DBs (which are built by create_all, never by migrations).
    with engine().begin() as conn:
        conn.exec_driver_sql("DROP TABLE IF EXISTS alembic_version")
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


# ── live-server boot (shared by the SDK and MCP suites) ──────────────────────

@contextmanager
def live_incant_server(tmp_path, db_suffix: str, admin_key: str = "incant_sk_dev_admin"):
    """A real uvicorn subprocess over a dedicated `<db>_<suffix>` database wiped
    and rebuilt through the real Alembic migrations, seeded with the example
    dataset. Yields the base URL. Used by tests that must exercise the true wire
    (incant-sdk, incant-mcp) rather than the in-process TestClient."""
    import os
    import socket
    import subprocess
    import sys
    import time
    import urllib.request

    u = make_url(TEST_DATABASE_URL)
    base = (u.database or "incant").removesuffix("_test")
    db_name = f"{base}_{db_suffix}"
    # Same safety rail as reset_schema(): this helper DROPs the public schema.
    if not db_name.endswith("_test"):
        raise RuntimeError(f"Refusing to wipe {db_name!r}: live-server suites must use a '_test' database")
    db_url = u.set(database=db_name).render_as_string(hide_password=False)
    _ensure_pg_database(db_url)
    eng = create_engine(db_url, isolation_level="AUTOCOMMIT", future=True)
    try:
        with eng.connect() as c:
            c.execute(text("DROP SCHEMA public CASCADE"))
            c.execute(text("CREATE SCHEMA public"))
    finally:
        eng.dispose()

    env = dict(os.environ)
    env.update({
        "INCANT_DATABASE_URL": db_url,
        "INCANT_REPO_PATH": f"{tmp_path}/repo",
        "INCANT_ALLOW_DEV_KEY": "1",
        "INCANT_BOOTSTRAP_ADMIN_KEY": admin_key,
        "INCANT_MODE": "full",
    })
    for step in ("init", "seed"):
        r = subprocess.run([sys.executable, "-m", "incant.cli", step],
                           env=env, capture_output=True, text=True)
        assert r.returncode == 0, f"incant {step}: {r.stdout}\n{r.stderr}"

    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
    base_url = f"http://127.0.0.1:{port}"
    log_path = f"{tmp_path}/server.log"
    with open(log_path, "w") as log:
        proc = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "incant.server:app",
             "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning"],
            env=env, stdout=log, stderr=subprocess.STDOUT)
    try:
        deadline = time.time() + 40
        while time.time() < deadline:
            if proc.poll() is not None:
                raise RuntimeError(f"server died:\n{open(log_path).read()[-3000:]}")
            try:
                with urllib.request.urlopen(base_url + "/readyz", timeout=1) as resp:
                    if resp.status == 200:
                        break
            except Exception:
                time.sleep(0.2)
        else:
            raise RuntimeError(f"not ready:\n{open(log_path).read()[-3000:]}")
        yield base_url
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            proc.kill()
