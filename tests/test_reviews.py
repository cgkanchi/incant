"""Review uniqueness under concurrency (draft_id, reviewer principal ID).

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
        s.add(models.Review(draft_id=draft_id, reviewer="bob",
                            reviewer_principal_id="legacy:bob", state="approved"))
    with pytest.raises(IntegrityError):
        with session_scope() as s:
            s.add(models.Review(draft_id=draft_id, reviewer="bob",
                                reviewer_principal_id="legacy:bob",
                                state="changes_requested"))


def test_add_review_retries_race_as_update(app):
    # Simulate the race: a concurrent submit committed bob's verdict first; our
    # add_review's initial lookup misses it (as if not yet visible), takes the insert
    # path, the unique constraint fires, and the retry re-reads + updates the winner.
    draft_id = _open_draft(app)
    with session_scope() as s:
        s.add(models.Review(draft_id=draft_id, reviewer="bob",
                            reviewer_principal_id="legacy:bob",
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


# ── git/DB draft agreement: the commit gate (Finding F1) ─────────────────────
#
# commit_draft reads the BYTES from git (the draft ref's HEAD) but checks the
# review policy against the DB (approvals bound to Draft.draft_sha). Draft writes
# advance the git ref before their outer DB transaction commits, so a failed
# outer commit can leave git ahead of the DB — and approvals cast against the
# recorded revision must not authorize publishing text nobody reviewed.

def test_commit_refuses_when_git_draft_ref_diverges_from_db(app):
    from incant.registry.service import DraftDiverged

    draft_id = _open_draft(app)
    with session_scope() as s:
        # One-approval policy; bob approves the CURRENT (recorded) revision.
        s.get(models.Project, "support").review_policy = 1
    with session_scope() as s:
        app.registry(s, "bob").add_review(draft_id, reviewer="bob", state="approved")
        approved_sha = app.registry(s, "bob").get_draft(draft_id).draft_sha

    # Simulate the failed-outer-commit residue: the git ref advances (the staged
    # write landed) while the DB row still records the approved revision.
    app.git.write_draft(draft_id, "support/system", 1, "unreviewed {{ y }}",
                        author_name="sam")
    assert app.git.head(app.git.draft_ref(draft_id)) != approved_sha

    with session_scope() as s:
        with pytest.raises(DraftDiverged, match="diverged"):
            app.registry(s, "sam").commit_draft(draft_id, author="sam")

    # Nothing landed: the draft is not committed and no commit was validated —
    # the unreviewed bytes never became publishable.
    with session_scope() as s:
        assert app.registry(s, "sam").get_draft(draft_id).status != "committed"
        assert s.execute(select(models.CommitValidation)).scalars().all() == []


def test_commit_recovers_after_resync_and_matching_draft_commits(app):
    from incant.registry.service import DraftDiverged

    draft_id = _open_draft(app)
    app.git.write_draft(draft_id, "support/system", 1, "diverged {{ y }}",
                        author_name="sam")
    with session_scope() as s:
        with pytest.raises(DraftDiverged):
            app.registry(s, "sam").commit_draft(draft_id, author="sam")

    # The prescribed recovery — re-save through the service — re-syncs git and DB
    # (put_draft_content records the new revision), after which a matching draft
    # commits normally.
    with session_scope() as s:
        reg = app.registry(s, "sam")
        reg.put_draft_content(draft_id, "resynced {{ x }}", author="sam")
        out = reg.commit_draft(draft_id, author="sam")
        assert out.validation["status"] == "valid"
    with session_scope() as s:
        assert app.registry(s, "sam").get_draft(draft_id).status == "committed"
