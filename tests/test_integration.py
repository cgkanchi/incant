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

    # A prompt-scoped rule may only target a version that exists (§7 integrity).
    _author_version(app, "support/system", 1, "capture {{ x }}", make_live=False)
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
        # The render hot path is DB-free (§8), so it never touches this session; the
        # OUTAGE is observed by the background POLLER, which does read the DB.
        def execute(self, *a, **k):
            raise SQLAlchemyError("db down")

        def get(self, *a, **k):
            raise SQLAlchemyError("db down")

        def rollback(self, *a, **k):
            pass

    # A single failed poll flips the node into the frozen (stale) posture (§10). The
    # request itself never sees the DB — get_snapshot serves the last-known-good copy.
    app.refresh_control_plane(BoomSession())
    frozen = app.get_snapshot(BoomSession(), "prod")
    assert frozen.stale is True  # serving continues on a stale-flagged copy

    # A healthy poll clears the freeze; the cached snapshot was never mutated, so the
    # flag drops automatically (no sticky mutation to unwind).
    with session_scope() as s:
        app.refresh_control_plane(s)
        recovered = app.get_snapshot(s, "prod")
    assert recovered.stale is False


def test_serve_continues_during_db_outage(app):
    # §10: a Postgres outage must not take down serving. After a warm render the
    # snapshot is cached; the render path does no per-request DB read, so serving
    # continues once the POLLER has observed the outage — flagged stale_rules.
    _author_version(app, "support/system", 1, "Hi {{ name }}")
    app.invalidate("prod")
    with session_scope() as s:
        warm = app.serve(s, "prod", "support/system", {}, {"name": "Acme"})
    assert warm["stale_rules"] is False and warm["prompt"] == "Hi Acme"

    from sqlalchemy.exc import SQLAlchemyError

    class BoomSession:
        def execute(self, *a, **k):
            raise SQLAlchemyError("db down")

        def get(self, *a, **k):
            raise SQLAlchemyError("db down")

        def rollback(self, *a, **k):
            pass

    # The outage is observed by the background poller, not the request.
    app.refresh_control_plane(BoomSession())
    out = app.serve(BoomSession(), "prod", "support/system", {}, {"name": "Zed"})
    assert out["prompt"] == "Hi Zed"       # still rendered from the frozen snapshot
    assert out["stale_rules"] is True


def test_validation_render_checks_test_contexts(app):
    # §2.2/§5: a template that compiles but fails at render against a test context
    # must be recorded invalid, not valid.
    with session_scope() as s:
        reg = app.registry(s, "sam")
        reg.create_prompt("support/note")
        # A test context that does NOT supply the required `who` variable.
        reg.set_test_context("support/note", "ctx1", {}, {"other": "x"})
        d = reg.create_draft("support/note", version_number=1, author="sam",
                             content="Hello {{ who }}")  # compiles fine
        outcome = reg.commit_draft(d.id, author="sam", message="v1")
    assert outcome.validation["status"] == "invalid"
    assert "render failed" in outcome.validation["error"]
    assert "ctx1" in outcome.validation["error"]


def test_validation_passes_when_context_supplies_vars(app):
    with session_scope() as s:
        reg = app.registry(s, "sam")
        reg.create_prompt("support/note2")
        reg.set_test_context("support/note2", "ok", {}, {"who": "Sam"})
        d = reg.create_draft("support/note2", version_number=1, author="sam",
                             content="Hello {{ who }}")
        outcome = reg.commit_draft(d.id, author="sam", message="v1")
    assert outcome.validation["status"] == "valid", outcome.validation


def test_targeting_revisions_and_rollback(app):
    # §2.4: build up rule history, then roll targeting back to an earlier version.
    _author_version(app, "support/system", 1, "v1 {{ x }}")
    _author_version(app, "support/system", 2, "v2 {{ x }}", make_live=False)

    with session_scope() as s:
        tgt = app.targeting(s, "op")
        tgt.upsert_rule("prod", {"id": "r1", "scope": "prompt", "prompt_id": "support/system",
                                 "priority": 5, "when": None, "serve": {"version": 1}})
        rv_after_r1 = s.get(models.Environment, "prod").rules_version
    # Now add a second rule and edit the first.
    with session_scope() as s:
        tgt = app.targeting(s, "op")
        tgt.upsert_rule("prod", {"id": "r2", "scope": "prompt", "prompt_id": "support/system",
                                 "priority": 1, "when": None, "serve": {"version": 2}})
        tgt.upsert_rule("prod", {"id": "r1", "scope": "prompt", "prompt_id": "support/system",
                                 "priority": 9, "when": None, "serve": {"version": 2}})

    # Revisions record each change, stamped with the rules_version.
    with session_scope() as s:
        revs = app.targeting(s, "op").list_revisions("prod")
        assert any(r.rule_id == "r2" for r in revs) and any(r.rule_id == "r1" for r in revs)

    # Roll back to just after r1 was first created: r2 gone (archived), r1 restored.
    with session_scope() as s:
        result = app.targeting(s, "op").rollback("prod", rv_after_r1)
        assert result["rules_changed"] >= 1
    with session_scope() as s:
        r1 = s.get(models.Rule, "r1")
        r2 = s.get(models.Rule, "r2")
        assert r1.priority == 5 and r1.serve == {"version": 1}  # restored to original
        assert r2.status == "archived"                           # created after target


def test_track_tip_auto_advances_live_pointer(app):
    # §2.3/§7: staging tracks tips. With a live pointer on staging, a new validated
    # commit auto-advances the pointer to the new tip.
    with session_scope() as s:
        s.add(models.Environment(id="staging", name="staging", protected=False, track_tip=True))
    _author_version(app, "support/system", 1, "v1a", env="staging")   # default + live on staging
    with session_scope() as s:
        first = app.targeting(s, "sam").current_live("staging", "support/system", 1)

    with session_scope() as s:
        reg = app.registry(s, "sam")
        d = reg.create_draft("support/system", version_number=1, author="sam", content="v1b")
        out = reg.commit_draft(d.id, author="sam")

    with session_scope() as s:
        advanced = app.auto_advance_tips(s, "sam", "support/system", 1, out.sha)
    assert "staging" in advanced
    with session_scope() as s:
        now = app.targeting(s, "sam").current_live("staging", "support/system", 1)
    assert now == out.sha and now != first  # pointer followed the new tip


def test_track_tip_no_advance_without_live_pointer(app):
    # No existing live pointer -> nothing to follow, no auto-advance.
    with session_scope() as s:
        s.add(models.Environment(id="staging", name="staging", protected=False, track_tip=True))
    with session_scope() as s:
        reg = app.registry(s, "sam")
        reg.create_prompt("support/x")
        d = reg.create_draft("support/x", version_number=1, author="sam", content="hi")
        out = reg.commit_draft(d.id, author="sam")
    with session_scope() as s:
        assert app.auto_advance_tips(s, "sam", "support/x", 1, out.sha) == []


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


# ── §7 targeting integrity ────────────────────────────────────────────

def test_upsert_rule_rejects_missing_version(app):
    from incant.targeting.service import TargetingError

    _author_version(app, "support/system", 1, "v1 {{ x }}", make_live=False)
    with session_scope() as s:
        with pytest.raises(TargetingError):
            # v7 was never authored for this prompt.
            app.targeting(s, "op").upsert_rule("prod", {
                "id": "bad", "scope": "prompt", "prompt_id": "support/system",
                "priority": 5, "when": None, "serve": {"version": 7}})


def test_upsert_rule_rejects_unvalidated_pinned_sha(app):
    from incant.targeting.service import TargetingError

    _author_version(app, "support/system", 1, "v1 {{ x }}", make_live=False)
    with session_scope() as s:
        with pytest.raises(TargetingError):
            app.targeting(s, "op").upsert_rule("prod", {
                "id": "pinned", "scope": "prompt", "prompt_id": "support/system",
                "priority": 5, "when": None,
                "serve": {"version": 1, "at": "sha", "sha": "deadbeef" * 5}})


def test_upsert_rule_rejects_missing_version_in_rollout(app):
    from incant.targeting.service import TargetingError

    _author_version(app, "support/system", 1, "v1 {{ x }}", make_live=False)
    with session_scope() as s:
        with pytest.raises(TargetingError):
            app.targeting(s, "op").upsert_rule("prod", {
                "id": "roll", "scope": "prompt", "prompt_id": "support/system",
                "priority": 5, "when": None,
                "serve": {"rollout": {"bucket_by": "user_id", "weights": [
                    {"version": 9, "weight": 50}, {"default": True, "weight": 50}]}}})


def test_set_default_rejects_missing_version(app):
    from incant.targeting.service import TargetingError

    _author_version(app, "support/system", 1, "v1 {{ x }}", make_live=False)
    with session_scope() as s:
        with pytest.raises(TargetingError):
            app.targeting(s, "op").set_default("prod", "support/system", 42)
