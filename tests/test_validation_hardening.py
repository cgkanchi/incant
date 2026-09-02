"""Hardening fixes: deterministic equal-priority rules (F3), git subjects with
separator bytes (F6), malformed-input validation (F7), strict bool/number clause
equality (F8), unknown-test-context refusal, and the password-change throttle gate.
"""

from __future__ import annotations

import pytest

from incant.core import parse_rule, resolve
from incant.core.clauses import eval_clause
from incant.core.model import Clause
from incant.core.parse import parse_condition
from incant.db import session_scope

from .conftest import snapshot, vinfo
from .test_server import auth, make_client

PID = "support/system"


# ── F3: equal-priority rules resolve deterministically ───────────────

def _same_priority_rules(order):
    rules = [
        parse_rule({"id": "za-rule", "scope": "prompt", "prompt_id": PID, "priority": 40,
                    "when": None, "serve": {"version": 3}}),
        parse_rule({"id": "ab-rule", "scope": "prompt", "prompt_id": PID, "priority": 40,
                    "when": None, "serve": {"version": 1}}),
    ]
    return rules if order == "za-first" else list(reversed(rules))


@pytest.mark.parametrize("order", ["za-first", "ab-first"])
def test_equal_priority_rules_lower_id_wins_regardless_of_input_order(order):
    # Same priority, opposite insertion orders: the LOWER id must win both times.
    # (Python's sort is stable, so a priority-only key leaked input order through.)
    snap = snapshot(
        versions={PID: {1: vinfo(1, live="c1"), 3: vinfo(3, live="c3")}},
        defaults={PID: 1},
        rules=_same_priority_rules(order),
    )
    res = resolve(snap, PID, {})
    assert res.rule_id == "ab-rule" and res.version == 1
    assert [r.id for r in snap.prompt_rules(PID)] == ["ab-rule", "za-rule"]


def test_equal_priority_global_rules_are_ordered_by_id_too():
    rules = [
        parse_rule({"id": "zz", "scope": "global", "priority": 7, "when": None,
                    "serve": {"label": "x"}}),
        parse_rule({"id": "aa", "scope": "global", "priority": 7, "when": None,
                    "serve": {"label": "x"}}),
    ]
    snap = snapshot(rules=rules)
    assert [r.id for r in snap.global_rules()] == ["aa", "zz"]


def test_snapshot_build_orders_equal_priority_rules_stably(tmp_path):
    # DB-built snapshots must agree with the in-memory sort — and with themselves
    # across rebuilds (the SELECT previously had no ORDER BY at all).
    with make_client(tmp_path) as client:
        for rid in ("za-rule", "ab-rule"):
            r = client.post("/mgmt/envs/prod/rules",
                            json={"id": rid, "scope": "prompt", "prompt_id": PID,
                                  "priority": 40, "when": None,
                                  "serve": {"version": 2}},
                            headers=auth())
            assert r.status_code == 200, r.text

        from incant.targeting import build_snapshot
        with session_scope() as s:
            orders = []
            for _ in range(3):
                snap = build_snapshot(s, "prod")
                mine = [x.id for x in snap.prompt_rules(PID) if x.priority == 40]
                orders.append(mine)
            assert orders[0] == ["ab-rule", "za-rule"]
            assert orders.count(orders[0]) == 3  # stable across rebuilds

        # And through the serving path: the lower id is the one that matches.
        r = client.post(f"/prompt/{PID}/evaluate", json={"flags": {}}, headers=auth())
        assert r.status_code == 200, r.text
        assert r.json()["matched_rule"] == {"scope": "prompt", "id": "ab-rule"}


# ── F6: user-controlled subjects must not break history parsing ─────

NASTY_MESSAGE = "nasty \x1f unit \x1e record subject\ncontinued line\n\nbody text"
# git folds the subject's embedded newlines to spaces; \x1f and \x1e pass through raw.
NASTY_SUBJECT = "nasty \x1f unit \x1e record subject continued line"


def test_history_and_latest_commits_survive_separator_bytes_in_subject(tmp_path):
    from incant.gitstore import GitStore

    g = GitStore(tmp_path / "repo")
    g.init()
    nasty_sha = g.commit_version(PID, 1, "content one", author_name="Mallory",
                                 author_email="m@x", message=NASTY_MESSAGE)
    plain_sha = g.commit_version("support/other", 1, "content two", author_name="Sam",
                                 author_email="s@x", message="a plain subject")

    rows = g.history(f"{PID}/v1.j2")
    assert [(r.sha, r.subject) for r in rows] == [(nasty_sha, NASTY_SUBJECT)]

    # latest_commits walks the whole branch (nasty record in the middle, plus the
    # seed commit's header-only record): every path must map to the right commit.
    latest = g.latest_commits()
    assert latest[f"{PID}/v1.j2"].sha == nasty_sha
    assert latest[f"{PID}/v1.j2"].subject == NASTY_SUBJECT
    assert latest["support/other/v1.j2"].sha == plain_sha
    assert latest["support/other/v1.j2"].subject == "a plain subject"
    # Author fields stay aligned even with separators loose in the subject.
    assert latest[f"{PID}/v1.j2"].author == "Mallory"


def test_commit_endpoint_message_with_separators_keeps_history_serving(tmp_path):
    # End to end: a commit message a user typed must never 500 the history endpoint.
    with make_client(tmp_path) as client:
        d = client.post(f"/mgmt/prompts/{PID}/drafts",
                        json={"version_number": 2, "content": "hello {{ customer_name }}"},
                        headers=auth()).json()
        from .test_server import make_key
        reviewer = make_key(client, "editor", project="support")
        client.post(f"/mgmt/drafts/{d['id']}/review", json={}, headers=auth(reviewer))
        r = client.post(f"/mgmt/drafts/{d['id']}/commit",
                        json={"message": NASTY_MESSAGE}, headers=auth())
        assert r.status_code == 200, r.text
        # /versions calls git.history() per version; /overview calls latest_commits().
        # Both previously unpacked "too many values" on a subject carrying \x1f/\x1e.
        h = client.get(f"/mgmt/prompts/{PID}/versions?environment=prod", headers=auth())
        assert h.status_code == 200, h.text
        v2 = next(x for x in h.json()["versions"] if x["version"] == 2)
        assert any("nasty" in e["subject"] for e in v2["history"])
        o = client.get("/mgmt/overview?environment=prod", headers=auth())
        assert o.status_code == 200, o.text


# ── F7: malformed inputs 422, never 500 ─────────────────────────────

RENDER_BODY = {"variables": {"customer_name": "Acme", "history": []}}


def _render(client, pin):
    return client.post(f"/prompt/{PID}", json={**RENDER_BODY, "pin": pin}, headers=auth())


def test_pin_versions_must_be_an_object(tmp_path):
    with make_client(tmp_path) as client:
        r = _render(client, {"versions": "x"})
        assert r.status_code == 422, r.text
        assert "pin.versions must be an object" in str(r.json()["detail"])


@pytest.mark.parametrize("bad", [1.9, True, 0, -3, "2"])
def test_pin_version_must_be_strict_positive_int(tmp_path, bad):
    with make_client(tmp_path) as client:
        entry = {"version": bad, "commit": "a" * 40}
        for pin in ({"versions": {PID: entry}},  # explicit shape
                    {PID: entry}):               # bare-map back-compat
            r = _render(client, pin)
            assert r.status_code == 422, r.text
            assert "positive integer" in str(r.json()["detail"])


def test_valid_pin_still_replays(tmp_path):
    with make_client(tmp_path) as client:
        first = client.post(f"/prompt/{PID}", json=RENDER_BODY, headers=auth())
        assert first.status_code == 200, first.text
        b = first.json()
        r = _render(client, {"versions": b["versions"], "rules_version": b["rules_version"]})
        assert r.status_code == 200, r.text
        assert r.json()["versions"] == b["versions"]


@pytest.mark.parametrize("field", ["version_number", "seed_from_version"])
@pytest.mark.parametrize("bad", [0, -1, True, 1.5])
def test_create_draft_rejects_non_positive_or_non_int_versions(tmp_path, field, bad):
    with make_client(tmp_path) as client:
        r = client.post(f"/mgmt/prompts/{PID}/drafts",
                        json={field: bad, "content": "x"}, headers=auth())
        assert r.status_code == 422, r.text


def test_create_draft_valid_version_still_works(tmp_path):
    with make_client(tmp_path) as client:
        r = client.post(f"/mgmt/prompts/{PID}/drafts",
                        json={"version_number": 2, "content": "hi {{ customer_name }}"},
                        headers=auth())
        assert r.status_code == 200, r.text


def test_review_state_must_be_a_known_literal(tmp_path):
    with make_client(tmp_path) as client:
        d = client.post(f"/mgmt/prompts/{PID}/drafts",
                        json={"version_number": 2, "content": "x {{ customer_name }}"},
                        headers=auth()).json()
        # Unknown state: 422 at validation, never the DB CHECK constraint (500).
        r = client.post(f"/mgmt/drafts/{d['id']}/review", json={"state": "bogus"},
                        headers=auth())
        assert r.status_code == 422, r.text
        # Both legal states still land.
        from .test_server import make_key
        reviewer = make_key(client, "editor", project="support")
        r = client.post(f"/mgmt/drafts/{d['id']}/review",
                        json={"state": "changes_requested"}, headers=auth(reviewer))
        assert r.status_code == 200, r.text


def _nested_not(depth: int) -> dict:
    cond: dict = {"flag": "x", "op": "exists"}
    for _ in range(depth):
        cond = {"not": cond}
    return cond


def test_parse_condition_caps_depth_with_valueerror_not_recursionerror():
    with pytest.raises(ValueError, match="nested too deep"):
        parse_condition(_nested_not(500))
    # A humanly-deep tree still parses fine.
    assert parse_condition(_nested_not(20)) is not None


def test_deep_condition_is_422_at_rule_save(tmp_path):
    with make_client(tmp_path) as client:
        r = client.post("/mgmt/envs/prod/rules",
                        json={"id": "deep", "scope": "prompt", "prompt_id": PID,
                              "priority": 40, "when": _nested_not(500),
                              "serve": {"version": 2}},
                        headers=auth())
        assert r.status_code == 422, r.text
        assert "nested too deep" in r.text


# ── F8: bool and number flags never compare equal ───────────────────

def _c(op, value=None, values=()):
    return Clause(flag="f", op=op, value=value, values=tuple(values))


def test_eq_never_crosses_bool_number_line():
    assert eval_clause(_c("eq", value=True), {"f": 1}) is False
    assert eval_clause(_c("eq", value=1), {"f": True}) is False
    assert eval_clause(_c("eq", value=False), {"f": 0}) is False
    assert eval_clause(_c("eq", value=0), {"f": False}) is False
    # Same-typed matches are untouched.
    assert eval_clause(_c("eq", value=True), {"f": True}) is True
    assert eval_clause(_c("eq", value=1), {"f": 1}) is True
    assert eval_clause(_c("eq", value=1), {"f": 1.0}) is True   # numeric stays numeric
    assert eval_clause(_c("eq", value=1), {"f": "1"}) is False  # string stays distinct


def test_neq_is_the_strict_complement():
    assert eval_clause(_c("neq", value=True), {"f": 1}) is True
    assert eval_clause(_c("neq", value=1), {"f": True}) is True
    assert eval_clause(_c("neq", value=True), {"f": True}) is False


def test_membership_uses_strict_equality():
    assert eval_clause(_c("in", values=[1, 2]), {"f": True}) is False
    assert eval_clause(_c("in", values=[True, 2]), {"f": 1}) is False
    assert eval_clause(_c("in", values=[True, 2]), {"f": True}) is True
    assert eval_clause(_c("in", values=[1, 2]), {"f": 1}) is True
    assert eval_clause(_c("not_in", values=[1, 2]), {"f": True}) is True
    assert eval_clause(_c("not_in", values=[True]), {"f": True}) is False


# ── unknown named test context refuses instead of falling back ──────

def test_draft_render_unknown_test_context_is_404(tmp_path):
    with make_client(tmp_path) as client:
        d = client.post(f"/mgmt/prompts/{PID}/drafts",
                        json={"version_number": 2, "content": "hi {{ customer_name }}"},
                        headers=auth()).json()
        r = client.post(f"/mgmt/drafts/{d['id']}/render",
                        json={"environment": "prod", "test_context": "no-such-context",
                              "variables": {"customer_name": "Acme"}},
                        headers=auth())
        assert r.status_code == 404, r.text
        assert "no-such-context" in r.json()["detail"]
        # Unnamed: unchanged — request values are used.
        r = client.post(f"/mgmt/drafts/{d['id']}/render",
                        json={"environment": "prod",
                              "variables": {"customer_name": "Acme"}},
                        headers=auth())
        assert r.status_code == 200, r.text
        assert "Acme" in r.json()["rendered"]


def test_draft_diff_unknown_test_context_is_404(tmp_path):
    with make_client(tmp_path) as client:
        d = client.post(f"/mgmt/prompts/{PID}/drafts",
                        json={"version_number": 2, "content": "hi {{ customer_name }}"},
                        headers=auth()).json()
        r = client.get(f"/mgmt/drafts/{d['id']}/diff"
                       "?mode=rendered&environment=prod&test_context=no-such-context",
                       headers=auth())
        assert r.status_code == 404, r.text
        assert "no-such-context" in r.json()["detail"]


def test_version_diff_unknown_test_context_is_404(tmp_path):
    with make_client(tmp_path) as client:
        v = client.get(f"/mgmt/prompts/{PID}/versions?environment=prod",
                       headers=auth()).json()
        v2 = next(x for x in v["versions"] if x["version"] == 2)
        q = (f"a_version=2&a_sha={v2['live_full_sha']}&b_version=2"
             f"&b_sha={v2['tip_full_sha']}&mode=rendered&environment=prod")
        r = client.get(f"/mgmt/prompts/{PID}/diff?{q}&test_context=no-such-context",
                       headers=auth())
        assert r.status_code == 404, r.text
        assert "no-such-context" in r.json()["detail"]
        # Unnamed: unchanged — the first context still backstops the render.
        r = client.get(f"/mgmt/prompts/{PID}/diff?{q}", headers=auth())
        assert r.status_code == 200, r.text
        assert r.json()["context"] == "enterprise-us"


# ── password change is throttled like every other password door ─────

def test_password_change_wrong_current_password_hits_the_gate(tmp_path):
    with make_client(tmp_path, auth_throttle_limit=3, auth_throttle_window=60.0) as client:
        r = client.post("/auth/setup", json={"name": "Pat", "email": "pat@example.com",
                                             "password": "correct-horse-battery"})
        assert r.status_code == 200, r.text
        csrf = r.json()["csrf"]
        for _ in range(3):
            r = client.post("/auth/password",
                            json={"current_password": "wrong-guess-entirely",
                                  "new_password": "an-even-better-one"},
                            headers={"X-Incant-CSRF": csrf})
            assert r.status_code == 403, r.text
        # Gate trips BEFORE verification — even the correct password waits now.
        r = client.post("/auth/password",
                        json={"current_password": "correct-horse-battery",
                              "new_password": "an-even-better-one"},
                        headers={"X-Incant-CSRF": csrf})
        assert r.status_code == 429 and r.headers.get("Retry-After"), r.text
