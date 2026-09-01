"""The opportunistic legacy→`v2$` key re-hash must stay a one-shot, off the serving
hot path of a read-only replica.

`AuthCache._upgrade_hash` is the one auth-time write in the system. A serve replica
runs against a read-only DB role (DESIGN.md §15): there the write fails, is swallowed,
and — before this fix — re-fired on EVERY request carrying that key. Even on the full
node a transient DB error used to retry per request. Now: serve mode never opens the
write session, and a failed upgrade is remembered on the cached entry until the next
full reload rebuilds it from the DB."""

from __future__ import annotations

import time
from contextlib import contextmanager

import pytest
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

import incant.db as dbmod
from incant import db, models
from incant.config import Settings, get_settings, set_settings
from incant.db import session_scope
from incant.server.auth import AuthCache, Binding, _KeyEntry, hash_key, key_prefix
from incant.server.auth import ensure_bootstrap_admin
from incant.service import AppContext, reset_app

from .conftest import db_url_for, reset_schema
from .test_server import ADMIN

RAW = "incant_sk_" + "a" * 32


def _cache_with_legacy_entry() -> AuthCache:
    """A warm cache holding ONE key stored in legacy (pepper-less) form."""
    cache = AuthCache()
    entry = _KeyEntry(prefix=key_prefix(RAW), hash=hash_key(RAW, pepper=""), revoked=False,
                      expires_at=None, principal_id="p1", principal_name="one",
                      bindings=(Binding("admin", None, None),))
    cache._entries = {key_prefix(RAW): [entry]}
    cache._loaded = True
    cache._last_refresh = time.monotonic()   # suppress any DB refresh
    return cache


@pytest.fixture()
def settings(tmp_path):
    """Pepper configured (so the legacy entry `needs_upgrade`); mode set per test."""
    prior = get_settings()

    def _set(mode: str) -> None:
        set_settings(Settings(database_url=db_url_for(tmp_path), mode=mode, key_pepper="pep"))

    yield _set
    set_settings(prior)


@contextmanager
def _forbidden_scope():
    raise AssertionError("serve mode must never open a committing session on the auth path")
    yield  # pragma: no cover


def test_serve_mode_never_attempts_the_write(settings, monkeypatch):
    settings("serve")
    monkeypatch.setattr(dbmod, "session_scope", _forbidden_scope)
    cache = _cache_with_legacy_entry()
    for _ in range(3):
        assert cache.identify(None, f"Bearer {RAW}").has("admin")
    # The entry is untouched: still legacy, still eligible for the full node to upgrade.
    entry = cache._entries[key_prefix(RAW)][0]
    assert not entry.hash.startswith("v2$") and not entry.upgrade_failed


class _FakeRow:
    hash = "legacy"


class _FakeSession:
    def __init__(self, row):
        self._row = row

    def execute(self, stmt):
        row = self._row

        class _Result:
            def scalars(self):
                return self

            def first(self):
                return row
        return _Result()


def test_full_mode_upgrades_exactly_once(settings, monkeypatch):
    settings("full")
    calls: list[int] = []
    row = _FakeRow()

    @contextmanager
    def counting_scope():
        calls.append(1)
        yield _FakeSession(row)

    monkeypatch.setattr(dbmod, "session_scope", counting_scope)
    cache = _cache_with_legacy_entry()
    cache.identify(None, f"Bearer {RAW}")
    cache.identify(None, f"Bearer {RAW}")
    cache.identify(None, f"Bearer {RAW}")
    assert calls == [1]                                   # one write, not one per request
    assert row.hash.startswith("v2$")                     # the DB row was re-hashed…
    assert cache._entries[key_prefix(RAW)][0].hash == row.hash  # …and the cache agrees


def test_full_mode_remembers_a_failed_upgrade(settings, monkeypatch):
    settings("full")
    calls: list[int] = []

    @contextmanager
    def failing_scope():
        calls.append(1)
        raise SQLAlchemyError("db down")
        yield  # pragma: no cover

    monkeypatch.setattr(dbmod, "session_scope", failing_scope)
    cache = _cache_with_legacy_entry()
    for _ in range(5):
        assert cache.identify(None, f"Bearer {RAW}").has("admin")   # auth still succeeds
    assert calls == [1]                                              # …but ONE failed write
    entry = cache._entries[key_prefix(RAW)][0]
    assert entry.upgrade_failed and not entry.hash.startswith("v2$")


def test_failed_upgrade_retries_after_the_next_reload(tmp_path, monkeypatch):
    # Real DB: the bootstrap admin key is stored legacy; then a pepper arrives. A DB
    # failure during the upgrade is remembered until the TTL reload (what the
    # control-plane poll drives) rebuilds the entry from the DB — then it succeeds.
    set_settings(Settings(database_url=db_url_for(tmp_path), repo_path=str(tmp_path / "repo"),
                          bootstrap_admin_key=ADMIN))
    db.reset_engine(); reset_app(); reset_schema()
    AppContext().initialize()
    with session_scope() as s:
        ensure_bootstrap_admin(s, ADMIN)
    get_settings().key_pepper = "later-pepper"

    real_scope = dbmod.session_scope
    calls: list[str] = []

    @contextmanager
    def failing_scope():
        calls.append("fail")
        raise SQLAlchemyError("db down")
        yield  # pragma: no cover

    cache = AuthCache()
    prefix = key_prefix(ADMIN)
    with real_scope() as s:
        monkeypatch.setattr(dbmod, "session_scope", failing_scope)
        cache.identify(s, f"Bearer {ADMIN}")          # cold load + failed upgrade
        cache.identify(s, f"Bearer {ADMIN}")          # remembered: no second attempt
        assert calls == ["fail"]
        assert cache._entries[prefix][0].upgrade_failed
        monkeypatch.setattr(dbmod, "session_scope", real_scope)
        cache._last_refresh = 0.0                     # TTL elapsed…
        cache.refresh(s)                              # …the background reload rebuilds entries
        assert not cache._entries[prefix][0].upgrade_failed
        cache.identify(s, f"Bearer {ADMIN}")          # the upgrade is attempted again — and lands
        assert cache._entries[prefix][0].hash.startswith("v2$")
    with real_scope() as s:
        row = s.execute(select(models.ApiKey).where(models.ApiKey.prefix == prefix)).scalars().first()
        assert row.hash.startswith("v2$")
