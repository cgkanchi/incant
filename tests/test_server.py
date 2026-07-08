"""HTTP-level tests over the FastAPI app: auth, serving, mgmt, the tweak flow."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from incant import db
from incant.config import Settings, set_settings
from incant.seed import seed
from incant.service import reset_app

ADMIN = "incant_sk_dev_admin"


@pytest.fixture()
def client(tmp_path):
    set_settings(Settings(
        database_url=f"sqlite:///{tmp_path/'incant.db'}",
        repo_path=str(tmp_path / "repo"),
        bootstrap_admin_key=ADMIN,
    ))
    db.reset_engine()
    reset_app()
    renderer_key = seed()
    from incant.server.app import create_app
    with TestClient(create_app()) as c:
        c.renderer_key = renderer_key
        yield c


def auth(key=ADMIN):
    return {"Authorization": f"Bearer {key}"}


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
    r = client.post(f"/mgmt/drafts/{draft_id}/commit", json={"author": "sam"}, headers=auth())
    assert r.status_code == 412

    # 3. a different reviewer approves, then commit succeeds
    client.post(f"/mgmt/drafts/{draft_id}/review", json={"reviewer": "rae"}, headers=auth())
    r = client.post(f"/mgmt/drafts/{draft_id}/commit", json={"author": "sam"}, headers=auth())
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


def test_kill_switch_over_http(client):
    r = client.post("/mgmt/envs/prod/kill?prompt_id=support/system",
                    json={"engaged": True}, headers=auth())
    assert r.status_code == 200
    # u_12 would normally get v2@tip via team-x-tip; kill forces the default (v2@live)
    r = client.post("/prompt/support/system/evaluate",
                    json={"flags": {"user_id": "u_12"}}, headers=auth())
    assert r.json()["matched_rule"] == "default"
