"""Snapshot-cache coherence: identity, ordering, and replay-overlay freshness.

Three verified bugs pinned here:

* **ABA on environment ids** — ``rules_version``/``content_version`` restart at 1
  when an environment is deleted and recreated under the same id, so a replica whose
  poll straddled the delete+recreate saw identical counters and kept serving the DEAD
  environment's snapshot (and its §9 replay memos) forever. ``incarnation`` — an
  immutable per-row identity — closes it: a changed incarnation is evict-then-rebuild,
  never "confirmed".
* **A slow poll regressing the cache** — ``_plan_refresh`` builds detached entries
  from an older DB read; applying them unconditionally could overwrite the newer
  snapshot a concurrent write had already installed. The swap is now
  generation-checked: never backward, while a forced equal-keys rebuild (an
  env-settings edit moves no counter) still lands.
* **Replay memos freezing current-state overlays** — a memoized historical snapshot
  captured ``servable`` and ``refinement_defaults`` at first build, so a SHA validated
  LATER was wrongly 409'd on replay (and defaults served stale). Both overlays now
  attach fresh from the current snapshot on every ``snapshot_at`` return.

Plus the lower-severity race: the ``_historical`` LRU ops run on request threads while
the poll thread evicts — a lock now guards them (stress-tested below).
"""

from __future__ import annotations

import threading
import time

import pytest
from sqlalchemy import delete

from incant import models
from incant.db import session_scope
from incant.service import AppContext, ServingError

from .test_integration import _author_version
from .test_integration import app as _integration_app

# Re-bound under the name pytest looks up (same idiom as test_propagation).
app = _integration_app

PID = "support/system"


def _recreate_env_with_same_counters(env_id: str = "prod") -> tuple[str, tuple[int, int]]:
    """Simulate a delete+recreate landing BETWEEN two polls: every env-scoped row and
    the environment itself vanish, then a brand-new row appears wearing the same id
    and the exact same freshness counters — the ABA a counter-only comparison cannot
    see. Returns the dead row's incarnation and the (rules, content) counters."""
    from incant.server.mgmt.admin import _ENV_SCOPED_MODELS

    with session_scope() as s:
        row = s.get(models.Environment, env_id)
        old_incarnation, counters = row.incarnation, (row.rules_version, row.content_version)
    with session_scope() as s:
        for m in _ENV_SCOPED_MODELS:
            s.execute(delete(m).where(m.environment_id == env_id))
        s.execute(delete(models.Environment).where(models.Environment.id == env_id))
    with session_scope() as s:  # a separate transaction: genuinely delete THEN recreate
        s.add(models.Environment(id=env_id, name=env_id,
                                 rules_version=counters[0], content_version=counters[1]))
    return old_incarnation, counters


# ── F2: environment-id reuse (ABA) ───────────────────────────────────────────


def test_recreated_env_with_identical_counters_is_rebuilt_not_confirmed(app):
    _author_version(app, PID, 1, "life-one")
    replica = AppContext()                       # a second node on the same DB + repo
    with session_scope() as s:
        before = replica.get_snapshot(s, "prod")
    assert before.defaults == {PID: 1}

    old_incarnation, counters = _recreate_env_with_same_counters()

    with session_scope() as s:
        replica.refresh_control_plane(s)
        after = replica.get_snapshot(s, "prod")
    entry = replica._snapshots["prod"]
    # The counters are byte-identical to what the replica cached — only the row
    # identity says this is a different environment. That must be enough.
    assert (entry.rules_version, entry.content_version) == counters
    assert entry.incarnation != old_incarnation
    assert after is not before
    assert after.defaults == {}                  # the new life's state, not the dead row's


def test_recreated_env_drops_the_old_lifes_replay_memos(app):
    _author_version(app, PID, 1, "life-one")
    with session_scope() as s:
        first = app.serve(s, "prod", PID, {}, {})
    rv = first["rules_version"]
    _author_version(app, PID, 2, "life-one v2")  # rv becomes historical
    with session_scope() as s:
        replayed = app.serve(s, "prod", PID, {}, {}, pin_rules_version=rv)
    assert replayed["prompt"] == "life-one"
    assert any(k[0] == "prod" for k in app._historical)   # the memo is cached

    _recreate_env_with_same_counters()
    with session_scope() as s:
        app.refresh_control_plane(s)
    # The memos died with the row — both the snapshots and the memoized refusals
    # (a stale refusal would 422 a genuinely replayable pin of the new life).
    assert not any(k[0] == "prod" for k in app._historical)
    assert not any(k[0] == "prod" for k in app._unreplayable)

    # The new life never recorded revision ``rv``: replaying it answers honestly
    # (422, pin.versions alternative) instead of resurrecting the deleted
    # environment's targeting from the memo.
    with session_scope() as s:
        with pytest.raises(ServingError) as ei:
            app.serve(s, "prod", PID, {}, {}, pin_rules_version=rv)
    assert ei.value.status == 422
    assert "no recorded targeting state" in ei.value.detail


# ── F5: an older refresh must never overwrite a newer snapshot ───────────────


def test_apply_refresh_never_moves_the_cache_backward(app):
    _author_version(app, PID, 1, "one")
    replica = AppContext()
    with session_scope() as s:
        replica.get_snapshot(s, "prod")
        # A poll's plan: a detached build from THIS (soon to be stale) DB state.
        stale_plan = replica._plan_refresh(s, force=("prod",))
    assert "prod" in stale_plan.rebuilt

    # While that plan was in flight, a write landed and a fresher pass installed it.
    _author_version(app, PID, 2, "two")
    with session_scope() as s:
        replica.refresh_control_plane(s)
    newer = replica._snapshots["prod"]
    stale_entry = stale_plan.rebuilt["prod"]
    assert newer.rules_version > stale_entry.rules_version

    before_confirm = newer.confirmed_at
    replica._apply_refresh(stale_plan)           # the slow poll finally applies…
    assert replica._snapshots["prod"] is newer   # …and yields: never backward
    assert replica._snapshots["prod"].snapshot.version_info(PID, 2) is not None
    # The stale plan's DB read still proved the env exists → freshness re-confirmed
    # (the §14 age gauge must not climb because a skip was the right call).
    assert replica._snapshots["prod"].confirmed_at >= before_confirm


def test_forced_plan_with_equal_keys_still_installs(app):
    """An env-settings edit moves no counter, yet its forced rebuild must land — the
    never-backward check compares strictly, not >=; equal keys install."""
    _author_version(app, PID, 1, "one")
    with session_scope() as s:
        old_snap = app.get_snapshot(s, "prod")
        plan = app._plan_refresh(s, force=("prod",))
        app._apply_refresh(plan)
    assert app._snapshots["prod"].snapshot is not old_snap


# ── F4: replay memos must not freeze current-state overlays ──────────────────


def test_replay_honours_shas_and_defaults_that_arrived_after_the_memo(app):
    _author_version(app, PID, 1, "v1a")
    with session_scope() as s:
        first = app.serve(s, "prod", PID, {}, {})
    rv = first["rules_version"]
    _author_version(app, PID, 2, "v2 content")   # rv becomes historical
    with session_scope() as s:
        replayed = app.serve(s, "prod", PID, {}, {}, pin_rules_version=rv)
    assert replayed["prompt"] == "v1a"           # memo cached with TODAY's overlays

    # Content moves with NO targeting change: a new SHA validates on v1 and a
    # variable default appears — both strictly AFTER the memo was built.
    with session_scope() as s:
        reg = app.registry(s, "sam")
        d = reg.create_draft(PID, version_number=1, author="sam", content="v1b {{ x }}")
        out = reg.commit_draft(d.id, author="sam")
        reg.set_refinement(PID, 1, "x", type="string", required=False, default="dflt")
    assert out.validation["status"] == "valid"
    app.invalidate("prod")                       # current snapshot rebuilds; memo remains

    # The SAME historical pin plus a pin.versions entry naming the new SHA: the
    # frozen-overlay bug 409'd this ("not a validated commit") and would have
    # rendered without the new default. Both overlays now attach fresh per call.
    with session_scope() as s:
        resp = app.serve(s, "prod", PID, {}, {},
                         pin={PID: (1, out.sha)}, pin_rules_version=rv)
    assert resp["prompt"] == "v1b dflt"
    assert resp["versions"][PID] == {"version": 1, "commit": out.sha}


# ── Lower: the replay LRU is shared across threads ───────────────────────────


def test_replay_lru_ops_survive_concurrent_churn(app, monkeypatch):
    """Request threads do get/move_to_end/popitem on ``_historical`` while the poll
    thread evicts; unsynchronized OrderedDict mutation raised sporadic KeyError (a
    500 on a healthy replay). With the lock this must never raise. Probabilistic by
    nature — a tiny LRU bound plus a churner maximizes the interleavings."""
    import incant.service as service_mod
    monkeypatch.setattr(service_mod, "_HISTORICAL_CACHE_MAX", 2)

    _author_version(app, PID, 1, "v1")
    with session_scope() as s:
        tgt = app.targeting(s, "op")
        for i in range(4):                      # four replayable historical revisions
            tgt.upsert_rule("prod", {"id": f"r{i}", "scope": "prompt", "prompt_id": PID,
                                     "priority": 10 + i, "when": None,
                                     "serve": {"version": 1}})
    app.invalidate("prod")
    with session_scope() as s:
        current_rv = app.get_snapshot(s, "prod").rules_version
    rvs = [current_rv - d for d in (1, 2, 3, 4)]

    errors: list[BaseException] = []
    stop = threading.Event()

    def replayer() -> None:
        try:
            for i in range(60):
                with session_scope() as s:
                    app.snapshot_at(s, "prod", rvs[i % len(rvs)])
        except BaseException as exc:  # noqa: BLE001 — the assertion IS "no exception"
            errors.append(exc)

    def churner() -> None:
        try:
            while not stop.is_set():
                app._drop_replay_states("prod")
                time.sleep(0.001)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    workers = [threading.Thread(target=replayer) for _ in range(3)]
    churn = threading.Thread(target=churner)
    churn.start()
    for t in workers:
        t.start()
    for t in workers:
        t.join()
    stop.set()
    churn.join()
    assert errors == []
