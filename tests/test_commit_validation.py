"""Commit validation is a VERDICT, never a 500 — and it says when the render check
did not run.

Two failure classes used to escape ``RegistryService.validate``:

* render-time ``IncludeCycle`` / ``IncludeDepthExceeded`` (a LIVE pointer — not the
  newest version the static check walks — closing the loop) and a bare ``KeyError``
  from an unfetchable resolved SHA are not ``RenderError`` subclasses, so they blew
  through the commit endpoint AND the draft GET (which validates on every read) as
  500s — locking the author out of the draft that needed fixing;
* a ``build_snapshot`` failure (no default environment) silently disabled the §5
  render check deployment-wide while commits kept being recorded ``valid``.
"""

from __future__ import annotations

import importlib

import pytest

from incant import db, models
from incant.config import Settings, set_settings
from incant.db import session_scope
from incant.service import AppContext, get_app, reset_app

from .conftest import db_url_for, reset_schema
from .test_server import auth, make_client


@pytest.fixture()
def ctx(tmp_path):
    """Registry-level context. The default environment ('prod') is NOT created here —
    tests that need it add the row, so the fresh-deployment case is the baseline."""
    set_settings(Settings(
        database_url=db_url_for(tmp_path),
        repo_path=str(tmp_path / "repo"),
    ))
    db.reset_engine()
    reset_app()
    reset_schema()
    c = AppContext()
    c.initialize()
    yield c


def _add_prod():
    with session_scope() as s:
        s.add(models.Environment(id="prod", name="prod"))


def _publish(ctx, prompt_id, version, content, *, default=True, live=True, env="prod"):
    """Create prompt if needed, draft + commit, then (optionally) default + live."""
    with session_scope() as s:
        reg = ctx.registry(s, "sam")
        if not reg.prompt_exists(prompt_id):
            reg.create_prompt(prompt_id)
        d = reg.create_draft(prompt_id, version_number=version, author="sam", content=content)
        out = reg.commit_draft(d.id, author="sam", message=f"v{version}")
        assert out.validation["status"] == "valid", out.validation
        tgt = ctx.targeting(s, "sam")
        if default:
            tgt.set_default(env, prompt_id, version)
        if live:
            tgt.make_live(env, prompt_id, version, out.sha, comment=f"v{version} live")
    return out


# ── the render check reports whether it ran ──────────────────────────


def test_missing_default_environment_skips_render_check_visibly(ctx, caplog):
    # Fresh deployment: no 'prod' environment yet. The commit must still succeed (the
    # environment is created later), but "valid" must not pretend the render ran.
    with session_scope() as s:
        reg = ctx.registry(s, "sam")
        reg.create_prompt("support/note")
        reg.set_test_context("support/note", "ctx1", {}, {})   # would fail: no `who`
        d = reg.create_draft("support/note", version_number=1, author="sam",
                             content="Hello {{ who }}")
        with caplog.at_level("WARNING", logger="incant.registry"):
            out = reg.commit_draft(d.id, author="sam")
    assert out.validation["status"] == "valid"
    assert out.validation["render_checked"] is False
    assert "prod" in out.validation["render_skipped_reason"]
    assert "render check" in caplog.text and "prod" in caplog.text


def test_render_check_is_marked_when_it_runs(ctx):
    _add_prod()
    with session_scope() as s:
        reg = ctx.registry(s, "sam")
        reg.create_prompt("support/note")
        reg.set_test_context("support/note", "ctx1", {}, {"who": "Sam"})
        d = reg.create_draft("support/note", version_number=1, author="sam",
                             content="Hello {{ who }}")
        out = reg.commit_draft(d.id, author="sam")
    assert out.validation["status"] == "valid"
    assert out.validation["render_checked"] is True
    assert out.validation["render_skipped_reason"] is None
    # …and the same environment DOES catch a real render failure (not a false "ran").
    with session_scope() as s:
        reg = ctx.registry(s, "sam")
        val = reg.validate("support/note", "Hello {{ nobody_supplies_this }}")
    assert val.status == "invalid" and val.render_checked is True
    assert "ctx1" in val.error


def test_no_test_contexts_is_reported_as_skipped(ctx):
    _add_prod()
    with session_scope() as s:
        reg = ctx.registry(s, "sam")
        reg.create_prompt("support/bare")
        val = reg.validate("support/bare", "Hello {{ who }}")
    assert val.status == "valid" and val.render_checked is False
    assert "no test contexts" in val.render_skipped_reason


# ── render-time failures are verdicts, not exceptions ────────────────


def test_unfetchable_resolved_content_is_a_verdict(ctx, monkeypatch):
    _add_prod()
    _publish(ctx, "support/frag", 1, "FRAG")

    def boom(prompt_id, version, commit_sha):
        raise KeyError(f"{prompt_id}/v{version}.j2 not present at {commit_sha}")

    monkeypatch.setattr(ctx.content, "get", boom)     # the store lost the blob
    with session_scope() as s:
        reg = ctx.registry(s, "sam")
        reg.create_prompt("support/main")
        reg.set_test_context("support/main", "ctx1", {}, {})
        val = reg.validate("support/main", '{% include "support/frag" %}')
    assert val.status == "invalid" and val.render_checked is True
    assert "resolved content missing from store" in val.error and "ctx1" in val.error
    assert "support/frag/v1.j2" in val.error


def test_include_depth_exceeded_is_a_verdict(ctx, monkeypatch):
    _add_prod()
    _publish(ctx, "support/frag", 1, "FRAG")
    # `incant.core.render` the attribute is the render() function; reach the module.
    monkeypatch.setattr(importlib.import_module("incant.core.render"), "DEPTH_LIMIT", 1)
    with session_scope() as s:
        reg = ctx.registry(s, "sam")
        reg.create_prompt("support/main")
        reg.set_test_context("support/main", "ctx1", {}, {})
        val = reg.validate("support/main", '{% include "support/frag" %}')
    assert val.status == "invalid"
    assert "include depth limit 1 exceeded" in val.error and "ctx1" in val.error


def test_live_pointer_include_cycle_is_a_verdict_not_a_500(tmp_path):
    # Static validation walks each included prompt's NEWEST version; the render walks
    # the default environment's LIVE pointers. b's newest (v2) is plain, but its live
    # default (v1) includes a — so a draft of a that includes b cycles at render time
    # only. Both the draft GET and the commit must answer with a verdict.
    with make_client(tmp_path) as client:
        ctx = get_app()
        with session_scope() as s:   # the seeded project wants a review; not under test
            s.get(models.Project, "support").review_policy = 0
        _publish(ctx, "support/a", 1, "plainA")
        _publish(ctx, "support/b", 1, '{% include "support/a" %}')
        _publish(ctx, "support/b", 2, "plainB", default=False, live=False)

        r = client.post("/mgmt/prompts/support/a/drafts",
                        json={"version_number": 2, "content": '{% include "support/b" %}'},
                        headers=auth())
        assert r.status_code == 200, r.text
        draft_id = r.json()["id"]

        # Without test contexts only the static checks run — and the payload says so.
        r = client.get(f"/mgmt/drafts/{draft_id}", headers=auth())
        assert r.status_code == 200, r.text
        lint = r.json()["lint"]
        assert lint["status"] == "valid" and lint["render_checked"] is False
        assert "no test contexts" in lint["render_skipped_reason"]

        with session_scope() as s:
            ctx.registry(s, "sam").set_test_context("support/a", "ctx1", {}, {})

        # The editor still opens on the cycling draft: the cycle is the lint verdict.
        r = client.get(f"/mgmt/drafts/{draft_id}", headers=auth())
        assert r.status_code == 200, r.text
        lint = r.json()["lint"]
        assert lint["status"] == "invalid" and lint["render_checked"] is True
        assert "include cycle" in lint["error"] and "ctx1" in lint["error"]

        r = client.post(f"/mgmt/drafts/{draft_id}/commit", json={}, headers=auth())
        assert r.status_code == 200, r.text
        v = r.json()["validation"]
        assert v["status"] == "invalid" and v["render_checked"] is True
        assert "include cycle" in v["error"] and "support/a" in v["error"]
        assert "ctx1" in v["error"]
