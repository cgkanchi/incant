"""Rule-mutation authorization.

Rule ids are globally unique, client-supplied strings that GET /rules exposes, and
``TargetingService.upsert_rule`` loads any existing rule by id then overwrites its
``prompt_id`` (guarding only cross-ENVIRONMENT capture). The invariant is DUAL
authorization: authority over where a rule lives NOW and where it will live. With one
project per deployment and prompt-scoped rules only, that collapses to "operator on the
project + env" — but the request/stored checks still both run, and a principal without
operator cannot create, edit, reorder or archive anything.

Boot/auth/idiom helpers are reused straight from tests/test_server.py.
"""

from __future__ import annotations

import pytest

from .test_server import auth, make_client, make_key

ENV = "prod"
A_PROMPT = "support/system"                 # version 2 exists
B_PROMPT = "support/style/language-rules"   # version 1 exists (the fragment)


@pytest.fixture()
def client(tmp_path):
    with make_client(tmp_path) as c:
        yield c


def _rules(client, env=ENV):
    return {r["id"]: r for r in
            client.get(f"/mgmt/envs/{env}/rules", headers=auth()).json()["rules"]}


def _seed_b_rule(client):
    r = client.post(f"/mgmt/envs/{ENV}/rules",
                    json={"id": "b-rule", "prompt_id": B_PROMPT,
                          "priority": 40, "serve": {"version": 1}, "comment": "fragment rule"},
                    headers=auth())
    assert r.status_code == 200, r.text


def test_operator_can_create_and_edit_own_rule(client):
    op = make_key(client, "operator", project="support", env=ENV)
    r = client.post(f"/mgmt/envs/{ENV}/rules",
                    json={"id": "a-rule", "prompt_id": A_PROMPT,
                          "priority": 12, "serve": {"version": 2}, "comment": "own"},
                    headers=auth(op))
    assert r.status_code == 200, r.text
    r = client.post(f"/mgmt/envs/{ENV}/rules",
                    json={"id": "a-rule", "prompt_id": A_PROMPT,
                          "priority": 18, "serve": {"version": 2}, "comment": "edited"},
                    headers=auth(op))
    assert r.status_code == 200, r.text
    assert _rules(client)["a-rule"]["priority"] == 18


def test_project_operator_governs_every_prompt_rule(client):
    # One project per deployment: a project-scoped operator legitimately edits ANY rule
    # (there is no foreign project to protect), including moving it between prompts.
    _seed_b_rule(client)
    op = make_key(client, "operator", project="support", env=ENV)
    r = client.post(f"/mgmt/envs/{ENV}/rules",
                    json={"id": "b-rule", "prompt_id": A_PROMPT,
                          "priority": 2, "serve": {"version": 2}, "comment": "retargeted"},
                    headers=auth(op))
    assert r.status_code == 200, r.text
    got = _rules(client)["b-rule"]
    assert got["prompt_id"] == A_PROMPT and got["comment"] == "retargeted"


def test_operator_scoped_to_another_env_is_refused(client):
    _seed_b_rule(client)
    staging_op = make_key(client, "operator", project="support", env="staging")
    r = client.post(f"/mgmt/envs/{ENV}/rules",
                    json={"id": "b-rule", "prompt_id": A_PROMPT,
                          "priority": 2, "serve": {"version": 2}, "comment": "wrong env"},
                    headers=auth(staging_op))
    assert r.status_code == 403, r.text
    assert _rules(client)["b-rule"]["prompt_id"] == B_PROMPT   # untouched
    r = client.patch(f"/mgmt/envs/{ENV}/rules/b-rule", json={"status": "archived"},
                     headers=auth(staging_op))
    assert r.status_code == 403


def test_viewer_cannot_mutate_rules(client):
    _seed_b_rule(client)
    viewer = make_key(client, "viewer", project="support", env=ENV)
    r = client.post(f"/mgmt/envs/{ENV}/rules",
                    json={"id": "v-rule", "prompt_id": A_PROMPT, "priority": 1,
                          "serve": {"version": 2}}, headers=auth(viewer))
    assert r.status_code == 403
    r = client.patch(f"/mgmt/envs/{ENV}/rules/b-rule", json={"status": "archived"},
                     headers=auth(viewer))
    assert r.status_code == 403
    assert _rules(client)["b-rule"]["status"] == "active"


def test_batch_authz_failure_persists_nothing(client):
    _seed_b_rule(client)
    staging_op = make_key(client, "operator", project="support", env="staging")
    rules = [
        {"id": "batch-a", "prompt_id": A_PROMPT, "priority": 12, "serve": {"version": 2}},
        {"id": "b-rule", "prompt_id": A_PROMPT, "priority": 1, "serve": {"version": 2}},
    ]
    r = client.post(f"/mgmt/envs/{ENV}/rules/batch", json={"rules": rules}, headers=auth(staging_op))
    assert r.status_code == 403, r.text
    got = _rules(client)
    assert "batch-a" not in got and got["b-rule"]["prompt_id"] == B_PROMPT


def test_rule_without_prompt_is_rejected(client):
    # There is no global scope any more: a rule must name its prompt (422 at the schema).
    r = client.post(f"/mgmt/envs/{ENV}/rules",
                    json={"id": "g", "priority": 5, "serve": {"version": 2}}, headers=auth())
    assert r.status_code == 422
    r = client.post(f"/mgmt/envs/{ENV}/rules",
                    json={"id": "g", "scope": "global", "priority": 5, "serve": {"version": 2}},
                    headers=auth())
    assert r.status_code == 422
