"""A cache MISS must be cheap. §8 ("No DB per request") already moved the periodic
whole-table auth reload to the background poll (:meth:`AuthCache.refresh`); what remained
on the request path was the ON-MISS reload — an UNKNOWN key prefix triggered a full
three-table SELECT under the cache lock once per ``min_refresh``, so distributed
invalid-credential traffic could sustain one whole-table reload per second on the request
path, with every concurrent missing-prefix request blocked on the lock behind it.

These tests pin the cheap-miss behaviour of :class:`AuthCache`:

* a bounded NEGATIVE CACHE turns a repeat miss into zero DB work (no query, no lock);
* a single INDEXED PROBE on the unique ``api_keys.prefix`` (migration a3f1c8e29b41)
  replaces the full reload — only a probe HIT (a genuinely fresh key this replica has not
  snapshotted) escalates to the three-table reload;
* the probe is throttled GLOBALLY (one ``_last_probe`` timestamp, mirroring the existing
  ``_last_refresh`` idiom): a burst of distinct unknown prefixes inside the window costs at
  most the single probe already in flight;
* ``invalidate()`` drops the negative cache so an in-process issuance of a
  previously-probed key still authenticates.

They drive the :class:`AppContext` seam directly — no HTTP, hence no per-IP throttle — and
count SQL statements with an engine event listener, exactly as ``test_overview_scale`` does.
"""

from __future__ import annotations

from contextlib import contextmanager

import pytest
from sqlalchemy import event

from incant import db, models
from incant.config import Settings, set_settings
from incant.db import session_scope
from incant.server.auth import (
    _NEG_CACHE_MAX,
    AuthError,
    DEV_ADMIN_KEY,
    ensure_bootstrap_admin,
    hash_key,
    issue_api_key,
    key_prefix,
)
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
    return ctx


@contextmanager
def count_statements():
    """Yield a list collecting every SQL statement executed on the app engine for the
    duration of the block (the ``test_overview_scale`` idiom). A COMMIT is issued at the
    connection level, not via ``cursor.execute``, so ``before_cursor_execute`` never fires
    for it — an all-in-memory identify therefore records ZERO statements."""
    eng = db.engine()
    statements: list[str] = []

    def _count(conn, cur, statement, params, context, executemany):
        statements.append(statement)

    event.listen(eng, "before_cursor_execute", _count)
    try:
        yield statements
    finally:
        event.remove(eng, "before_cursor_execute", _count)


def _warm_with_admin(ctx) -> None:
    """Insert the dev admin key and warm the auth cache (the cold full load) with a real
    session, so subsequent misses exercise the warm-miss path, not the cold load."""
    with session_scope() as s:
        ensure_bootstrap_admin(s, DEV_ADMIN_KEY)
    with session_scope() as s:
        ctx.authenticate(s, f"Bearer {DEV_ADMIN_KEY}")


def test_unknown_key_first_miss_probes_once_then_negative_caches(app):
    _warm_with_admin(app)
    unknown = "incant_sk_" + "0" * 32

    # First miss: exactly ONE targeted (indexed) probe, then 401 — never the three-table
    # full reload the old on-miss path ran.
    with count_statements() as stmts:
        with session_scope() as s:
            with pytest.raises(AuthError) as ei:
                app.auth.identify(s, f"Bearer {unknown}")
    assert ei.value.status == 401
    assert len(stmts) == 1, stmts
    assert key_prefix(unknown) in app.auth._negcache  # absence was recorded

    # Second miss on the same key: the negative cache answers — ZERO DB work, still 401.
    with count_statements() as stmts2:
        with session_scope() as s:
            with pytest.raises(AuthError) as ei2:
                app.auth.identify(s, f"Bearer {unknown}")
    assert ei2.value.status == 401
    assert stmts2 == [], stmts2


def test_freshly_issued_key_resolves_via_probe_then_full_reload(app):
    _warm_with_admin(app)

    # Issue a new key directly, WITHOUT invalidate_auth — models a key issued on ANOTHER
    # replica. This process's cache is warm and does not yet know it.
    with session_scope() as s:
        s.add(models.Principal(id="p_svc", kind="service", subject="svc", name="svc"))
        s.flush()
        s.add(models.RoleBinding(principal_id="p_svc", role="viewer"))
        raw, _row = issue_api_key(s, principal_id="p_svc", name="svc key")

    # First identify: one indexed probe HITS the fresh row, escalating to the full
    # three-table reload — one probe + three loads = four statements.
    with count_statements() as stmts:
        with session_scope() as s:
            ident = app.auth.identify(s, f"Bearer {raw}")
    assert ident.principal_id == "p_svc" and ident.has("viewer")
    assert len(stmts) == 4, stmts

    # Second identify: the key is in the warm table now — ZERO DB work.
    with count_statements() as stmts2:
        with session_scope() as s:
            ident2 = app.auth.identify(s, f"Bearer {raw}")
    assert ident2.principal_id == "p_svc"
    assert stmts2 == [], stmts2


def test_invalidate_clears_negative_cache_so_a_probed_then_issued_key_works(app):
    _warm_with_admin(app)

    # A concrete raw key, probed while absent -> recorded in the negative cache, 401.
    raw = "incant_sk_" + "a" * 32
    with session_scope() as s:
        with pytest.raises(AuthError):
            app.auth.identify(s, f"Bearer {raw}")
    assert key_prefix(raw) in app.auth._negcache

    # Now actually create THAT EXACT key (same prefix + hash), as an in-process issuance
    # would, and invalidate() — which must drop the now-stale "absent" verdict.
    with session_scope() as s:
        s.add(models.Principal(id="p_late", kind="service", subject="late", name="late"))
        s.flush()
        s.add(models.RoleBinding(principal_id="p_late", role="viewer"))
        s.add(models.ApiKey(principal_id="p_late", prefix=key_prefix(raw),
                            hash=hash_key(raw), name="late key"))
    app.invalidate_auth()
    assert app.auth._negcache == set()  # invalidate wiped the stale absence verdict

    # The same key now authenticates: the next identify cold-reloads and finds it.
    with session_scope() as s:
        ident = app.auth.identify(s, f"Bearer {raw}")
    assert ident.principal_id == "p_late" and ident.has("viewer")


def test_min_refresh_gates_a_burst_of_distinct_unknown_probes(app):
    _warm_with_admin(app)

    # A burst of DISTINCT unknown prefixes within the (default 1s) min_refresh window.
    # Global gating means only the FIRST does a targeted probe; the rest fail fast without
    # touching the DB and — being unconfirmed — are NOT negative-cached, so they earn a
    # real probe once the window elapses.
    keys = [f"incant_sk_{i}" + "x" * 30 for i in range(6)]  # distinct 20-char prefixes
    with count_statements() as stmts:
        with session_scope() as s:
            for k in keys:
                with pytest.raises(AuthError):
                    app.auth.identify(s, f"Bearer {k}")
    assert len(stmts) == 1, stmts  # <= one targeted query for the whole burst

    # Only the first probe confirmed absence; the gated ones were left unconfirmed.
    assert key_prefix(keys[0]) in app.auth._negcache
    assert key_prefix(keys[1]) not in app.auth._negcache


def test_negative_cache_overflow_clears_and_stays_correct(app):
    _warm_with_admin(app)
    app.auth._min_refresh = 0.0  # let the probe fire regardless of the throttle window

    # Fill the negative cache right up to its bound with dummy prefixes.
    app.auth._negcache.update(f"dummy_{i}" for i in range(_NEG_CACHE_MAX))
    assert len(app.auth._negcache) >= _NEG_CACHE_MAX

    # One more confirmed-absent prefix tips it over: _negcache_add drops the whole set
    # rather than grow unbounded, then records just this probe's prefixes.
    unknown = "incant_sk_" + "z" * 32
    with session_scope() as s:
        with pytest.raises(AuthError):
            app.auth.identify(s, f"Bearer {unknown}")
    assert len(app.auth._negcache) < _NEG_CACHE_MAX      # bounded: the flood was dropped
    assert key_prefix(unknown) in app.auth._negcache     # the fresh verdict was kept

    # Still correct: the just-recorded prefix answers from the negative cache — ZERO DB.
    with count_statements() as stmts:
        with session_scope() as s:
            with pytest.raises(AuthError):
                app.auth.identify(s, f"Bearer {unknown}")
    assert stmts == [], stmts
