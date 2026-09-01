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


# ── staged publishes: a failed outer transaction must leave NO residue on main ─

def test_failed_publish_leaves_main_untouched(app):
    # Publish v1 cleanly so a Version row + a validated tip both already exist on main.
    with session_scope() as s:
        reg = app.registry(s, "sam")
        d0 = reg.create_draft("support/system", version_number=1, author="sam",
                              content="hi {{ x }}")
        reg.commit_draft(d0.id, author="sam")
    # Happy path: a SUCCESSFUL publish promoted the staged commit and dropped both the
    # pending and draft refs.
    assert not app.git.draft_ref_exists(d0.id)
    assert app.git.list_pending_refs() == []
    with session_scope() as s:
        assert app.registry(s, "sam").get_draft(d0.id).status == "committed"
    head_before = app.git.head()

    # Open a NEW draft editing that SAME live version.
    with session_scope() as s:
        draft_id = app.registry(s, "sam").create_draft(
            "support/system", version_number=1, author="sam",
            content="hi {{ x }} (edit)").id

    # commit_draft inside a session we then ROLL BACK — the outer DB transaction
    # "fails". The publish was STAGED on a pending ref, never on main.
    s = session_factory()()
    try:
        app.registry(s, "sam").commit_draft(draft_id, author="sam", force=True)
        assert app.git.head() == head_before        # staged, not promoted
        assert len(app.git.list_pending_refs()) == 1
        s.flush()
        s.rollback()
    finally:
        s.close()

    # User work is fully recoverable: the draft ref survives AND the row is still open.
    assert app.git.draft_ref_exists(draft_id)
    with session_scope() as s:
        assert app.registry(s, "sam").get_draft(draft_id).status == "open"

    # And — the staged-publish upgrade — there is NO residue at all: main never moved,
    # the pending ref was cleaned up on rollback, and the drift sweep finds nothing.
    assert app.git.head() == head_before
    assert app.git.list_pending_refs() == []
    with session_scope() as s:
        result = reconcile_main_commits(s, app.git)
    assert result.unvalidated_tips == 0
    assert result.git_orphans == 0 and result.missing_files == 0

    # Re-commit cleanly. Main advances only when the transaction commits; the draft
    # ref persists while the transaction is open and vanishes with the promotion.
    with session_scope() as s:
        app.registry(s, "sam").commit_draft(draft_id, author="sam", force=True)
        assert app.git.draft_ref_exists(draft_id)   # inside the txn → not yet dropped
        assert app.git.head() == head_before        # inside the txn → not yet promoted
    assert not app.git.draft_ref_exists(draft_id)    # committed → promoted + cleaned
    assert app.git.head() != head_before
    assert app.git.list_pending_refs() == []
    with session_scope() as s:
        assert app.registry(s, "sam").get_draft(draft_id).status == "committed"


# ── pending-promotion recovery (crash between DB commit and promotion) ────────

def _stranded_publish(app, content="hi {{ x }} (stranded)"):
    """Simulate a crash after the DB transaction committed but before promotion:
    publish normally, then reset main to the pre-publish tip and restore the
    pending ref — exactly the state a killed process leaves behind."""
    head_before = app.git.head()
    with session_scope() as s:
        reg = app.registry(s, "sam")
        d = reg.create_draft("support/system", version_number=1, author="sam",
                             content=content, )
        draft_id = d.id
        outcome = reg.commit_draft(draft_id, author="sam", force=True)
    sha = outcome.sha
    app.git._git("update-ref", "refs/heads/main", head_before)     # undo the promotion
    app.git._git("update-ref", app.git.pending_ref(draft_id), sha)  # re-strand the ref
    return draft_id, sha, head_before


def test_recovery_promotes_committed_stranded_publish(app, caplog):
    from incant.registry import recover_pending_promotions

    # Baseline publish so the version exists.
    with session_scope() as s:
        reg = app.registry(s, "sam")
        d0 = reg.create_draft("support/system", version_number=1, author="sam",
                              content="hi {{ x }}")
        reg.commit_draft(d0.id, author="sam")

    draft_id, sha, _ = _stranded_publish(app)
    with caplog.at_level("WARNING", logger="incant.reconcile"):
        with session_scope() as s:
            result = recover_pending_promotions(s, app.git)
    assert result.promoted == 1 and result.discarded == 0 and result.rebased == 0
    assert app.git.head() == sha                     # the owed promotion landed
    assert app.git.list_pending_refs() == []
    assert "promoted staged publish" in caplog.text


def test_recovery_discards_uncommitted_staging_residue(app):
    from incant.registry import recover_pending_promotions

    # Baseline publish, then a stranded pending ref WITHOUT its DB rows — the
    # transaction never committed (crash mid-transaction).
    with session_scope() as s:
        reg = app.registry(s, "sam")
        d0 = reg.create_draft("support/system", version_number=1, author="sam",
                              content="hi {{ x }}")
        reg.commit_draft(d0.id, author="sam")
    head = app.git.head()
    sha, _parent = app.git.commit_version_pending(
        "support/system", 1, "never committed {{ x }}",
        author_name="sam", author_email="sam@x", message="stranded", draft_id="d_crash")
    with session_scope() as s:
        result = recover_pending_promotions(s, app.git)
    assert result.discarded == 1 and result.promoted == 0
    assert app.git.head() == head                    # main untouched
    assert app.git.list_pending_refs() == []


def test_recovery_rebases_when_main_diverged(app):
    from incant.registry import recover_pending_promotions

    with session_scope() as s:
        reg = app.registry(s, "sam")
        d0 = reg.create_draft("support/system", version_number=1, author="sam",
                              content="hi {{ x }}")
        reg.commit_draft(d0.id, author="sam")

    draft_id, sha, _ = _stranded_publish(app, content="hi {{ x }} (stranded edit)")
    # Main moves on independently before recovery runs.
    other = app.git.commit_version("support/system", 1, "hi {{ x }} (newer)",
                                   author_name="sam", author_email="sam@x", message="newer")
    with session_scope() as s:
        s.add(models.CommitValidation(
            sha=other, blob_sha="", path="support/system/v1.j2",
            prompt_id="support/system", version_number=1, status="valid",
            extracted_variables={},
        ))
    with session_scope() as s:
        result = recover_pending_promotions(s, app.git)
    assert result.rebased == 1
    assert app.git.list_pending_refs() == []
    assert not app.git.draft_ref_exists(draft_id)
    # The stranded content was replayed atop the diverged main with a fresh,
    # equally-validated commit.
    assert app.git.read("support/system/v1.j2") == "hi {{ x }} (stranded edit)"
    tip = app.git.head()
    assert tip != sha and app.git.is_ancestor(other)
    with session_scope() as s:
        rows = s.execute(
            select(models.CommitValidation).where(models.CommitValidation.sha == tip)
        ).scalars().all()
        assert len(rows) == 1 and rows[0].status == "valid"
    # The ORIGINAL sha — already returned to the client as full_sha, possibly a
    # pointer's to_sha, still listed as a validated commit — must not become
    # unreachable garbage in a reflog-less bare repo. It is anchored, and its content
    # is still readable by sha (a §9 pin replay / a pointer at it keeps working).
    assert app.git.recovered_sha(draft_id) == sha
    assert app.git.head(app.git.recovered_ref(draft_id)) == sha
    assert app.git.read("support/system/v1.j2", ref=sha) == "hi {{ x }} (stranded edit)"


def test_recovery_replay_commits_row_before_main_moves(app, caplog):
    """The replay must follow the staged protocol: its CommitValidation row lands in
    the caller's transaction and main moves only in after_commit. A failure after the
    row is added but before commit therefore leaves main untouched and the pending
    ref in place (no unvalidated tip, nothing lost), and the next pass completes."""
    from incant.registry import recover_pending_promotions

    with session_scope() as s:
        reg = app.registry(s, "sam")
        d0 = reg.create_draft("support/system", version_number=1, author="sam",
                              content="hi {{ x }}")
        reg.commit_draft(d0.id, author="sam")
    draft_id, sha, _ = _stranded_publish(app, content="hi {{ x }} (stranded edit)")
    other = app.git.commit_version("support/system", 1, "hi {{ x }} (newer)",
                                   author_name="sam", author_email="sam@x", message="newer")
    with session_scope() as s:
        s.add(models.CommitValidation(
            sha=other, blob_sha="", path="support/system/v1.j2",
            prompt_id="support/system", version_number=1, status="valid",
            extracted_variables={},
        ))

    # Pass 1 inside a transaction we then ROLL BACK ("failure after the row was added").
    s = session_factory()()
    try:
        result = recover_pending_promotions(s, app.git)
        assert result.rebased == 1
        assert app.git.head() == other                    # main has NOT moved
        pending = app.git.list_pending_refs()
        assert len(pending) == 1 and pending[0][0] == draft_id
        replay_sha = pending[0][1]
        assert replay_sha != sha                          # the pending ref now holds the replay
        assert app.git.recovered_sha(draft_id) == sha     # the original was anchored FIRST
        s.flush()
        s.rollback()
    finally:
        s.close()

    # After the rollback: main untouched, no row for the replay, no unvalidated tip,
    # pending residue still there for the next pass, original still reachable.
    assert app.git.head() == other
    assert len(app.git.list_pending_refs()) == 1
    with session_scope() as s:
        assert s.execute(select(models.CommitValidation).where(
            models.CommitValidation.sha == replay_sha)).first() is None
        assert reconcile_main_commits(s, app.git).unvalidated_tips == 0
    assert app.git.read("support/system/v1.j2", ref=sha) == "hi {{ x }} (stranded edit)"

    # Pass 2 (a committed transaction) converges: the pending ref holds a row-less
    # replay, but the anchor names the committed original → replayed again and promoted.
    with caplog.at_level("WARNING", logger="incant.reconcile"):
        with session_scope() as s:
            result = recover_pending_promotions(s, app.git)
    assert result.rebased == 1 and result.discarded == 0
    assert app.git.list_pending_refs() == []
    assert app.git.read("support/system/v1.j2") == "hi {{ x }} (stranded edit)"
    tip = app.git.head()
    with session_scope() as s:
        rows = s.execute(select(models.CommitValidation).where(
            models.CommitValidation.sha == tip)).scalars().all()
        assert len(rows) == 1 and rows[0].status == "valid"
        assert reconcile_main_commits(s, app.git).unvalidated_tips == 0
    assert app.git.recovered_sha(draft_id) == sha
    assert "promoted as" in caplog.text


def test_recovery_replays_stranded_chain_in_order(app):
    """One transaction stages publishes as a chain (B's parent is A). If recovery
    walked them by refname, B ('d_aaa') would come first, fail to fast-forward and be
    replayed as a synthetic sha — then A too. In chain order both simply promote."""
    from incant.registry import recover_pending_promotions

    with session_scope() as s:
        reg = app.registry(s, "sam")
        d0 = reg.create_draft("support/system", version_number=1, author="sam",
                              content="hi {{ x }}")
        reg.commit_draft(d0.id, author="sam")
    head = app.git.head()
    # Draft ids chosen so refname order is the REVERSE of chain order.
    sha_a, parent_a = app.git.commit_version_pending(
        "support/system", 1, "A {{ x }}", author_name="sam", author_email="sam@x",
        message="A", draft_id="d_zzz")
    sha_b, parent_b = app.git.commit_version_pending(
        "support/system", 1, "B {{ x }}", author_name="sam", author_email="sam@x",
        message="B", draft_id="d_aaa", parent=sha_a)
    assert parent_a == head and parent_b == sha_a
    assert [d for d, _ in app.git.list_pending_refs()] == ["d_aaa", "d_zzz"]
    with session_scope() as s:
        for sha in (sha_a, sha_b):
            s.add(models.CommitValidation(
                sha=sha, blob_sha="", path="support/system/v1.j2",
                prompt_id="support/system", version_number=1, status="valid",
                extracted_variables={},
            ))

    with session_scope() as s:
        result = recover_pending_promotions(s, app.git)
    assert result.promoted == 2 and result.rebased == 0 and result.discarded == 0
    assert app.git.head() == sha_b and app.git.is_ancestor(sha_a)
    assert app.git.read("support/system/v1.j2") == "B {{ x }}"
    assert app.git.list_pending_refs() == []
    # Nothing diverged, so nothing needed anchoring.
    assert app.git.recovered_sha("d_zzz") is None and app.git.recovered_sha("d_aaa") is None


def test_recovered_ref_reaches_replicas_by_mirror_fetch(app, tmp_path):
    """Replicas only ever see refs/* through mirror fetches; the anchor must travel
    with them so a pointer at the original sha resolves content on every node."""
    import subprocess

    from incant.gitstore import GitStore
    from incant.registry import recover_pending_promotions

    with session_scope() as s:
        reg = app.registry(s, "sam")
        d0 = reg.create_draft("support/system", version_number=1, author="sam",
                              content="hi {{ x }}")
        reg.commit_draft(d0.id, author="sam")
    remote = str(tmp_path / "backup.git")
    subprocess.run(["git", "init", "--bare", "--quiet", remote], check=True)
    app.git.push_mirror(remote)
    replica = GitStore(tmp_path / "replica")
    replica.clone_mirror(remote)                          # hydrated BEFORE the recovery

    draft_id, sha, _ = _stranded_publish(app, content="hi {{ x }} (stranded edit)")
    other = app.git.commit_version("support/system", 1, "hi {{ x }} (newer)",
                                   author_name="sam", author_email="sam@x", message="newer")
    with session_scope() as s:
        s.add(models.CommitValidation(
            sha=other, blob_sha="", path="support/system/v1.j2",
            prompt_id="support/system", version_number=1, status="valid",
            extracted_variables={},
        ))
    with session_scope() as s:
        assert recover_pending_promotions(s, app.git).rebased == 1

    app.git.push_mirror(remote)
    replica.mirror_fetch(remote)                          # the steady-state follow path
    assert replica.head() == app.git.head()
    assert replica.recovered_sha(draft_id) == sha
    assert replica.read("support/system/v1.j2", ref=sha) == "hi {{ x }} (stranded edit)"


def test_recovery_revalidates_replay_against_final_tree(app, caplog):
    """The verdict is not a pure function of the content: a fragment edited while the
    publish was stranded can close an include cycle. The replay re-runs the static
    checks against the tree it actually lands in and records THAT verdict."""
    from incant.registry import recover_pending_promotions

    with session_scope() as s:
        reg = app.registry(s, "sam")
        reg.create_prompt("support/frag")
        df = reg.create_draft("support/frag", version_number=1, author="sam", content="FRAG")
        reg.commit_draft(df.id, author="sam")
        d0 = reg.create_draft("support/system", version_number=1, author="sam",
                              content="hi {{ x }}")
        reg.commit_draft(d0.id, author="sam")
    stranded = '{% include "support/frag" %} {{ x }}'
    draft_id, sha, _ = _stranded_publish(app, content=stranded)   # valid when staged
    with session_scope() as s:
        row = s.execute(select(models.CommitValidation).where(
            models.CommitValidation.sha == sha)).scalar_one()
        assert row.status == "valid"
    # While stranded, the fragment starts including support/system: the replayed
    # content now closes a cycle on the tree it will land in.
    other = app.git.commit_version("support/frag", 1, '{% include "support/system" %}',
                                   author_name="sam", author_email="sam@x", message="loop")
    with session_scope() as s:
        s.add(models.CommitValidation(
            sha=other, blob_sha="", path="support/frag/v1.j2",
            prompt_id="support/frag", version_number=1, status="valid",
            extracted_variables={},
        ))
    with caplog.at_level("WARNING", logger="incant.reconcile"):
        with session_scope() as s:
            assert recover_pending_promotions(s, app.git).rebased == 1
    tip = app.git.head()
    assert app.git.read("support/system/v1.j2") == stranded
    with session_scope() as s:
        new_row = s.execute(select(models.CommitValidation).where(
            models.CommitValidation.sha == tip)).scalar_one()
        assert new_row.status == "invalid" and "include cycle" in (new_row.error or "")
        # The original sha's verdict is history, untouched.
        old_row = s.execute(select(models.CommitValidation).where(
            models.CommitValidation.sha == sha)).scalar_one()
        assert old_row.status == "valid"
    assert "is invalid on the current tree" in caplog.text


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
