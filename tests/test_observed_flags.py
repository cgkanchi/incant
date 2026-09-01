"""§7 observed flags: the serving API's flag values feed the targeting composer.

Three layers, each pinned here: the request-path observer (pure, no DB — dedupe,
typing, caps, suppression), the writer (one guarded upsert), the census (TTL prune +
high-cardinality suppression), then the endpoints and the spec merge end to end
through the real app. The hot path contract — observe() never touches the DB and
costs dict ops — is asserted directly.
"""

from __future__ import annotations

import datetime as dt
import time

import pytest
from sqlalchemy import select, text

from incant import models
from incant.config import Settings
from incant.db import session_scope
from incant.service import AppContext, get_app
from incant.targeting.observed import (
    MAX_VALUE_LEN, FlagObserver, Observation, flush_observations, forget_flag,
    load_suppressions, prune_and_census, record_suppressions, scalar_of, typed_value,
)

from .test_server import auth, make_client, make_key


# ── observer (pure) ──────────────────────────────────────────────────

class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


def _obs(clock=None, **kw):
    kw.setdefault("dedupe_seconds", 60)
    kw.setdefault("max_pending", 100)
    kw.setdefault("value_cap", 50)
    kw.setdefault("exclude", {"secret"})
    return FlagObserver(enabled=True, clock=clock or Clock(), **kw)


def test_scalar_typing_and_inverse():
    assert scalar_of(True) == ("bool", "true")
    assert scalar_of(0) == ("int", "0")
    assert scalar_of(2.5) == ("float", "2.5")
    assert scalar_of("x") == ("str", "x")
    for bad in ([1], {"k": 1}, None, "", float("nan"), float("inf")):
        assert scalar_of(bad) is None, bad
    for raw in (True, False, 7, -3, 2.5, "pro"):
        vt, txt = scalar_of(raw)
        assert typed_value(txt, vt) == raw and type(typed_value(txt, vt)) is type(raw)


def test_observe_records_scalars_only_and_types_them():
    o = _obs()
    n = o.observe("prod", {"a": 1, "b": True, "c": 2.5, "d": "x", "e": [1], "f": {"k": 1},
                           "g": None, "h": ""})
    assert n == 4
    got = {(x.flag, x.value, x.value_type) for x in o.drain()}
    assert got == {("a", "1", "int"), ("b", "true", "bool"), ("c", "2.5", "float"), ("d", "x", "str")}
    assert o.stats.ignored == 4 and o.pending_size() == 0


def test_excluded_and_overlong_values_never_recorded():
    o = _obs()
    assert o.observe("prod", {"secret": "s3", "ok": "v", "long": "x" * (MAX_VALUE_LEN + 1)}) == 1
    assert [x.flag for x in o.drain()] == ["ok"]


def test_dedupe_window_then_requeue(clock=None):
    clock = Clock()
    o = _obs(clock)
    assert o.observe("prod", {"plan": "pro"}) == 1
    assert o.observe("prod", {"plan": "pro"}) == 0          # within the window: a hit
    assert o.stats.deduped == 1
    o.drain()
    clock.t += 61
    assert o.observe("prod", {"plan": "pro"}) == 1          # window expired: queued again
    # Different environment is a different key.
    assert o.observe("staging", {"plan": "pro"}) == 1


def test_queue_bound_drops_never_grows():
    o = _obs(max_pending=3)
    assert o.observe("prod", {"a": 1, "b": 2, "c": 3, "d": 4}) == 3
    assert o.stats.dropped == 1 and o.pending_size() == 3
    # The dropped value was NOT marked seen, so it queues once there is room.
    o.drain()
    assert o.observe("prod", {"d": 4}) == 1


def test_unmark_lets_a_failed_batch_requeue():
    o = _obs()
    o.observe("prod", {"plan": "pro"})
    batch = o.drain()
    assert o.observe("prod", {"plan": "pro"}) == 0          # still marked
    o.unmark(batch)
    assert o.observe("prod", {"plan": "pro"}) == 1


def test_local_suppression_trips_past_the_cap():
    o = _obs(value_cap=5)
    for i in range(5):
        assert o.observe("prod", {"uid": f"u{i}", "plan": "pro" if i else "team"}) >= 1
    assert not o.is_suppressed("prod", "uid")
    o.observe("prod", {"uid": "u5"})                        # 6th distinct value
    assert o.is_suppressed("prod", "uid")
    assert o.take_new_suppressions() == {("prod", "uid")}
    assert o.take_new_suppressions() == set()
    pending = o.drain()
    assert all(x.flag != "uid" for x in pending)            # its queued values were purged
    assert {x.flag for x in pending} == {"plan"}
    assert o.observe("prod", {"uid": "u6"}) == 0            # dropped at the source
    assert not o.is_suppressed("prod", "plan")


def test_sweep_evicts_expired_marks_and_distinct_counts():
    clock = Clock()
    o = _obs(clock, value_cap=3)
    o.observe("prod", {"a": "1", "b": "2"})
    o.drain()
    assert o.seen_size() == 2
    clock.t += 61
    assert o.sweep() == 2 and o.seen_size() == 0
    # Distinct counts fell back too: three new values do not trip a cap of 3.
    o.observe("prod", {"a": "x"}); o.observe("prod", {"a": "y"}); o.observe("prod", {"a": "z"})
    assert not o.is_suppressed("prod", "a")


def test_disabled_observer_is_a_noop():
    o = FlagObserver(enabled=False)
    assert o.observe("prod", {"a": 1}) == 0 and o.pending_size() == 0


def test_serve_mode_context_has_observer_disabled(tmp_path):
    ctx = AppContext(Settings(mode="serve", repo_path=str(tmp_path / "repo"),
                              database_url="postgresql+psycopg://x:y@localhost/z"))
    assert ctx.observer.enabled is False
    ctx = AppContext(Settings(mode="full", observe_flags=False, repo_path=str(tmp_path / "repo")))
    assert ctx.observer.enabled is False


def test_hot_path_budget_is_dict_ops():
    """Steady state (every value already seen) must cost dict lookups only. Generous
    budget so CI noise cannot fail it; a stray lock or allocation per flag would."""
    o = _obs(max_pending=10_000)
    flags = {f"f{i}": f"v{i}" for i in range(10)}
    o.observe("prod", flags)
    start = time.perf_counter()
    for _ in range(10_000):
        o.observe("prod", flags)
    elapsed = time.perf_counter() - start
    assert elapsed < 1.0, f"10k×10-flag observes took {elapsed:.2f}s"
    assert o.stats.deduped == 100_000


# ── writer + census (DB) ─────────────────────────────────────────────

@pytest.fixture()
def client(tmp_path):
    with make_client(tmp_path) as c:
        yield c


def _now():
    return dt.datetime.now(dt.timezone.utc)


def _rows(env="prod", flag=None):
    with session_scope() as s:
        q = select(models.ObservedFlag).where(models.ObservedFlag.environment_id == env)
        if flag:
            q = q.where(models.ObservedFlag.flag == flag)
        return [(r.flag, r.value, r.value_type, r.last_seen) for r in s.execute(q).scalars()]


def test_flush_upserts_and_guards_the_refresh(client):
    t0 = _now()
    with session_scope() as s:
        assert flush_observations(s, [Observation("prod", "plan", "pro", "str", t0),
                                      Observation("prod", "beta", "true", "bool", t0)]) == 2
    assert {(f, v, t) for f, v, t, _ in _rows()} >= {("plan", "pro", "str"), ("beta", "true", "bool")}
    # 30 minutes later: inside the guard, no refresh (rowcount 0, last_seen unchanged).
    with session_scope() as s:
        assert flush_observations(s, [Observation("prod", "plan", "pro", "str",
                                                  t0 + dt.timedelta(minutes=30))]) == 0
    assert [ls for f, v, _, ls in _rows(flag="plan")] == [t0]
    # 2 hours later: refreshed.
    with session_scope() as s:
        assert flush_observations(s, [Observation("prod", "plan", "pro", "str",
                                                  t0 + dt.timedelta(hours=2))]) == 1
    assert [ls for f, v, _, ls in _rows(flag="plan")] == [t0 + dt.timedelta(hours=2)]


def test_flush_skips_unknown_environment_instead_of_failing_the_batch(client):
    with session_scope() as s:
        assert flush_observations(s, [Observation("gone", "plan", "pro", "str", _now()),
                                      Observation("prod", "plan", "team", "str", _now())]) == 1
    assert [(f, v) for f, v, _, _ in _rows(flag="plan")] == [("plan", "team")]
    with session_scope() as s:
        assert flush_observations(s, []) == 0


def test_census_prunes_stale_and_suppresses_high_cardinality(client):
    old = _now() - dt.timedelta(days=40)
    with session_scope() as s:
        flush_observations(s, [Observation("prod", "stale", f"s{i}", "str", old) for i in range(3)]
                              + [Observation("prod", "uid", f"u{i}", "str", _now()) for i in range(60)]
                              + [Observation("prod", "plan", "pro", "str", _now())])
    with session_scope() as s:
        res = prune_and_census(s, ttl_days=30, value_cap=50)
    assert res.pruned == 3
    assert res.suppressed == [("prod", "uid", 60)] and res.total_suppressed == 1
    assert _rows(flag="stale") == [] and _rows(flag="uid") == []
    assert [(f, v) for f, v, _, _ in _rows(flag="plan")] == [("plan", "pro")]
    with session_scope() as s:
        assert load_suppressions(s) == {("prod", "uid")}
        sup = s.get(models.ObservedFlagSuppression, ("prod", "uid"))
        assert sup.values_seen == 60
    # Suppression rows survive the TTL prune; a second census is idempotent.
    with session_scope() as s:
        res = prune_and_census(s, ttl_days=30, value_cap=50)
    assert res.pruned == 0 and res.suppressed == [] and res.total_suppressed == 1
    # forget clears both.
    with session_scope() as s:
        forget_flag(s, "prod", "uid")
        assert load_suppressions(s) == set()


def test_record_suppressions_purges_values_and_is_idempotent(client):
    with session_scope() as s:
        flush_observations(s, [Observation("prod", "uid", "u1", "str", _now())])
        record_suppressions(s, [("prod", "uid"), ("gone", "x")], 50)
        record_suppressions(s, [("prod", "uid")], 50)
        assert load_suppressions(s) == {("prod", "uid")}
    assert _rows(flag="uid") == []


# ── end to end through the app ───────────────────────────────────────

def _render(client, flags, key=None, prompt="support/system", env="prod"):
    r = client.post(f"/prompt/{prompt}", headers=auth(key or client.renderer_key),
                    json={"environment": env, "flags": flags,
                          "variables": {"customer_name": "Acme", "history": []}})
    assert r.status_code == 200, r.text
    return r.json()


def _flush(client):
    from incant.server.app import _observed_flags_pass
    return _observed_flags_pass(get_app())


def test_render_observes_then_lists_and_searches(client):
    _render(client, {"tier": "pro", "region": "eu", "beta_opt_in": True, "n": 3})
    _render(client, {"tier": "gold", "region": "eu"})
    assert get_app().observer.pending_size() == 5
    assert _flush(client) == 5
    assert get_app().observer.pending_size() == 0

    r = client.get("/mgmt/envs/prod/flags", headers=auth())
    assert r.status_code == 200, r.text
    flags = {f["name"]: f for f in r.json()["flags"]}
    assert flags["tier"]["values_seen"] == 2 and flags["tier"]["in_rules"] is True
    assert flags["region"]["values_seen"] == 1 and flags["region"]["last_seen"]
    assert flags["n"]["in_rules"] is False and flags["n"]["suppressed"] is False
    # Flags rules consult appear even with zero traffic.
    assert flags["user_id"]["in_rules"] is True and flags["user_id"]["values_seen"] == 0

    r = client.get("/mgmt/envs/prod/flags/tier/values", headers=auth())
    vals = {v["value"]: v for v in r.json()["values"]}
    assert vals["gold"]["sources"] == ["traffic"]
    assert vals["pro"]["sources"] == ["traffic", "rules"]           # seen AND named by a rule
    assert vals["enterprise"]["sources"] == ["rules"] and vals["enterprise"]["last_seen"] is None
    # Typed round-trip: the composer gets JSON types back, not strings.
    r = client.get("/mgmt/envs/prod/flags/beta_opt_in/values", headers=auth())
    assert r.json()["values"][0]["value"] is True and r.json()["values"][0]["value_type"] == "bool"
    r = client.get("/mgmt/envs/prod/flags/n/values", headers=auth())
    assert r.json()["values"][0]["value"] == 3


def test_values_search_prefix_first_then_infix_then_recency(client):
    t = _now()
    with session_scope() as s:
        flush_observations(s, [
            Observation("prod", "plan", "annual-team", "str", t - dt.timedelta(minutes=3)),
            Observation("prod", "plan", "team", "str", t - dt.timedelta(minutes=2)),
            Observation("prod", "plan", "team-plus", "str", t - dt.timedelta(minutes=1)),
            Observation("prod", "plan", "solo", "str", t),
        ])
    r = client.get("/mgmt/envs/prod/flags/plan/values?q=team", headers=auth())
    got = [v["value"] for v in r.json()["values"]]
    assert got[:2] == ["team-plus", "team"] or got[:2] == ["team", "team-plus"]  # prefix matches first
    assert got[2] == "annual-team" and "solo" not in got
    # Case-insensitive; LIKE metacharacters are literal.
    r = client.get("/mgmt/envs/prod/flags/plan/values?q=TEAM", headers=auth())
    assert len(r.json()["values"]) == 3
    r = client.get("/mgmt/envs/prod/flags/plan/values?q=%25", headers=auth())
    assert r.json()["values"] == []
    # Empty q: most recent first, bounded by limit.
    r = client.get("/mgmt/envs/prod/flags/plan/values?limit=2", headers=auth())
    assert [v["value"] for v in r.json()["values"]] == ["solo", "team-plus"]


def test_flag_endpoints_are_viewer_gated_and_env_checked(client):
    for path in ("/mgmt/envs/prod/flags", "/mgmt/envs/prod/flags/tier/values"):
        assert client.get(path, headers=auth(client.renderer_key)).status_code == 403
        assert client.get(path.replace("prod", "nope"), headers=auth()).status_code == 404
    viewer = make_key(client, "viewer", env="prod")
    assert client.get("/mgmt/envs/prod/flags", headers=auth(viewer)).status_code == 200
    assert client.get("/mgmt/envs/staging/flags", headers=auth(viewer)).status_code == 403
    # forget needs operator
    assert client.delete("/mgmt/envs/prod/flags/tier", headers=auth(viewer)).status_code == 403


def test_excluded_flag_is_never_observed(tmp_path):
    with make_client(tmp_path, observed_flags_exclude="user_id, email") as client:
        _render(client, {"tier": "pro", "user_id": "u_12", "email": "a@b.c"})
        assert {o.flag for o in get_app().observer.drain()} == {"tier"}


def test_only_the_serving_api_observes(client):
    # mgmt preview renders through the real resolution but is test traffic.
    r = client.post("/mgmt/prompts/support/system/preview", headers=auth(),
                    json={"environment": "prod", "flags": {"tier": "preview-only"},
                          "variables": {"customer_name": "Acme", "history": []}})
    assert r.status_code == 200, r.text
    assert get_app().observer.pending_size() == 0
    # evaluate + evaluate-all do observe.
    r = client.post("/prompt/support/system/evaluate", headers=auth(client.renderer_key),
                    json={"environment": "prod", "flags": {"tier": "from-evaluate"}})
    assert r.status_code == 200, r.text
    r = client.post("/evaluate", headers=auth(client.renderer_key),
                    json={"environment": "prod", "flags": {"region": "from-evaluate-all"}})
    assert r.status_code == 200, r.text
    assert {(o.flag, o.value) for o in get_app().observer.drain()} == {
        ("tier", "from-evaluate"), ("region", "from-evaluate-all")}
    # A failed resolution observes nothing.
    r = client.post("/prompt/nope/does-not-exist/evaluate", headers=auth(client.renderer_key),
                    json={"environment": "prod", "flags": {"tier": "never"}})
    assert r.status_code in (403, 404)
    assert get_app().observer.pending_size() == 0


def test_spec_merges_observed_values_for_rule_flags(client):
    _render(client, {"tier": "gold"})
    _flush(client)
    r = client.get("/prompt/support/system/spec", headers=auth(client.renderer_key))
    assert r.status_code == 200, r.text
    tier = next(f for f in r.json()["flags"] if f["name"] == "tier")
    assert "gold" in tier["values"] and "pro" in tier["values"] and "enterprise" in tier["values"]
    assert tier["observed"] is True and tier["suppressed"] is False


def test_suppressed_flag_is_not_suggested_and_forget_restores_it(client):
    with session_scope() as s:
        flush_observations(s, [Observation("prod", "uid", f"u{i}", "str", _now()) for i in range(60)])
        prune_and_census(s, ttl_days=30, value_cap=50)
        get_app().observer.set_suppressed(load_suppressions(s))
    r = client.get("/mgmt/envs/prod/flags/uid/values", headers=auth())
    assert r.json() == {"environment": "prod", "flag": "uid", "q": "", "values": [],
                        "suppressed": True, "values_seen": 60}
    r = client.get("/mgmt/envs/prod/flags", headers=auth())
    uid = next(f for f in r.json()["flags"] if f["name"] == "uid")
    assert uid["suppressed"] is True and uid["values_seen"] == 60
    # Observations for it are dropped at the source while suppressed.
    _render(client, {"uid": "u-new"})
    assert get_app().observer.pending_size() == 0
    # Spec reports it suppressed if a rule ever consults it — here via the flags list.
    op = make_key(client, "operator", env="prod")
    r = client.delete("/mgmt/envs/prod/flags/uid", headers=auth(op))
    assert r.status_code == 200 and r.json()["values_removed"] == 0
    assert not get_app().observer.is_suppressed("prod", "uid")
    _render(client, {"uid": "u-new"})
    assert get_app().observer.pending_size() == 1
    audit = client.get("/mgmt/audit?action=observed_flag.forget", headers=auth()).json()
    assert any(e["action"] == "observed_flag.forget" for e in audit["audit"])


def test_writer_pass_trips_and_persists_local_suppression(tmp_path):
    with make_client(tmp_path, observed_flags_value_cap=100) as client:
        for i in range(101):
            _render(client, {"uid": f"u{i}"})
        assert get_app().observer.is_suppressed("prod", "uid")
        _flush(client)
        with session_scope() as s:
            assert load_suppressions(s) == {("prod", "uid")}
            sup = s.get(models.ObservedFlagSuppression, ("prod", "uid"))
            assert sup.values_seen == 100
        assert _rows(flag="uid") == []


def test_writer_pass_unmarks_on_db_failure_and_recovers(client, monkeypatch):
    import sys
    appmod = sys.modules["incant.server.app"]  # the package exports `app` = the FastAPI instance
    _render(client, {"tier": "pro"})
    calls = {"n": 0}
    real = appmod.flush_observations

    def boom(session, obs):
        calls["n"] += 1
        raise RuntimeError("db down")
    monkeypatch.setattr(appmod, "flush_observations", boom)
    assert _flush(client) == 0 and calls["n"] == 1
    # The batch was un-marked: the same value queues again on the next request.
    _render(client, {"tier": "pro"})
    assert get_app().observer.pending_size() == 1
    monkeypatch.setattr(appmod, "flush_observations", real)
    assert _flush(client) == 1
    assert [(f, v) for f, v, _, _ in _rows(flag="tier")] == [("tier", "pro")]


def test_deleting_an_environment_cascades_observed_rows(client):
    r = client.post("/mgmt/envs", json={"id": "scratch"}, headers=auth())
    assert r.status_code == 200, r.text
    with session_scope() as s:
        flush_observations(s, [Observation("scratch", "plan", "pro", "str", _now())])
        record_suppressions(s, [("scratch", "uid")], 50)
    r = client.delete("/mgmt/envs/scratch?confirm=scratch", headers=auth())
    assert r.status_code == 200, r.text
    with session_scope() as s:
        assert s.execute(text("SELECT count(*) FROM observed_flags WHERE environment_id='scratch'")).scalar() == 0
        assert s.execute(text("SELECT count(*) FROM observed_flag_suppressions WHERE environment_id='scratch'")).scalar() == 0


# ── retries must never masquerade as cardinality ─────────────────────

def test_failed_flushes_never_ratchet_toward_suppression():
    """unmark unwinds the distinct count with the mark. A single live value re-queued on
    every failed pass used to leave one orphan increment per retry, so cap+1 failed
    passes suppressed a one-value flag; now the count mirrors the marks exactly."""
    o = _obs(value_cap=5)
    for _ in range(3 * 5):                                   # far more retries than the cap
        assert o.observe("prod", {"plan": "pro"}) == 1
        o.unmark(o.drain())                                  # the writer's failure path
    assert not o.is_suppressed("prod", "plan") and o.take_new_suppressions() == set()
    # And the count really is back at zero: cap-many distinct values still don't trip.
    for i in range(5):
        o.observe("prod", {"plan": f"p{i}"})
    assert not o.is_suppressed("prod", "plan")


def test_unmark_of_an_already_swept_key_does_not_underflow():
    clock = Clock()
    o = _obs(clock, value_cap=2)
    o.observe("prod", {"a": "1", "a2": "1"})
    batch = o.drain()
    clock.t += 61
    o.sweep()                                                # marks expired: count rebuilt to 0
    o.unmark(batch)                                          # nothing left to decrement
    o.observe("prod", {"a": "x"}); o.observe("prod", {"a": "y"})
    assert not o.is_suppressed("prod", "a")                  # 2 live values == cap, no trip


def test_sweep_rebuilds_distinct_counts_from_live_marks():
    """Belt and braces: the count is re-derived from the surviving marks on every
    sweep, so even a slipped increment cannot accumulate toward a false suppression."""
    o = _obs(value_cap=3)
    o.observe("prod", {"a": "1"})
    o._distinct[("prod", "a")] = 99                          # a slipped count, by hand
    o.sweep()
    assert o._distinct == {("prod", "a"): 1}


def test_restore_suppressions_keeps_a_trip_pending():
    o = _obs(value_cap=2)
    for i in range(3):
        o.observe("prod", {"uid": f"u{i}"})
    tripped = o.take_new_suppressions()
    assert tripped == {("prod", "uid")} and o.take_new_suppressions() == set()
    o.restore_suppressions(tripped)                          # the pass that took it failed
    assert o.take_new_suppressions() == {("prod", "uid")}


def test_writer_pass_retries_never_suppress_an_ordinary_flag(tmp_path, monkeypatch):
    import sys
    appmod = sys.modules["incant.server.app"]
    with make_client(tmp_path, observed_flags_value_cap=100) as client:
        def boom(session, obs):
            raise RuntimeError("db down")
        monkeypatch.setattr(appmod, "flush_observations", boom)
        for _ in range(105):                                 # > cap failed passes, ONE value
            _render(client, {"tier": "pro"})
            assert _flush(client) == 0
        assert not get_app().observer.is_suppressed("prod", "tier")
        assert get_app().observer.take_new_suppressions() == set()


def test_writer_pass_keeps_a_tripped_suppression_pending_across_a_failed_flush(tmp_path, monkeypatch):
    import sys
    appmod = sys.modules["incant.server.app"]
    with make_client(tmp_path, observed_flags_value_cap=100) as client:
        for i in range(101):
            _render(client, {"uid": f"u{i}"})
        assert get_app().observer.is_suppressed("prod", "uid")
        real = appmod.flush_observations

        def boom(session, obs):
            raise RuntimeError("db down")
        monkeypatch.setattr(appmod, "flush_observations", boom)
        assert _flush(client) == 0
        with session_scope() as s:
            assert load_suppressions(s) == set()             # not persisted yet…
        monkeypatch.setattr(appmod, "flush_observations", real)
        _flush(client)
        with session_scope() as s:
            assert load_suppressions(s) == {("prod", "uid")}  # …but not lost either
        assert _rows(flag="uid") == []
