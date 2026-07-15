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


# ── (project, environment)-scoped viewer: no navigable dead ends ─────
#
# A binding scoped to (project=support, env=prod) used to hit dead-end 403s on reads the
# overview itself linked to: get_versions required project-only viewer (which an env-scoped
# binding can't satisfy), the env-agnostic prompt reads did the same, and revisions/pointer
# history required env-WIDE viewer. These pin the fixes end to end.

def test_scoped_viewer_end_to_end(client):
    # Seed a GLOBAL rule so the revisions log has an env-wide revision to EXCLUDE from the
    # project-scoped view (the seed's beta-gets-v3/team-x-tip give the support side to INCLUDE).
    assert client.post(
        "/mgmt/envs/prod/rules",
        json={"id": "glob-test", "scope": "global", "priority": 5,
              "serve": {"label": "voice-v2"}, "comment": "global rule"},
        headers=auth()).status_code == 200

    v = make_key(client, "viewer", project="support", env="prod")

    # overview — passes `environment`, so the (support, prod) viewer sees support's prompts.
    ov = client.get("/mgmt/overview?environment=prod", headers=auth(v))
    assert ov.status_code == 200, ov.text
    pids = {p["prompt_id"] for proj in ov.json()["projects"] for p in proj["prompts"]}
    assert "support/system" in pids

    # get_versions — authorizes on the concrete env now, so the env-scoped viewer passes.
    vs = client.get("/mgmt/prompts/support/system/versions?environment=prod", headers=auth(v))
    assert vs.status_code == 200, vs.text
    v2 = next(x for x in vs.json()["versions"] if x["version"] == 2)

    # Env-AGNOSTIC prompt reads (ANY_ENVIRONMENT): variables, test contexts, source diff, drafts.
    assert client.get("/mgmt/prompts/support/system/variables?version=2",
                      headers=auth(v)).status_code == 200
    assert client.get("/mgmt/prompts/support/system/test-contexts",
                      headers=auth(v)).status_code == 200
    diff = client.get(
        f"/mgmt/prompts/support/system/diff?a_version=2&a_sha={v2['live_full_sha']}"
        f"&b_version=2&b_sha={v2['tip_full_sha']}&mode=source", headers=auth(v))
    assert diff.status_code == 200, diff.text
    dl = client.get("/mgmt/prompts/support/system/drafts", headers=auth(v))
    assert dl.status_code == 200, dl.text
    assert dl.json()["drafts"], "seed leaves an open draft on support/system"
    assert client.get(f"/mgmt/drafts/{dl.json()['drafts'][0]['id']}",
                      headers=auth(v)).status_code == 200

    # rules?project=support — the narrower door (covered in depth above; pinned live in the flow).
    assert client.get("/mgmt/envs/prod/rules?project=support",
                      headers=auth(v)).status_code == 200

    # revisions — the env-wide door 403s, a project they can't see 403s, and the project door
    # 200s filtered to support (its own rule revisions in, the global-rule revision out).
    assert client.get("/mgmt/envs/prod/revisions", headers=auth(v)).status_code == 403
    assert client.get("/mgmt/envs/prod/revisions?project=shared",
                      headers=auth(v)).status_code == 403
    rv = client.get("/mgmt/envs/prod/revisions?project=support", headers=auth(v))
    assert rv.status_code == 200, rv.text
    revs = rv.json()["revisions"]
    for rev in revs:                                  # every kept revision names a support prompt
        pid = (rev["snapshot"] or {}).get("prompt_id")
        assert pid and pid.split("/", 1)[0] == "support", rev
    assert any(rev["kind"] == "rule" and rev["rule_id"] in ("beta-gets-v3", "team-x-tip")
               for rev in revs), "support's own rule revisions are included"
    assert not any(rev.get("rule_id") == "glob-test" for rev in revs), \
        "global-rule revisions are env-wide info, excluded in project mode"

    # per-prompt publish history — now authorized on (prompt's project, env), not env-wide.
    assert client.get("/mgmt/envs/prod/pointers?prompt_id=support/system&version=2",
                      headers=auth(v)).status_code == 200

    # Scoping still enforced: the SAME key can't read another project's versions or variables.
    assert client.get("/mgmt/prompts/shared/style/language-rules/versions?environment=prod",
                      headers=auth(v)).status_code == 403
    assert client.get("/mgmt/prompts/shared/style/language-rules/variables?version=1",
                      headers=auth(v)).status_code == 403


def test_env_wide_revisions_and_history_unchanged(client):
    # Regression pins for the unscoped callers. WITHOUT the project param, revisions still
    # returns the full log — global-rule + segment revisions and cross-project defaults all
    # present — and the per-prompt publish history stays readable by an env-wide viewer.
    client.post("/mgmt/envs/prod/rules",
                json={"id": "glob-test", "scope": "global", "priority": 5,
                      "serve": {"label": "voice-v2"}, "comment": "global rule"},
                headers=auth())
    full = client.get("/mgmt/envs/prod/revisions", headers=auth()).json()["revisions"]
    assert any(rev.get("rule_id") == "glob-test" for rev in full)     # global-rule revision kept
    assert any(rev["kind"] == "segment" for rev in full)              # segment revision kept
    assert any((rev["snapshot"] or {}).get("prompt_id") == "shared/style/language-rules"
               for rev in full)                                       # cross-project default kept

    # An env-wide viewer (project=None, env=prod) reads both, unchanged.
    ew = make_key(client, "viewer", env="prod")
    assert client.get("/mgmt/envs/prod/revisions", headers=auth(ew)).status_code == 200
    assert client.get("/mgmt/envs/prod/pointers?prompt_id=support/system&version=2",
                      headers=auth(ew)).status_code == 200


# ── Identity.has: ANY_ENVIRONMENT sentinel semantics ─────────────────

def test_any_environment_sentinel_semantics():
    from incant.server.auth import ANY_ENVIRONMENT, Binding, Identity

    env_scoped = Identity("p", "n", [Binding("viewer", "support", "prod")])
    # The dead-end the sentinel fixes: a project-only check (environment defaults to None) is
    # NOT satisfied by an env-scoped binding.
    assert env_scoped.has("viewer", project="support") is False
    # ANY_ENVIRONMENT waives the env dimension → the env-scoped binding now matches.
    assert env_scoped.has("viewer", project="support", environment=ANY_ENVIRONMENT) is True
    # It still matches its OWN concrete env and rejects a different one.
    assert env_scoped.has("viewer", project="support", environment="prod") is True
    assert env_scoped.has("viewer", project="support", environment="staging") is False
    # Project MISMATCH still fails even under ANY_ENVIRONMENT — scoping isn't lost.
    assert env_scoped.has("viewer", project="other", environment=ANY_ENVIRONMENT) is False

    # An instance-wide binding (both None) covers everything, incl. ANY_ENVIRONMENT.
    inst = Identity("p", "n", [Binding("admin", None, None)])
    assert inst.has("viewer", project="support", environment=ANY_ENVIRONMENT) is True
    assert inst.has("viewer", project="anything", environment="whatever") is True

    # A project-only binding (no env) matches ANY and any concrete env for its project.
    proj_only = Identity("p", "n", [Binding("viewer", "support", None)])
    assert proj_only.has("viewer", project="support", environment=ANY_ENVIRONMENT) is True
    assert proj_only.has("viewer", project="support", environment="prod") is True
    assert proj_only.has("viewer", project="other", environment=ANY_ENVIRONMENT) is False
