"""End-to-end: author -> validate -> commit -> target -> make live -> serve."""

from __future__ import annotations

import pytest

from incant import db, models
from incant.config import Settings, set_settings
from incant.db import session_scope
from incant.registry import ReviewRequired
from incant.service import AppContext, ServingError, reset_app

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
        s.add(models.Environment(id="prod", name="prod", protected=False, track_tip=False))
    yield ctx


def _author_version(ctx, prompt_id, version, content, *, seed=None, make_live=True, env="prod"):
    """Create prompt if needed, draft+commit a version, register default+live."""
    with session_scope() as s:
        reg = ctx.registry(s, "sam")
        if not reg.prompt_exists(prompt_id):
            reg.create_prompt(prompt_id)
        d = reg.create_draft(prompt_id, version_number=version, seed_from_version=seed,
                             author="sam", content=content)
        outcome = reg.commit_draft(d.id, author="sam", message=f"v{version}")
        assert outcome.validation["status"] == "valid", outcome.validation
        tgt = ctx.targeting(s, "sam")
        tgt.set_default(env, prompt_id, version)
        if make_live:
            tgt.make_live(env, prompt_id, version, outcome.sha, comment=f"v{version} live")
    ctx.invalidate(env)
    return outcome


def test_rule_cannot_be_captured_across_environments(app):
    from incant.targeting.service import TargetingError

    with session_scope() as s:
        s.add(models.Environment(id="staging", name="staging", protected=False, track_tip=False))
    # A rule 'r1' lives in prod.
    with session_scope() as s:
        app.targeting(s, "op").upsert_rule("prod", {
            "id": "r1", "scope": "prompt", "prompt_id": "support/system",
            "priority": 1, "when": None, "serve": {"version": 1}})
    # An operator scoped to staging cannot edit or archive the prod rule via staging.
    with session_scope() as s:
        with pytest.raises(TargetingError):
            app.targeting(s, "op").upsert_rule("staging", {
                "id": "r1", "scope": "prompt", "prompt_id": "support/system",
                "priority": 9, "when": None, "serve": {"version": 1}})
    with session_scope() as s:
        with pytest.raises(TargetingError):
            app.targeting(s, "op").set_rule_status("staging", "r1", "archived")
    with session_scope() as s:
        r = s.get(models.Rule, "r1")
        assert r.environment_id == "prod" and r.priority == 1 and r.status == "active"


def test_stale_flag_clears_after_db_recovery(app):
    from sqlalchemy.exc import SQLAlchemyError

    with session_scope() as s:
        primed = app.get_snapshot(s, "prod")
    assert primed.stale is False

    class BoomSession:
        def get(self, *a, **k):
            raise SQLAlchemyError("db down")

    frozen = app.get_snapshot(BoomSession(), "prod")
    assert frozen.stale is True  # serving continues on a stale-flagged copy

    with session_scope() as s:
        recovered = app.get_snapshot(s, "prod")
    assert recovered.stale is False  # flag clears once the DB is back (no sticky mutation)


def test_full_loop_render(app):
    _author_version(app, "shared/style/language-rules", 1, "Write in plain English.")
    _author_version(
        app, "support/system", 1,
        'You are a support agent for {{ customer_name }}.\n'
        '{% include "shared/style/language-rules" %}',
    )
    with session_scope() as s:
        resp = app.serve(s, "prod", "support/system", {}, {"customer_name": "Acme"})
    assert "support agent for Acme" in resp["prompt"]
    assert "Write in plain English." in resp["prompt"]
    assert resp["matched_rule"] == "default"
    assert set(resp["versions"]) == {"support/system", "shared/style/language-rules"}
    assert resp["content_fallback"] is False


def test_tweak_flow_tip_then_make_live(app):
    _author_version(app, "support/system", 2, "v2 formal tone. {{ customer_name }}")
    # Tweak: new commit on v2 -> tip ahead of live.
    with session_scope() as s:
        reg = app.registry(s, "sam")
        d = reg.create_draft("support/system", version_number=2, author="sam",
                             content="v2 warm tone. {{ customer_name }}")
        tip = reg.commit_draft(d.id, author="sam", message="warm tweak")
    app.invalidate("prod")

    # Live pointer still pins the old SHA; serving unchanged.
    with session_scope() as s:
        resp = app.serve(s, "prod", "support/system", {}, {"customer_name": "Acme"})
    assert "formal tone" in resp["prompt"]

    # Target team-x to v2@tip, verify they get the tweak.
    with session_scope() as s:
        tgt = app.targeting(s, "sam")
        tgt.upsert_rule("prod", {
            "id": "team-x-tip", "scope": "prompt", "prompt_id": "support/system",
            "priority": 20, "when": {"flag": "user_id", "op": "in", "values": ["u_12"]},
            "serve": {"version": 2, "at": "tip"},
        })
    app.invalidate("prod")
    with session_scope() as s:
        member = app.serve(s, "prod", "support/system", {"user_id": "u_12"}, {"customer_name": "Acme"})
        other = app.serve(s, "prod", "support/system", {"user_id": "u_99"}, {"customer_name": "Acme"})
    assert "warm tone" in member["prompt"]
    assert "formal tone" in other["prompt"]

    # Make live -> everyone gets the tweak.
    with session_scope() as s:
        tgt = app.targeting(s, "sam")
        tgt.make_live("prod", "support/system", 2, tip.sha, comment="warm tone live")
    app.invalidate("prod")
    with session_scope() as s:
        resp = app.serve(s, "prod", "support/system", {}, {"customer_name": "Acme"})
    assert "warm tone" in resp["prompt"]


def test_rollback_via_pointer_history(app):
    out1 = _author_version(app, "support/greeting", 1, "hello v1")
    with session_scope() as s:
        reg = app.registry(s, "sam")
        d = reg.create_draft("support/greeting", version_number=1, author="sam", content="hello v2tweak")
        out2 = reg.commit_draft(d.id, author="sam", message="tweak")
        app.targeting(s, "sam").make_live("prod", "support/greeting", 1, out2.sha, comment="tweak live")
    app.invalidate("prod")
    with session_scope() as s:
        assert "v2tweak" in app.serve(s, "prod", "support/greeting", {}, {})["prompt"]
    # Roll back to the original SHA (still validated) — instant pointer move.
    with session_scope() as s:
        app.targeting(s, "sam").make_live("prod", "support/greeting", 1, out1.sha, comment="rollback")
    app.invalidate("prod")
    with session_scope() as s:
        assert "hello v1" == app.serve(s, "prod", "support/greeting", {}, {})["prompt"]


def test_review_policy_blocks_commit(app):
    with session_scope() as s:
        reg = app.registry(s, "sam")
        reg.ensure_project("support", review_policy=1)
        reg.create_prompt("support/system")
        d = reg.create_draft("support/system", version_number=1, author="sam", content="hi {{ x }}")
        with pytest.raises(ReviewRequired):
            reg.commit_draft(d.id, author="sam")
        # A different reviewer approves -> commit unlocked.
        reg.add_review(d.id, reviewer="rae", state="approved")
        out = reg.commit_draft(d.id, author="sam")
    assert out.validation["status"] == "valid"


def test_kill_switch_forces_default(app):
    _author_version(app, "support/system", 1, "default-v1")
    _author_version(app, "support/system", 2, "v2-content", make_live=True)
    # default is v2 now (set by _author_version); add a rule serving v1, then kill.
    with session_scope() as s:
        tgt = app.targeting(s, "sam")
        tgt.set_default("prod", "support/system", 2)
        tgt.upsert_rule("prod", {
            "id": "everyone-v1", "scope": "prompt", "prompt_id": "support/system",
            "priority": 1, "when": None, "serve": {"version": 1},
        })
    app.invalidate("prod")
    with session_scope() as s:
        assert "default-v1" in app.serve(s, "prod", "support/system", {}, {})["prompt"]
    # Kill switch: bypass rules, serve env default (v2).
    with session_scope() as s:
        app.targeting(s, "sam").set_kill("prod", "support/system", True)
    app.invalidate("prod")
    with session_scope() as s:
        assert "v2-content" in app.serve(s, "prod", "support/system", {}, {})["prompt"]


def test_missing_required_variable_is_422(app):
    _author_version(app, "support/system", 1, "hi {{ name }}")
    with session_scope() as s:
        with pytest.raises(ServingError) as e:
            app.serve(s, "prod", "support/system", {}, {})
    assert e.value.status == 422 and e.value.extra.get("variable") == "name"
