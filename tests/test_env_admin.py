"""Environment lifecycle admin: create (hardened), patch, delete, rename, and the
`default` marker on GET /mgmt/envs. Reuses the client/auth idioms from test_server."""

from __future__ import annotations

from sqlalchemy import func, select

from incant import models
from incant.db import session_scope

from .test_server import ADMIN, auth, client, make_key  # noqa: F401 (client is a fixture)


# The seven tables scoped to one environment by ``environment_id`` — each seeded/asserted.
_ENV_MODELS = (
    models.Rule, models.Segment, models.EnvDefault, models.KillSwitch,
    models.PointerMove, models.RuleRevision, models.RoleBinding,
)


def _seed_env_rows(env: str) -> None:
    """Seed one row of each non-binding env-scoped kind. (The RoleBinding is seeded
    separately, via a real key issuance, so it has a valid principal FK.)"""
    with session_scope() as s:
        s.add(models.Rule(id=f"rule-{env}", environment_id=env, scope="global",
                          serve={"version": 1}))
        s.add(models.Segment(environment_id=env, name=f"seg-{env}", clauses={"all": []}))
        s.add(models.EnvDefault(environment_id=env, prompt_id="support/system",
                                version_number=2))
        s.add(models.KillSwitch(environment_id=env, prompt_id="support/system", engaged=True))
        s.add(models.PointerMove(environment_id=env, prompt_id="support/system",
                                 version_number=2, to_sha="deadbeefdeadbeef"))
        s.add(models.RuleRevision(environment_id=env, kind="rule", rules_version=1,
                                  snapshot={}))


def _env_row_counts(env: str) -> dict:
    with session_scope() as s:
        return {
            m.__tablename__: s.execute(
                select(func.count()).select_from(m).where(m.environment_id == env)
            ).scalar()
            for m in _ENV_MODELS
        }


def _env_ids(client) -> set:
    return {e["id"] for e in client.get("/mgmt/envs", headers=auth()).json()["environments"]}


# ── create (hardened) ────────────────────────────────────────────────

def test_create_env_slug_validation_400(client):
    for bad in ["Prod", "has space", "a/b", "UPPER", "-lead", "trail-", "x" * 33, ""]:
        r = client.post("/mgmt/envs", json={"id": bad}, headers=auth())
        assert r.status_code == 400, (bad, r.status_code)
    # A valid slug still succeeds.
    assert client.post("/mgmt/envs", json={"id": "qa_1-b"}, headers=auth()).status_code == 200


def test_create_env_duplicate_409(client):
    assert client.post("/mgmt/envs", json={"id": "dev"}, headers=auth()).status_code == 200
    r = client.post("/mgmt/envs", json={"id": "dev"}, headers=auth())
    assert r.status_code == 409
    # Duplicating a seeded env is also 409 (no silent idempotent no-op).
    assert client.post("/mgmt/envs", json={"id": "prod"}, headers=auth()).status_code == 409


def test_create_env_non_admin_403(client):
    viewer = make_key(client, "viewer", project="support")
    assert client.post("/mgmt/envs", json={"id": "nope"}, headers=auth(viewer)).status_code == 403


def test_create_env_writes_audit(client):
    assert client.post("/mgmt/envs", json={"id": "fresh", "protected": True},
                       headers=auth()).status_code == 200
    rows = client.get("/mgmt/audit?action=env.create", headers=auth()).json()["audit"]
    row = next(a for a in rows if a["object_id"] == "fresh")
    assert row["after"]["protected"] is True and row["after"]["track_tip"] is False


# ── patch ─────────────────────────────────────────────────────────────

def test_patch_env_protected_roundtrip(client):
    client.post("/mgmt/envs", json={"id": "flip"}, headers=auth())
    r = client.patch("/mgmt/envs/flip", json={"protected": True}, headers=auth())
    assert r.status_code == 200 and r.json()["protected"] is True
    r = client.patch("/mgmt/envs/flip", json={"protected": False}, headers=auth())
    assert r.status_code == 200 and r.json()["protected"] is False
    # The toggle is audited as env.update.
    rows = client.get("/mgmt/audit?action=env.update", headers=auth()).json()["audit"]
    assert any(a["object_id"] == "flip" for a in rows)


# ── delete ────────────────────────────────────────────────────────────

def test_delete_env_requires_confirm(client):
    client.post("/mgmt/envs", json={"id": "tmp"}, headers=auth())
    # No confirm → 409 confirmation_required (same shape as the lock ceremony).
    r = client.delete("/mgmt/envs/tmp", headers=auth())
    assert r.status_code == 409
    assert r.json()["detail"]["error"] == "confirmation_required"
    assert r.json()["detail"]["expected"] == "tmp"
    # Wrong token → still refused.
    assert client.delete("/mgmt/envs/tmp?confirm=wrong", headers=auth()).status_code == 409
    # Correct token → deleted.
    assert client.delete("/mgmt/envs/tmp?confirm=tmp", headers=auth()).status_code == 200
    assert "tmp" not in _env_ids(client)


def test_delete_default_env_refused(client):
    # prod is the configured default environment.
    r = client.delete("/mgmt/envs/prod?confirm=prod", headers=auth())
    assert r.status_code == 409
    assert "prod" in _env_ids(client)


def test_delete_protected_env_refused_until_unprotected(client):
    client.post("/mgmt/envs", json={"id": "locked", "protected": True}, headers=auth())
    # Protected → refused even with the right confirm token.
    assert client.delete("/mgmt/envs/locked?confirm=locked", headers=auth()).status_code == 409
    # Unprotect, then delete succeeds.
    client.patch("/mgmt/envs/locked", json={"protected": False}, headers=auth())
    assert client.delete("/mgmt/envs/locked?confirm=locked", headers=auth()).status_code == 200
    assert "locked" not in _env_ids(client)


def test_delete_env_non_admin_403(client):
    client.post("/mgmt/envs", json={"id": "tmp2"}, headers=auth())
    viewer = make_key(client, "viewer", project="support")
    assert client.delete("/mgmt/envs/tmp2?confirm=tmp2", headers=auth(viewer)).status_code == 403


def test_delete_env_removes_all_scoped_rows_and_spares_others(client):
    client.post("/mgmt/envs", json={"id": "doomed"}, headers=auth())
    client.post("/mgmt/envs", json={"id": "kept"}, headers=auth())
    # Seed six kinds directly + the seventh (RoleBinding) via a real env-scoped key.
    _seed_env_rows("doomed")
    _seed_env_rows("kept")
    make_key(client, "viewer", env="doomed", name="viewer-doomed")
    make_key(client, "viewer", env="kept", name="viewer-kept")
    assert all(c == 1 for c in _env_row_counts("doomed").values()), _env_row_counts("doomed")

    r = client.delete("/mgmt/envs/doomed?confirm=doomed", headers=auth())
    assert r.status_code == 200, r.text
    assert r.json()["deleted"]["pointer_moves"] == 1
    # Every one of the seven kinds is gone for the deleted env…
    gone = _env_row_counts("doomed")
    assert all(c == 0 for c in gone.values()), gone
    assert "doomed" not in _env_ids(client)
    # …and the unrelated env's rows survive untouched.
    assert all(c == 1 for c in _env_row_counts("kept").values()), _env_row_counts("kept")
    # Audit row written.
    rows = client.get("/mgmt/audit?action=env.delete", headers=auth()).json()["audit"]
    assert any(a["object_id"] == "doomed" for a in rows)


# ── rename ────────────────────────────────────────────────────────────

def test_rename_default_env_refused(client):
    r = client.post("/mgmt/envs/prod/rename", json={"new_id": "production"}, headers=auth())
    assert r.status_code == 409
    assert "prod" in _env_ids(client)


def test_rename_duplicate_target_409(client):
    client.post("/mgmt/envs", json={"id": "src"}, headers=auth())
    # staging exists from the seed.
    r = client.post("/mgmt/envs/src/rename", json={"new_id": "staging"}, headers=auth())
    assert r.status_code == 409


def test_rename_bad_slug_400(client):
    client.post("/mgmt/envs", json={"id": "src2"}, headers=auth())
    r = client.post("/mgmt/envs/src2/rename", json={"new_id": "Bad Name"}, headers=auth())
    assert r.status_code == 400


def test_rename_protected_requires_confirm(client):
    client.post("/mgmt/envs", json={"id": "plock", "protected": True}, headers=auth())
    # No confirm on a locked env → confirmation_required, echoing the CURRENT id.
    r = client.post("/mgmt/envs/plock/rename", json={"new_id": "plock2"}, headers=auth())
    assert r.status_code == 409 and r.json()["detail"]["error"] == "confirmation_required"
    assert r.json()["detail"]["expected"] == "plock"
    # Correct confirm (current id) → success, protection preserved.
    r = client.post("/mgmt/envs/plock/rename",
                    json={"new_id": "plock2", "confirm": "plock"}, headers=auth())
    assert r.status_code == 200 and r.json()["id"] == "plock2" and r.json()["protected"] is True


def test_rename_moves_all_rows_preserves_version_and_authz(client):
    client.post("/mgmt/envs", json={"id": "old"}, headers=auth())
    _seed_env_rows("old")
    old_key = make_key(client, "viewer", env="old", name="viewer-old")
    # A distinctive rules_version to check it survives the rename.
    with session_scope() as s:
        s.get(models.Environment, "old").rules_version = 7

    r = client.post("/mgmt/envs/old/rename", json={"new_id": "renamed"}, headers=auth())
    assert r.status_code == 200, r.text
    assert r.json()["rules_version"] == 7

    ids = _env_ids(client)
    assert "old" not in ids and "renamed" in ids
    # All seven kinds moved to the new id; none linger under the old id.
    assert all(c == 0 for c in _env_row_counts("old").values()), _env_row_counts("old")
    assert all(c == 1 for c in _env_row_counts("renamed").values()), _env_row_counts("renamed")

    # The old id 404s on GET rules; the new id serves them (rules_version preserved).
    assert client.get("/mgmt/envs/old/rules", headers=auth()).status_code == 404
    got = client.get("/mgmt/envs/renamed/rules", headers=auth())
    assert got.status_code == 200, got.text
    assert got.json()["rules_version"] == 7
    assert any(rule["id"] == "rule-old" for rule in got.json()["rules"])

    # The env-scoped role binding now authorizes against the NEW env id, not the old one.
    who = client.get("/mgmt/whoami", headers=auth(old_key)).json()
    assert {"role": "viewer", "project_id": None, "environment_id": "renamed"} in who["roles"]
    # ...and that identity really can read the new env's rules but not a different env's.
    assert client.get("/mgmt/envs/renamed/rules", headers=auth(old_key)).status_code == 200
    assert client.get("/mgmt/envs/staging/rules", headers=auth(old_key)).status_code == 403


# ── GET /mgmt/envs default marker ─────────────────────────────────────

def test_list_envs_marks_exactly_one_default(client):
    envs = client.get("/mgmt/envs", headers=auth()).json()["environments"]
    defaults = [e for e in envs if e.get("default")]
    assert len(defaults) == 1
    assert defaults[0]["id"] == "prod"
    # staging is present and not the default.
    assert any(e["id"] == "staging" and e["default"] is False for e in envs)
