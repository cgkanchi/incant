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


def test_releaser_moves_pointer_directly(client):
    sha = _tip_sha(client)
    rel = make_key(client, "releaser", env="prod")
    # Pointer moves are unilateral — a releaser advances the live pointer directly,
    # no propose→approve ceremony. prod is locked, so echo the prompt id to confirm.
    r = client.post("/mgmt/envs/prod/pointers",
                    json={"prompt_id": "support/system", "version_number": 2,
                          "to_sha": sha, "confirm": "support/system"},
                    headers=auth(rel))
    assert r.status_code == 200 and r.json()["status"] == "live", r.text
    tl = client.get("/mgmt/envs/prod/pointers?prompt_id=support/system&version=2",
                    headers=auth()).json()
    assert tl["moves"][0]["full_sha"] == sha


def test_locked_env_requires_typed_confirmation(client):
    sha = _tip_sha(client)
    base = {"prompt_id": "support/system", "version_number": 2, "to_sha": sha}
    # No confirm on locked prod -> refused with a confirmation-required error.
    r = client.post("/mgmt/envs/prod/pointers", json=base, headers=auth())
    assert r.status_code == 409 and r.json()["detail"]["error"] == "confirmation_required"
    assert r.json()["detail"]["expected"] == "support/system"
    # Wrong token -> still refused.
    r = client.post("/mgmt/envs/prod/pointers", json={**base, "confirm": "prod"}, headers=auth())
    assert r.status_code == 409
    # Correct token (the prompt id) -> applied.
    r = client.post("/mgmt/envs/prod/pointers", json={**base, "confirm": "support/system"},
                    headers=auth())
    assert r.status_code == 200 and r.json()["status"] == "live", r.text


def test_unlocked_env_needs_no_confirmation(client):
    # staging is not protected -> pointer move applies with no confirm token.
    sha = _tip_sha(client)
    r = client.post("/mgmt/envs/staging/pointers",
                    json={"prompt_id": "support/system", "version_number": 2, "to_sha": sha},
                    headers=auth())
    assert r.status_code == 200 and r.json()["status"] == "live", r.text


def test_operator_cannot_move_pointer(client):
    sha = _tip_sha(client)
    op = make_key(client, "operator", project="support")
    # An operator edits rules but can't release — pointer moves require releaser.
    r = client.post("/mgmt/envs/prod/pointers",
                    json={"prompt_id": "support/system", "version_number": 2, "to_sha": sha},
                    headers=auth(op))
    assert r.status_code == 403


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
    # v2's live pointer in prod was published by Dana (the v2 baseline author/releaser).
    assert sysprompt["live_by"] == "Dana"
    assert sysprompt["live_at"]  # ISO timestamp of the pointer move
    # v3 exists and has a prod live pointer -> newest version is published.
    assert sysprompt["newest_version"] == 3
    assert sysprompt["newest_version_live"] is True

    r = client.get("/mgmt/prompts/support/system/versions?environment=prod", headers=auth())
    assert r.status_code == 200, r.text
    data = r.json()
    versions = {v["version"]: v for v in data["versions"]}
    assert versions[3]["label"] == "voice-v2"
    assert versions[1]["status"] == "archived"
    # each live version reports the publishing principal.
    assert versions[2]["live_by"] == "Dana"
    names = {v["name"] for v in data["variables"]}
    assert "customer_name" in names


def test_overview_flags_unpublished_newer_version(client):
    # support/greeting has v1 live (default) + v2 committed but never made live in prod.
    r = client.get("/mgmt/overview?environment=prod", headers=auth())
    assert r.status_code == 200, r.text
    projects = {p["project"]: p for p in r.json()["projects"]}
    greeting = next(p for p in projects["support"]["prompts"]
                    if p["prompt_id"] == "support/greeting")
    assert greeting["live_version"] == 1
    assert greeting["live_by"] == "Maya"          # v1 published by Maya
    assert greeting["newest_version"] == 2         # v2 exists...
    assert greeting["newest_version_live"] is False  # ...but was never published -> draft badge


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

    # 3. self-review is allowed by default: the author's own approval satisfies the
    # policy (the reviewer identity comes from the principal, never the body).
    client.post(f"/mgmt/drafts/{draft_id}/review", json={}, headers=auth())
    r = client.post(f"/mgmt/drafts/{draft_id}/commit", json={}, headers=auth())
    assert r.status_code == 200, r.text
    new_sha = r.json()["full_sha"]

    # 4. make live directly — pointer moves are unilateral and releaser-gated
    # (admin implies releaser). prod is locked, so confirm with the prompt id.
    r = client.post("/mgmt/envs/prod/pointers",
                    json={"prompt_id": "support/system", "version_number": 2,
                          "to_sha": new_sha, "comment": "tweak live",
                          "confirm": "support/system"},
                    headers=auth())
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "live"

    # 5. serving now reflects the tweak
    r = client.post("/prompt/support/system",
                    json={"variables": {"customer_name": "Acme", "history": []}},
                    headers=auth(client.renderer_key))
    assert "BRAND NEW LINE" in r.json()["prompt"]


def test_self_review_optout_requires_distinct_reviewer(client):
    # Disable self-review on the support project -> the author's own approval no
    # longer counts; a distinct reviewer is required to satisfy the policy.
    assert client.patch("/mgmt/projects/support", json={"allow_self_review": False},
                        headers=auth()).status_code == 200
    r = client.post("/mgmt/prompts/support/system/drafts",
                    json={"version_number": 2, "author": "sam",
                          "content": "You are a support agent for {{ customer_name }}.\nOPTOUT."},
                    headers=auth())
    draft_id = r.json()["id"]
    # Author (admin) self-reviews -> still blocked.
    client.post(f"/mgmt/drafts/{draft_id}/review", json={}, headers=auth())
    assert client.post(f"/mgmt/drafts/{draft_id}/commit", json={}, headers=auth()).status_code == 412
    # A distinct principal approves -> commit unlocked.
    reviewer = make_key(client, "editor", project="support")
    client.post(f"/mgmt/drafts/{draft_id}/review", json={}, headers=auth(reviewer))
    assert client.post(f"/mgmt/drafts/{draft_id}/commit", json={}, headers=auth()).status_code == 200


def test_admin_manage_users_roles_keys(client):
    # Create a user: principal + first key + initial role binding.
    r = client.post("/mgmt/keys", json={"principal_name": "dana", "role": "operator",
                                        "project_id": "support"}, headers=auth())
    assert r.status_code == 200, r.text
    dana_key, pid = r.json()["key"], r.json()["principal_id"]

    # It shows up in the admin listing with its binding and key.
    d = client.get("/mgmt/principals", headers=auth()).json()
    assert "operator" in d["roles"] and "support" in d["projects"] and "prod" in d["environments"]
    me = next(p for p in d["principals"] if p["id"] == pid)
    assert me["name"] == "dana"
    assert any(b["role"] == "operator" and b["project_id"] == "support" for b in me["bindings"])
    assert len(me["keys"]) == 1 and me["keys"][0]["revoked"] is False
    # The new key authenticates.
    assert client.get("/mgmt/envs", headers=auth(dana_key)).status_code == 200

    # Grant a second role (env-scoped releaser) -> dana can now release on prod.
    assert client.post(f"/mgmt/principals/{pid}/bindings",
                       json={"role": "releaser", "environment_id": "prod"},
                       headers=auth()).status_code == 200
    sha = _tip_sha(client)
    move = {"prompt_id": "support/system", "version_number": 2, "to_sha": sha,
            "confirm": "support/system"}
    assert client.post("/mgmt/envs/prod/pointers", json=move, headers=auth(dana_key)).status_code == 200

    # Remove the releaser binding -> dana loses release rights.
    me = next(p for p in client.get("/mgmt/principals", headers=auth()).json()["principals"]
              if p["id"] == pid)
    bid = next(b["id"] for b in me["bindings"] if b["role"] == "releaser")
    assert client.delete(f"/mgmt/principals/{pid}/bindings/{bid}", headers=auth()).status_code == 200
    assert client.post("/mgmt/envs/prod/pointers", json=move, headers=auth(dana_key)).status_code == 403

    # Issue a second key, revoke the first -> first stops authenticating, second works.
    k2 = client.post(f"/mgmt/principals/{pid}/keys", headers=auth()).json()["key"]
    first_kid = next(k["id"] for k in
                     next(p for p in client.get("/mgmt/principals", headers=auth()).json()["principals"]
                          if p["id"] == pid)["keys"] if not k["revoked"])
    assert client.post(f"/mgmt/keys/{first_kid}/revoke", headers=auth()).status_code == 200
    assert client.get("/mgmt/envs", headers=auth(dana_key)).status_code == 401
    assert client.get("/mgmt/envs", headers=auth(k2)).status_code == 200


def test_access_management_requires_admin(client):
    viewer = make_key(client, "viewer", project="support")
    assert client.get("/mgmt/principals", headers=auth(viewer)).status_code == 403
    assert client.post("/mgmt/keys", json={"principal_name": "x", "role": "viewer"},
                       headers=auth(viewer)).status_code == 403


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

    # Make it live + default (prod is locked -> confirm with the prompt id), then it serves.
    client.post("/mgmt/envs/prod/defaults",
                json={"prompt_id": "growth/welcome", "version_number": 1,
                      "confirm": "growth/welcome"}, headers=auth())
    client.post("/mgmt/envs/prod/pointers",
                json={"prompt_id": "growth/welcome", "version_number": 1,
                      "to_sha": sha, "confirm": "growth/welcome"}, headers=auth())
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


def test_whoami(client):
    r = client.get("/mgmt/whoami", headers=auth())
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["principal_id"] and body["name"] == "bootstrap-admin"
    # any authenticated identity works; a viewer (no authoring role) still gets it
    viewer = make_key(client, "viewer", project="support")
    assert client.get("/mgmt/whoami", headers=auth(viewer)).json()["name"]
    # no credential -> 401
    assert client.get("/mgmt/whoami").status_code == 401


def test_draft_diff_default_against_base_source(client):
    from incant.seed import SYSTEM_V2_WARM
    body = SYSTEM_V2_WARM + "\nAn extra safety line."
    d = client.post("/mgmt/prompts/support/system/drafts",
                    json={"version_number": 2, "content": body}, headers=auth()).json()
    r = client.get(f"/mgmt/drafts/{d['id']}/diff", headers=auth())
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["mode"] == "source"
    # default target is the draft's own v2 at its base -> "what did I change"
    assert "An extra safety line." in j["diff"]
    assert j["right"] == body
    assert "support agent" in j["left"]        # v2's committed text at base
    assert d["base_sha"][:7] in j["diff"]      # fromfile label = v2@<base7>


def test_draft_diff_explicit_against(client):
    v = client.get("/mgmt/prompts/support/system/versions?environment=prod", headers=auth()).json()
    v2 = next(x for x in v["versions"] if x["version"] == 2)
    d = client.post("/mgmt/prompts/support/system/drafts",
                    json={"version_number": 2,
                          "content": "Totally new body for {{ customer_name }}."},
                    headers=auth()).json()
    q = f"against_version=2&against_sha={v2['live_full_sha']}"
    r = client.get(f"/mgmt/drafts/{d['id']}/diff?{q}", headers=auth())
    assert r.status_code == 200, r.text
    j = r.json()
    assert "Totally new body" in j["diff"]
    assert v2["live_full_sha"][:7] in j["diff"]   # fromfile references the explicit sha


def test_draft_diff_rendered_has_left_right(client):
    from incant.seed import SYSTEM_V2_WARM
    v = client.get("/mgmt/prompts/support/system/versions?environment=prod", headers=auth()).json()
    v2 = next(x for x in v["versions"] if x["version"] == 2)
    d = client.post("/mgmt/prompts/support/system/drafts",
                    json={"version_number": 2, "content": SYSTEM_V2_WARM}, headers=auth()).json()
    q = (f"against_version=2&against_sha={v2['live_full_sha']}"
         f"&mode=rendered&environment=prod&test_context=enterprise-us")
    r = client.get(f"/mgmt/drafts/{d['id']}/diff?{q}", headers=auth())
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["mode"] == "rendered"
    assert j["context"] == "enterprise-us"
    # left = v2@live (formal), right = draft (warm + fragment) — both rendered
    assert "formal, professional tone" in j["left"]
    assert "Write in plain English" in j["right"]
    assert "formal, professional tone" in j["diff"]   # removed line
    assert "Write in plain English" in j["diff"]       # added line


def test_discard_draft_removes_from_listing(client):
    d = client.post("/mgmt/prompts/support/system/drafts",
                    json={"version_number": 2, "content": "temp draft body"}, headers=auth()).json()
    lst = client.get("/mgmt/prompts/support/system/drafts", headers=auth()).json()
    assert any(x["id"] == d["id"] for x in lst["drafts"])
    r = client.post(f"/mgmt/drafts/{d['id']}/discard", headers=auth())
    assert r.status_code == 200, r.text
    assert r.json() == {"ok": True, "draft_id": d["id"], "status": "discarded"}
    # list_drafts filters to open/approved -> a discarded draft vanishes
    lst = client.get("/mgmt/prompts/support/system/drafts", headers=auth()).json()
    assert not any(x["id"] == d["id"] for x in lst["drafts"])
    # discarding again is a no-op error
    assert client.post(f"/mgmt/drafts/{d['id']}/discard", headers=auth()).status_code == 400


def test_discard_after_commit_is_400(client):
    # Fresh project (review_policy 0) -> commit needs no approval.
    client.post("/mgmt/prompts", json={"prompt_id": "growth/done"}, headers=auth())
    d = client.post("/mgmt/prompts/growth/done/drafts",
                    json={"version_number": 1, "content": "hello {{ name }}"}, headers=auth()).json()
    assert client.post(f"/mgmt/drafts/{d['id']}/commit", json={}, headers=auth()).status_code == 200
    r = client.post(f"/mgmt/drafts/{d['id']}/discard", headers=auth())
    assert r.status_code == 400, r.text


def test_structured_409_on_commit_conflict(client):
    # Fresh project (review_policy 0). Establish a v1 baseline, then branch two
    # drafts off the same base; committing the second after the first conflicts.
    client.post("/mgmt/prompts", json={"prompt_id": "growth/conflict"}, headers=auth())
    d0 = client.post("/mgmt/prompts/growth/conflict/drafts",
                     json={"version_number": 1, "content": "v1 base line"}, headers=auth()).json()
    assert client.post(f"/mgmt/drafts/{d0['id']}/commit", json={}, headers=auth()).status_code == 200
    a = client.post("/mgmt/prompts/growth/conflict/drafts",
                    json={"version_number": 1, "content": "A change wins"}, headers=auth()).json()
    b = client.post("/mgmt/prompts/growth/conflict/drafts",
                    json={"version_number": 1, "content": "B change loses"}, headers=auth()).json()
    assert client.post(f"/mgmt/drafts/{a['id']}/commit", json={}, headers=auth()).status_code == 200
    r = client.post(f"/mgmt/drafts/{b['id']}/commit", json={}, headers=auth())
    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    assert "changed since this draft's base" in detail["detail"]
    assert detail["base_sha"] and detail["current_sha"]
    assert detail["base_sha"] != detail["current_sha"]
    # the diff shows the intervening change (base -> current tip)
    assert "A change wins" in detail["diff"]
    assert "v1 base line" in detail["diff"]


def test_draft_listing_enriched_fields(client):
    d = client.post("/mgmt/prompts/support/system/drafts",
                    json={"version_number": 2, "content": "listing draft"}, headers=auth()).json()
    lst = client.get("/mgmt/prompts/support/system/drafts", headers=auth()).json()
    item = next(x for x in lst["drafts"] if x["id"] == d["id"])
    assert item["base_sha"] and len(item["base_sha"]) == 7
    assert item["updated_at"]                      # ISO timestamp
    # single-draft GET exposes the full base sha
    one = client.get(f"/mgmt/drafts/{d['id']}", headers=auth()).json()
    assert one["base_full_sha"] and one["base_full_sha"].startswith(item["base_sha"])


# ── review: comments + changes_requested ─────────────────────────────

def test_draft_comments_create_and_list(client):
    d = client.post("/mgmt/prompts/support/system/drafts",
                    json={"version_number": 2, "content": "commented draft"}, headers=auth()).json()
    # empty listing to start
    r = client.get(f"/mgmt/drafts/{d['id']}/comments", headers=auth())
    assert r.status_code == 200 and r.json()["comments"] == []
    # a viewer (no editor on the project) may still comment — reviewers need only read.
    viewer = make_key(client, "viewer", project="support")
    r = client.post(f"/mgmt/drafts/{d['id']}/comments",
                    json={"anchor": "source:2", "body": "tighten this line",
                          "author": "spoofed"}, headers=auth(viewer))
    assert r.status_code == 200, r.text
    c = r.json()
    assert c["body"] == "tighten this line" and c["anchor"] == "source:2"
    # author is the authenticated principal, never the body-supplied "spoofed".
    assert c["author"] == "viewer-support" and c["author"] != "spoofed"
    assert c["id"] and c["created_at"]
    lst = client.get(f"/mgmt/drafts/{d['id']}/comments", headers=auth()).json()["comments"]
    assert [x["body"] for x in lst] == ["tighten this line"]


def test_comment_empty_body_is_422(client):
    d = client.post("/mgmt/prompts/support/system/drafts",
                    json={"version_number": 2, "content": "x"}, headers=auth()).json()
    assert client.post(f"/mgmt/drafts/{d['id']}/comments",
                       json={"body": ""}, headers=auth()).status_code == 422
    assert client.post(f"/mgmt/drafts/{d['id']}/comments",
                       json={"body": "   "}, headers=auth()).status_code == 422


def test_comment_on_committed_draft_is_409(client):
    # fresh project (review_policy 0) -> commit needs no approval.
    client.post("/mgmt/prompts", json={"prompt_id": "growth/cdone"}, headers=auth())
    d = client.post("/mgmt/prompts/growth/cdone/drafts",
                    json={"version_number": 1, "content": "hi {{ name }}"}, headers=auth()).json()
    assert client.post(f"/mgmt/drafts/{d['id']}/commit", json={}, headers=auth()).status_code == 200
    r = client.post(f"/mgmt/drafts/{d['id']}/comments", json={"body": "too late"}, headers=auth())
    assert r.status_code == 409


def test_changes_requested_review_flow(client):
    from incant.seed import SYSTEM_V2_WARM
    body = SYSTEM_V2_WARM + "\nExtra review line."
    d = client.post("/mgmt/prompts/support/system/drafts",
                    json={"version_number": 2, "content": body}, headers=auth()).json()
    reviewer = make_key(client, "editor", project="support")
    # 1. changes_requested is recorded + visible but does NOT count -> commit blocked.
    r = client.post(f"/mgmt/drafts/{d['id']}/review",
                    json={"state": "changes_requested"}, headers=auth(reviewer)).json()
    assert r["approvals"] == []
    assert {"reviewer": "editor-support", "state": "changes_requested"} in r["reviews"]
    assert client.post(f"/mgmt/drafts/{d['id']}/commit", json={}, headers=auth()).status_code == 412
    # 2. same principal approving replaces changes_requested -> now counts, commit unlocks.
    r = client.post(f"/mgmt/drafts/{d['id']}/review",
                    json={"state": "approved"}, headers=auth(reviewer)).json()
    assert r["approvals"] == ["editor-support"]
    assert r["reviews"] == [{"reviewer": "editor-support", "state": "approved"}]
    assert client.get(f"/mgmt/drafts/{d['id']}", headers=auth()).json()["status"] == "approved"
    # 3. flipping back to changes_requested clears the approval -> re-locks.
    r = client.post(f"/mgmt/drafts/{d['id']}/review",
                    json={"state": "changes_requested"}, headers=auth(reviewer)).json()
    assert r["approvals"] == []
    assert client.get(f"/mgmt/drafts/{d['id']}", headers=auth()).json()["status"] == "open"
    assert client.post(f"/mgmt/drafts/{d['id']}/commit", json={}, headers=auth()).status_code == 412
    # 4. re-approve -> commit succeeds.
    client.post(f"/mgmt/drafts/{d['id']}/review", json={"state": "approved"}, headers=auth(reviewer))
    assert client.post(f"/mgmt/drafts/{d['id']}/commit", json={}, headers=auth()).status_code == 200


# ── audit: detail + filters ──────────────────────────────────────────

def test_audit_detail_and_filters(client):
    # creating a key emits a `principal.create` audit row; a binding add emits `binding.add`.
    r = client.post("/mgmt/keys", json={"principal_name": "aud", "role": "viewer",
                                        "project_id": "support"}, headers=auth())
    pid = r.json()["principal_id"]
    client.post(f"/mgmt/principals/{pid}/bindings",
                json={"role": "editor", "project_id": "support"}, headers=auth())
    body = client.get("/mgmt/audit", headers=auth()).json()
    # distinct-value lists for the filter dropdowns.
    assert "principal.create" in body["actions"] and "binding.add" in body["actions"]
    assert "bootstrap-admin" in body["actors"]
    # each row carries the full detail incl. id/object_type/before/after.
    row = body["audit"][0]
    assert {"id", "actor", "action", "object_type", "object_id", "before", "after", "at"} <= row.keys()
    # action filter.
    only = client.get("/mgmt/audit?action=principal.create", headers=auth()).json()["audit"]
    assert only and all(a["action"] == "principal.create" for a in only)
    creates = [a for a in only if a["object_id"] == pid]
    assert creates and creates[0]["after"]["name"] == "aud"      # before/after present
    # substring filter on object_id.
    byobj = client.get(f"/mgmt/audit?object={pid}", headers=auth()).json()["audit"]
    assert byobj and all(pid in a["object_id"] for a in byobj)
    # actor filter.
    byactor = client.get("/mgmt/audit?actor=bootstrap-admin", headers=auth()).json()["audit"]
    assert byactor and all(a["actor"] == "bootstrap-admin" for a in byactor)
    # limit is honoured (and capped at 500 server-side).
    assert len(client.get("/mgmt/audit?limit=1", headers=auth()).json()["audit"]) == 1


# ── overview + whoami extras ─────────────────────────────────────────

def test_overview_description_and_open_drafts(client):
    client.post("/mgmt/prompts", json={"prompt_id": "growth/welcome",
                                       "description": "welcome message"}, headers=auth())
    ov = client.get("/mgmt/overview?environment=prod", headers=auth()).json()
    projects = {p["project"]: p for p in ov["projects"]}
    wp = next(p for p in projects["growth"]["prompts"] if p["prompt_id"] == "growth/welcome")
    assert wp["description"] == "welcome message" and wp["open_drafts"] == 0
    # support/system carries one seeded open draft and no description.
    sysrow = next(p for p in projects["support"]["prompts"] if p["prompt_id"] == "support/system")
    assert sysrow["description"] == "" and sysrow["open_drafts"] == 1
    # opening another draft bumps the count.
    client.post("/mgmt/prompts/support/system/drafts",
                json={"version_number": 2, "content": "another"}, headers=auth())
    ov2 = client.get("/mgmt/overview?environment=prod", headers=auth()).json()
    support2 = next(p for p in ov2["projects"] if p["project"] == "support")
    sysrow2 = next(p for p in support2["prompts"] if p["prompt_id"] == "support/system")
    assert sysrow2["open_drafts"] == 2


def test_whoami_roles(client):
    # bootstrap admin holds an instance-wide admin binding (project/env unscoped).
    me = client.get("/mgmt/whoami", headers=auth()).json()
    assert {"role": "admin", "project_id": None, "environment_id": None} in me["roles"]
    # a project+env scoped key reports exactly its scoped binding.
    op = make_key(client, "operator", project="support", env="prod", name="opx")
    who = client.get("/mgmt/whoami", headers=auth(op)).json()
    assert who["name"] == "opx"
    assert who["roles"] == [{"role": "operator", "project_id": "support", "environment_id": "prod"}]
