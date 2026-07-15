"""Review uniqueness under concurrency (uq_review on draft_id, reviewer).

A concurrent double-submit must not create duplicate rows (which would later make every
scalar_one_or_none read raise MultipleResultsFound). add_review retries the lost insert
race as an update.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from incant import db, models
from incant.config import Settings, set_settings
from incant.db import session_scope
from incant.service import AppContext, reset_app

from .conftest import db_url_for, reset_schema


@pytest.fixture()
def app(tmp_path):
    set_settings(Settings(database_url=db_url_for(tmp_path), repo_path=str(tmp_path / "repo")))
    db.reset_engine()
    reset_app()
    reset_schema()
    ctx = AppContext()
    ctx.initialize()
    with session_scope() as s:
        ctx.registry(s, "sam").create_prompt("support/system")
    yield ctx


def _open_draft(ctx) -> str:
    with session_scope() as s:
        d = ctx.registry(s, "sam").create_draft(
            "support/system", version_number=1, author="sam", content="hi {{ x }}")
        return d.id


def test_duplicate_review_insert_hits_unique_constraint(app):
    # The constraint itself: two raw rows for the same (draft, reviewer) can't coexist.
    draft_id = _open_draft(app)
    with session_scope() as s:
        s.add(models.Review(draft_id=draft_id, reviewer="bob", state="approved"))
    with pytest.raises(IntegrityError):
        with session_scope() as s:
            s.add(models.Review(draft_id=draft_id, reviewer="bob", state="changes_requested"))


def test_add_review_retries_race_as_update(app):
    # Simulate the race: a concurrent submit committed bob's verdict first; our
    # add_review's initial lookup misses it (as if not yet visible), takes the insert
    # path, the unique constraint fires, and the retry re-reads + updates the winner.
    draft_id = _open_draft(app)
    with session_scope() as s:
        s.add(models.Review(draft_id=draft_id, reviewer="bob",
                            state="changes_requested", reviewed_sha="stale"))

    with session_scope() as s:
        reg = app.registry(s, "sam")
        calls = {"n": 0}
        real = reg._find_review

        def flaky(did, rev):
            calls["n"] += 1
            return None if calls["n"] == 1 else real(did, rev)

        reg._find_review = flaky
        r = reg.add_review(draft_id, reviewer="bob", state="approved")
        assert r.state == "approved"        # landed as an update, not a duplicate
        assert calls["n"] == 2              # missed once, re-read on the retry

    # Exactly one row remains for (draft, bob), now approved and re-stamped current.
    with session_scope() as s:
        rows = s.execute(
            select(models.Review).where(models.Review.draft_id == draft_id,
                                        models.Review.reviewer == "bob")
        ).scalars().all()
        assert len(rows) == 1
        assert rows[0].state == "approved"


def test_add_review_normal_upsert_is_single_row(app):
    # No race: repeated verdicts by one reviewer keep updating the single row.
    draft_id = _open_draft(app)
    with session_scope() as s:
        reg = app.registry(s, "sam")
        reg.add_review(draft_id, reviewer="bob", state="changes_requested")
        reg.add_review(draft_id, reviewer="bob", state="approved")
    with session_scope() as s:
        rows = s.execute(
            select(models.Review).where(models.Review.draft_id == draft_id)
        ).scalars().all()
        assert len(rows) == 1 and rows[0].state == "approved"
