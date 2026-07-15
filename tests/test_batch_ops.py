"""Atomic batch mgmt endpoints: POST /mgmt/envs/{env}/rules/batch and
POST /mgmt/envs/{env}/publish.

Both replace UI flows that used to fire several separately-committed HTTP requests for
what the user saw as one action — a composer priority-shift plan, a reorder swap, and a
pointer-move-then-archive-loop — where a mid-sequence failure left half-applied state
(colliding priorities; a moved pointer whose test-rule archives never ran). Each endpoint
now does all its work inside ONE request/transaction, so any failure rolls the whole thing
back and nothing persists (DESIGN.md §7). The boot/auth/idiom helpers are reused straight
from tests/test_server.py.
"""

from __future__ import annotations

import pytest

from .test_server import _tip_sha, auth, make_client, make_key


@pytest.fixture()
def client(tmp_path):
    with make_client(tmp_path) as c:
        yield c


def _rules(client, env):
    return {r["id"]: r for r in
            client.get(f"/mgmt/envs/{env}/rules", headers=auth()).json()["rules"]}


def _live_sha(client, env="prod", prompt_id="support/system", version=2):
    tl = client.get(f"/mgmt/envs/{env}/pointers?prompt_id={prompt_id}&version={version}",
                    headers=auth()).json()
    return tl["moves"][0]["full_sha"] if tl["moves"] else None


# ── POST /mgmt/envs/{env}/rules/batch ────────────────────────────────

def test_rules_batch_happy_path(client):
    # Two rules land in one request, each at exactly the priority it was sent with.
    rules = [
        {"id": "b-r1", "scope": "prompt", "prompt_id": "support/system",
         "priority": 15, "serve": {"version": 2}, "comment": "batch rule one"},
        {"id": "b-r2", "scope": "prompt", "prompt_id": "support/system",
         "priority": 25, "serve": {"version": 2}, "comment": "batch rule two"},
    ]
    r = client.post("/mgmt/envs/staging/rules/batch", json={"rules": rules}, headers=auth())
    assert r.status_code == 200, r.text
    assert r.json()["count"] == 2
    got = _rules(client, "staging")
    assert got["b-r1"]["priority"] == 15 and got["b-r1"]["comment"] == "batch rule one"
    assert got["b-r2"]["priority"] == 25 and got["b-r2"]["comment"] == "batch rule two"


def test_rules_batch_atomic_rollback(client):
    # First rule is valid; the second serves a version that doesn't exist -> TargetingError
    # -> 400. Because it's all one transaction, the valid first rule must NOT persist.
    rules = [
        {"id": "atom-ok", "scope": "prompt", "prompt_id": "support/system",
         "priority": 15, "serve": {"version": 2}, "comment": "should roll back"},
        {"id": "atom-bad", "scope": "prompt", "prompt_id": "support/system",
         "priority": 25, "serve": {"version": 999}, "comment": "no such version"},
    ]
    r = client.post("/mgmt/envs/staging/rules/batch", json={"rules": rules}, headers=auth())
    assert r.status_code == 400, r.text
    assert "atom-ok" not in _rules(client, "staging")  # rolled back with the failed batch


def test_rules_batch_rbac_scopes(client):
    op = make_key(client, "operator", project="support")
    # A project operator may batch-upsert rules for its OWN project's prompt.
    own = [{"id": "own-1", "scope": "prompt", "prompt_id": "support/system",
            "priority": 12, "serve": {"version": 2}, "comment": "own project"}]
    assert client.post("/mgmt/envs/staging/rules/batch", json={"rules": own},
                       headers=auth(op)).status_code == 200
    # A prompt-scoped rule for ANOTHER project ANYWHERE in the batch -> 403; and because
    # authz is checked before any upsert, the sibling own-project rule persists nothing.
    mixed = [
        {"id": "own-2", "scope": "prompt", "prompt_id": "support/system",
         "priority": 13, "serve": {"version": 2}, "comment": "own project"},
        {"id": "other-1", "scope": "prompt", "prompt_id": "shared/style/language-rules",
         "priority": 14, "serve": {"version": 1}, "comment": "another project"},
    ]
    r = client.post("/mgmt/envs/staging/rules/batch", json={"rules": mixed}, headers=auth(op))
    assert r.status_code == 403, r.text
    got = _rules(client, "staging")
    assert "own-2" not in got and "other-1" not in got


def test_rules_batch_locked_env_needs_no_confirm(client):
    # DESIGN.md §7: rule edits are low-friction (operator, no approval ceremony) — unlike
    # pointer-class changes they carry NO type-to-confirm even on a locked env. rules/batch
    # matches the single upsert endpoint exactly: prod is protected, yet a batch lands with
    # no confirm token. (This is precisely what keeps composer-save / reorder working on
    # prod; requiring confirm here would break the reorder arrows and plan-carrying saves.)
    rules = [{"id": "locked-r1", "scope": "prompt", "prompt_id": "support/system",
              "priority": 33, "serve": {"version": 2}, "comment": "no confirm needed"}]
    r = client.post("/mgmt/envs/prod/rules/batch", json={"rules": rules}, headers=auth())
    assert r.status_code == 200, r.text
    assert "locked-r1" in _rules(client, "prod")


# ── POST /mgmt/envs/{env}/publish ────────────────────────────────────

def test_publish_moves_pointer_and_archives(client):
    # The tip sha is a commit validated for exactly (support/system, 2) — tuple-correct for
    # make_live. team-x-tip is the seeded prompt-scoped @tip rule that becomes redundant.
    sha = _tip_sha(client)
    assert _live_sha(client) != sha  # pointer currently at the v2 baseline, not the tip
    r = client.post("/mgmt/envs/prod/publish",
                    json={"prompt_id": "support/system", "version_number": 2, "to_sha": sha,
                          "confirm": "support/system", "archive_rule_ids": ["team-x-tip"]},
                    headers=auth())
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "live" and body["archived"] == 1
    assert _live_sha(client) == sha                       # pointer advanced to the tip
    assert _rules(client, "prod")["team-x-tip"]["status"] == "archived"


def test_publish_atomic_bad_rule_id_rolls_back_pointer(client):
    # A nonexistent archive id 404s AFTER the pointer move has been staged; the whole
    # transaction rolls back, so the pointer did NOT move.
    sha = _tip_sha(client)
    before = _live_sha(client)
    assert before is not None and before != sha
    r = client.post("/mgmt/envs/prod/publish",
                    json={"prompt_id": "support/system", "version_number": 2, "to_sha": sha,
                          "confirm": "support/system", "archive_rule_ids": ["ghost-rule"]},
                    headers=auth())
    assert r.status_code == 404, r.text
    assert _live_sha(client) == before                    # pointer move rolled back
    assert _rules(client, "prod")["team-x-tip"]["status"] == "active"  # nothing archived


def test_publish_requires_releaser(client):
    # An operator edits rules but can't release — the pointer move is releaser-gated, so
    # the whole publish is refused and nothing changes.
    sha = _tip_sha(client)
    before = _live_sha(client)
    op = make_key(client, "operator", project="support", env="prod")
    r = client.post("/mgmt/envs/prod/publish",
                    json={"prompt_id": "support/system", "version_number": 2, "to_sha": sha,
                          "confirm": "support/system", "archive_rule_ids": ["team-x-tip"]},
                    headers=auth(op))
    assert r.status_code == 403, r.text
    assert _live_sha(client) == before
    assert _rules(client, "prod")["team-x-tip"]["status"] == "active"


def test_publish_locked_env_requires_confirm(client):
    # prod is protected: the pointer half of the act carries type-to-confirm, identical to
    # the single /pointers endpoint. No token -> 409 and NOTHING happens (no move, no
    # archive); the correct token (the prompt id) -> applied.
    sha = _tip_sha(client)
    base = {"prompt_id": "support/system", "version_number": 2, "to_sha": sha,
            "archive_rule_ids": ["team-x-tip"]}
    before = _live_sha(client)
    r = client.post("/mgmt/envs/prod/publish", json=base, headers=auth())
    assert r.status_code == 409 and r.json()["detail"]["error"] == "confirmation_required"
    assert r.json()["detail"]["expected"] == "support/system"
    assert _live_sha(client) == before                    # pointer untouched
    assert _rules(client, "prod")["team-x-tip"]["status"] == "active"  # archive never ran
    # Wrong token is still refused.
    assert client.post("/mgmt/envs/prod/publish", json={**base, "confirm": "prod"},
                       headers=auth()).status_code == 409
    # Correct token -> pointer moves and the rule is archived.
    r = client.post("/mgmt/envs/prod/publish", json={**base, "confirm": "support/system"},
                    headers=auth())
    assert r.status_code == 200 and r.json()["archived"] == 1, r.text
    assert _live_sha(client) == sha
    assert _rules(client, "prod")["team-x-tip"]["status"] == "archived"
