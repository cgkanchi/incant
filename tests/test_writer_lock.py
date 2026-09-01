"""Single full-writer enforcement: one `full` node per database, claimed via a
session-level advisory lock at boot — and MONITORED afterwards (§15). The boot claim
is a statement about the past: the lock lives on one connection, and if that
connection dies the lock is silently gone while the node keeps writing. So the
control-poll loop re-checks the role every tick, re-claims a blip, and fail-stops
when another node holds it. Every scenario here runs against a real Postgres: the
pg_locks key split, backend termination, contention from a second connection."""

from __future__ import annotations

import asyncio
import sys

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import OperationalError

from incant import db
from incant.config import Settings, set_settings
from incant.server import metrics

from .conftest import db_url_for
from .test_server import auth, make_client


@pytest.fixture()
def pg(tmp_path):
    set_settings(Settings(database_url=db_url_for(tmp_path), repo_path=str(tmp_path / "repo")))
    db.reset_engine()
    yield
    db.release_full_writer_role()
    db.reset_engine()


def _lock_pid() -> int:
    return db._writer_conn.execute(select(func.pg_backend_pid())).scalar()


def _terminate(pid: int) -> None:
    """What idle_in_transaction_session_timeout, a Postgres restart or a proxy reset
    do to the lock connection — from the outside, without telling the process."""
    with db.engine().connect() as other:
        other.execute(select(func.pg_terminate_backend(pid)))


def _hold_from_elsewhere():
    """A second node's claim: its own autocommit connection holding the lock."""
    other = db.engine().connect().execution_options(isolation_level="AUTOCOMMIT")
    assert other.execute(select(func.pg_try_advisory_lock(db._FULL_WRITER_LOCK_ID))).scalar() is True
    return other


def _let_go(other) -> None:
    other.execute(select(func.pg_advisory_unlock(db._FULL_WRITER_LOCK_ID)))
    other.close()


def test_second_full_writer_is_refused(pg):
    db.claim_full_writer_role()
    # A competing node (simulated on its own connection) cannot take the lock…
    with db.engine().connect() as other:
        got = other.execute(
            select(func.pg_try_advisory_lock(db._FULL_WRITER_LOCK_ID))).scalar()
        assert got is False
    # …until the owner releases it.
    db.release_full_writer_role()
    with db.engine().connect() as other:
        got = other.execute(
            select(func.pg_try_advisory_lock(db._FULL_WRITER_LOCK_ID))).scalar()
        assert got is True
        other.execute(select(func.pg_advisory_unlock(db._FULL_WRITER_LOCK_ID)))


def test_claim_is_idempotent_within_the_process(pg):
    db.claim_full_writer_role()
    db.claim_full_writer_role()   # the same process already owns the role — no error
    db.release_full_writer_role()


def test_contention_is_its_own_error_type(pg):
    other = _hold_from_elsewhere()
    try:
        with pytest.raises(db.WriterRoleTaken):
            db.claim_full_writer_role()
    finally:
        _let_go(other)


def test_role_is_held_after_claim_and_the_lock_connection_sits_idle(pg):
    """The pg_locks probe finds our 64-bit key (classid = high word, objid = low word,
    objsubid = 1) on our own backend — and that backend is plain `idle`, never `idle in
    transaction`: the lock connection is autocommit, so hosted Postgres' idle-in-
    transaction timeout has nothing to terminate and vacuum sees no pinned xmin."""
    assert db.writer_role_held() is False           # nothing claimed yet
    db.claim_full_writer_role()
    assert db.writer_role_held() is True
    pid = _lock_pid()
    with db.engine().connect() as other:
        state = other.execute(
            text("SELECT state FROM pg_stat_activity WHERE pid = :pid"), {"pid": pid}).scalar()
    assert state == "idle"


def test_terminated_lock_backend_is_detected_and_reclaimed_when_free(pg):
    db.claim_full_writer_role()
    old_pid = _lock_pid()
    _terminate(old_pid)
    assert db.writer_role_held() is False           # dead connection ⇒ lock gone
    db.reclaim_full_writer_role()                   # nobody else claimed: heals
    assert db.writer_role_held() is True
    assert _lock_pid() != old_pid                   # on a fresh backend


def test_reclaim_refuses_when_another_node_took_the_role(pg):
    db.claim_full_writer_role()
    _terminate(_lock_pid())
    other = _hold_from_elsewhere()
    try:
        with pytest.raises(db.WriterRoleTaken):
            db.reclaim_full_writer_role()
        assert db.writer_role_held() is False
    finally:
        _let_go(other)


# ── the node's reaction (through the real app) ────────────────────────

@pytest.fixture()
def node(tmp_path, monkeypatch):
    """A booted full node whose fail-stop action is a recorder, not a SIGTERM."""
    appmod = sys.modules["incant.server.app"]  # the package exports `app` = the instance
    terminated = []
    monkeypatch.setattr(appmod, "_terminate_process", lambda: terminated.append(True))
    with make_client(tmp_path) as client:
        client.terminated = terminated
        client.appmod = appmod
        yield client


def _render(client):
    return client.post("/prompt/support/system", headers=auth(client.renderer_key),
                       json={"environment": "prod", "flags": {},
                             "variables": {"customer_name": "Acme", "history": []}})


def test_boot_holds_the_role_and_a_healthy_tick_keeps_it(node):
    assert db.writer_role_held() is True
    assert metrics.writer_lock_held._value.get() == 1
    node.appmod._writer_role_pass(node.app)
    assert db.writer_role_held() is True and not node.terminated
    assert node.get("/readyz").status_code == 200 and node.get("/healthz").status_code == 200


def test_lost_role_taken_by_another_node_fail_stops(node):
    _terminate(_lock_pid())
    other = _hold_from_elsewhere()
    try:
        node.appmod._writer_role_pass(node.app)
    finally:
        _let_go(other)
    # The injected fail-stop ran exactly once, and everything it promises is true:
    assert node.terminated == [True]
    assert db.writer_role_lost() is True
    assert node.app.state.ready is False
    assert metrics.writer_lock_held._value.get() == 0
    assert node.get("/readyz").status_code == 503
    h = node.get("/healthz")
    assert h.status_code == 503 and h.json()["status"] == "writer_role_lost"
    # Management writes are refused — the writing session dependency says why…
    r = node.post("/mgmt/envs", json={"id": "scratch"}, headers=auth())
    assert r.status_code == 503 and "writer role lost" in r.json()["detail"]
    # …while the read-only serving path keeps answering until the listener closes.
    assert _render(node).status_code == 200
    # A later tick has nothing more to decide (no second fail-stop).
    node.appmod._writer_role_pass(node.app)
    assert node.terminated == [True]


def test_writer_loops_sit_out_once_the_role_is_lost(node):
    """Every writer loop runs its pass through the one gate; once the role is lost the
    pass is skipped outright — a flush, census, reconcile or push from a node that no
    longer owns the database could race the rightful writer."""
    calls = []
    assert asyncio.run(node.appmod._writer_pass(lambda tag: calls.append(tag) or tag, "before")) == "before"
    db.mark_writer_role_lost()
    assert asyncio.run(node.appmod._writer_pass(calls.append, "after")) is None
    assert calls == ["before"]                      # skipped, not run
    # Concretely: the observed-flags writer leaves a queued observation queued.
    from incant.service import get_app
    node.post("/prompt/support/system", headers=auth(node.renderer_key),
              json={"environment": "prod", "flags": {"tier": "pro"},
                    "variables": {"customer_name": "Acme", "history": []}})
    assert get_app().observer.pending_size() == 1
    asyncio.run(node.appmod._writer_pass(node.appmod._observed_flags_pass, get_app()))
    assert get_app().observer.pending_size() == 1


def test_postgres_outage_on_reclaim_is_not_contention(node, monkeypatch):
    """A dead lock connection plus an unreachable Postgres is an OUTAGE: nobody can take
    the lock from a server that is down, and §10 serves frozen through outages — so no
    fail-stop, just a warning and a retry next tick, which wins the role back."""
    _terminate(_lock_pid())
    real = node.appmod.reclaim_full_writer_role

    def unreachable():
        raise OperationalError("connection refused", None, None)
    monkeypatch.setattr(node.appmod, "reclaim_full_writer_role", unreachable)
    node.appmod._writer_role_pass(node.app)
    assert not node.terminated
    assert db.writer_role_lost() is False and node.app.state.ready is True
    assert metrics.writer_lock_held._value.get() == 0  # honest meanwhile
    assert node.get("/readyz").status_code == 200
    # Postgres answers again: the next tick re-claims.
    monkeypatch.setattr(node.appmod, "reclaim_full_writer_role", real)
    node.appmod._writer_role_pass(node.app)
    assert db.writer_role_held() is True
    assert metrics.writer_lock_held._value.get() == 1 and not node.terminated
