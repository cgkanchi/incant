"""Startup git↔DB draft reconciliation sweep (Item 3)."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from incant import db, models
from incant.config import Settings, set_settings
from incant.db import session_factory, session_scope
from incant.registry import MainReconcileResult, reconcile_drafts, reconcile_main_commits
from incant.server import metrics as _metrics
from incant.service import AppContext, reset_app

from .conftest import db_url_for, reset_schema


@pytest.fixture()
def app(tmp_path):
    set_settings(Settings(
        database_url=db_url_for(tmp_path),
        repo_path=str(tmp_path / "repo"),
    ))
    db.reset_engine()
    reset_app()
    reset_schema()
    ctx = AppContext()
    ctx.initialize()
    with session_scope() as s:
        reg = ctx.registry(s, "sam")
        reg.create_prompt("support/system")
    yield ctx


def _open_draft(ctx) -> str:
    with session_scope() as s:
        d = ctx.registry(s, "sam").create_draft(
            "support/system", version_number=1, author="sam", content="hi {{ x }}")
        return d.id


def test_sweep_deletes_orphan_git_ref(app):
    # A draft ref in git whose DB row is gone → orphan; the sweep deletes the ref.
    draft_id = _open_draft(app)
    assert app.git.draft_ref_exists(draft_id)
    with session_scope() as s:
        s.delete(app.registry(s, "sam").get_draft(draft_id))  # strand the git ref

    with session_scope() as s:
        result = reconcile_drafts(s, app.git)
    assert result.orphan_refs_deleted == 1
    assert not app.git.draft_ref_exists(draft_id)


def test_sweep_discards_refless_open_draft(app):
    # An open DB draft whose git ref vanished → the sweep marks it discarded.
    draft_id = _open_draft(app)
    app.git.delete_draft(draft_id)  # strand the DB row
    assert not app.git.draft_ref_exists(draft_id)

    with session_scope() as s:
        result = reconcile_drafts(s, app.git)
    assert result.drafts_discarded == 1
    with session_scope() as s:
        assert app.registry(s, "sam").get_draft(draft_id).status == "discarded"


def test_sweep_leaves_healthy_drafts_untouched(app):
    draft_id = _open_draft(app)
    with session_scope() as s:
        result = reconcile_drafts(s, app.git)
    assert result.orphan_refs_deleted == 0 and result.drafts_discarded == 0
    assert app.git.draft_ref_exists(draft_id)
    with session_scope() as s:
        assert app.registry(s, "sam").get_draft(draft_id).status == "open"


# ── main-commit orphan detection (detect-and-log, never auto-repair) ─────────

def test_main_reconcile_detects_git_orphan(app, caplog):
    # A commit landed on main with no DB Version row (DB txn failed after the git write).
    app.git.commit_version("ghost/thing", 1, "orphan content",
                           author_name="ghost", author_email="ghost@x", message="orphan")
    with caplog.at_level("WARNING", logger="incant.reconcile"):
        with session_scope() as s:
            result = reconcile_main_commits(s, app.git)
    assert result.git_orphans == 1 and result.missing_files == 0
    assert "ORPHAN commit" in caplog.text and "ghost/thing" in caplog.text
    # Detect-only: the orphan is NOT auto-registered as a Version row.
    with session_scope() as s:
        rows = s.execute(
            select(models.Version).where(models.Version.prompt_id == "ghost/thing")
        ).scalars().all()
        assert rows == []


def test_main_reconcile_detects_db_version_missing_file(app, caplog):
    # A DB Version row whose file never made it onto main.
    with session_scope() as s:
        s.add(models.Version(prompt_id="support/system", number=5))
    with caplog.at_level("WARNING", logger="incant.reconcile"):
        with session_scope() as s:
            result = reconcile_main_commits(s, app.git)
    assert result.missing_files == 1 and result.git_orphans == 0
    assert "NO file on refs/heads/main" in caplog.text and "support/system v5" in caplog.text


def test_main_reconcile_clean_when_aligned(app):
    # A properly-authored version (git file + DB Version row) is neither orphan nor missing.
    with session_scope() as s:
        reg = app.registry(s, "sam")
        d = reg.create_draft("support/system", version_number=1, author="sam", content="hi")
        reg.commit_draft(d.id, author="sam")
    with session_scope() as s:
        result = reconcile_main_commits(s, app.git)
    assert result.git_orphans == 0 and result.missing_files == 0
    assert result.scanned_files >= 1 and result.scanned_versions >= 1


# ── unvalidated-tip detection (a rolled-back commit_draft outer transaction) ──

def test_main_reconcile_detects_unvalidated_tip(app, caplog):
    # Publish v1 normally so the Version row + a validated tip both exist on main.
    with session_scope() as s:
        reg = app.registry(s, "sam")
        d = reg.create_draft("support/system", version_number=1, author="sam",
                             content="hi {{ x }}")
        reg.commit_draft(d.id, author="sam")
    # Now land a NEW commit on main for that SAME existing version straight through the
    # GitStore — exactly what commit_draft leaves behind when its outer DB transaction
    # rolls back after commit_version advanced main: the tip moves, but no CommitValidation
    # row is ever written for it (and no DB row rolls forward).
    app.git.commit_version("support/system", 1, "hi {{ x }} (unvalidated edit)",
                           author_name="mallory", author_email="mallory@x",
                           message="content-plane commit whose control-plane txn failed")
    with caplog.at_level("WARNING", logger="incant.reconcile"):
        with session_scope() as s:
            result = reconcile_main_commits(s, app.git)
    assert result.unvalidated_tips == 1
    # The Version row survived from the earlier publish, so this is NOT an orphan — the
    # orphan/missing checks alone would have silently missed the divergence.
    assert result.git_orphans == 0 and result.missing_files == 0
    assert "UNVALIDATED tip" in caplog.text and "support/system v1" in caplog.text
    assert "unvalidated tip commit(s)" in result.summary()
    # Detect-only: no CommitValidation row is fabricated for the row-less tip.
    tip_sha = app.git.head()
    with session_scope() as s:
        rows = s.execute(
            select(models.CommitValidation).where(models.CommitValidation.sha == tip_sha)
        ).scalars().all()
        assert rows == []


def test_main_reconcile_validated_tip_is_clean(app):
    # A normally-published version's tip commit HAS a CommitValidation row → no drift.
    with session_scope() as s:
        reg = app.registry(s, "sam")
        d = reg.create_draft("support/system", version_number=1, author="sam", content="hi")
        reg.commit_draft(d.id, author="sam")
    with session_scope() as s:
        result = reconcile_main_commits(s, app.git)
    assert result.unvalidated_tips == 0
    assert result.git_orphans == 0 and result.missing_files == 0
    assert result.scanned_files >= 1


# ── deferred draft-ref deletion: a failed publish must not strand user work ───

def test_commit_ref_survives_failed_outer_commit(app):
    # Publish v1 cleanly so a Version row + a validated tip both already exist on main.
    with session_scope() as s:
        reg = app.registry(s, "sam")
        d0 = reg.create_draft("support/system", version_number=1, author="sam",
                              content="hi {{ x }}")
        reg.commit_draft(d0.id, author="sam")
    # Happy path (Issue 2a): a SUCCESSFUL publish fired the deferred after_commit hook, so
    # the ref is gone and the draft is committed — normal publish is still green.
    assert not app.git.draft_ref_exists(d0.id)
    with session_scope() as s:
        assert app.registry(s, "sam").get_draft(d0.id).status == "committed"

    # Open a NEW draft editing that SAME live version.
    with session_scope() as s:
        draft_id = app.registry(s, "sam").create_draft(
            "support/system", version_number=1, author="sam",
            content="hi {{ x }} (edit)").id

    # commit_draft inside a session we then ROLL BACK — the outer DB transaction "fails"
    # after commit_version already advanced main. The deferred ref-delete must NOT fire and
    # the staged rows (CommitValidation + status→committed) must roll back with it.
    s = session_factory()()
    try:
        app.registry(s, "sam").commit_draft(draft_id, author="sam", force=True)
        s.flush()
        s.rollback()
    finally:
        s.close()

    # User work is fully recoverable: the draft ref survives AND the row is still open
    # (editable), exactly the property the old mid-transaction delete destroyed.
    assert app.git.draft_ref_exists(draft_id)
    with session_scope() as s:
        assert app.registry(s, "sam").get_draft(draft_id).status == "open"

    # The only residue is exactly one UNVALIDATED main tip — the Version row survived the
    # earlier publish, so it is NOT an orphan — and it is DETECTED, not silently swallowed.
    with session_scope() as s:
        result = reconcile_main_commits(s, app.git)
    assert result.unvalidated_tips == 1
    assert result.git_orphans == 0 and result.missing_files == 0

    # Re-commit cleanly (fresh transaction, force=True to bypass the intervening-tip check).
    # The ref must persist WHILE the transaction is open (delete is deferred) and vanish
    # ONLY once the commit fires the after_commit hook.
    with session_scope() as s:
        app.registry(s, "sam").commit_draft(draft_id, author="sam", force=True)
        assert app.git.draft_ref_exists(draft_id)   # inside the txn → not yet dropped
    assert not app.git.draft_ref_exists(draft_id)    # committed → after_commit fired
    with session_scope() as s:
        assert app.registry(s, "sam").get_draft(draft_id).status == "committed"


# ── reconcile-result exposure seam (ctx holder + metrics gauges) ──────────────

def test_record_reconcile_exposes_result_and_metrics(app):
    # Issue 2b: record_reconcile stashes the latest result on the ctx (read by /healthz)
    # AND publishes it to the incant_reconcile_* gauges — the unit-testable seam.
    result = MainReconcileResult(
        git_orphans=2, missing_files=1, unvalidated_tips=3,
        scanned_files=9, scanned_versions=7,
    )
    app.record_reconcile(result)
    assert app.last_reconcile is result
    assert _metrics.reconcile_git_orphans._value.get() == 2
    assert _metrics.reconcile_unvalidated_tips._value.get() == 3
    assert _metrics.reconcile_missing_files._value.get() == 1
