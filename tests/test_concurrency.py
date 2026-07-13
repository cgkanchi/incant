"""Multi-user concurrency — only meaningful against a real Postgres pool.

SQLite serializes writers, so it cannot exercise the lost-update race the atomic
`rules_version` bump defends against. These tests are skipped unless
INCANT_TEST_DATABASE_URL points at Postgres.
"""

from __future__ import annotations

import concurrent.futures as cf

import pytest
from sqlalchemy import select

from incant import db, models
from incant.config import Settings, set_settings
from incant.db import session_scope
from incant.service import AppContext, reset_app

from .conftest import EFFECTIVE_TEST_URL, reset_schema

pytestmark = pytest.mark.skipif(
    not (EFFECTIVE_TEST_URL and EFFECTIVE_TEST_URL.startswith("postgres")),
    reason="concurrency tests require Postgres (set INCANT_TEST_DATABASE_URL)",
)


@pytest.fixture()
def app(tmp_path):
    set_settings(Settings(database_url=EFFECTIVE_TEST_URL, repo_path=str(tmp_path / "repo")))
    db.reset_engine()
    reset_app()
    reset_schema()
    ctx = AppContext()
    ctx.initialize()
    with session_scope() as s:
        s.add(models.Environment(id="prod", name="prod", protected=False, track_tip=False))
    return ctx


def _rules_version(env_id="prod") -> int:
    with session_scope() as s:
        return s.get(models.Environment, env_id).rules_version


def test_concurrent_rule_upserts_never_lose_a_bump(app):
    # A prompt-scoped rule may only target a version that exists (§7 integrity), so
    # author support/system v1 before the concurrent upserts.
    with session_scope() as s:
        reg = app.registry(s, "sam")
        reg.create_prompt("support/system")
        d = reg.create_draft("support/system", version_number=1, author="sam", content="v1 {{ x }}")
        reg.commit_draft(d.id, author="sam")
    start = _rules_version()
    N = 24

    def upsert(i: int):
        with session_scope() as s:
            tgt = app.targeting(s, f"op{i}")
            tgt.upsert_rule("prod", {
                "id": f"rule-{i}", "scope": "prompt", "prompt_id": "support/system",
                "priority": i, "when": None, "serve": {"version": 1},
            })

    with cf.ThreadPoolExecutor(max_workers=N) as ex:
        errors = [f.exception() for f in
                  [ex.submit(upsert, i) for i in range(N)]]
    assert not any(errors), [e for e in errors if e]

    # Every concurrent mutation advanced the counter exactly once — no lost updates.
    assert _rules_version() == start + N
    with session_scope() as s:
        rules = s.execute(select(models.Rule).where(models.Rule.environment_id == "prod")).scalars().all()
        assert len(rules) == N


def test_concurrent_pointer_moves_are_all_recorded(app):
    # Author two validated commits, then hammer make-live/revert between them.
    with session_scope() as s:
        reg = app.registry(s, "sam")
        reg.create_prompt("support/system")
        d1 = reg.create_draft("support/system", version_number=1, author="sam", content="one")
        a = reg.commit_draft(d1.id, author="sam")
        d2 = reg.create_draft("support/system", version_number=1, author="sam", content="two")
        b = reg.commit_draft(d2.id, author="sam")
        app.targeting(s, "sam").set_default("prod", "support/system", 1)
    shas = [a.sha, b.sha]

    def move(i: int):
        with session_scope() as s:
            app.targeting(s, f"op{i}").make_live(
                "prod", "support/system", 1, shas[i % 2], comment=f"move {i}")

    with cf.ThreadPoolExecutor(max_workers=16) as ex:
        errors = [f.exception() for f in [ex.submit(move, i) for i in range(16)]]
    assert not any(errors), [e for e in errors if e]

    with session_scope() as s:
        moves = s.execute(select(models.PointerMove).where(
            models.PointerMove.environment_id == "prod")).scalars().all()
        assert len(moves) == 16  # append-only: every move recorded, none lost
