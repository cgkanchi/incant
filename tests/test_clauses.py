from incant.core import parse_condition
from incant.core.clauses import eval_condition
from incant.core.model import Segment


def cond(d):
    return parse_condition(d)


def test_operators():
    f = {"tier": "pro", "count": 5, "name": "acme-corp", "ver": "1.4.2"}
    assert eval_condition(cond({"flag": "tier", "op": "eq", "value": "pro"}), f, {})
    assert not eval_condition(cond({"flag": "tier", "op": "neq", "value": "pro"}), f, {})
    assert eval_condition(cond({"flag": "tier", "op": "in", "values": ["pro", "ent"]}), f, {})
    assert eval_condition(cond({"flag": "tier", "op": "not_in", "values": ["free"]}), f, {})
    assert eval_condition(cond({"flag": "name", "op": "contains", "value": "corp"}), f, {})
    assert eval_condition(cond({"flag": "name", "op": "starts_with", "value": "acme"}), f, {})
    assert eval_condition(cond({"flag": "name", "op": "ends_with", "value": "corp"}), f, {})
    assert eval_condition(cond({"flag": "name", "op": "matches", "value": r"acme-\w+"}), f, {})
    assert eval_condition(cond({"flag": "count", "op": "gt", "value": 3}), f, {})
    assert eval_condition(cond({"flag": "count", "op": "gte", "value": 5}), f, {})
    assert eval_condition(cond({"flag": "count", "op": "lt", "value": 9}), f, {})
    assert eval_condition(cond({"flag": "count", "op": "lte", "value": 5}), f, {})
    assert eval_condition(cond({"flag": "ver", "op": "semver_gt", "value": "1.4.0"}), f, {})
    assert eval_condition(cond({"flag": "ver", "op": "semver_lt", "value": "2.0.0"}), f, {})
    assert eval_condition(cond({"flag": "tier", "op": "exists"}), f, {})


def test_absent_flag_never_matches_and_never_errors():
    f = {}
    for op, extra in [
        ("eq", {"value": "x"}), ("neq", {"value": "x"}), ("in", {"values": ["x"]}),
        ("gt", {"value": 1}), ("contains", {"value": "x"}), ("matches", {"value": ".*"}),
    ]:
        assert eval_condition(cond({"flag": "missing", "op": op, **extra}), f, {}) is False
    # exists on an absent flag is simply False
    assert eval_condition(cond({"flag": "missing", "op": "exists"}), f, {}) is False


def test_incomparable_types_do_not_raise():
    assert eval_condition(cond({"flag": "x", "op": "gt", "value": 1}), {"x": "str"}, {}) is False


def test_all_any_not_composition():
    f = {"a": 1, "b": 2}
    c = cond({"all": [{"flag": "a", "op": "eq", "value": 1}, {"flag": "b", "op": "eq", "value": 2}]})
    assert eval_condition(c, f, {})
    c = cond({"any": [{"flag": "a", "op": "eq", "value": 9}, {"flag": "b", "op": "eq", "value": 2}]})
    assert eval_condition(c, f, {})
    c = cond({"not": {"flag": "a", "op": "eq", "value": 9}})
    assert eval_condition(c, f, {})


def test_none_condition_always_matches():
    assert eval_condition(None, {}, {}) is True


def test_segment_reference():
    segs = {"beta": Segment("beta", cond({"flag": "beta_opt_in", "op": "eq", "value": True}))}
    c = cond({"segment": "beta"})
    assert eval_condition(c, {"beta_opt_in": True}, segs)
    assert not eval_condition(c, {"beta_opt_in": False}, segs)
    # unknown segment never matches
    assert not eval_condition(cond({"segment": "ghost"}), {}, segs)


def test_segment_cycle_is_safe():
    segs = {"a": Segment("a", cond({"segment": "a"}))}
    assert eval_condition(cond({"segment": "a"}), {}, segs) is False
