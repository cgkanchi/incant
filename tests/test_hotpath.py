"""The render + auth hot paths are DB-free when the node is warm.

DESIGN.md §8 ("No DB per request") and §10 ("Postgres … sits on the refresh/write paths
only, never per-request") promise the serving path touches the DB zero times on a warm
node. Freshness is pulled in off the request path by the background poll
(:meth:`AppContext.refresh_control_plane`), never by a request.

These tests drive the sync seam directly — no event loop, no HTTP — and prove "DB-free"
the hard way: they hand the hot path a session whose every method raises, and assert it
still serves. A single stray DB access would blow up instead of returning.
"""

from __future__ import annotations

import pytest
from sqlalchemy.exc import SQLAlchemyError

from incant import db, models
from incant.config import Settings, set_settings
from incant.db import session_scope
from incant.server.auth import DEV_ADMIN_KEY, ensure_bootstrap_admin, issue_api_key
from incant.service import AppContext, reset_app

from .conftest import db_url_for, reset_schema


class BoomSession:
    """A session whose every attribute access yields a method that raises. Standing in
    for the request-path session, it is the proof that the code under test does no DB
    I/O: if it touches the session at all, the test errors instead of passing."""

    def __getattr__(self, name):
        def _boom(*a, **k):
            raise SQLAlchemyError(f"db down: {name}")
        return _boom


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
    return ctx


def _author_version(ctx, prompt_id, version, content, *, make_live=True, env="prod"):
    """Create prompt if needed, draft+commit a version, register default+live."""
    with session_scope() as s:
        reg = ctx.registry(s, "sam")
        if not reg.prompt_exists(prompt_id):
            reg.create_prompt(prompt_id)
        d = reg.create_draft(prompt_id, version_number=version, author="sam", content=content)
        outcome = reg.commit_draft(d.id, author="sam", message=f"v{version}")
        assert outcome.validation["status"] == "valid", outcome.validation
        tgt = ctx.targeting(s, "sam")
        tgt.set_default(env, prompt_id, version)
        if make_live:
            tgt.make_live(env, prompt_id, version, outcome.sha, comment=f"v{version} live")
    ctx.invalidate(env)
    return outcome


def test_warm_snapshot_is_served_without_touching_the_db(app):
    # Prime the snapshot with a real session (cold miss → build from DB → cache).
    _author_version(app, "support/system", 1, "Hi {{ name }}")
    with session_scope() as s:
        primed = app.get_snapshot(s, "prod")
    assert primed.stale is False

    # Warm + DB healthy: get_snapshot serves from memory and NEVER reads the session.
    snap = app.get_snapshot(BoomSession(), "prod")
    assert snap is primed        # same cached object — no rebuild, no DB read
    assert snap.stale is False   # DB healthy, snapshot merely untouched — not frozen


def test_refresh_control_plane_picks_up_an_external_rules_bump(app):
    # A live snapshot cached in-process.
    _author_version(app, "support/system", 1, "v1 {{ x }}")
    with session_scope() as s:
        first = app.get_snapshot(s, "prod")
    first_rv = first.rules_version

    # An operator on ANOTHER replica adds a rule, bumping prod.rules_version. We do NOT
    # call invalidate() — that models the same-process write; here the write is external,
    # so only the poll can propagate it.
    with session_scope() as s:
        app.targeting(s, "op").upsert_rule("prod", {
            "id": "r1", "scope": "prompt", "prompt_id": "support/system",
            "priority": 5, "when": None, "serve": {"version": 1},
        })
        bumped_rv = s.get(models.Environment, "prod").rules_version
    assert bumped_rv != first_rv

    # Until a poll runs, the hot path keeps serving the OLD snapshot from memory.
    with session_scope() as s:
        stale_read = app.get_snapshot(s, "prod")
    assert stale_read is first
    assert stale_read.rules_version == first_rv
    assert not any(r.id == "r1" for r in stale_read.rules)

    # The background poll pulls the bump in and atomically swaps the cache entry.
    with session_scope() as s:
        app.refresh_control_plane(s)
        refreshed = app.get_snapshot(s, "prod")
    assert refreshed.rules_version == bumped_rv
    assert any(r.id == "r1" for r in refreshed.rules)


def test_auth_warm_path_does_no_db_read(app):
    # Insert a real admin key and warm the auth cache with a real session.
    with session_scope() as s:
        ensure_bootstrap_admin(s, DEV_ADMIN_KEY)
    with session_scope() as s:
        ident = app.authenticate(s, f"Bearer {DEV_ADMIN_KEY}")
    assert ident.has("admin")

    # Warm + key present: a second identify authenticates from memory alone, even when
    # handed a session whose every method raises.
    ident2 = app.authenticate(BoomSession(), f"Bearer {DEV_ADMIN_KEY}")
    assert ident2.principal_id == ident.principal_id and ident2.has("admin")


def test_on_miss_reload_picks_up_a_freshly_issued_key(app):
    # Warm the cache with the admin key.
    with session_scope() as s:
        ensure_bootstrap_admin(s, DEV_ADMIN_KEY)
    with session_scope() as s:
        app.authenticate(s, f"Bearer {DEV_ADMIN_KEY}")

    # Issue a NEW key directly, WITHOUT invalidate_auth — this exercises the throttled
    # on-miss reload (the path that lets a key issued on another replica authenticate),
    # not the same-process invalidate path.
    with session_scope() as s:
        s.add(models.Principal(id="p_svc", kind="service", subject="svc", name="svc"))
        s.flush()
        s.add(models.RoleBinding(principal_id="p_svc", role="viewer"))
        raw, _row = issue_api_key(s, principal_id="p_svc", name="svc key")

    # Defeat the min-refresh throttle so the miss reload fires deterministically. The
    # throttle otherwise suppresses a reload within ~1s of the last load; in production
    # the freshly-issued key simply becomes usable within that window.
    app.auth._min_refresh = 0.0

    with session_scope() as s:
        ident = app.authenticate(s, f"Bearer {raw}")
    assert ident.principal_id == "p_svc" and ident.has("viewer")
