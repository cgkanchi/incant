"""Refined warm-failure criterion (§10): a live pointer with no servable content at
all fails warming; a servable fallback degrades (warns) but succeeds; missing tips and
pointer-less versions are tolerated silently."""

from __future__ import annotations

import datetime as dt
import logging

import pytest
from sqlalchemy.exc import SQLAlchemyError

from incant import db, models
from incant.config import Settings, set_settings
from incant.db import session_scope
from incant.server.app import _warm_all
from incant.service import AppContext, WarmError, reset_app

from .conftest import db_url_for, reset_schema

_T0 = dt.datetime(2020, 1, 1, tzinfo=dt.timezone.utc)
_T1 = _T0 + dt.timedelta(minutes=1)


class FakeContent:
    """Content-store stub: ``warm`` succeeds only for SHAs in ``servable``."""

    def __init__(self, servable):
        self.servable = set(servable)
        self.warmed: list[tuple[str, int, str]] = []

    def warm(self, prompt_id, version, sha):
        if sha not in self.servable:
            raise KeyError(sha)
        self.warmed.append((prompt_id, version, sha))
        return object()


@pytest.fixture()
def app(tmp_path):
    set_settings(Settings(database_url=db_url_for(tmp_path), repo_path=str(tmp_path / "repo")))
    db.reset_engine()
    reset_app()
    reset_schema()
    ctx = AppContext()
    ctx.initialize()
    with session_scope() as s:
        s.add(models.Environment(id="prod", name="prod"))
        s.add(models.Project(id="support", name="support"))
        s.flush()
        s.add(models.Prompt(id="support/system", project_id="support"))
    return ctx


def _version(s, number=1, prompt_id="support/system"):
    s.add(models.Version(prompt_id=prompt_id, number=number))


def _pointer(s, to_sha, moved_at, version=1, prompt_id="support/system", env="prod"):
    s.add(models.PointerMove(environment_id=env, prompt_id=prompt_id,
                             version_number=version, to_sha=to_sha, moved_at=moved_at))


def _validation(s, sha, version=1, prompt_id="support/system"):
    s.add(models.CommitValidation(sha=sha, blob_sha="b" + sha,
                                  path=f"{prompt_id}/v{version}.j2", prompt_id=prompt_id,
                                  version_number=version, status="valid"))


def test_warm_fails_when_live_and_all_fallbacks_unfetchable(app):
    # A live pointer (sha_live, fallback sha_prev) but NOTHING is fetchable → WarmError.
    with session_scope() as s:
        _version(s)
        _pointer(s, "sha_prev", _T0)
        _pointer(s, "sha_live", _T1)
    app.content = FakeContent(servable=set())
    with pytest.raises(WarmError) as ei:
        with session_scope() as s:
            app.warm(s, "prod")
    assert ei.value.prompt_id == "support/system"
    assert ei.value.version == 1
    assert ei.value.sha == "sha_live"


def test_warm_degrades_when_live_missing_but_fallback_present(app, caplog):
    # Live SHA unfetchable but a previous-live fallback warms → WARNING, still succeeds.
    with session_scope() as s:
        _version(s)
        _pointer(s, "sha_prev", _T0)
        _pointer(s, "sha_live", _T1)
    app.content = FakeContent(servable={"sha_prev"})
    with caplog.at_level(logging.WARNING, logger="incant.service"):
        with session_scope() as s:
            app.warm(s, "prod")  # no raise
    assert "previous-live" in caplog.text
    assert ("support/system", 1, "sha_prev") in app.content.warmed


def test_warm_tolerates_missing_tip_with_no_live_pointer(app, caplog):
    # A committed tip that was never made live: live_sha is None, so a missing tip is
    # best-effort and warming succeeds silently (no WarmError, no warning).
    with session_scope() as s:
        _version(s)
        _validation(s, "tip_sha")  # gives the version a tip_sha, but no pointer move
    app.content = FakeContent(servable=set())
    with caplog.at_level(logging.WARNING, logger="incant.service"):
        with session_scope() as s:
            app.warm(s, "prod")  # must not raise
    assert "previous-live" not in caplog.text


def test_warm_succeeds_when_live_is_fetchable(app):
    with session_scope() as s:
        _version(s)
        _pointer(s, "sha_live", _T0)
    app.content = FakeContent(servable={"sha_live"})
    with session_scope() as s:
        app.warm(s, "prod")
    assert ("support/system", 1, "sha_live") in app.content.warmed


def test_warm_all_reports_not_ready_then_ready(app):
    # _warm_all turns a WarmError into "not ready"; the retry loop keeps calling it.
    with session_scope() as s:
        _version(s)
        _pointer(s, "sha_prev", _T0)
        _pointer(s, "sha_live", _T1)
    app.content = FakeContent(servable=set())
    assert _warm_all(app) is False                       # nothing servable → not ready
    app.content = FakeContent(servable={"sha_live", "sha_prev"})
    assert _warm_all(app) is True                        # content available → ready


class BoomSession:
    """A session whose every DB access raises — proves the render hot path is DB-free
    (§8/§10). If get_snapshot touches it at all, the test fails loudly."""

    def execute(self, *a, **k):
        raise SQLAlchemyError("db down")

    def get(self, *a, **k):
        raise SQLAlchemyError("db down")

    def rollback(self, *a, **k):
        pass


def test_warm_installs_snapshot_for_zero_db_serving(app):
    # Issue 1: warm() must INSTALL the snapshot it built, so the first render after
    # readiness is a pure memory hit — not a cold snapshot build (a DB read) that would
    # 503 a "ready" node the instant Postgres died.
    with session_scope() as s:
        _version(s)
        _pointer(s, "sha_live", _T0)
    app.content = FakeContent(servable={"sha_live"})
    with session_scope() as s:
        app.warm(s, "prod")

    # A session that raises on ANY DB call still yields the warmed snapshot — no DB read —
    # and it is NOT stale (the DB is healthy; the freeze flag only sets on an observed
    # outage, and the cached snapshot was never mutated).
    snap = app.get_snapshot(BoomSession(), "prod")
    assert snap.environment == "prod"
    assert snap.stale is False


def test_warm_does_not_install_snapshot_on_failure(app):
    # The install sits on the happy path only: a WarmError (nothing servable) must leave
    # NO cached snapshot, so serving falls through to the cold build rather than caching an
    # environment the node cannot actually serve.
    with session_scope() as s:
        _version(s)
        _pointer(s, "sha_live", _T0)
    app.content = FakeContent(servable=set())
    with pytest.raises(WarmError):
        with session_scope() as s:
            app.warm(s, "prod")
    # Nothing cached → get_snapshot over a dead session raises the cold-miss 503.
    from incant.service import ServingError
    with pytest.raises(ServingError):
        app.get_snapshot(BoomSession(), "prod")
