"""Startup git↔DB draft reconciliation sweep (Item 3)."""

from __future__ import annotations

import pytest

from incant import db, models
from incant.config import Settings, set_settings
from incant.db import session_scope
from incant.registry import reconcile_drafts
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
