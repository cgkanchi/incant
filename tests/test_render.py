import pytest

from incant.core import (
    IncludeCycle,
    IncludeDepthExceeded,
    MissingVariable,
    parse_rule,
    render,
)

from .conftest import DictContent, snapshot, vinfo

SYS = "support/system"
FRAG = "shared/style/language-rules"


def test_basic_render():
    content = DictContent({(SYS, "c1"): "Hello {{ name }}!"})
    snap = snapshot(versions={SYS: {1: vinfo(1, live="c1")}}, defaults={SYS: 1})
    r = render(snap, SYS, {}, {"name": "Acme"}, content)
    assert r.text == "Hello Acme!"
    assert r.root.version == 1
    assert r.contributions[SYS].commit == "c1"


def test_missing_required_variable_raises_named():
    content = DictContent({(SYS, "c1"): "Hi {{ name }}"})
    snap = snapshot(versions={SYS: {1: vinfo(1, live="c1")}}, defaults={SYS: 1})
    with pytest.raises(MissingVariable) as e:
        render(snap, SYS, {}, {}, content)
    assert e.value.name == "name"


def test_defaults_applied_pre_render():
    content = DictContent({(SYS, "c1"): "{% if tone %}{{ tone }}{% endif %}done"})
    snap = snapshot(versions={SYS: {1: vinfo(1, live="c1")}}, defaults={SYS: 1})
    r = render(snap, SYS, {}, {}, content, defaults={"tone": "warm"})
    assert r.text == "warmdone"


def test_include_resolves_through_targeting():
    content = DictContent({
        (SYS, "c1"): 'A {% include "shared/style/language-rules" %} B',
        (FRAG, "f1"): "PLAIN-ENGLISH",
    })
    snap = snapshot(
        versions={SYS: {1: vinfo(1, live="c1")}, FRAG: {1: vinfo(1, live="f1")}},
        defaults={SYS: 1, FRAG: 1},
    )
    r = render(snap, SYS, {}, {}, content)
    assert r.text == "A PLAIN-ENGLISH B"
    # both prompts reported in contributions with resolved SHAs
    assert set(r.contributions) == {SYS, FRAG}
    assert r.contributions[FRAG].commit == "f1"


def test_include_follows_flag_targeting():
    # A rule targets the fragment's v2 for enterprise; everyone else gets v1.
    content = DictContent({
        (SYS, "c1"): '[{% include "shared/style/language-rules" %}]',
        (FRAG, "f1"): "v1-rules",
        (FRAG, "f2"): "v2-rules",
    })
    snap = snapshot(
        versions={
            SYS: {1: vinfo(1, live="c1")},
            FRAG: {1: vinfo(1, live="f1"), 2: vinfo(2, live="f2")},
        },
        defaults={SYS: 1, FRAG: 1},
        rules=[parse_rule({"id": "ent", "scope": "prompt", "prompt_id": FRAG, "priority": 1,
                           "when": {"flag": "tier", "op": "eq", "value": "enterprise"},
                           "serve": {"version": 2}})],
    )
    assert render(snap, SYS, {"tier": "enterprise"}, {}, content).text == "[v2-rules]"
    assert render(snap, SYS, {"tier": "free"}, {}, content).text == "[v1-rules]"


def test_cycle_detected_at_render():
    content = DictContent({
        ("a", "ca"): '{% include "b" %}',
        ("b", "cb"): '{% include "a" %}',
    })
    snap = snapshot(
        versions={"a": {1: vinfo(1, live="ca")}, "b": {1: vinfo(1, live="cb")}},
        defaults={"a": 1, "b": 1},
    )
    with pytest.raises(IncludeCycle):
        render(snap, "a", {}, {}, content)


def test_diamond_include_is_allowed():
    # top includes left and right, both include the same shared fragment: not a cycle.
    content = DictContent({
        ("top", "t"): '{% include "left" %}{% include "right" %}',
        ("left", "l"): 'L{% include "shared" %}',
        ("right", "r"): 'R{% include "shared" %}',
        ("shared", "s"): "S",
    })
    snap = snapshot(
        versions={k: {1: vinfo(1, live=v)} for k, v in
                  [("top", "t"), ("left", "l"), ("right", "r"), ("shared", "s")]},
        defaults={"top": 1, "left": 1, "right": 1, "shared": 1},
    )
    assert render(snap, "top", {}, {}, content).text == "LSRS"


def test_sandbox_blocks_attribute_access():
    from incant.core import RenderError
    content = DictContent({(SYS, "c1"): "{{ ''.__class__ }}"})
    snap = snapshot(versions={SYS: {1: vinfo(1, live="c1")}}, defaults={SYS: 1})
    with pytest.raises((RenderError,)):
        render(snap, SYS, {}, {}, content)


def test_content_fallback_flag_propagates():
    content = DictContent({(SYS, "old"): "old-content"})
    dead = {"live"}
    snap = snapshot(
        versions={SYS: {1: vinfo(1, live="live", previous=("old",))}},
        defaults={SYS: 1},
        servable=lambda p, s: s not in dead,
    )
    r = render(snap, SYS, {}, {}, content)
    assert r.text == "old-content" and r.content_fallback is True
    assert r.contributions[SYS].content_fallback is True
