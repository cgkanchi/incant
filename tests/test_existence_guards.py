"""Service-level existence guards (Finding F9).

Refinements, test contexts, and kill switches carry no FK to prompts/versions, so
a typo'd write used to create DORMANT config that silently activated if a prompt
or version with that name appeared later. The services now refuse writes naming a
prompt (and, for refinements, a version) that doesn't exist.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from incant import db, models
from incant.config import Settings, set_settings
from incant.db import session_scope
from incant.registry import RegistryError
from incant.service import AppContext, reset_app
from incant.targeting.service import TargetingError

from .conftest import db_url_for, reset_schema


@pytest.fixture()
def app(tmp_path):
    set_settings(Settings(database_url=db_url_for(tmp_path),
                          repo_path=str(tmp_path / "repo")))
    db.reset_engine()
    reset_app()
    reset_schema()
    ctx = AppContext()
    ctx.initialize()
    with session_scope() as s:
        s.add(models.Environment(id="prod", name="prod"))
    with session_scope() as s:
        # A real prompt with a committed v1, so the positive paths have a target.
        reg = ctx.registry(s, "sam")
        reg.create_prompt("support/system")
        d = reg.create_draft("support/system", version_number=1, author="sam",
                             content="hi {{ x }}")
        reg.commit_draft(d.id, author="sam")
    yield ctx


# ── refinements ──────────────────────────────────────────────────────────────

def test_refinement_refuses_unknown_prompt(app):
    with session_scope() as s:
        with pytest.raises(RegistryError, match="unknown prompt"):
            app.registry(s, "sam").set_refinement(
                "support/sytsem", 1, "x", required=True)  # note the typo
    with session_scope() as s:
        assert s.execute(select(models.VariableRefinement)).scalars().all() == []


def test_refinement_refuses_unknown_version(app):
    with session_scope() as s:
        with pytest.raises(RegistryError, match="unknown version 9"):
            app.registry(s, "sam").set_refinement(
                "support/system", 9, "x", required=True)
    with session_scope() as s:
        assert s.execute(select(models.VariableRefinement)).scalars().all() == []


def test_refinement_on_real_prompt_and_version_works(app):
    with session_scope() as s:
        r = app.registry(s, "sam").set_refinement(
            "support/system", 1, "x", type="string", required=True)
        assert r.name == "x" and r.required is True
    with session_scope() as s:
        rows = app.registry(s, "sam").get_refinements("support/system", 1)
        assert [row.name for row in rows] == ["x"]


# ── test contexts ────────────────────────────────────────────────────────────

def test_test_context_refuses_unknown_prompt(app):
    with session_scope() as s:
        with pytest.raises(RegistryError, match="unknown prompt"):
            app.registry(s, "sam").set_test_context(
                "support/nope", "ctx", {}, {"x": 1})
    with session_scope() as s:
        assert s.execute(select(models.TestContext)).scalars().all() == []


def test_test_context_on_real_prompt_works(app):
    with session_scope() as s:
        t = app.registry(s, "sam").set_test_context(
            "support/system", "ctx", {"tier": "pro"}, {"x": 1})
        assert t.name == "ctx"
    with session_scope() as s:
        names = [t.name for t in app.registry(s, "sam").get_test_contexts("support/system")]
        assert names == ["ctx"]


# ── kill switches ────────────────────────────────────────────────────────────

def test_kill_refuses_unknown_prompt(app):
    with session_scope() as s:
        with pytest.raises(TargetingError, match="unknown prompt"):
            app.targeting(s, "op").set_kill("prod", "support/sytsem", True)
    with session_scope() as s:
        assert s.execute(select(models.KillSwitch)).scalars().all() == []


def test_kill_on_real_prompt_works_both_ways(app):
    with session_scope() as s:
        k = app.targeting(s, "op").set_kill("prod", "support/system", True)
        assert k.engaged is True
    with session_scope() as s:
        k = app.targeting(s, "op").set_kill("prod", "support/system", False)
        assert k.engaged is False
