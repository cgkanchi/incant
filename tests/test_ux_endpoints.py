"""Endpoints behind three UX-honesty fixes.

  * GET /mgmt/envs/{env}/rules?project=<p> — a NARROWER door on the env-wide rule list so a
    project-scoped viewer (whom the UI's best-role-in-any-scope chrome gating lets reach a
    prompt screen) reads the rules governing THEIR project instead of a swallowed-403 empty
    list. The param requires viewer on that project (in this env) and returns the project's
    own prompt-scoped rules plus every global rule; other projects' rules stay hidden. No
    param → unchanged (env-wide viewer, full list).

  * GET /mgmt/overview — a per-prompt `drafts_needing_review` count that is truthful under a
    review policy: OPEN drafts on prompts whose PROJECT requires review (review_policy > 0).

Boot/auth/idiom helpers are reused straight from tests/test_server.py.
"""

from __future__ import annotations

import pytest

from .test_server import auth, make_client, make_key


@pytest.fixture()
def client(tmp_path):
    with make_client(tmp_path) as c:
        yield c


# ── GET /mgmt/envs/{env}/rules?project=<p> ───────────────────────────

def test_project_scoped_rules_read(client):
    # Seed gives project `support` two prompt-scoped rules (beta-gets-v3, team-x-tip). Add a
    # rule to project `shared` (something to EXCLUDE) and a global rule (something to INCLUDE)
    # so the filter has both sides to prove. Rule upserts carry no type-to-confirm even on a
    # locked env (DESIGN.md §7), so no confirm token is needed on prod.
    assert client.post(
        "/mgmt/envs/prod/rules",
        json={"id": "shared-only", "scope": "prompt",
              "prompt_id": "shared/style/language-rules", "priority": 30,
              "serve": {"version": 1}, "comment": "shared project rule"},
        headers=auth()).status_code == 200
    assert client.post(
        "/mgmt/envs/prod/rules",
        json={"id": "glob-voice", "scope": "global", "priority": 5,
              "serve": {"label": "voice-v2"}, "comment": "global rule"},
        headers=auth()).status_code == 200

    # A principal with viewer ONLY on project `support` (+ prod).
    viewer = make_key(client, "viewer", project="support", env="prod")

    # No param → the env-WIDE viewer check → 403 (a project viewer isn't instance/env-wide).
    assert client.get("/mgmt/envs/prod/rules", headers=auth(viewer)).status_code == 403
    # Scoped to a project they can't see → 403.
    assert client.get("/mgmt/envs/prod/rules?project=shared",
                      headers=auth(viewer)).status_code == 403

    # Scoped to their OWN project → 200 with support's prompt-scoped rules + the global rule,
    # and NOT shared's rule.
    r = client.get("/mgmt/envs/prod/rules?project=support", headers=auth(viewer))
    assert r.status_code == 200, r.text
    body = r.json()
    ids = {rule["id"] for rule in body["rules"]}
    assert {"beta-gets-v3", "team-x-tip"} <= ids   # support's own prompt-scoped rules
    assert "glob-voice" in ids                      # global rules govern every project's prompts
    assert "shared-only" not in ids                 # another project's rule stays hidden
    # Every returned prompt-scoped rule really is support's (global rules aside).
    for rule in body["rules"]:
        assert rule["scope"] == "global" or rule["prompt_id"].split("/", 1)[0] == "support"
    # kills/defaults are filtered to the project too, for internal consistency.
    assert all(k.split("/", 1)[0] == "support" for k in body["defaults"])
    assert "shared/style/language-rules" not in body["defaults"]
    assert all(k.split("/", 1)[0] == "support" for k in body["kills"])


def test_env_wide_rules_read_unchanged(client):
    # Regression pin: an env-wide viewer (bootstrap admin holds instance-wide admin) with NO
    # param gets the full, UNFILTERED response — every project's rules and defaults.
    r = client.get("/mgmt/envs/prod/rules", headers=auth())
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["protected"] is True
    ids = {rule["id"] for rule in body["rules"]}
    assert {"beta-gets-v3", "team-x-tip"} <= ids
    # defaults span BOTH projects (no project filter applied) — support/system and the shared
    # fragment both carry a prod default in the seed.
    assert body["defaults"]["support/system"] == 2
    assert "shared/style/language-rules" in body["defaults"]
    # passing project=None explicitly (query absent) is the unfiltered path; the shape carries
    # kills + defaults maps as before.
    assert "kills" in body and "defaults" in body


# ── GET /mgmt/overview: drafts_needing_review ────────────────────────

def test_overview_drafts_needing_review(client):
    def rows_by_id(environment="prod"):
        ov = client.get(f"/mgmt/overview?environment={environment}", headers=auth()).json()
        return {p["prompt_id"]: p for proj in ov["projects"] for p in proj["prompts"]}

    # support has review_policy=1 and one seeded OPEN draft on support/system → needs review.
    sys = rows_by_id()["support/system"]
    assert sys["open_drafts"] == 1
    assert sys["drafts_needing_review"] == 1

    # A no-review project (a freshly created project defaults to review_policy 0): an open
    # draft there is in-flight but does NOT "need review".
    client.post("/mgmt/prompts", json={"prompt_id": "growth/welcome"}, headers=auth())
    client.post("/mgmt/prompts/growth/welcome/drafts",
                json={"version_number": 1, "content": "Hi {{ name }}"}, headers=auth())
    welcome = rows_by_id()["growth/welcome"]
    assert welcome["open_drafts"] == 1
    assert welcome["drafts_needing_review"] == 0     # policy 0 → nothing to review

    # An APPROVED draft under a review policy is no longer outstanding. support/greeting has no
    # seeded draft; create one under support (policy 1) and self-approve it (allowed by default).
    d = client.post("/mgmt/prompts/support/greeting/drafts",
                    json={"version_number": 1,
                          "content": "Hello {{ customer_name }} — approved draft"},
                    headers=auth()).json()
    client.post(f"/mgmt/drafts/{d['id']}/review", json={}, headers=auth())
    assert client.get(f"/mgmt/drafts/{d['id']}", headers=auth()).json()["status"] == "approved"
    greet = rows_by_id()["support/greeting"]
    assert greet["open_drafts"] == 1                 # open OR approved → in-flight
    assert greet["drafts_needing_review"] == 0       # approved → not awaiting review
