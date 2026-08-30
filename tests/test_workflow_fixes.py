"""Regression tests for the deep product-review workflow findings: malformed
drafts must stay editable, a brand-new prompt must be publishable, stop-test
must actually promote the tested version, new versions seed from published (not
tip) content, unservable rules warn at save time, and the example dataset
enforces its own environment story."""

from __future__ import annotations

from .test_server import auth, make_client


def _versions(client, pid, env="prod"):
    r = client.get(f"/mgmt/prompts/{pid}/versions?environment={env}", headers=auth())
    assert r.status_code == 200, r.text
    return {v["version"]: v for v in r.json()["versions"]}


def _env_rules(client, env="prod"):
    return client.get(f"/mgmt/envs/{env}/rules", headers=auth()).json()


# ── 1. malformed Jinja is an ordinary editor state, never a dead end ──

def test_malformed_draft_saves_reads_and_recovers(tmp_path):
    with make_client(tmp_path) as client:
        d = client.post("/mgmt/prompts/support/greeting/drafts",
                        json={"version_number": 2}, headers=auth()).json()

        # Mid-typing syntax error autosaves fine and reports itself as data.
        r = client.put(f"/mgmt/drafts/{d['id']}/content",
                       json={"content": "{% if broken %}",
                             "base_revision": d["draft_sha"]}, headers=auth())
        assert r.status_code == 200, r.text
        saved = r.json()
        assert "template error" in saved["variables"].get("error", "")
        assert saved["lint"]["status"] == "invalid"

        # The draft still LOADS (this returned 500 and bricked the editor).
        r = client.get(f"/mgmt/drafts/{d['id']}", headers=auth())
        assert r.status_code == 200, r.text

        # And the fix lands by chaining off the returned revision — no 409 trap.
        r = client.put(f"/mgmt/drafts/{d['id']}/content",
                       json={"content": "{% if broken %}yes{% endif %}",
                             "base_revision": saved["draft_sha"]}, headers=auth())
        assert r.status_code == 200, r.text
        assert r.json()["lint"]["status"] == "valid"
        assert "error" not in r.json()["variables"]


# ── 2. a brand-new prompt publishes through the same door as everyone ──

def test_first_publish_sets_pointer_and_default(tmp_path):
    with make_client(tmp_path) as client:
        client.patch("/mgmt/projects/support", json={"review_policy": 0}, headers=auth())
        client.post("/mgmt/prompts", json={"prompt_id": "support/fresh"}, headers=auth())
        d = client.post("/mgmt/prompts/support/fresh/drafts",
                        json={"version_number": 1, "content": "Hello {{ name }}"},
                        headers=auth()).json()
        out = client.post(f"/mgmt/drafts/{d['id']}/commit", json={}, headers=auth()).json()

        v1 = _versions(client, "support/fresh")[1]
        assert v1["live_sha"] is None and v1["tip_ahead"] == 0  # the trap state

        r = client.post("/mgmt/envs/prod/publish",
                        json={"prompt_id": "support/fresh", "version_number": 1,
                              "to_sha": out["full_sha"], "confirm": "support/fresh",
                              "make_default": True}, headers=auth())
        assert r.status_code == 200, r.text

        v1 = _versions(client, "support/fresh")[1]
        assert v1["live_full_sha"] == out["full_sha"] and v1["is_default"]
        r = client.post("/prompt/support/fresh",
                        json={"flags": {}, "variables": {"name": "Ada"}}, headers=auth())
        assert r.status_code == 200 and "Hello Ada" in r.json()["prompt"], r.text


# ── 3. stop-test PROMOTES the tested version — cohort and control converge on it ──

def test_publish_make_default_promotes_tested_version(tmp_path):
    with make_client(tmp_path) as client:
        # Seeded state: default v2, beta cohort tests v3@live via beta-gets-v3.
        assert _env_rules(client)["defaults"]["support/system"] == 2
        v3 = _versions(client, "support/system")[3]

        r = client.post("/mgmt/envs/prod/publish",
                        json={"prompt_id": "support/system", "version_number": 3,
                              "to_sha": v3["live_full_sha"], "confirm": "support/system",
                              "archive_rule_ids": ["beta-gets-v3"], "make_default": True},
                        headers=auth())
        assert r.status_code == 200, r.text

        d = _env_rules(client)
        assert d["defaults"]["support/system"] == 3  # everyone now gets v3
        assert {x["id"]: x for x in d["rules"]}["beta-gets-v3"]["status"] == "archived"
        # Control traffic (no flags) actually receives v3 content now.
        r = client.post("/prompt/support/system",
                        json={"flags": {}, "variables": {"customer_name": "Acme",
                                                         "history": []}}, headers=auth())
        assert r.status_code == 200 and "support team" in r.json()["prompt"], r.text


# ── new versions seed from PUBLISHED content, not unpublished tip edits ──

def test_new_version_seeds_from_live_sha(tmp_path):
    with make_client(tmp_path) as client:
        client.patch("/mgmt/projects/support", json={"review_policy": 0}, headers=auth())
        # support/system v2: live = formal baseline; tip = warm rewrite (2 ahead).
        v2 = _versions(client, "support/system")[2]
        assert v2["live_full_sha"] != v2["tip_full_sha"]

        d = client.post("/mgmt/prompts/support/system/drafts",
                        json={"seed_from_version": 2, "seed_from_sha": v2["live_full_sha"],
                              "title": "New version"}, headers=auth()).json()
        assert d["version_number"] == 4
        assert "formal" in d["content"]          # published baseline...
        assert "warm and concise" not in d["content"]  # ...not the unpublished tip


# ── unservable serve targets warn at SAVE time, not after baffling traffic ──

def test_rule_with_unservable_target_warns_on_create(tmp_path):
    with make_client(tmp_path) as client:
        # support/greeting v2 is committed but has never been published in prod.
        rule = {"id": "greet-v2", "scope": "prompt", "prompt_id": "support/greeting",
                "priority": 70, "serve": {"version": 2}, "comment": "unservable on arrival",
                "when": {"flag": "beta_opt_in", "op": "eq", "value": True}}
        r = client.post("/mgmt/envs/prod/rules", json=rule, headers=auth())
        assert r.status_code == 200, r.text
        assert "can't serve yet" in r.json().get("warning", ""), r.json()

        # Same honesty through the batch door (the composer's path).
        rule["id"] = "greet-v2-b"; rule["priority"] = 71
        r = client.post("/mgmt/envs/prod/rules/batch", json={"rules": [rule]}, headers=auth())
        assert r.status_code == 200, r.text
        assert any("can't serve yet" in w for w in r.json().get("warnings", [])), r.json()

        # A servable target stays warning-free.
        ok = {"id": "sys-v2", "scope": "prompt", "prompt_id": "support/system",
              "priority": 72, "serve": {"version": 2}, "comment": "fine",
              "when": {"flag": "beta_opt_in", "op": "eq", "value": True}}
        r = client.post("/mgmt/envs/prod/rules", json=ok, headers=auth())
        assert r.status_code == 200 and "warning" not in r.json(), r.text
