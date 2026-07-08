from incant.core import extract


def test_design_example_inference():
    # The exact template from the design's draft editor screen.
    source = (
        "You are a support agent for {{ customer_name }}.\n"
        "Match the customer's tone; default to warm and concise.\n"
        "{% if plan_name %}The customer is on the {{ plan_name }} plan.{% endif %}\n"
        '{% include "shared/style/language-rules" %}\n'
        "Never promise timelines you cannot verify.\n"
        "{% for m in history %}{{ m.text }}{% endfor %}\n"
    )
    v = extract(source)
    assert v.names == {"customer_name", "plan_name", "history"}
    assert v.required == {"customer_name"}      # bare {{ }} usage
    assert v.optional == {"plan_name", "history"}  # guarded by if / for-iterable
    assert v.includes == ("shared/style/language-rules",)


def test_default_filter_marks_optional():
    v = extract("{{ greeting | default('hi') }} {{ name }}")
    assert v.required == {"name"}
    assert v.optional == {"greeting"}


def test_is_defined_guard():
    v = extract("{% if tone is defined %}{{ tone }}{% endif %}")
    assert v.optional == {"tone"}
    assert v.required == set()


def test_var_used_both_guarded_and_bare_is_required():
    v = extract("{{ x }}{% if x %}{{ x }}{% endif %}")
    assert v.required == {"x"}


def test_condition_reference_is_optional():
    # tier only appears in an if-test comparison -> optional (guarded usage).
    v = extract("{% if tier == 'pro' %}pro{% endif %}")
    assert v.optional == {"tier"}
