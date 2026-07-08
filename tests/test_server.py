"""HTTP-level tests over the FastAPI app: auth, serving, mgmt, the tweak flow."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import datetime as dt

from sqlalchemy import select

from incant import db, models
from incant.config import Settings, set_settings
from incant.db import session_scope
from incant.seed import seed
from incant.server.auth import hash_key, key_prefix
from incant.service import reset_app

from .conftest import db_url_for, reset_schema

ADMIN = "incant_sk_dev_admin"


@pytest.fixture()
def client(tmp_path):
    set_settings(Settings(
        database_url=db_url_for(tmp_path),
        repo_path=str(tmp_path / "repo"),
        bootstrap_admin_key=ADMIN,
    ))
    db.reset_engine()
    reset_app()
    reset_schema()
    renderer_key = seed()
    from incant.server.app import create_app
    with TestClient(create_app()) as c:
        c.renderer_key = renderer_key
        yield c


def auth(key=ADMIN):
    return {"Authorization": f"Bearer {key}"}


def make_key(client, role, project=None, env=None, name=None):
    """Issue a distinct principal's key (needed to review someone else's draft)."""
    r = client.post("/mgmt/keys", json={"principal_name": name or f"{role}-{project or 'inst'}",
                                        "role": role, "project_id": project,
                                        "environment_id": env}, headers=auth())
    assert r.status_code == 200, r.text
    return r.json()["key"]


def _tip_sha(client, prompt_id="support/system", version=2):
    v = client.get(f"/mgmt/prompts/{prompt_id}/versions?environment=prod", headers=auth()).json()
    return next(x for x in v["versions"] if x["version"] == version)["tip_full_sha"]


def test_protected_pointer_proposes_then_distinct_releaser_approves(client):
    sha = _tip_sha(client)
    op = make_key(client, "operator", project="support")
    # Operator proposes on protected prod -> pending approval, no move yet.
    r = client.post("/mgmt/envs/prod/pointers",
                    json={"prompt_id": "support/system", "version_number": 2, "to_sha": sha},
                    headers=auth(op))
    assert r.status_code == 200 and r.json()["status"] == "proposed", r.text
    # A project-scoped operator can't view env-wide approvals; admin lists them.
    assert client.get("/mgmt/envs/prod/approvals", headers=auth(op)).status_code == 403
    aps = client.get("/mgmt/envs/prod/approvals", headers=auth()).json()["approvals"]
    apid = aps[-1]["id"]
    # Admin (distinct principal, implies releaser) approves -> becomes live at sha.
    r = client.post(f"/mgmt/envs/prod/approvals/{apid}/approve", headers=auth())
    assert r.status_code == 200 and r.json()["status"] == "approved", r.text
    tl = client.get("/mgmt/envs/prod/pointers?prompt_id=support/system&version=2",
                    headers=auth()).json()
    assert tl["moves"][0]["full_sha"] == sha


def test_releaser_cannot_approve_own_proposal(client):
    sha = _tip_sha(client)
    rel = make_key(client, "releaser", env="prod")
    r = client.post("/mgmt/envs/prod/pointers",
                    json={"prompt_id": "support/system", "version_number": 2, "to_sha": sha},
                    headers=auth(rel))
    assert r.json()["status"] == "proposed"
    apid = client.get("/mgmt/envs/prod/approvals", headers=auth(rel)).json()["approvals"][-1]["id"]
    r = client.post(f"/mgmt/envs/prod/approvals/{apid}/approve", headers=auth(rel))
    assert r.status_code == 400  # approver must differ from proposer


def test_operator_cannot_force_release(client):
    sha = _tip_sha(client)
    op = make_key(client, "operator", project="support")
    r = client.post("/mgmt/envs/prod/pointers",
                    json={"prompt_id": "support/system", "version_number": 2,
                          "to_sha": sha, "force": True},
                    headers=auth(op))
    assert r.status_code == 403  # force is a releaser-gated break-glass


def test_project_operator_cannot_create_global_rule(client):
    op = make_key(client, "operator", project="support")
    # A prompt-scoped rule in their own project is allowed.
    r = client.post("/mgmt/envs/prod/rules",
                    json={"id": "r-sup", "scope": "prompt", "prompt_id": "support/system",
                          "priority": 5, "serve": {"version": 2}}, headers=auth(op))
    assert r.status_code == 200, r.text
    # A global rule governs every project -> forbidden for a project operator.
    r = client.post("/mgmt/envs/prod/rules",
                    json={"id": "r-glob", "scope": "global", "priority": 5,
                          "serve": {"version": 2}}, headers=auth(op))
    assert r.status_code == 403


def test_health_and_ready(client):
    assert client.get("/healthz").text == "ok"
    assert client.get("/readyz").status_code == 200


def test_serving_requires_credentials(client):
    r = client.post("/prompt/support/system", json={"variables": {"customer_name": "Acme"}})
    assert r.status_code == 401


def test_render_with_renderer_key(client):
    r = client.post(
        "/prompt/support/system",
        json={"flags": {}, "variables": {"customer_name": "Acme", "history": []}},
        headers=auth(client.renderer_key),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert "support agent for Acme" in body["prompt"]
    # Live v2 is the formal baseline; the shared fragment enters only once the warm
    # tweak (tip) is made live — the tip/live gap is the testing window.
    assert "formal, professional tone" in body["prompt"]
    assert body["matched_rule"] == "default"
    assert body["versions"]["support/system"]["version"] == 2


def test_tip_serves_fragment_via_rule(client):
    # team-x (u_12) gets v2@tip, which includes the shared style fragment.
    r = client.post(
        "/prompt/support/system",
        json={"flags": {"user_id": "u_12"},
              "variables": {"customer_name": "Acme", "history": []}},
        headers=auth(client.renderer_key),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert "Write in plain English" in body["prompt"]              # fragment expanded
    assert "shared/style/language-rules" in body["versions"]       # reported as contributor


def test_renderer_key_scoped_to_project(client):
    # renderer key is scoped to support/prod; shared prompts render via includes but
    # a direct render of a shared prompt is out of scope -> 403.
    r = client.post("/prompt/shared/style/language-rules", json={},
                    headers=auth(client.renderer_key))
    assert r.status_code == 403


def test_evaluate_endpoint(client):
    r = client.post("/prompt/support/system/evaluate",
                    json={"flags": {"user_id": "u_12"}}, headers=auth())
    assert r.status_code == 200, r.text
    body = r.json()
    # team-x-tip targets u_12 to v2@tip
    assert body["version"] == 2
    assert body["matched_rule"]["id"] == "team-x-tip"


def test_mgmt_overview_and_versions(client):
    r = client.get("/mgmt/overview?environment=prod", headers=auth())
    assert r.status_code == 200, r.text
    projects = {p["project"]: p for p in r.json()["projects"]}
    assert "support" in projects
    sysprompt = next(p for p in projects["support"]["prompts"] if p["prompt_id"] == "support/system")
    assert sysprompt["live_version"] == 2
    assert sysprompt["tip_ahead"] == 2  # two tweak commits ahead of the live pointer

    r = client.get("/mgmt/prompts/support/system/versions?environment=prod", headers=auth())
    assert r.status_code == 200, r.text
    data = r.json()
    versions = {v["version"]: v for v in data["versions"]}
    assert versions[3]["label"] == "voice-v2"
    assert versions[1]["status"] == "archived"
    names = {v["name"] for v in data["variables"]}
    assert "customer_name" in names


def test_rules_console(client):
    r = client.get("/mgmt/envs/prod/rules", headers=auth())
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["protected"] is True
    ids = {rule["id"] for rule in body["rules"]}
    assert {"beta-gets-v3", "team-x-tip"} <= ids


def test_tweak_flow_over_http(client):
    # 1. open a draft on v2 with a new tweak
    r = client.post("/mgmt/prompts/support/system/drafts",
                    json={"version_number": 2, "author": "sam",
                          "content": "You are a support agent for {{ customer_name }}.\nBRAND NEW LINE."},
                    headers=auth())
    assert r.status_code == 200, r.text
    draft_id = r.json()["id"]
    assert r.json()["lint"]["status"] == "valid"

    # 2. commit is blocked until review policy (1 approval) is met
    r = client.post(f"/mgmt/drafts/{draft_id}/commit", json={}, headers=auth())
    assert r.status_code == 412

    # 2b. self-approval doesn't count: the admin authored the draft, so its own
    # review can't satisfy the policy (identity comes from the principal).
    client.post(f"/mgmt/drafts/{draft_id}/review", json={}, headers=auth())
    r = client.post(f"/mgmt/drafts/{draft_id}/commit", json={}, headers=auth())
    assert r.status_code == 412

    # 3. a *different* principal approves, then commit succeeds
    reviewer = make_key(client, "editor", project="support")
    client.post(f"/mgmt/drafts/{draft_id}/review", json={}, headers=auth(reviewer))
    r = client.post(f"/mgmt/drafts/{draft_id}/commit", json={}, headers=auth())
    assert r.status_code == 200, r.text
    new_sha = r.json()["full_sha"]

    # 4. make live (prod is protected; admin implies releaser) with force
    r = client.post("/mgmt/envs/prod/pointers",
                    json={"prompt_id": "support/system", "version_number": 2,
                          "to_sha": new_sha, "comment": "tweak live", "force": True},
                    headers=auth())
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "live"

    # 5. serving now reflects the tweak
    r = client.post("/prompt/support/system",
                    json={"variables": {"customer_name": "Acme", "history": []}},
                    headers=auth(client.renderer_key))
    assert "BRAND NEW LINE" in r.json()["prompt"]


def test_create_new_prompt_flow(client):
    # New prompt in a new project (review_policy 0 -> commit needs no approval).
    r = client.post("/mgmt/prompts", json={"prompt_id": "growth/welcome",
                                           "description": "welcome message"}, headers=auth())
    assert r.status_code == 200, r.text
    assert r.json()["prompt_id"] == "growth/welcome"

    # It shows up immediately in the overview with zero versions.
    ov = client.get("/mgmt/overview?environment=prod", headers=auth()).json()
    growth = next(p for p in ov["projects"] if p["project"] == "growth")
    wp = next(p for p in growth["prompts"] if p["prompt_id"] == "growth/welcome")
    assert wp["versions"] == 0 and wp["live_version"] is None

    # Start a v1 draft (empty content lints valid), write, commit.
    d = client.post("/mgmt/prompts/growth/welcome/drafts",
                    json={"version_number": 1, "content": ""}, headers=auth()).json()
    assert d["lint"]["status"] == "valid"
    client.put(f"/mgmt/drafts/{d['id']}/content",
               json={"content": "Welcome, {{ name }}!"}, headers=auth())
    r = client.post(f"/mgmt/drafts/{d['id']}/commit", json={"author": "sam"}, headers=auth())
    assert r.status_code == 200, r.text
    sha = r.json()["full_sha"]

    # Make it live + default, then it serves.
    client.post("/mgmt/envs/prod/defaults",
                json={"prompt_id": "growth/welcome", "version_number": 1}, headers=auth())
    client.post("/mgmt/envs/prod/pointers",
                json={"prompt_id": "growth/welcome", "version_number": 1,
                      "to_sha": sha, "force": True}, headers=auth())
    r = client.post("/prompt/growth/welcome", json={"variables": {"name": "Kai"}}, headers=auth())
    assert r.status_code == 200 and r.json()["prompt"] == "Welcome, Kai!"


def test_create_duplicate_prompt_is_409(client):
    client.post("/mgmt/prompts", json={"prompt_id": "growth/dup"}, headers=auth())
    r = client.post("/mgmt/prompts", json={"prompt_id": "growth/dup"}, headers=auth())
    assert r.status_code == 409


def test_rendered_diff_uses_test_context(client):
    v = client.get("/mgmt/prompts/support/system/versions?environment=prod", headers=auth()).json()
    v2 = next(x for x in v["versions"] if x["version"] == 2)
    q = (f"a_version=2&a_sha={v2['live_full_sha']}&b_version=2&b_sha={v2['tip_full_sha']}"
         f"&mode=rendered&environment=prod")
    r = client.get(f"/mgmt/prompts/support/system/diff?{q}", headers=auth())
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["context"] == "enterprise-us"       # picked a test context that supplies vars
    assert "formal, professional tone" in body["diff"]      # removed line
    assert "Write in plain English" in body["diff"]         # fragment expanded, added line


def test_rendered_diff_missing_vars_is_graceful_not_500(client):
    # A prompt with a required var and NO test context must not 500 the diff endpoint.
    client.post("/mgmt/prompts", json={"prompt_id": "support/novars"}, headers=auth())
    d = client.post("/mgmt/prompts/support/novars/drafts",
                    json={"version_number": 1, "content": "Hi {{ who }}"}, headers=auth()).json()
    reviewer = make_key(client, "editor", project="support")
    client.post(f"/mgmt/drafts/{d['id']}/review", json={}, headers=auth(reviewer))
    sha = client.post(f"/mgmt/drafts/{d['id']}/commit", json={}, headers=auth()).json()["full_sha"]
    q = f"a_version=1&a_sha={sha}&b_version=1&b_sha={sha}&mode=rendered&environment=prod"
    r = client.get(f"/mgmt/prompts/support/novars/diff?{q}", headers=auth())
    assert r.status_code == 200
    assert "error" in r.json()  # graceful render-failed note, not a 500


def test_auth_cache_survives_db_outage(client):
    # §8/§10: keys are checked in-memory; auth continues through a DB outage.
    from sqlalchemy.exc import SQLAlchemyError
    from incant.server.auth import AuthCache

    cache = AuthCache()
    with session_scope() as s:
        ident = cache.identify(s, f"Bearer {ADMIN}")
    assert ident.has("admin")

    cache._last_refresh = 0.0  # force a refresh attempt on next identify

    class BoomSession:
        def execute(self, *a, **k):
            raise SQLAlchemyError("db down")

        def rollback(self):
            pass

    ident2 = cache.identify(BoomSession(), f"Bearer {ADMIN}")  # falls back to cache
    assert ident2.principal_id == ident.principal_id and ident2.has("admin")


def test_created_key_authenticates_immediately(client):
    # Issuing a key invalidates the in-memory table, so it works on the next call.
    r = client.post("/mgmt/keys", json={"principal_name": "svc", "role": "viewer"}, headers=auth())
    assert r.status_code == 200, r.text
    newkey = r.json()["key"]
    r = client.get("/mgmt/overview?environment=prod", headers=auth(newkey))
    assert r.status_code == 200, r.text


def test_serving_path_does_not_write(client):
    # §8/§15: read replicas must not write on the render path. last_used_at stays null.
    client.post("/prompt/support/system",
                json={"variables": {"customer_name": "Acme", "history": []}},
                headers=auth(client.renderer_key))
    with session_scope() as s:
        used = [k.last_used_at for k in
                s.execute(select(models.ApiKey)).scalars().all()]
    assert all(u is None for u in used)


def test_expired_key_is_rejected(client):
    raw = "incant_sk_expired_000000000000000000"
    with session_scope() as s:
        s.add(models.Principal(id="p_exp", kind="service", subject="exp", name="exp"))
        s.flush()
        s.add(models.ApiKey(principal_id="p_exp", prefix=key_prefix(raw), hash=hash_key(raw),
                            name="exp",
                            expires_at=dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=1)))
        s.add(models.RoleBinding(principal_id="p_exp", role="admin"))
    r = client.get("/mgmt/overview?environment=prod", headers=auth(raw))
    assert r.status_code == 401


def test_unknown_environment_is_404_not_500(client):
    r = client.post("/evaluate", json={"flags": {}, "environment": "ghost"}, headers=auth())
    assert r.status_code == 404, r.text
    r = client.get("/prompts?environment=ghost", headers=auth())
    assert r.status_code == 404, r.text


def test_pin_replay_roundtrip(client):
    # Capture a targeted response (u_12 gets v2@tip via team-x-tip), then replay it
    # with a pin under DIFFERENT flags — the pin reproduces the exact output (§9).
    r1 = client.post("/prompt/support/system",
                     json={"flags": {"user_id": "u_12"},
                           "variables": {"customer_name": "Acme", "history": []}},
                     headers=auth(client.renderer_key))
    b1 = r1.json()
    assert b1["versions"]["support/system"]["version"] == 2

    pin = {"versions": b1["versions"], "rules_version": b1["rules_version"]}
    r2 = client.post("/prompt/support/system",
                     json={"variables": {"customer_name": "Acme", "history": []}, "pin": pin},
                     headers=auth(client.renderer_key))
    assert r2.status_code == 200, r2.text
    b2 = r2.json()
    # Without the pin these differ (default = v2@live, no fragment); with it they match.
    assert b2["prompt"] == b1["prompt"]
    assert b2["versions"]["support/system"]["commit"] == b1["versions"]["support/system"]["commit"]


def test_effective_schema_unions_include_closure(client):
    # §2.10/§4: a fragment's required variable must surface in the parent's schema.
    reviewer = make_key(client, "editor", project="support")

    def author(pid, content):
        client.post("/mgmt/prompts", json={"prompt_id": pid}, headers=auth())
        d = client.post(f"/mgmt/prompts/{pid}/drafts",
                        json={"version_number": 1, "content": content}, headers=auth()).json()
        client.post(f"/mgmt/drafts/{d['id']}/review", json={}, headers=auth(reviewer))
        r = client.post(f"/mgmt/drafts/{d['id']}/commit", json={}, headers=auth())
        assert r.json()["validation"]["status"] == "valid", r.text

    author("support/frag", "tone: {{ brand }}")                       # fragment requires brand
    author("support/parent", 'Hi {{ who }} {% include "support/frag" %}')

    v = client.get("/mgmt/prompts/support/parent/variables?version=1", headers=auth()).json()
    names = {x["name"]: x for x in v["variables"]}
    assert "who" in names and "brand" in names                        # union over closure
    assert names["brand"]["required"] is True                         # surfaced from fragment


def test_kill_switch_over_http(client):
    r = client.post("/mgmt/envs/prod/kill?prompt_id=support/system",
                    json={"engaged": True}, headers=auth())
    assert r.status_code == 200
    # u_12 would normally get v2@tip via team-x-tip; kill forces the default (v2@live)
    r = client.post("/prompt/support/system/evaluate",
                    json={"flags": {"user_id": "u_12"}}, headers=auth())
    assert r.json()["matched_rule"] == "default"
