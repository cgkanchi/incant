import pytest

from incant.core import (
    Skip,
    UnresolvedPrompt,
    Unservable,
    parse_rule,
    resolve,
)

from .conftest import snapshot, vinfo

PID = "support/system"


def base_snapshot(**kw):
    versions = {
        PID: {
            1: vinfo(1, live="c_v1_live"),
            2: vinfo(2, live="c_v2_live", tip="c_v2_tip", previous=("c_v2_old",)),
            3: vinfo(3, live="c_v3_live", tip="c_v3_tip"),
        }
    }
    return snapshot(versions=versions, defaults={PID: 2}, **kw)


def rule(**kw):
    base = {"id": "r", "prompt_id": PID, "priority": 10, "when": None, "serve": {"version": 3}}
    return parse_rule({**base, **kw})


def test_default_serves_live_pointer():
    snap = base_snapshot()
    res = resolve(snap, PID, {})
    assert (res.version, res.commit, res.at, res.match_scope) == (2, "c_v2_live", "live", "default")


def test_prompt_rule_at_tip():
    snap = base_snapshot(rules=[
        rule(id="team-x", priority=20,
             when={"flag": "user_id", "op": "in", "values": ["u_12"]},
             serve={"version": 2, "at": "tip"})
    ])
    res = resolve(snap, PID, {"user_id": "u_12"})
    assert (res.version, res.commit, res.at, res.rule_id) == (2, "c_v2_tip", "tip", "team-x")
    # non-matching flag falls through to default
    res2 = resolve(snap, PID, {"user_id": "u_99"})
    assert res2.match_scope == "default"


def test_priority_first_match_wins():
    snap = base_snapshot(rules=[
        rule(id="low", priority=30, serve={"version": 1}),
        rule(id="high", priority=10, serve={"version": 3}),
    ])
    res = resolve(snap, PID, {})
    assert res.rule_id == "high" and res.version == 3


def test_rules_are_scoped_to_their_prompt():
    versions = {"other/p": {1: vinfo(1, live="o1")}, PID: {1: vinfo(1, live="c1")}}
    snap = snapshot(versions=versions, defaults={"other/p": 1, PID: 1}, rules=[
        rule(id="only-system", serve={"version": 1}),
    ])
    assert resolve(snap, PID, {}).rule_id == "only-system"
    assert resolve(snap, "other/p", {}).match_scope == "default"


def test_pinned_sha_serve_target():
    snap = base_snapshot(rules=[rule(id="pin", serve={"version": 2, "at": "sha", "sha": "c" * 40})],
                         servable=lambda p, v, s: True)
    res = resolve(snap, PID, {})
    assert (res.at, res.commit) == ("sha", "c" * 40)


def test_within_version_fallback_when_live_unservable():
    dead = {"c_v2_live"}
    snap = base_snapshot(servable=lambda p, v, s: s not in dead)
    res = resolve(snap, PID, {})
    assert res.commit == "c_v2_old" and res.content_fallback is True


def test_unservable_raises_when_no_history_servable():
    snap = base_snapshot(servable=lambda p, v, s: False)
    with pytest.raises(Unservable):
        resolve(snap, PID, {})


def test_paused_rule_ignored():
    snap = base_snapshot(rules=[rule(id="p", priority=1, status="paused", serve={"version": 3})])
    assert resolve(snap, PID, {}).match_scope == "default"


def test_skip_recorded_for_unservable_rule_target():
    dead = {"c_v3_live", "c_v3_tip"}
    snap = base_snapshot(servable=lambda p, v, s: s not in dead, rules=[rule(id="g", priority=1)])
    skips: list[Skip] = []
    res = resolve(snap, PID, {}, skips=skips)
    assert res.match_scope == "default"  # v3 unservable, rule skipped
    assert skips and skips[0].rule_id == "g" and skips[0].reason == "no servable pointer in history"


def test_skip_recorded_for_missing_and_archived_versions():
    versions = {PID: {1: vinfo(1, live="c1"), 3: vinfo(3, live="c3", status="archived")}}
    snap = snapshot(versions=versions, defaults={PID: 1}, rules=[
        rule(id="gone", priority=1, serve={"version": 9}),
        rule(id="old", priority=2, serve={"version": 3}),
    ])
    skips: list[Skip] = []
    assert resolve(snap, PID, {}, skips=skips).match_scope == "default"
    assert [(s.rule_id, s.reason) for s in skips] == [
        ("gone", "version 9 does not exist"), ("old", "version 3 is archived")]


def test_kill_switch_bypasses_rules():
    snap = base_snapshot(rules=[rule(id="r", priority=1)], killed={PID})
    skips: list[Skip] = []
    assert resolve(snap, PID, {}, skips=skips).match_scope == "default" and skips == []


def test_unresolved_when_no_default():
    snap = snapshot(versions={PID: {1: vinfo(1, live="x")}}, defaults={})
    with pytest.raises(UnresolvedPrompt):
        resolve(snap, PID, {})
