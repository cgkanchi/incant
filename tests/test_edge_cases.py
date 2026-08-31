"""Lifecycle and serving edge cases — the seams where targeting systems rot.

Each test here pins one behaviour that the coverage audit found either unasserted or
wrong: what happens when a segment vanishes under a rule, an include cannot resolve,
one environment's data goes bad, an environment is renamed or deleted, labels collide,
a rollback names things that no longer exist, or the emergency levers meet the edges of
the model. Regressions here are the kind that look healthy until production.
"""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import select, text

from incant import models
from incant.core import IncludeDepthExceeded, parse_condition, parse_rule, resolve
from incant.core.clauses import eval_condition
from incant.core.model import EnvSnapshot
from incant.db import session_scope
from incant.service import ServingError, get_app
from incant.targeting.observed import Observation, flush_observations, record_suppressions
from incant.targeting.snapshot import build_snapshot

from .conftest import snapshot, vinfo
from .test_integration import _author_version, app  # noqa: F401 - fixture
from .test_server import auth, make_client, make_key

PID = "support/system"


@pytest.fixture()
def client(tmp_path):
    with make_client(tmp_path) as c:
        yield c


def _render(client, flags, env="prod", key=None, prompt=PID):
    return client.post(f"/prompt/{prompt}", headers=auth(key or client.renderer_key),
                       json={"environment": env, "flags": flags,
                             "variables": {"customer_name": "Acme", "history": []}})


def _flush():
    from incant.server.app import _observed_flags_pass
    return _observed_flags_pass(get_app())


# ── 1. environment rename keeps observed flags ───────────────────────

def test_rename_env_repoints_observed_flags_instead_of_cascading_them_away(client):
    assert client.post("/mgmt/envs", json={"id": "scratch"}, headers=auth()).status_code == 200
    with session_scope() as s:
        flush_observations(s, [Observation("scratch", "plan", "pro", "str",
                                           dt.datetime.now(dt.timezone.utc))])
        record_suppressions(s, [("scratch", "uid")], 50)
    r = client.post("/mgmt/envs/scratch/rename", json={"new_id": "scratch2"}, headers=auth())
    assert r.status_code == 200, r.text
    with session_scope() as s:
        rows = s.execute(select(models.ObservedFlag)).scalars().all()
        assert [(o.environment_id, o.flag, o.value) for o in rows] == [("scratch2", "plan", "pro")]
        sups = s.execute(select(models.ObservedFlagSuppression)).scalars().all()
        assert [(x.environment_id, x.flag) for x in sups] == [("scratch2", "uid")]


# ── 2. a missing segment is a counted skip, never "everyone" ─────────

def _rules_snapshot(rules, segments=None):
    versions = {PID: {1: vinfo(1, live="c1"), 2: vinfo(2, live="c2")}}
    return snapshot(versions=versions, defaults={PID: 1}, rules=[parse_rule(r) for r in rules],
                    segments=segments or {})


def test_rule_with_missing_segment_is_skipped_and_counted_even_under_not():
    rule = {"id": "neg", "scope": "prompt", "prompt_id": PID, "priority": 1,
            "when": {"not": {"segment": "gone"}}, "serve": {"version": 2}}
    skips = []
    res = resolve(_rules_snapshot([rule]), PID, {"user_id": "u_1"}, skips=skips)
    assert res.version == 1 and res.match_scope == "default"      # NOT "match everyone"
    assert [(s.rule_id, s.reason) for s in skips] == [("neg", "segment 'gone' does not exist")]
    # Nested under all/any as well; a later healthy rule still gets its turn.
    rules = [
        {"id": "broken", "scope": "prompt", "prompt_id": PID, "priority": 1,
         "when": {"all": [{"flag": "tier", "op": "eq", "value": "pro"},
                          {"any": [{"segment": "gone"}]}]}, "serve": {"version": 2}},
        {"id": "healthy", "scope": "prompt", "prompt_id": PID, "priority": 2,
         "when": {"flag": "tier", "op": "eq", "value": "pro"}, "serve": {"version": 2}},
    ]
    skips = []
    res = resolve(_rules_snapshot(rules), PID, {"tier": "pro"}, skips=skips)
    assert res.rule_id == "healthy" and [s.rule_id for s in skips] == ["broken"]


def test_segment_cycle_at_eval_is_a_counted_skip():
    from incant.core.model import Segment
    segs = {"a": Segment("a", parse_condition({"segment": "b"})),
            "b": Segment("b", parse_condition({"segment": "a"}))}
    rule = {"id": "cyc", "scope": "prompt", "prompt_id": PID, "priority": 1,
            "when": {"segment": "a"}, "serve": {"version": 2}}
    skips = []
    res = resolve(_rules_snapshot([rule], segs), PID, {}, skips=skips)
    assert res.match_scope == "default" and "cycle" in skips[0].reason


# ── 3. include failures name the fragment and never 500 ─────────────

def test_unresolvable_include_names_the_fragment_not_the_root(app):
    _author_version(app, "support/frag", 1, "FRAG")
    _author_version(app, "support/main", 1, '{% include "support/frag" %}')
    with session_scope() as s:
        s.add(models.Environment(id="staging", name="staging"))
        s.flush()
        app.targeting(s, "sam").ensure_baseline("staging")
    # main is served in staging; frag has no default there.
    _author_version(app, "support/main", 1, '{% include "support/frag" %}', env="staging")
    with session_scope() as s:
        assert app.serve(s, "prod", "support/main", {}, {})["prompt"] == "FRAG"
        with pytest.raises(ServingError) as ei:
            app.serve(s, "staging", "support/main", {}, {})
    assert ei.value.status == 404
    assert "support/frag" in ei.value.detail and "included by 'support/main'" in ei.value.detail
    assert "'support/main' exists but serves nothing" not in ei.value.detail


def test_targeting_induced_include_cycle_is_a_409_not_a_500(app):
    # Static validation checks the include graph against each included prompt's NEWEST
    # version. A rule that serves an OLDER version which includes back is invisible to
    # it and closes the cycle at render time only — that must be a 409, never a 500.
    _author_version(app, "support/a", 1, "plainA")
    _author_version(app, "support/b", 1, '{% include "support/a" %}')   # a@v1 is plain: valid
    _author_version(app, "support/b", 2, "plainB")                         # newest b is plain
    _author_version(app, "support/a", 2, '{% include "support/b" %}')   # static graph: a→b@v2: valid
    with session_scope() as s:
        app.targeting(s, "sam").upsert_rule("prod", {
            "id": "vip-old-b", "scope": "prompt", "prompt_id": "support/b", "priority": 1,
            "when": {"flag": "tier", "op": "eq", "value": "vip"}, "serve": {"version": 1}})
    app.invalidate("prod")
    with session_scope() as s:
        assert app.serve(s, "prod", "support/a", {}, {})["prompt"] == "plainB"      # no cycle
        with pytest.raises(ServingError) as ei:
            app.serve(s, "prod", "support/a", {"tier": "vip"}, {})                  # a→b@v1→a
    assert ei.value.status == 409 and "include cycle" in ei.value.detail


def test_include_depth_exceeded_is_a_409(app, monkeypatch):
    _author_version(app, "support/deep", 1, "x")
    import incant.service as svc

    def boom(*a, **k):
        raise IncludeDepthExceeded(32, ["support/deep", "…"])
    monkeypatch.setattr(svc, "render", boom)
    with session_scope() as s:
        with pytest.raises(ServingError) as ei:
            app.serve(s, "prod", "support/deep", {}, {})
    assert ei.value.status == 409 and "depth limit" in ei.value.detail


# ── 4. one poisoned environment does not stall propagation ──────────

def test_bad_data_in_one_environment_keeps_the_others_refreshing(client):
    from incant.server.metrics import snapshot_build_failures_total
    appctx = get_app()
    with session_scope() as s:
        appctx.get_snapshot(s, "prod")
        appctx.get_snapshot(s, "staging")
        before_prod = appctx._snapshots["prod"].rules_version
        before_staging = appctx._snapshots["staging"].rules_version
        rid = s.execute(text("SELECT id FROM rules WHERE environment_id='prod' LIMIT 1")).scalar()
        s.execute(text("UPDATE rules SET clauses = '{\"bogus\": 1}' WHERE id = :rid"), {"rid": rid})
        s.execute(text("UPDATE environments SET rules_version = rules_version + 1"))
    failures_before = snapshot_build_failures_total.labels("prod")._value.get()
    with session_scope() as s:
        appctx.refresh_control_plane(s)
    assert appctx._snapshots["staging"].rules_version == before_staging + 1     # refreshed
    assert appctx._snapshots["prod"].rules_version == before_prod              # last good kept
    assert appctx._db_healthy is True                                           # not an outage
    assert snapshot_build_failures_total.labels("prod")._value.get() == failures_before + 1
    # prod still serves from its last good snapshot.
    assert _render(client, {}).status_code == 200


# ── 5. labels name exactly one version; resolution is deterministic ─

def test_version_for_label_prefers_highest_active_and_ignores_archived():
    snap = EnvSnapshot(environment="prod", rules_version=1, versions={PID: {
        1: vinfo(1, live="c1", label="stable"),
        2: vinfo(2, live="c2", label="stable"),
        3: vinfo(3, live="c3", label="stable", status="archived"),
    }})
    assert snap.version_for_label(PID, "stable") == 2
    assert snap.version_for_label(PID, "nope") is None


def test_label_rule_skips_prompt_without_label_silently(client=None):
    """Design: a global label rule simply does not apply to a prompt lacking the label —
    that is a fall-through, not a skip, and must stay uncounted."""
    rule = {"id": "lbl", "scope": "global", "priority": 1, "when": None, "serve": {"label": "voice-v2"}}
    skips = []
    res = resolve(_rules_snapshot([rule]), PID, {}, skips=skips)
    assert res.match_scope == "default" and skips == []


# ── 6. a deleted environment is evicted from every node's cache ──────

def test_environment_deleted_elsewhere_is_evicted_on_refresh(client):
    assert client.post("/mgmt/envs", json={"id": "gone"}, headers=auth()).status_code == 200
    appctx = get_app()
    with session_scope() as s:
        appctx.get_snapshot(s, "gone")
        assert "gone" in appctx._snapshots
        # Simulate another node deleting it: raw SQL, so THIS node's cache is not invalidated.
        s.execute(text("DELETE FROM rule_revisions WHERE environment_id='gone'"))
        s.execute(text("DELETE FROM environments WHERE id='gone'"))
    with session_scope() as s:
        appctx.refresh_control_plane(s)
    assert "gone" not in appctx._snapshots
    with session_scope() as s:
        with pytest.raises(ServingError) as ei:
            appctx.get_snapshot(s, "gone")
    assert ei.value.status == 404


# ── 7/8. write-time validation: global versions, segment refs, cycles ─

def test_global_rule_explicit_version_must_exist_for_some_prompt(client):
    body = {"id": "g99", "scope": "global", "priority": 1, "when": None, "serve": {"version": 99}}
    r = client.post("/mgmt/envs/prod/rules", json=body, headers=auth())
    assert r.status_code == 400 and "no prompt" in r.json()["detail"], r.text
    body["serve"] = {"version": 2}
    assert client.post("/mgmt/envs/prod/rules", json=body, headers=auth()).status_code == 200


def test_dangling_segment_references_are_refused_at_write_time(client):
    r = client.post("/mgmt/envs/prod/rules",
                    json={"id": "dangling", "scope": "prompt", "prompt_id": PID, "priority": 1,
                          "when": {"not": {"segment": "nope"}}, "serve": {"version": 2}},
                    headers=auth())
    assert r.status_code == 400 and "unknown segment" in r.json()["detail"], r.text
    r = client.post("/mgmt/envs/prod/segments",
                    json={"name": "outer", "when": {"all": [{"segment": "nope"}]}}, headers=auth())
    assert r.status_code == 400 and "unknown segment" in r.json()["detail"], r.text
    # Segment-in-segment is fine…
    assert client.post("/mgmt/envs/prod/segments", json={"name": "inner", "when": {
        "flag": "tier", "op": "eq", "value": "pro"}}, headers=auth()).status_code == 200
    assert client.post("/mgmt/envs/prod/segments", json={"name": "outer", "when": {
        "segment": "inner"}}, headers=auth()).status_code == 200
    # …until it closes a cycle.
    r = client.post("/mgmt/envs/prod/segments", json={"name": "inner", "when": {
        "any": [{"flag": "tier", "op": "eq", "value": "pro"}, {"segment": "outer"}]}}, headers=auth())
    assert r.status_code == 400 and "segment cycle" in r.json()["detail"], r.text
    # Rule listing surfaces a reference that went stale another way (rollback-style).
    with session_scope() as s:
        s.execute(text("UPDATE rules SET clauses = '{\"segment\": \"vanished\"}' "
                       "WHERE id = (SELECT id FROM rules WHERE environment_id='prod' LIMIT 1)"))
        s.execute(text("UPDATE environments SET rules_version = rules_version + 1 WHERE id='prod'"))
    get_app().invalidate("prod")
    rules = client.get("/mgmt/envs/prod/rules", headers=auth()).json()["rules"]
    assert any("'vanished' does not exist" in (r["unservable_reason"] or "") for r in rules)


# ── 9. rollback reports what it could not restore ───────────────────

def test_rollback_skips_archived_defaults_and_unvalidated_pointers(app):
    pid = "support/rb"
    _author_version(app, pid, 1, "one")
    v2 = _author_version(app, pid, 2, "two")                 # default v2, live sha2
    with session_scope() as s:
        target = s.get(models.Environment, "prod").rules_version
    _author_version(app, pid, 2, "two-b")                    # v2 live moves to sha2b
    with session_scope() as s:
        app.targeting(s, "sam").set_default("prod", pid, 1)
        app.registry(s, "sam").update_version(pid, 2, status="archived")
        # The recorded pointer's SHA is no longer a validated commit.
        s.execute(text("DELETE FROM commit_validations WHERE sha = :sha"), {"sha": v2.sha})
    with session_scope() as s:
        result = app.targeting(s, "sam").rollback("prod", target)
        assert result["changed"]["defaults_skipped"] == 1
        assert result["changed"]["pointers_skipped"] == 1
        d = s.execute(select(models.EnvDefault).where(
            models.EnvDefault.environment_id == "prod", models.EnvDefault.prompt_id == pid)).scalar_one()
        assert d.version_number == 1                          # archived v2 not restored


# ── 10. spec merge keeps 1 and True apart ────────────────────────────

def test_spec_flag_values_keep_int_and_bool_distinct(client):
    assert client.post("/mgmt/envs/prod/rules", json={
        "id": "n-rule", "scope": "prompt", "prompt_id": PID, "priority": 1,
        "when": {"flag": "n", "op": "eq", "value": 1}, "serve": {"version": 2}},
        headers=auth()).status_code == 200
    assert _render(client, {"n": True}).status_code == 200
    assert _render(client, {"n": 1}).status_code == 200
    _flush()
    spec = client.get(f"/prompt/{PID}/spec", headers=auth(client.renderer_key)).json()
    vals = next(f for f in spec["flags"] if f["name"] == "n")["values"]
    assert any(v is True for v in vals) and any(v == 1 and v is not True for v in vals), vals


# ── archived semantics over HTTP ─────────────────────────────────────

def test_set_default_to_archived_version_refused(client):
    assert client.post("/mgmt/envs/staging/rules", json={
        "id": "keep-v3-out", "scope": "prompt", "prompt_id": PID, "priority": 1,
        "when": {"flag": "x", "op": "eq", "value": 1}, "serve": {"version": 3}},
        headers=auth()).status_code == 200
    r = client.patch(f"/mgmt/prompts/{PID}/versions/3", json={"status": "archived"}, headers=auth())
    assert r.status_code == 200, r.text
    r = client.post("/mgmt/envs/staging/defaults",
                    json={"prompt_id": PID, "version_number": 3}, headers=auth())
    assert r.status_code == 400 and "archived" in r.json()["detail"], r.text


# ── protected-environment ceremony gaps ─────────────────────────────

def test_protected_env_defaults_need_releaser_and_confirm(client):
    op = make_key(client, "operator", env="prod")
    rel = make_key(client, "releaser", env="prod")
    body = {"prompt_id": PID, "version_number": 3}
    r = client.post("/mgmt/envs/prod/defaults", json={**body, "confirm": PID}, headers=auth(op))
    assert r.status_code == 403                                      # operator is not enough
    r = client.post("/mgmt/envs/prod/defaults", json=body, headers=auth(rel))
    assert r.status_code == 409 and r.json()["detail"]["error"] == "confirmation_required"
    r = client.post("/mgmt/envs/prod/defaults", json={**body, "confirm": PID}, headers=auth(rel))
    assert r.status_code == 200, r.text


def test_protected_env_rollback_requires_confirm(client):
    with session_scope() as s:
        rv = s.get(models.Environment, "prod").rules_version
    r = client.post("/mgmt/envs/prod/rollback", json={"to_rules_version": rv}, headers=auth())
    assert r.status_code == 409 and r.json()["detail"]["error"] == "confirmation_required"
    r = client.post("/mgmt/envs/prod/rollback", json={"to_rules_version": rv, "confirm": "prod"},
                    headers=auth())
    assert r.status_code == 200, r.text


# ── pointer history, fallback, pins ─────────────────────────────────

def test_pointer_history_stops_at_a_rollback_tombstone(app):
    pid = "support/tomb"
    a = _author_version(app, pid, 1, "A")
    b = _author_version(app, pid, 1, "B")
    with session_scope() as s:
        snap = build_snapshot(s, "prod")
        assert snap.versions[pid][1].previous_live == (a.sha,)
        s.add(models.PointerMove(environment_id="prod", prompt_id=pid, version_number=1,
                                 from_sha=b.sha, to_sha=None, moved_by="rollback", comment="tombstone"))
        s.flush()
        app.targeting(s, "sam").make_live("prod", pid, 1, b.sha, comment="re-live")
    with session_scope() as s:
        snap = build_snapshot(s, "prod")
        vi = snap.versions[pid][1]
        assert vi.live_sha == b.sha and vi.previous_live == ()   # fallback cannot reach past it


def test_content_fallback_sets_header_flag_and_metric(client, monkeypatch):
    from incant.server import metrics
    from .test_server import _tip_sha
    rel = make_key(client, "releaser", env="prod")
    old = client.get(f"/mgmt/prompts/{PID}/versions?environment=prod", headers=auth()).json()
    old_live = next(v for v in old["versions"] if v["version"] == 2)["live_full_sha"]
    new_live = _tip_sha(client)
    r = client.post("/mgmt/envs/prod/pointers", headers=auth(rel),
                    json={"prompt_id": PID, "version_number": 2, "to_sha": new_live, "confirm": PID})
    assert r.status_code == 200, r.text
    appctx = get_app()
    real = appctx.content.get

    def missing_live(prompt_id, version, sha):
        if sha == new_live:
            raise KeyError("simulated unfetchable content")
        return real(prompt_id, version, sha)
    monkeypatch.setattr(appctx.content, "get", missing_live)
    before = metrics.content_fallbacks_total.labels(PID, "prod")._value.get()
    r = _render(client, {})
    assert r.status_code == 200, r.text
    assert r.headers.get("X-Incant-Content-Fallback") == "true"
    body = r.json()
    assert body["content_fallback"] is True
    assert body["versions"][PID]["commit"] == old_live and body["versions"][PID]["fallback"] is True
    assert metrics.content_fallbacks_total.labels(PID, "prod")._value.get() == before + 1


def test_pin_of_validation_failed_commit_is_refused(client):
    with session_scope() as s:
        s.get(models.Project, "support").review_policy = 0    # commit without approvals
        s.flush()
        reg = get_app().registry(s, "sam")
        d = reg.create_draft("support/greeting", version_number=2, author="sam", content="{{ broken")
        outcome = reg.commit_draft(d.id, author="sam", message="bad")
        assert outcome.validation["status"] != "valid"
        bad_sha = outcome.sha
    get_app().invalidate("prod")
    r = client.post("/prompt/support/greeting", headers=auth(client.renderer_key),
                    json={"environment": "prod", "flags": {}, "variables": {"customer_name": "A"},
                          "pin": {"versions": {"support/greeting": {"version": 2, "commit": bad_sha}}}})
    assert r.status_code == 409 and "not a validated commit" in r.text, r.text


# ── environment deletion vs. scoped keys ────────────────────────────

def test_key_scoped_to_a_deleted_env_authenticates_but_holds_no_roles(client):
    assert client.post("/mgmt/envs", json={"id": "scratch"}, headers=auth()).status_code == 200
    k = make_key(client, "viewer", env="scratch")
    assert client.get("/mgmt/envs/scratch/rules", headers=auth(k)).status_code == 200
    assert client.delete("/mgmt/envs/scratch?confirm=scratch", headers=auth()).status_code == 200
    r = client.get("/mgmt/envs/prod/rules", headers=auth(k))
    assert r.status_code == 403, r.text                         # known key, no bindings — not 401


# ── clause operator matrix ───────────────────────────────────────────

VALUE_OPS = ["eq", "neq", "in", "not_in", "contains", "starts_with", "ends_with",
             "gt", "gte", "lt", "lte", "semver_gt", "semver_lt"]


@pytest.mark.parametrize("op", VALUE_OPS)
def test_missing_flag_never_matches_for_every_operator(op):
    body = {"flag": "f", "op": op}
    body["values" if op in ("in", "not_in") else "value"] = ["1.0.0"] if op in ("in", "not_in") else "1.0.0"
    assert eval_condition(parse_condition(body), {}, {}) is False
    assert eval_condition(parse_condition({"flag": "f", "op": "exists"}), {}, {}) is False


def test_incomparable_and_odd_values_never_raise():
    c = parse_condition
    assert eval_condition(c({"flag": "f", "op": "contains", "value": "x"}), {"f": 5}, {}) is False
    assert eval_condition(c({"flag": "f", "op": "gt", "value": 1}), {"f": None}, {}) is False
    assert eval_condition(c({"flag": "f", "op": "lt", "value": "a"}), {"f": 1}, {}) is False
    assert eval_condition(c({"flag": "f", "op": "eq", "value": [1, 2]}), {"f": [1, 2]}, {}) is True
    assert eval_condition(c({"flag": "f", "op": "semver_gt", "value": "1.2.0"}), {"f": "abc"}, {}) is False
    assert eval_condition(c({"flag": "f", "op": "semver_gt", "value": "garbage"}), {"f": "1.3.0"}, {}) is False
    assert eval_condition(c({"flag": "f", "op": "semver_lt", "value": "2.0.0"}), {"f": "1.2.3+build"}, {}) is True


# ── observed flags: unicode ──────────────────────────────────────────

def test_observed_flags_roundtrip_unicode_values(client):
    assert _render(client, {"plan": "プレミアム🚀"}).status_code == 200
    _flush()
    r = client.get("/mgmt/envs/prod/flags/plan/values?q=%E3%83%97", headers=auth())
    assert [v["value"] for v in r.json()["values"]] == ["プレミアム🚀"]


# ── kill switch vs. reproducibility ─────────────────────────────────

def test_pins_and_replays_do_not_bypass_the_kill_switch(client):
    """The kill switch beats reproducibility, exactly as validation does: a pin naming
    a killed prompt — and a rules_version replay of one — is a 409 with a
    machine-readable `error: "killed"`, never the killed content and never a silent
    substitution. Lifting the kill makes the same pin serve the same commit again."""
    r = _render(client, {})
    assert r.status_code == 200, r.text
    entry = {"version": r.json()["versions"][PID]["version"],
             "commit": r.json()["versions"][PID]["commit"]}
    rv = r.json()["rules_version"]
    assert client.post("/mgmt/envs/prod/kill?prompt_id=support/system",
                       json={"engaged": True}, headers=auth()).status_code == 200

    body = {"environment": "prod", "flags": {},
            "variables": {"customer_name": "Acme", "history": []}}
    r = client.post(f"/prompt/{PID}", headers=auth(client.renderer_key),
                    json={**body, "pin": {"versions": {PID: entry}}})
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["error"] == "killed"
    assert "kill switch" in r.json()["detail"]["detail"]
    r = client.post(f"/prompt/{PID}", headers=auth(client.renderer_key),
                    json={**body, "pin": {"rules_version": rv}})
    assert r.status_code == 409 and r.json()["detail"]["error"] == "killed", r.text
    # The normal (unpinned) path still degrades to the environment default.
    assert _render(client, {}).status_code == 200

    assert client.post("/mgmt/envs/prod/kill?prompt_id=support/system",
                       json={"engaged": False}, headers=auth()).status_code == 200
    r = client.post(f"/prompt/{PID}", headers=auth(client.renderer_key),
                    json={**body, "pin": {"versions": {PID: entry}}})
    assert r.status_code == 200 and r.json()["versions"][PID]["commit"] == entry["commit"]

