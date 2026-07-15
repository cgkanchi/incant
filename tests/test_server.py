"""HTTP-level tests over the FastAPI app: auth, serving, mgmt, the tweak flow."""

from __future__ import annotations

from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient

import datetime as dt

from sqlalchemy import select

from incant import db, models
from incant.config import Settings, set_settings
from incant.db import session_scope
from incant.registry import sweep_expired_sessions
from incant.seed import seed
from incant.server.auth import hash_key, key_prefix, new_session_id, new_session_token
from incant.service import reset_app

from .conftest import db_url_for, reset_schema

ADMIN = "incant_sk_dev_admin"


@contextmanager
def make_client(tmp_path, **settings_overrides):
    """Boot a fresh app + TestClient. `settings_overrides` tweak Settings (e.g.
    key_pepper, metrics_token, auth_throttle_limit) for a specific scenario."""
    set_settings(Settings(
        database_url=db_url_for(tmp_path),
        repo_path=str(tmp_path / "repo"),
        bootstrap_admin_key=ADMIN,
        **settings_overrides,
    ))
    db.reset_engine()
    reset_app()
    reset_schema()
    renderer_key = seed()
    from incant.server.app import create_app
    with TestClient(create_app()) as c:
        c.renderer_key = renderer_key
        yield c


@pytest.fixture()
def client(tmp_path):
    with make_client(tmp_path) as c:
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
    # Serving now reports FULL 40-char SHAs (SHA-exact reproducibility, §4/§9).
    assert len(b1["versions"]["support/system"]["commit"]) == 40


def test_pin_rejects_abbreviated_sha(client):
    # An abbreviated (7-char) SHA must not silently resolve — 422 naming the problem.
    b1 = client.post("/prompt/support/system",
                     json={"flags": {"user_id": "u_12"},
                           "variables": {"customer_name": "Acme", "history": []}},
                     headers=auth(client.renderer_key)).json()
    entry = dict(b1["versions"]["support/system"])
    entry["commit"] = entry["commit"][:7]  # abbreviate the pin
    r = client.post("/prompt/support/system",
                    json={"variables": {"customer_name": "Acme", "history": []},
                          "pin": {"versions": {"support/system": entry}}},
                    headers=auth(client.renderer_key))
    assert r.status_code == 422, r.text
    assert "40-character" in str(r.json()["detail"])


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
    rev = next(x for x in r["reviews"] if x["reviewer"] == "editor-support")
    # verdict is recorded against the current content -> current: True (Finding 1).
    assert rev["state"] == "changes_requested" and rev["current"] is True
    assert client.post(f"/mgmt/drafts/{d['id']}/commit", json={}, headers=auth()).status_code == 412
    # 2. same principal approving replaces changes_requested -> now counts, commit unlocks.
    r = client.post(f"/mgmt/drafts/{d['id']}/review",
                    json={"state": "approved"}, headers=auth(reviewer)).json()
    assert r["approvals"] == ["editor-support"]
    assert len(r["reviews"]) == 1
    rev = r["reviews"][0]
    assert rev["reviewer"] == "editor-support" and rev["state"] == "approved"
    assert rev["current"] is True and rev["reviewed_sha"]
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


# ── review binds to reviewed revision (Finding 1) ────────────────────

def test_approval_does_not_survive_content_edit(client):
    # An approval is bound to the exact draft revision it reviewed. Editing the content
    # afterwards drops the draft back to "open", blocks commit (412), and lists the old
    # verdict as current:false — until the current content is re-approved.
    base = "You are a support agent for {{ customer_name }}."
    r = client.post("/mgmt/prompts/support/system/drafts",
                    json={"version_number": 2, "content": base + "\nLINE ONE."}, headers=auth())
    draft_id = r.json()["id"]

    # self-approval (allowed by default) satisfies the 1-approval policy.
    client.post(f"/mgmt/drafts/{draft_id}/review", json={}, headers=auth())
    got = client.get(f"/mgmt/drafts/{draft_id}", headers=auth()).json()
    assert got["status"] == "approved"
    assert got["reviewers"] == ["bootstrap-admin"]
    assert got["reviews"][0]["current"] is True

    # edit the content under the approval -> the verdict is no longer current.
    put = client.put(f"/mgmt/drafts/{draft_id}/content",
                     json={"content": base + "\nLINE TWO EDITED."}, headers=auth())
    assert put.status_code == 200, put.text
    body = put.json()
    assert body["status"] == "open"                     # dropped back to open
    assert body["reviewers"] == []                      # stale approval no longer counts
    stale = next(x for x in body["reviews"] if x["reviewer"] == "bootstrap-admin")
    assert stale["state"] == "approved" and stale["current"] is False   # kept as history

    # commit is blocked again until the current content is re-approved.
    assert client.post(f"/mgmt/drafts/{draft_id}/commit", json={}, headers=auth()).status_code == 412
    client.post(f"/mgmt/drafts/{draft_id}/review", json={}, headers=auth())
    assert client.get(f"/mgmt/drafts/{draft_id}", headers=auth()).json()["status"] == "approved"
    assert client.post(f"/mgmt/drafts/{draft_id}/commit", json={}, headers=auth()).status_code == 200


def test_commit_succeeds_when_content_unchanged_after_approval(client):
    # Regression: an approval that is NOT followed by a content edit stays current, so
    # commit proceeds normally.
    r = client.post("/mgmt/prompts/support/system/drafts",
                    json={"version_number": 2,
                          "content": "You are a support agent for {{ customer_name }}.\nUNCHANGED."},
                    headers=auth())
    draft_id = r.json()["id"]
    client.post(f"/mgmt/drafts/{draft_id}/review", json={}, headers=auth())
    assert client.post(f"/mgmt/drafts/{draft_id}/commit", json={}, headers=auth()).status_code == 200


# ── autosave optimistic concurrency (Finding 2) ──────────────────────

def test_put_content_optimistic_concurrency(client):
    base = "You are a support agent for {{ customer_name }}."
    r = client.post("/mgmt/prompts/support/system/drafts",
                    json={"version_number": 2, "content": base + "\nR0."}, headers=auth())
    d = r.json()
    draft_id, sha0 = d["id"], d["draft_sha"]
    assert sha0                                        # create response exposes draft_sha

    # correct base_revision -> 200 and a NEW draft_sha for the client to chain from.
    r1 = client.put(f"/mgmt/drafts/{draft_id}/content",
                    json={"content": base + "\nR1.", "base_revision": sha0}, headers=auth())
    assert r1.status_code == 200, r1.text
    sha1 = r1.json()["draft_sha"]
    assert sha1 and sha1 != sha0

    # a second, in-flight autosave still based on sha0 (stale) -> 409 stale_write with
    # the current tip + content so the client can recover instead of clobbering R1.
    r2 = client.put(f"/mgmt/drafts/{draft_id}/content",
                    json={"content": base + "\nLOSER.", "base_revision": sha0}, headers=auth())
    assert r2.status_code == 409, r2.text
    detail = r2.json()["detail"]
    assert detail["error"] == "stale_write"
    assert detail["current_sha"] == sha1
    assert "R1." in detail["current_content"] and "LOSER" not in detail["current_content"]

    # omitting base_revision -> legacy unconditional write (back-compat).
    r3 = client.put(f"/mgmt/drafts/{draft_id}/content",
                    json={"content": base + "\nR2."}, headers=auth())
    assert r3.status_code == 200, r3.text
    assert "R2." in client.get(f"/mgmt/drafts/{draft_id}", headers=auth()).json()["content"]


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


# ── Security review: headers, key pepper, throttling, /metrics, key lifecycle ──

from incant.server.auth import verify_key, needs_upgrade  # noqa: E402
from incant.service import get_app  # noqa: E402

_SEC_HEADERS = {
    "Content-Security-Policy",
    "X-Content-Type-Options",
    "X-Frame-Options",
    "Referrer-Policy",
    "Permissions-Policy",
}


def test_security_headers_present_on_ui_and_mgmt(client):
    for resp in (client.get("/"), client.get("/mgmt/overview?environment=prod", headers=auth())):
        for h in _SEC_HEADERS:
            assert h in resp.headers, (resp.request.url, h)
        assert resp.headers["X-Content-Type-Options"] == "nosniff"
        assert resp.headers["X-Frame-Options"] == "DENY"
        assert resp.headers["Referrer-Policy"] == "no-referrer"
        assert "script-src 'self'" in resp.headers["Content-Security-Policy"]
        assert "frame-ancestors 'none'" in resp.headers["Content-Security-Policy"]
    # HSTS is off unless TLS enforcement is enabled.
    assert "Strict-Transport-Security" not in client.get("/").headers


def test_hsts_emitted_when_tls_enforced(tmp_path):
    with make_client(tmp_path, enforce_tls=True) as c:
        r = c.get("/")
        assert r.headers["Strict-Transport-Security"] == "max-age=31536000; includeSubDomains"


# ── key pepper (defense-in-depth, versioned hashing) ──

def test_hash_and_verify_key_formats():
    # pepper-less: legacy plain SHA-256, no version tag.
    legacy = hash_key("incant_sk_abc", pepper="")
    assert not legacy.startswith("v2$") and len(legacy) == 64
    assert verify_key("incant_sk_abc", legacy, pepper="")
    assert not verify_key("wrong", legacy, pepper="")
    # peppered: v2$ HMAC, and a legacy hash still verifies (both formats accepted).
    v2 = hash_key("incant_sk_abc", pepper="pep")
    assert v2.startswith("v2$")
    assert verify_key("incant_sk_abc", v2, pepper="pep")
    assert not verify_key("incant_sk_abc", v2, pepper="")  # v2 unverifiable w/o pepper
    assert verify_key("incant_sk_abc", legacy, pepper="pep")  # legacy still ok w/ pepper
    assert needs_upgrade(legacy, pepper="pep") and not needs_upgrade(v2, pepper="pep")
    assert not needs_upgrade(legacy, pepper="")


def test_pepper_v2_issue_and_verify(tmp_path):
    with make_client(tmp_path, key_pepper="s3cr3t") as c:
        r = c.post("/mgmt/keys", json={"principal_name": "svc", "role": "viewer"}, headers=auth())
        assert r.status_code == 200, r.text
        new_key, pid = r.json()["key"], r.json()["principal_id"]
        # the new key authenticates...
        assert c.get("/mgmt/whoami", headers=auth(new_key)).status_code == 200
        # ...and is stored in v2$ form.
        with session_scope() as s:
            k = s.execute(select(models.ApiKey).where(models.ApiKey.principal_id == pid)).scalars().first()
            assert k.hash.startswith("v2$")


def test_legacy_key_upgraded_when_pepper_set(tmp_path):
    # Boot with no pepper: bootstrap admin key is stored legacy (plain SHA-256).
    with make_client(tmp_path) as c:
        with session_scope() as s:
            before = s.execute(
                select(models.ApiKey).where(models.ApiKey.prefix == key_prefix(ADMIN))
            ).scalars().first()
            assert not before.hash.startswith("v2$")
        # Now a pepper is configured; the cache must be reloaded to re-read from DB.
        get_app().settings.key_pepper = "later-pepper"
        get_app().invalidate_auth()
        # A successful auth with the legacy key upgrades it in place.
        assert c.get("/mgmt/whoami", headers=auth()).status_code == 200
        with session_scope() as s:
            after = s.execute(
                select(models.ApiKey).where(models.ApiKey.prefix == key_prefix(ADMIN))
            ).scalars().first()
            assert after.hash.startswith("v2$")
        # It still authenticates on the next request (now via the v2 path).
        assert c.get("/mgmt/whoami", headers=auth()).status_code == 200


def test_pepperless_legacy_behavior_unchanged(client):
    # Default fixture has no pepper: issued keys stay legacy plain SHA-256.
    r = client.post("/mgmt/keys", json={"principal_name": "svc2", "role": "viewer"}, headers=auth())
    pid = r.json()["principal_id"]
    with session_scope() as s:
        k = s.execute(select(models.ApiKey).where(models.ApiKey.principal_id == pid)).scalars().first()
        assert not k.hash.startswith("v2$") and len(k.hash) == 64


# ── failed-auth throttling ──

BAD = "incant_sk_not_a_real_key_000000000000"


def test_failed_auth_throttled_after_limit(tmp_path):
    with make_client(tmp_path, auth_throttle_limit=3, auth_throttle_window=60) as c:
        for _ in range(3):
            assert c.get("/mgmt/envs", headers=auth(BAD)).status_code == 401
        # 4th failure from this IP is refused with 429 + Retry-After.
        r = c.get("/mgmt/envs", headers=auth(BAD))
        assert r.status_code == 429 and int(r.headers["Retry-After"]) >= 1
        # Even a *valid* key is refused while the IP is throttled.
        assert c.get("/mgmt/envs", headers=auth()).status_code == 429


def test_missing_or_empty_credential_never_throttles(tmp_path):
    # A signed-out UI fires unauthenticated fetches on every load; those must not
    # count as brute-force attempts or the browser throttles itself out of the
    # sign-in screen. Only presented-and-wrong tokens count.
    with make_client(tmp_path, auth_throttle_limit=3, auth_throttle_window=60) as c:
        for _ in range(10):
            assert c.get("/mgmt/envs").status_code == 401                       # no header
            assert c.get("/mgmt/envs", headers={"Authorization": "Bearer "}).status_code == 401
        # not throttled: a presented valid key still works immediately
        assert c.get("/mgmt/envs", headers=auth()).status_code == 200


def test_successful_auth_never_throttles(tmp_path):
    with make_client(tmp_path, auth_throttle_limit=3, auth_throttle_window=60) as c:
        for _ in range(10):
            assert c.get("/mgmt/envs", headers=auth()).status_code == 200


def test_throttle_disabled_when_limit_zero(tmp_path):
    with make_client(tmp_path, auth_throttle_limit=0) as c:
        for _ in range(25):
            assert c.get("/mgmt/envs", headers=auth(BAD)).status_code == 401


def test_throttle_window_expiry_resets(tmp_path):
    with make_client(tmp_path, auth_throttle_limit=2, auth_throttle_window=30) as c:
        clock = {"t": 1000.0}
        get_app().throttle._now = lambda: clock["t"]
        assert c.get("/mgmt/envs", headers=auth(BAD)).status_code == 401
        assert c.get("/mgmt/envs", headers=auth(BAD)).status_code == 401
        assert c.get("/mgmt/envs", headers=auth()).status_code == 429  # throttled now
        clock["t"] += 31  # advance past the window
        assert c.get("/mgmt/envs", headers=auth()).status_code == 200  # reset


def test_throttle_is_per_ip_via_xff(tmp_path):
    # XFF is only honored from a trusted proxy — the TestClient's direct peer is
    # "testclient", so trust it and the first XFF hop becomes the throttle key.
    with make_client(tmp_path, auth_throttle_limit=2, auth_throttle_window=60,
                     trusted_proxies="testclient") as c:
        h = {**auth(BAD), "X-Forwarded-For": "9.9.9.9"}
        for _ in range(2):
            assert c.get("/mgmt/envs", headers=h).status_code == 401
        assert c.get("/mgmt/envs", headers=h).status_code == 429
        # A different client IP (first XFF hop) is unaffected.
        assert c.get("/mgmt/envs",
                     headers={**auth(), "X-Forwarded-For": "8.8.8.8"}).status_code == 200


# ── /metrics authentication ──

def test_metrics_requires_auth(client):
    assert client.get("/metrics").status_code == 401


def test_metrics_exposes_git_reads_counter(client):
    # The memory-first fall-through counter is registered and scrapeable.
    r = client.get("/metrics", headers=auth())
    assert r.status_code == 200
    assert "incant_content_git_reads_total" in r.text


# ── key-prefix collision tolerance (Item 2) ──────────────────────────

def test_auth_cache_prefix_collision_both_authenticate(client):
    # Two distinct keys forced into the SAME lookup bucket (a prefix collision) must
    # both authenticate: the cache verifies the full hash against every candidate.
    import time as _time
    from incant.server.auth import AuthCache, _KeyEntry, hash_key

    shared = "incant_sk_" + "c" * 10          # exactly 20 chars -> identical [:20] prefix
    raw1, raw2 = shared + "1111", shared + "2222"
    cache = AuthCache()
    cache._entries = {shared: [
        _KeyEntry(prefix=shared, hash=hash_key(raw1, pepper=""), revoked=False,
                  expires_at=None, principal_id="p1", principal_name="one", bindings=()),
        _KeyEntry(prefix=shared, hash=hash_key(raw2, pepper=""), revoked=False,
                  expires_at=None, principal_id="p2", principal_name="two", bindings=()),
    ]}
    cache._loaded = True
    cache._last_refresh = _time.monotonic()   # suppress any DB refresh
    assert cache.identify(None, f"Bearer {raw1}").principal_id == "p1"
    assert cache.identify(None, f"Bearer {raw2}").principal_id == "p2"
    # A key sharing the prefix but with no matching hash is still rejected.
    from incant.server.auth import AuthError
    with pytest.raises(AuthError):
        cache.identify(None, f"Bearer {shared}9999")


def test_legacy_16char_prefix_row_still_authenticates(client):
    # A row stored with the OLD 16-char prefix must keep authenticating even though new
    # keys store 20 chars (identify probes both lengths).
    raw = "incant_sk_legacy00000000000000000000"
    with session_scope() as s:
        s.add(models.Principal(id="p_legacy", kind="service", subject="legacy", name="legacy"))
        s.flush()
        s.add(models.ApiKey(principal_id="p_legacy", prefix=raw[:16], hash=hash_key(raw),
                            name="legacy"))
        s.add(models.RoleBinding(principal_id="p_legacy", role="admin"))
    get_app().invalidate_auth()
    assert client.get("/mgmt/overview?environment=prod", headers=auth(raw)).status_code == 200


def test_issuance_retries_on_prefix_collision(client, monkeypatch):
    import incant.server.auth as authmod

    first = client.post("/mgmt/keys", json={"principal_name": "first", "role": "viewer"},
                        headers=auth()).json()["key"]
    # Force the next issuance to first pick a raw that COLLIDES on prefix with `first`,
    # then a unique one — the SAVEPOINT retry must recover and return the fresh key.
    colliding = first[:20] + "000000000000"
    fresh = "incant_sk_" + "a1b2c3d4" * 4
    seq = iter([colliding, fresh])
    monkeypatch.setattr(authmod, "_new_raw_key", lambda: next(seq))

    r = client.post("/mgmt/keys", json={"principal_name": "second", "role": "viewer"},
                    headers=auth())
    assert r.status_code == 200, r.text
    assert r.json()["key"] == fresh                       # retried past the collision
    assert client.get("/mgmt/whoami", headers=auth(fresh)).status_code == 200


# ── session administration (Item 4) ──────────────────────────────────

def _insert_session(principal_id, *, remember=False, ttl_hours=12):
    now = dt.datetime.now(dt.timezone.utc)
    with session_scope() as s:
        s.add(models.Session(
            id=new_session_id(), token_hash=hash_key(new_session_token()),
            principal_id=principal_id, created_at=now,
            expires_at=now + dt.timedelta(hours=ttl_hours), last_seen_at=now,
            csrf_token="csrf-x", remember=remember))


def test_list_sessions_marks_current_cookie_session(client):
    body = login(client)                       # session A (cookie in the jar)
    pid = body["principal_id"]
    _insert_session(pid)                        # session B (same principal)
    r = client.get("/auth/sessions")           # cookie-authenticated
    assert r.status_code == 200, r.text
    sessions = r.json()["sessions"]
    assert len(sessions) == 2
    assert sum(1 for s in sessions if s["current"]) == 1   # exactly the cookie session
    for s in sessions:
        assert {"id", "created_at", "last_seen_at", "expires_at",
                "remember", "current"} <= s.keys()


def test_list_sessions_bearer_marks_none_current(client):
    pid = client.get("/mgmt/whoami", headers=auth()).json()["principal_id"]
    _insert_session(pid)
    _insert_session(pid)
    r = client.get("/auth/sessions", headers=auth())       # bearer — no current session
    assert r.status_code == 200, r.text
    sessions = r.json()["sessions"]
    assert len(sessions) == 2 and all(not s["current"] for s in sessions)


def test_sign_out_everywhere_kills_all_sessions(client):
    body = login(client)                       # session A (cookie)
    pid = body["principal_id"]
    _insert_session(pid)                        # a second session for the same principal
    # CSRF is required in cookie mode.
    assert client.delete("/auth/sessions").status_code == 403
    r = client.delete("/auth/sessions", headers={"X-Incant-CSRF": body["csrf"]})
    assert r.status_code == 204, r.text
    assert r.headers["X-Incant-Sessions-Deleted"] == "2"
    with session_scope() as s:
        remaining = s.execute(
            select(models.Session).where(models.Session.principal_id == pid)
        ).scalars().all()
    assert remaining == []
    assert client.get("/auth/session").status_code == 401   # cookie cleared too


def test_admin_revoke_principal_sessions(client):
    pid = client.post("/mgmt/keys", json={"principal_name": "svc", "role": "viewer"},
                      headers=auth()).json()["principal_id"]
    _insert_session(pid)
    _insert_session(pid)
    r = client.delete(f"/mgmt/principals/{pid}/sessions", headers=auth())
    assert r.status_code == 200, r.text
    assert r.json()["revoked"] == 2
    with session_scope() as s:
        assert s.execute(
            select(models.Session).where(models.Session.principal_id == pid)
        ).scalars().first() is None
        actions = [a.action for a in s.execute(select(models.AuditLog)).scalars()]
    assert "session.revoke_all" in actions
    assert client.delete("/mgmt/principals/p_nope/sessions", headers=auth()).status_code == 404


def test_principals_payload_includes_active_session_count(client):
    body = login(client)                       # +1 active session on the admin principal
    pid = body["principal_id"]
    _insert_session(pid)                        # +1 more
    _insert_session(pid, ttl_hours=-1)          # expired -> must NOT count
    d = client.get("/mgmt/principals", headers=auth()).json()
    me = next(p for p in d["principals"] if p["id"] == pid)
    assert me["sessions"] == 2


def test_metrics_ok_with_viewer_key(client):
    r = client.get("/metrics", headers=auth())  # admin implies viewer
    assert r.status_code == 200 and "incant_render_seconds" in r.text
    # a renderer-only key holds no viewer -> refused.
    assert client.get("/metrics", headers=auth(client.renderer_key)).status_code == 401


def test_metrics_ok_with_metrics_token(tmp_path):
    with make_client(tmp_path, metrics_token="prom-tok") as c:
        assert c.get("/metrics", headers={"Authorization": "Bearer prom-tok"}).status_code == 200
        assert c.get("/metrics", headers={"Authorization": "Bearer wrong"}).status_code == 401
        assert c.get("/metrics").status_code == 401


# ── key lifecycle: expiry at issuance + rotation ──

def test_key_issued_with_expiry(client):
    r = client.post("/mgmt/keys",
                    json={"principal_name": "temp", "role": "viewer", "expires_in_days": 7},
                    headers=auth())
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["expires_at"] is not None
    pid = body["principal_id"]
    # the listing surfaces expires_at for the UI.
    d = client.get("/mgmt/principals", headers=auth()).json()
    me = next(p for p in d["principals"] if p["id"] == pid)
    assert me["keys"][0]["expires_at"] is not None


def test_issue_key_on_principal_with_expiry(client):
    pid = client.post("/mgmt/keys", json={"principal_name": "p", "role": "viewer"},
                      headers=auth()).json()["principal_id"]
    r = client.post(f"/mgmt/principals/{pid}/keys", json={"expires_in_days": 1}, headers=auth())
    assert r.status_code == 200 and r.json()["expires_at"] is not None
    # body-less issuance still works (no expiry).
    r2 = client.post(f"/mgmt/principals/{pid}/keys", headers=auth())
    assert r2.status_code == 200 and r2.json()["expires_at"] is None


def test_rotate_key_roundtrip(client):
    r = client.post("/mgmt/keys", json={"principal_name": "rot", "role": "operator",
                                        "project_id": "support"}, headers=auth())
    old_key, pid = r.json()["key"], r.json()["principal_id"]
    old_kid = next(k["id"] for k in
                   next(p for p in client.get("/mgmt/principals", headers=auth()).json()["principals"]
                        if p["id"] == pid)["keys"])
    assert client.get("/mgmt/envs", headers=auth(old_key)).status_code == 200

    rot = client.post(f"/mgmt/keys/{old_kid}/rotate", json={"expires_in_days": 30}, headers=auth())
    assert rot.status_code == 200, rot.text
    new_key = rot.json()["key"]
    assert rot.json()["expires_at"] is not None and rot.json()["revoked_key_id"] == old_kid
    # Old key stops authenticating; new key works and keeps the same principal/role.
    assert client.get("/mgmt/envs", headers=auth(old_key)).status_code == 401
    assert client.get("/mgmt/envs", headers=auth(new_key)).status_code == 200
    who = client.get("/mgmt/whoami", headers=auth(new_key)).json()
    assert who["roles"] == [{"role": "operator", "project_id": "support", "environment_id": None}]
    # An audit row records the rotation.
    with session_scope() as s:
        actions = [a.action for a in s.execute(select(models.AuditLog)).scalars()]
    assert "key.rotate" in actions


def test_rotate_unknown_key_404(client):
    assert client.post("/mgmt/keys/999999/rotate", headers=auth()).status_code == 404


# ── browser sessions (HttpOnly cookie auth + CSRF) ───────────────────

def login(client, key=ADMIN, remember=False):
    """Exchange an API key for a session cookie (stored in the client's jar).
    Returns the JSON body (principal_id/name/roles/csrf) plus the raw Set-Cookie."""
    r = client.post("/auth/session", json={"key": key, "remember": remember})
    assert r.status_code == 200, r.text
    body = r.json()
    body["_set_cookie"] = r.headers.get("set-cookie", "")
    return body


def test_login_sets_httponly_samesite_cookie(client):
    body = login(client)
    # Response shape the UI codes against.
    assert body["principal_id"] and body["name"] == "bootstrap-admin"
    assert body["csrf"] and isinstance(body["roles"], list)
    assert {"role": "admin", "project_id": None, "environment_id": None} in body["roles"]
    # Cookie flags: HttpOnly + SameSite=Strict + Path=/, and (no remember) no Max-Age.
    cookie = body["_set_cookie"]
    assert cookie.startswith("incant_session=")
    low = cookie.lower()
    assert "httponly" in low and "samesite=strict" in low and "path=/" in low
    assert "max-age" not in low                       # session cookie (not remembered)
    # The token itself is never JS-readable content we return in the body.
    assert "incant_session" not in str(body["roles"])


def test_login_remember_sets_max_age(client):
    body = login(client, remember=True)
    low = body["_set_cookie"].lower()
    assert "max-age=" in low                          # persistent cookie
    # 30-day absolute lifetime.
    assert "max-age=2592000" in low


def test_login_secure_flag_when_tls_enforced(tmp_path):
    with make_client(tmp_path, enforce_tls=True) as c:
        body = login(c)
        assert "secure" in body["_set_cookie"].lower()


def test_login_bad_key_throttles(tmp_path):
    with make_client(tmp_path, auth_throttle_limit=3, auth_throttle_window=60) as c:
        for _ in range(3):
            assert c.post("/auth/session", json={"key": BAD}).status_code == 401
        # 4th bad login from this IP is refused with 429 — a bad key is a presented
        # credential and counts toward the throttle.
        r = c.post("/auth/session", json={"key": BAD})
        assert r.status_code == 429 and int(r.headers["Retry-After"]) >= 1
        # Even a valid login is refused while the IP is throttled.
        assert c.post("/auth/session", json={"key": ADMIN}).status_code == 429


def test_get_session_roundtrip(client):
    body = login(client)
    r = client.get("/auth/session")                   # cookie sent from the jar
    assert r.status_code == 200, r.text
    who = r.json()
    assert who["principal_id"] == body["principal_id"]
    assert who["name"] == "bootstrap-admin"
    assert who["csrf"] == body["csrf"]
    assert {"role": "admin", "project_id": None, "environment_id": None} in who["roles"]


def test_get_session_no_cookie_401(client):
    assert client.get("/auth/session").status_code == 401


def test_cookie_auth_get_needs_no_csrf(client):
    login(client)
    # A safe GET is cookie-authenticated with no CSRF header required.
    r = client.get("/mgmt/overview?environment=prod")
    assert r.status_code == 200, r.text


def test_cookie_auth_post_without_csrf_is_403(client):
    login(client)
    # A cookie-authenticated mutation without the CSRF header is refused.
    r = client.post("/mgmt/prompts", json={"prompt_id": "growth/csrf-a"})
    assert r.status_code == 403, r.text
    assert r.json()["detail"] == "csrf_required"


def test_cookie_auth_post_with_csrf_succeeds(client):
    body = login(client)
    r = client.post("/mgmt/prompts", json={"prompt_id": "growth/csrf-b"},
                    headers={"X-Incant-CSRF": body["csrf"]})
    assert r.status_code == 200, r.text
    assert r.json()["prompt_id"] == "growth/csrf-b"
    # A wrong CSRF token is still refused.
    r2 = client.post("/mgmt/prompts", json={"prompt_id": "growth/csrf-c"},
                     headers={"X-Incant-CSRF": "not-the-token"})
    assert r2.status_code == 403 and r2.json()["detail"] == "csrf_required"


def test_bearer_post_needs_no_csrf_even_with_cookie(client):
    # Log in (cookie in the jar), then mutate with a bearer header and NO CSRF token.
    # Bearer takes precedence and is CSRF-immune, so it succeeds.
    login(client)
    r = client.post("/mgmt/prompts", json={"prompt_id": "growth/bearer-nocsrf"},
                    headers=auth())
    assert r.status_code == 200, r.text


def test_delete_session_signs_out(client):
    body = login(client)
    assert client.get("/auth/session").status_code == 200
    # DELETE without CSRF is refused...
    assert client.delete("/auth/session").status_code == 403
    # ...with the CSRF header it signs out: cookie cleared + row deleted.
    r = client.delete("/auth/session", headers={"X-Incant-CSRF": body["csrf"]})
    assert r.status_code == 204, r.text
    assert "max-age=0" in r.headers.get("set-cookie", "").lower()
    with session_scope() as s:
        assert s.execute(select(models.Session)).scalars().first() is None
    # Subsequent whoami is unauthenticated (the jar dropped the cleared cookie).
    assert client.get("/auth/session").status_code == 401


def test_expired_session_401_and_swept(client):
    login(client)
    # Force the session past its absolute expiry.
    with session_scope() as s:
        row = s.execute(select(models.Session)).scalars().first()
        row.expires_at = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=1)
    # An expired cookie authenticates nothing (and is not a throttle-worthy guess).
    assert client.get("/auth/session").status_code == 401
    # The startup sweep deletes expired rows.
    with session_scope() as s:
        assert sweep_expired_sessions(s) >= 1
    with session_scope() as s:
        assert s.execute(select(models.Session)).scalars().first() is None


def test_serving_endpoint_refuses_cookie_auth(client):
    login(client)
    # Sessions are control-plane only; the serving hot path is bearer-only. The cookie
    # in the jar is ignored → 401 (no bearer credential).
    r = client.post("/prompt/support/system",
                    json={"variables": {"customer_name": "Acme", "history": []}})
    assert r.status_code == 401, r.text


def test_xff_honored_only_from_trusted_proxy(tmp_path):
    # Untrusted peer: XFF is ignored, so every request shares the direct-peer bucket.
    with make_client(tmp_path, auth_throttle_limit=2, auth_throttle_window=60) as c:
        h = {**auth(BAD), "X-Forwarded-For": "9.9.9.9"}
        assert c.get("/mgmt/envs", headers=h).status_code == 401
        assert c.get("/mgmt/envs", headers=h).status_code == 401
        # A different XFF is the SAME untrusted peer → already throttled.
        assert c.get("/mgmt/envs",
                     headers={**auth(), "X-Forwarded-For": "8.8.8.8"}).status_code == 429
    # Trusted peer: the first XFF hop is honored, so distinct clients bucket apart.
    with make_client(tmp_path, auth_throttle_limit=2, auth_throttle_window=60,
                     trusted_proxies="testclient") as c:
        h = {**auth(BAD), "X-Forwarded-For": "9.9.9.9"}
        assert c.get("/mgmt/envs", headers=h).status_code == 401
        assert c.get("/mgmt/envs", headers=h).status_code == 401
        assert c.get("/mgmt/envs", headers=h).status_code == 429   # 9.9.9.9 throttled
        # 8.8.8.8 is a different client the proxy saw → unaffected.
        assert c.get("/mgmt/envs",
                     headers={**auth(), "X-Forwarded-For": "8.8.8.8"}).status_code == 200
