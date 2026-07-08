import pytest

from incant.core import (
    Rule,
    ServeVersion,
    Skip,
    UnresolvedPrompt,
    Unservable,
    parse_rule,
    resolve,
)
from incant.core.model import RolloutBand, ServeLabel, ServeRollout

from .conftest import snapshot, vinfo

PID = "support/system"


def base_snapshot(**kw):
    versions = {
        PID: {
            1: vinfo(1, live="c_v1_live"),
            2: vinfo(2, live="c_v2_live", tip="c_v2_tip", previous=("c_v2_old",)),
            3: vinfo(3, live="c_v3_live", tip="c_v3_tip", label="voice-v2"),
        }
    }
    return snapshot(versions=versions, defaults={PID: 2}, **kw)


def test_default_serves_live_pointer():
    snap = base_snapshot()
    res = resolve(snap, PID, {})
    assert (res.version, res.commit, res.at, res.match_scope) == (2, "c_v2_live", "live", "default")


def test_prompt_rule_at_tip():
    snap = base_snapshot(rules=[
        parse_rule({"id": "team-x", "scope": "prompt", "prompt_id": PID, "priority": 20,
                    "when": {"flag": "user_id", "op": "in", "values": ["u_12"]},
                    "serve": {"version": 2, "at": "tip"}})
    ])
    res = resolve(snap, PID, {"user_id": "u_12"})
    assert (res.version, res.commit, res.at, res.rule_id) == (2, "c_v2_tip", "tip", "team-x")
    # non-matching flag falls through to default
    res2 = resolve(snap, PID, {"user_id": "u_99"})
    assert res2.match_scope == "default"


def test_priority_first_match_wins():
    snap = base_snapshot(rules=[
        parse_rule({"id": "low", "scope": "prompt", "prompt_id": PID, "priority": 30,
                    "when": None, "serve": {"version": 1}}),
        parse_rule({"id": "high", "scope": "prompt", "prompt_id": PID, "priority": 10,
                    "when": None, "serve": {"version": 3}}),
    ])
    res = resolve(snap, PID, {})
    assert res.rule_id == "high" and res.version == 3


def test_global_rules_beat_prompt_rules():
    snap = base_snapshot(rules=[
        parse_rule({"id": "p", "scope": "prompt", "prompt_id": PID, "priority": 1,
                    "when": None, "serve": {"version": 1}}),
        parse_rule({"id": "g", "scope": "global", "priority": 99,
                    "when": None, "serve": {"label": "voice-v2"}}),
    ])
    res = resolve(snap, PID, {})
    assert res.match_scope == "global" and res.version == 3 and res.label == "voice-v2"


def test_global_label_skips_prompt_without_label():
    # A prompt with no version carrying the label skips the global rule and continues.
    versions = {"other/p": {1: vinfo(1, live="o1")}}
    snap = snapshot(versions=versions, defaults={"other/p": 1}, rules=[
        parse_rule({"id": "g", "scope": "global", "priority": 1,
                    "when": None, "serve": {"label": "voice-v2"}}),
    ])
    res = resolve(snap, "other/p", {})
    assert res.match_scope == "default" and res.version == 1


def test_rollout_label_and_default_bands():
    snap = base_snapshot(rules=[
        parse_rule({"id": "exp", "scope": "global", "priority": 1, "when": None,
                    "serve": {"rollout": {"bucket_by": "user_id",
                                          "weights": [{"label": "voice-v2", "weight": 50},
                                                      {"default": True, "weight": 50}]}}}),
    ])
    labelled = defaulted = 0
    for i in range(400):
        res = resolve(snap, PID, {"user_id": f"u_{i}"})
        if res.label == "voice-v2":
            labelled += 1
        else:
            defaulted += 1  # default band -> falls through to env default (v2)
            assert res.match_scope == "default"
    assert labelled > 0 and defaulted > 0


def test_rollout_missing_bucket_by_falls_through():
    snap = base_snapshot(rules=[
        parse_rule({"id": "exp", "scope": "global", "priority": 1, "when": None,
                    "serve": {"rollout": {"bucket_by": "user_id",
                                          "weights": [{"label": "voice-v2", "weight": 100}]}}}),
    ])
    res = resolve(snap, PID, {})  # no user_id flag
    assert res.match_scope == "default"


def test_within_version_fallback_when_live_unservable():
    dead = {"c_v2_live"}
    snap = base_snapshot(servable=lambda p, s: s not in dead)
    res = resolve(snap, PID, {})
    assert res.commit == "c_v2_old" and res.content_fallback is True


def test_unservable_raises_when_no_history_servable():
    snap = base_snapshot(servable=lambda p, s: False)
    with pytest.raises(Unservable):
        resolve(snap, PID, {})


def test_paused_rule_ignored():
    snap = base_snapshot(rules=[
        parse_rule({"id": "p", "scope": "prompt", "prompt_id": PID, "priority": 1,
                    "status": "paused", "when": None, "serve": {"version": 3}}),
    ])
    assert resolve(snap, PID, {}).match_scope == "default"


def test_skip_recorded_for_unservable_rule_target():
    dead = {"c_v3_live", "c_v3_tip"}
    snap = base_snapshot(servable=lambda p, s: s not in dead, rules=[
        parse_rule({"id": "g", "scope": "global", "priority": 1, "when": None,
                    "serve": {"label": "voice-v2"}}),
    ])
    skips: list[Skip] = []
    res = resolve(snap, PID, {}, skips=skips)
    assert res.match_scope == "default"  # v3 unservable, rule skipped
    assert skips and skips[0].rule_id == "g"


def test_unresolved_when_no_default():
    snap = snapshot(versions={PID: {1: vinfo(1, live="x")}}, defaults={})
    with pytest.raises(UnresolvedPrompt):
        resolve(snap, PID, {})
