"""Single full-writer enforcement: one `full` node per database, claimed via a
session-level advisory lock at boot."""

from __future__ import annotations

import pytest
from sqlalchemy import func, select

from incant import db
from incant.config import Settings, set_settings

from .conftest import db_url_for


@pytest.fixture()
def pg(tmp_path):
    set_settings(Settings(database_url=db_url_for(tmp_path), repo_path=str(tmp_path / "repo")))
    db.reset_engine()
    yield
    db.release_full_writer_role()
    db.reset_engine()


def test_second_full_writer_is_refused(pg):
    db.claim_full_writer_role()
    # A competing node (simulated on its own connection) cannot take the lock…
    with db.engine().connect() as other:
        got = other.execute(
            select(func.pg_try_advisory_lock(db._FULL_WRITER_LOCK_ID))).scalar()
        assert got is False
    # …until the owner releases it.
    db.release_full_writer_role()
    with db.engine().connect() as other:
        got = other.execute(
            select(func.pg_try_advisory_lock(db._FULL_WRITER_LOCK_ID))).scalar()
        assert got is True
        other.execute(select(func.pg_advisory_unlock(db._FULL_WRITER_LOCK_ID)))


def test_claim_is_idempotent_within_the_process(pg):
    db.claim_full_writer_role()
    db.claim_full_writer_role()   # the same process already owns the role — no error
    db.release_full_writer_role()
