import pytest

from incant.core import (
    IncludeCycle,
    MissingVariable,
    RenderError,
    parse_rule,
    render,
)

from .conftest import DictContent, snapshot, vinfo

SYS = "support/system"
FRAG = "support/style/language-rules"


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


def test_missing_required_in_filter_raises_not_silent():
    # §1.2: `{{ history | length }}` with history missing must 422, not render "0".
    content = DictContent({(SYS, "c1"): "n={{ history | length }}"})
    snap = snapshot(versions={SYS: {1: vinfo(1, live="c1")}}, defaults={SYS: 1})
    with pytest.raises(MissingVariable) as e:
        render(snap, SYS, {}, {}, content)
    assert e.value.name == "history"


def test_missing_required_in_inline_if_raises_not_silent():
    # §1.2: `{{ 'yes' if x else 'no' }}` with x missing must 422, not render "no".
    content = DictContent({(SYS, "c1"): "{{ 'yes' if x else 'no' }}"})
    snap = snapshot(versions={SYS: {1: vinfo(1, live="c1")}}, defaults={SYS: 1})
    with pytest.raises(MissingVariable) as e:
        render(snap, SYS, {}, {}, content)
    assert e.value.name == "x"


def test_missing_required_in_comparison_test_raises():
    # §1.2 mirror: `{% if tier == 'pro' %}` — tier is required; missing must 422.
    content = DictContent({(SYS, "c1"): "{% if tier == 'pro' %}P{% endif %}done"})
    snap = snapshot(versions={SYS: {1: vinfo(1, live="c1")}}, defaults={SYS: 1})
    with pytest.raises(MissingVariable) as e:
        render(snap, SYS, {}, {}, content)
    assert e.value.name == "tier"


def test_guarded_optional_renders_when_missing():
    # The other half: guarded-optional vars still render (empty) without a value.
    content = DictContent({
        (SYS, "c1"): "{% if plan %}{{ plan }}{% endif %}{% for m in items %}{{ m }}{% endfor %}ok"
    })
    snap = snapshot(versions={SYS: {1: vinfo(1, live="c1")}}, defaults={SYS: 1})
    assert render(snap, SYS, {}, {}, content).text == "ok"


def test_fragments_optional_var_is_lenient_across_closure():
    # A guarded-optional variable inside an included fragment renders leniently too.
    content = DictContent({
        (SYS, "c1"): 'top {% include "support/style/language-rules" %}',
        (FRAG, "f1"): "{% if extra %}{{ extra }}{% endif %}frag",
    })
    snap = snapshot(
        versions={SYS: {1: vinfo(1, live="c1")}, FRAG: {1: vinfo(1, live="f1")}},
        defaults={SYS: 1, FRAG: 1},
    )
    assert render(snap, SYS, {}, {}, content).text == "top frag"


def test_defaults_applied_pre_render():
    content = DictContent({(SYS, "c1"): "{% if tone %}{{ tone }}{% endif %}done"})
    snap = snapshot(versions={SYS: {1: vinfo(1, live="c1")}}, defaults={SYS: 1})
    r = render(snap, SYS, {}, {}, content, defaults={"tone": "warm"})
    assert r.text == "warmdone"


def test_include_resolves_through_targeting():
    content = DictContent({
        (SYS, "c1"): 'A {% include "support/style/language-rules" %} B',
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
        (SYS, "c1"): '[{% include "support/style/language-rules" %}]',
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


def test_nested_fragment_defaults_follow_resolved_versions():
    leaf = "shared/leaf"
    content = DictContent({
        (SYS, "c1"): '{% include "support/style/language-rules" %}',
        (FRAG, "f1"): 'style={% include "shared/leaf" %}',
        (leaf, "l1"): "{{ tone }}-{{ audience }}",
    })
    snap = snapshot(
        versions={SYS: {1: vinfo(1, live="c1")}, FRAG: {1: vinfo(1, live="f1")},
                  leaf: {1: vinfo(1, live="l1")}},
        defaults={SYS: 1, FRAG: 1, leaf: 1},
    )
    snap.refinement_defaults = {
        (FRAG, 1): {"tone": "warm"},
        (leaf, 1): {"audience": "developers"},
    }
    assert render(snap, SYS, {}, {}, content).text == "style=warm-developers"
    # Request variables always beat configured defaults from any contributor.
    assert render(snap, SYS, {}, {"tone": "direct"}, content).text == \
        "style=direct-developers"


def test_fragment_default_follows_flag_targeted_version():
    content = DictContent({
        (SYS, "c1"): '{% include "support/style/language-rules" %}',
        (FRAG, "f1"): "{{ voice }}", (FRAG, "f2"): "{{ voice }}",
    })
    snap = snapshot(
        versions={SYS: {1: vinfo(1, live="c1")},
                  FRAG: {1: vinfo(1, live="f1"), 2: vinfo(2, live="f2")}},
        defaults={SYS: 1, FRAG: 1},
        rules=[parse_rule({"id": "ent", "scope": "prompt", "prompt_id": FRAG,
                           "priority": 1,
                           "when": {"flag": "tier", "op": "eq", "value": "enterprise"},
                           "serve": {"version": 2}})],
    )
    snap.refinement_defaults = {
        (FRAG, 1): {"voice": "plain"},
        (FRAG, 2): {"voice": "formal"},
    }
    assert render(snap, SYS, {"tier": "free"}, {}, content).text == "plain"
    assert render(snap, SYS, {"tier": "enterprise"}, {}, content).text == "formal"


def test_conflicting_contributor_defaults_are_rejected():
    content = DictContent({
        (SYS, "c1"): '{% include "support/style/language-rules" %}',
        (FRAG, "f1"): "{{ tone }}",
    })
    snap = snapshot(
        versions={SYS: {1: vinfo(1, live="c1")}, FRAG: {1: vinfo(1, live="f1")}},
        defaults={SYS: 1, FRAG: 1},
    )
    snap.refinement_defaults = {
        (SYS, 1): {"tone": "warm"},
        (FRAG, 1): {"tone": "formal"},
    }
    with pytest.raises(RenderError, match="conflicting refinement default"):
        render(snap, SYS, {}, {}, content)


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


def test_pin_bypasses_targeting():
    # §9 replay: a pin resolves the prompt to the exact (version, commit), ignoring
    # rules/defaults/tip.
    content = DictContent({(SYS, "v1c"): "one {{ n }}", (SYS, "v2c"): "two {{ n }}"})
    snap = snapshot(
        versions={SYS: {1: vinfo(1, live="v1c"), 2: vinfo(2, live="v2c")}},
        defaults={SYS: 2},
    )
    assert render(snap, SYS, {}, {"n": "x"}, content).text == "two x"       # default v2
    r = render(snap, SYS, {}, {"n": "x"}, content, pin={SYS: (1, "v1c")})   # pinned v1
    assert r.text == "one x"
    assert r.contributions[SYS].version == 1 and r.contributions[SYS].commit == "v1c"


def test_pin_bypasses_include_targeting():
    content = DictContent({
        (SYS, "c1"): '[{% include "support/style/language-rules" %}]',
        (FRAG, "f1"): "v1-rules", (FRAG, "f2"): "v2-rules",
    })
    snap = snapshot(
        versions={SYS: {1: vinfo(1, live="c1")},
                  FRAG: {1: vinfo(1, live="f1"), 2: vinfo(2, live="f2")}},
        defaults={SYS: 1, FRAG: 1},
    )
    # Default fragment is v1; pin the fragment to v2.
    r = render(snap, SYS, {}, {}, content, pin={FRAG: (2, "f2")})
    assert r.text == "[v2-rules]"


def test_within_version_fallback_on_unfetchable_live_content():
    # §1.5/§10: the live SHA is validated (servable=True) but its content is
    # unfetchable (cache lost + store unreachable -> KeyError). Serve the previous
    # live SHA's content with content_fallback=True, rather than 409-ing.
    content = DictContent({(SYS, "old"): "old-content"})   # note: no (SYS, "live")
    snap = snapshot(
        versions={SYS: {1: vinfo(1, live="live", previous=("old",))}},
        defaults={SYS: 1},                                 # servable default: all True
    )
    r = render(snap, SYS, {}, {}, content)
    assert r.text == "old-content" and r.content_fallback is True
    assert r.contributions[SYS].commit == "old"
    assert r.contributions[SYS].content_fallback is True


def test_pinned_sha_does_not_fall_back_on_missing_content():
    # A pinned-SHA resolution must NOT degrade to a previous SHA — it 409s (KeyError).
    import pytest as _pytest
    live_sha = "a" * 40
    content = DictContent({(SYS, "old"): "old"})
    snap = snapshot(
        versions={SYS: {1: vinfo(1, live=live_sha, previous=("old",))}},
        defaults={SYS: 1},
        rules=[parse_rule({"id": "pin", "scope": "prompt", "prompt_id": SYS, "priority": 1,
                           "when": None,
                           "serve": {"version": 1, "at": "sha", "sha": live_sha}})],
    )
    with _pytest.raises(KeyError):
        render(snap, SYS, {}, {}, content)


def test_content_fallback_flag_propagates():
    content = DictContent({(SYS, "old"): "old-content"})
    dead = {"live"}
    snap = snapshot(
        versions={SYS: {1: vinfo(1, live="live", previous=("old",))}},
        defaults={SYS: 1},
        servable=lambda p, v, s: s not in dead,
    )
    r = render(snap, SYS, {}, {}, content)
    assert r.text == "old-content" and r.content_fallback is True
    assert r.contributions[SYS].content_fallback is True


def test_compiled_cache_thread_safe_under_churn(monkeypatch):
    # Sync routes run in a thread pool: the LRU's compound get/move/evict must hold
    # under contention with a deliberately tiny capacity forcing constant eviction.
    import concurrent.futures as cf

    import sys

    import incant.core.render  # noqa: F401 — the package re-exports the render FUNCTION
    render_mod = sys.modules["incant.core.render"]  # under the same name; go via sys.modules

    monkeypatch.setattr(render_mod, "_CACHE_MAX", 4)

    def churn(worker: int) -> str:
        for i in range(300):
            blob = f"w{worker}-b{i % 9}"
            render_mod._compile(render_mod._ENV, blob, f"text {worker} {i % 9}", blob)
            render_mod._extract_cached(blob, f"{{{{ v{i % 9} }}}}")
        return "ok"

    with cf.ThreadPoolExecutor(max_workers=8) as pool:
        results = [f.result() for f in [pool.submit(churn, w) for w in range(8)]]
    assert results == ["ok"] * 8
    assert len(render_mod._COMPILED) <= 4 and len(render_mod._EXTRACT) <= 4


# ── rendered-output budget ────────────────────────────────────────────

@pytest.fixture()
def tiny_render_budget():
    from incant.core.render import MAX_RENDER_BYTES, configure_limits
    configure_limits(max_render_bytes=64)
    yield
    configure_limits(max_render_bytes=MAX_RENDER_BYTES)


def test_output_over_the_render_budget_is_a_render_error(tiny_render_budget):
    # The sandbox bounds `range`, not a caller-supplied list: this is the unbounded case.
    content = DictContent({(SYS, "c1"): "{% for m in items %}{{ m }}{% endfor %}"})
    snap = snapshot(versions={SYS: {1: vinfo(1, live="c1")}}, defaults={SYS: 1})
    assert render(snap, SYS, {}, {"items": ["x"] * 64}, content).text == "x" * 64  # at the cap
    with pytest.raises(RenderError) as e:
        render(snap, SYS, {}, {"items": ["x"] * 65}, content)
    assert "65 bytes" in str(e.value) and "64-byte" in str(e.value)
    assert e.value.prompt_id == SYS
    # Measured in encoded bytes, not characters: 40 two-byte characters is 80 bytes.
    with pytest.raises(RenderError):
        render(snap, SYS, {}, {"items": ["é"] * 40}, content)


def test_render_budget_default_is_generous():
    from incant.core.render import MAX_RENDER_BYTES, _max_render_bytes
    assert _max_render_bytes == MAX_RENDER_BYTES == 2 * 1024 * 1024


def test_render_wall_clock_budget_cuts_a_runaway_loop():
    # A caller-supplied outer list x a large inner loop burns time while emitting
    # almost no output — the byte cap never fires, so the deadline must. Checked
    # between template writes: each outer iteration emits one chunk.
    from incant.core.render import MAX_RENDER_SECONDS, configure_limits
    configure_limits(max_render_seconds=0.05)
    try:
        content = DictContent({(SYS, "c1"):
            "{% for a in xs %}.{% for b in ys %}{% endfor %}{% endfor %}"})
        snap = snapshot(versions={SYS: {1: vinfo(1, live="c1")}}, defaults={SYS: 1})
        with pytest.raises(RenderError) as e:
            render(snap, SYS, {}, {"xs": list(range(5000)), "ys": list(range(20000))}, content)
        assert "time budget" in str(e.value)
    finally:
        configure_limits(max_render_seconds=MAX_RENDER_SECONDS)


def test_render_time_budget_default_is_generous():
    from incant.core.render import MAX_RENDER_SECONDS, _max_render_seconds
    assert _max_render_seconds == MAX_RENDER_SECONDS == 5.0

