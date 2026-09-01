from incant.core import parse_condition
from incant.core.clauses import eval_condition


def cond(d):
    return parse_condition(d)


def test_operators():
    f = {"tier": "pro", "count": 5, "name": "acme-corp", "ver": "1.4.2"}
    assert eval_condition(cond({"flag": "tier", "op": "eq", "value": "pro"}), f)
    assert not eval_condition(cond({"flag": "tier", "op": "neq", "value": "pro"}), f)
    assert eval_condition(cond({"flag": "tier", "op": "in", "values": ["pro", "ent"]}), f)
    assert eval_condition(cond({"flag": "tier", "op": "not_in", "values": ["free"]}), f)
    assert eval_condition(cond({"flag": "name", "op": "contains", "value": "corp"}), f)
    assert eval_condition(cond({"flag": "name", "op": "starts_with", "value": "acme"}), f)
    assert eval_condition(cond({"flag": "name", "op": "ends_with", "value": "corp"}), f)
    assert eval_condition(cond({"flag": "count", "op": "gt", "value": 3}), f)
    assert eval_condition(cond({"flag": "count", "op": "gte", "value": 5}), f)
    assert eval_condition(cond({"flag": "count", "op": "lt", "value": 9}), f)
    assert eval_condition(cond({"flag": "count", "op": "lte", "value": 5}), f)
    assert eval_condition(cond({"flag": "ver", "op": "semver_gt", "value": "1.4.0"}), f)
    assert eval_condition(cond({"flag": "ver", "op": "semver_lt", "value": "2.0.0"}), f)
    assert eval_condition(cond({"flag": "tier", "op": "exists"}), f)


def test_absent_flag_never_matches_and_never_errors():
    f = {}
    for op, extra in [
        ("eq", {"value": "x"}), ("neq", {"value": "x"}), ("in", {"values": ["x"]}),
        ("gt", {"value": 1}), ("contains", {"value": "x"}),
    ]:
        assert eval_condition(cond({"flag": "missing", "op": op, **extra}), f) is False
    # exists on an absent flag is simply False
    assert eval_condition(cond({"flag": "missing", "op": "exists"}), f) is False


def test_incomparable_types_do_not_raise():
    assert eval_condition(cond({"flag": "x", "op": "gt", "value": 1}), {"x": "str"}) is False


def test_all_any_not_composition():
    f = {"a": 1, "b": 2}
    c = cond({"all": [{"flag": "a", "op": "eq", "value": 1}, {"flag": "b", "op": "eq", "value": 2}]})
    assert eval_condition(c, f)
    c = cond({"any": [{"flag": "a", "op": "eq", "value": 9}, {"flag": "b", "op": "eq", "value": 2}]})
    assert eval_condition(c, f)
    c = cond({"not": {"flag": "a", "op": "eq", "value": 9}})
    assert eval_condition(c, f)


def test_none_condition_always_matches():
    assert eval_condition(None, {}) is True


def test_removed_constructs_are_refused_with_a_reason():
    import pytest
    from incant.core import parse_rule, parse_serve
    with pytest.raises(ValueError, match="segments were removed"):
        cond({"segment": "beta"})
    with pytest.raises(ValueError, match="label and rollout"):
        parse_serve({"label": "voice-v2"})
    with pytest.raises(ValueError, match="label and rollout"):
        parse_serve({"rollout": {"bucket_by": "user_id", "weights": [{"default": True, "weight": 100}]}})
    with pytest.raises(ValueError, match="global rules were removed"):
        parse_rule({"id": "g", "scope": "global", "priority": 1, "when": None, "serve": {"version": 1}})
    # The pre-1.1.0 `scope: prompt` key is tolerated (old payloads and captured states).
    r = parse_rule({"id": "p", "scope": "prompt", "prompt_id": "support/system", "priority": 1,
                    "when": None, "serve": {"version": 1}})
    assert r.prompt_id == "support/system"
