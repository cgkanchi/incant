"""Rule-mutation authorization: the DUAL-authz invariant that closes the rehoming attack.

Rule ids are globally unique, client-supplied strings that GET /rules exposes, and
``TargetingService.upsert_rule`` loads any existing rule by id then overwrites its
scope/prompt_id (guarding only cross-ENVIRONMENT capture). Authorizing the REQUEST scope
alone let a project-A operator hijack an env-wide GLOBAL rule (POST its id re-scoped to A)
or rehome a known project-B rule into A. The fix requires authority over BOTH the stored
scope and the requested scope: creating a rule needs authority over where it will live;
editing one needs authority over where it lives NOW *and* where it will live.

Boot/auth/idiom helpers are reused straight from tests/test_server.py, as tests/test_batch_ops.py does.
"""

from __future__ import annotations

import pytest

from .test_server import auth, make_client, make_key

# Project A = "support" (has support/system @ v2); Project B = "shared" (has
# shared/style/language-rules @ v1). Both are seeded with prompts/versions.
ENV = "prod"
A_PROMPT = "support/system"          # project A, version 2 exists
B_PROMPT = "shared/style/language-rules"  # project B, version 1 exists


@pytest.fixture()
def client(tmp_path):
    with make_client(tmp_path) as c:
        yield c


def _rules(client, env=ENV):
    return {r["id"]: r for r in
            client.get(f"/mgmt/envs/{env}/rules", headers=auth()).json()["rules"]}


def _seed_cross_scope_rules(client):
    """As admin, plant a GLOBAL rule and a project-B (shared) prompt-scoped rule that a
    project-A operator can see (GET /rules) but must not be able to hijack/rehome."""
    r = client.post(f"/mgmt/envs/{ENV}/rules",
                    json={"id": "glob-1", "scope": "global", "priority": 50,
                          "serve": {"version": 2}, "comment": "env-wide global"},
                    headers=auth())
    assert r.status_code == 200, r.text
    r = client.post(f"/mgmt/envs/{ENV}/rules",
                    json={"id": "b-rule", "scope": "prompt", "prompt_id": B_PROMPT,
                          "priority": 40, "serve": {"version": 1}, "comment": "project B"},
                    headers=auth())
    assert r.status_code == 200, r.text
    got = _rules(client)
    assert got["glob-1"]["scope"] == "global" and got["glob-1"]["prompt_id"] is None
    assert got["b-rule"]["prompt_id"] == B_PROMPT


# ── regression pins: the legitimate operator path still works ─────────

def test_a_operator_can_create_and_edit_own_scoped_rule(client):
    op = make_key(client, "operator", project="support", env=ENV)
    # Create an A-scoped rule.
    r = client.post(f"/mgmt/envs/{ENV}/rules",
                    json={"id": "a-rule", "scope": "prompt", "prompt_id": A_PROMPT,
                          "priority": 12, "serve": {"version": 2}, "comment": "own"},
                    headers=auth(op))
    assert r.status_code == 200, r.text
    # Edit it (same scope) — stored scope A and requested scope A are both authorized.
    r = client.post(f"/mgmt/envs/{ENV}/rules",
                    json={"id": "a-rule", "scope": "prompt", "prompt_id": A_PROMPT,
                          "priority": 18, "serve": {"version": 2}, "comment": "edited"},
                    headers=auth(op))
    assert r.status_code == 200, r.text
    assert _rules(client)["a-rule"]["priority"] == 18


# ── the hijack / rehoming attacks are refused, nothing changes ────────

def test_a_operator_cannot_hijack_global_rule(client):
    _seed_cross_scope_rules(client)
    op = make_key(client, "operator", project="support", env=ENV)
    # Take the GLOBAL rule's id and POST it re-scoped to project A. Requested scope (A) is
    # authorized, but the STORED scope is global -> needs env-wide operator -> 403.
    r = client.post(f"/mgmt/envs/{ENV}/rules",
                    json={"id": "glob-1", "scope": "prompt", "prompt_id": A_PROMPT,
                          "priority": 1, "serve": {"version": 2}, "comment": "hijack"},
                    headers=auth(op))
    assert r.status_code == 403, r.text
    got = _rules(client)["glob-1"]
    assert got["scope"] == "global" and got["prompt_id"] is None   # untouched
    assert got["comment"] == "env-wide global" and got["priority"] == 50


def test_a_operator_cannot_rehome_project_b_rule(client):
    _seed_cross_scope_rules(client)
    op = make_key(client, "operator", project="support", env=ENV)
    # Take project-B's rule id and rehome it into project A. Requested scope (A) is
    # authorized, but the STORED scope is project B -> needs operator on B -> 403.
    r = client.post(f"/mgmt/envs/{ENV}/rules",
                    json={"id": "b-rule", "scope": "prompt", "prompt_id": A_PROMPT,
                          "priority": 2, "serve": {"version": 2}, "comment": "rehome"},
                    headers=auth(op))
    assert r.status_code == 403, r.text
    got = _rules(client)["b-rule"]
    assert got["prompt_id"] == B_PROMPT and got["comment"] == "project B"  # untouched


def test_a_operator_cannot_flip_own_rule_to_global(client):
    op = make_key(client, "operator", project="support", env=ENV)
    r = client.post(f"/mgmt/envs/{ENV}/rules",
                    json={"id": "a-rule", "scope": "prompt", "prompt_id": A_PROMPT,
                          "priority": 5, "serve": {"version": 2}, "comment": "own"},
                    headers=auth(op))
    assert r.status_code == 200, r.text
    # Flip the OWN A-rule to global -> the REQUESTED-scope check (global needs env-wide) 403s.
    r = client.post(f"/mgmt/envs/{ENV}/rules",
                    json={"id": "a-rule", "scope": "global", "prompt_id": None,
                          "priority": 5, "serve": {"version": 2}, "comment": "escalate"},
                    headers=auth(op))
    assert r.status_code == 403, r.text
    got = _rules(client)["a-rule"]
    assert got["scope"] == "prompt" and got["prompt_id"] == A_PROMPT  # untouched


# ── env-wide operator holds authority over both ends -> re-scope allowed ──

def test_env_wide_operator_can_rescope_both_directions(client):
    _seed_cross_scope_rules(client)
    envop = make_key(client, "operator", env=ENV)  # env-wide, no project scope
    # global -> prompt: env-wide op authorizes the stored (global) AND requested (A) scopes.
    r = client.post(f"/mgmt/envs/{ENV}/rules",
                    json={"id": "glob-1", "scope": "prompt", "prompt_id": A_PROMPT,
                          "priority": 50, "serve": {"version": 2}, "comment": "now scoped"},
                    headers=auth(envop))
    assert r.status_code == 200, r.text
    assert _rules(client)["glob-1"]["scope"] == "prompt"
    # prompt -> global: env-wide op authorizes the stored (project B) AND requested (global).
    r = client.post(f"/mgmt/envs/{ENV}/rules",
                    json={"id": "b-rule", "scope": "global", "prompt_id": None,
                          "priority": 40, "serve": {"version": 1}, "comment": "now global"},
                    headers=auth(envop))
    assert r.status_code == 200, r.text
    got = _rules(client)["b-rule"]
    assert got["scope"] == "global" and got["prompt_id"] is None


# ── batch carries the identical invariant, atomically ─────────────────

def test_batch_hijack_attempt_persists_nothing(client):
    _seed_cross_scope_rules(client)
    op = make_key(client, "operator", project="support", env=ENV)
    # One legitimate A-rule + one global-rule hijack. Authz is checked for every item up
    # front, so the hijack 403s the whole batch and the legitimate sibling never lands.
    rules = [
        {"id": "batch-a", "scope": "prompt", "prompt_id": A_PROMPT,
         "priority": 12, "serve": {"version": 2}, "comment": "legit"},
        {"id": "glob-1", "scope": "prompt", "prompt_id": A_PROMPT,
         "priority": 1, "serve": {"version": 2}, "comment": "hijack"},
    ]
    r = client.post(f"/mgmt/envs/{ENV}/rules/batch", json={"rules": rules}, headers=auth(op))
    assert r.status_code == 403, r.text
    got = _rules(client)
    assert "batch-a" not in got                       # legitimate sibling rolled back
    assert got["glob-1"]["scope"] == "global"         # global rule untouched


# ── same-class endpoint check: PATCH /rules/{id} status ───────────────
#
# patch_rule already authorizes on the STORED scope (it only changes status, never
# scope/prompt_id), so an own-project operator cannot archive a global or foreign rule.
# These pins verify that (no fix was needed there) and that own-project archive still works.

def test_patch_status_on_foreign_or_global_rule_forbidden(client):
    _seed_cross_scope_rules(client)
    op = make_key(client, "operator", project="support", env=ENV)
    # Archiving the GLOBAL rule needs env-wide operator -> 403.
    r = client.patch(f"/mgmt/envs/{ENV}/rules/glob-1", json={"status": "archived"},
                     headers=auth(op))
    assert r.status_code == 403, r.text
    # Archiving project-B's rule needs operator on B -> 403.
    r = client.patch(f"/mgmt/envs/{ENV}/rules/b-rule", json={"status": "archived"},
                     headers=auth(op))
    assert r.status_code == 403, r.text
    got = _rules(client)
    assert got["glob-1"]["status"] == "active" and got["b-rule"]["status"] == "active"
    # An own-project rule CAN be archived (releaser/operator on its stored scope).
    client.post(f"/mgmt/envs/{ENV}/rules",
                json={"id": "a-own", "scope": "prompt", "prompt_id": A_PROMPT,
                      "priority": 9, "serve": {"version": 2}}, headers=auth(op))
    r = client.patch(f"/mgmt/envs/{ENV}/rules/a-own", json={"status": "archived"},
                     headers=auth(op))
    assert r.status_code == 200, r.text
    assert _rules(client)["a-own"]["status"] == "archived"
