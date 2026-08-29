"""Backup pushes to remotes (§6) and the serve-replica content follow (§13/§15)."""

from __future__ import annotations

import subprocess
import logging

import pytest
from sqlalchemy import select

from incant import db, models
from incant.config import Settings, set_settings
from incant.db import session_scope
from incant.gitstore import GitError, GitStore, redact_url
from incant.service import AppContext, reset_app

from .conftest import db_url_for, reset_schema
from .test_server import auth, make_client, make_key


@pytest.fixture()
def app(tmp_path):
    set_settings(Settings(
        database_url=db_url_for(tmp_path),
        repo_path=str(tmp_path / "repo"),
    ))
    db.reset_engine()
    reset_app()
    reset_schema()
    ctx = AppContext()
    ctx.initialize()
    with session_scope() as s:
        s.add(models.Environment(id="prod", name="prod", protected=False, track_tip=False))
    yield ctx


def _bare(path) -> str:
    subprocess.run(["git", "init", "--bare", "--initial-branch=main", str(path)],
                   capture_output=True, check=True)
    return str(path)


def _head(repo_path) -> str:
    return subprocess.run(
        ["git", "--git-dir", str(repo_path), "rev-parse", "refs/heads/main"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()


def _commit(ctx, prompt_id="support/system", version=1, content="hello {{ x }}"):
    with session_scope() as s:
        reg = ctx.registry(s, "sam")
        if not reg.prompt_exists(prompt_id):
            reg.create_prompt(prompt_id)
        d = reg.create_draft(prompt_id, version_number=version, author="sam", content=content)
        return reg.commit_draft(d.id, author="sam", message=f"v{version}").sha


def _add_remote(url, enabled=True, auth_ref=None) -> int:
    with session_scope() as s:
        r = models.Remote(url=url, enabled=enabled, auth_ref=auth_ref)
        s.add(r)
        s.flush()
        return r.id


# ── pusher (full node) ───────────────────────────────────────────────

def test_push_pending_drains_to_remote(app, tmp_path):
    sha = _commit(app)
    remote = _bare(tmp_path / "backup.git")
    _add_remote(remote)

    with session_scope() as s:
        statuses = app.backup.push_pending(s)
    assert statuses[0].error is None
    assert statuses[0].last_pushed_sha == app.git.head()
    assert statuses[0].pending_commits == 0 and statuses[0].lag_seconds == 0.0
    # The remote is a full mirror: main matches, and the commit is reachable.
    assert _head(remote) == app.git.head()
    assert sha  # the authored commit is on main, hence at the remote


def test_mirror_push_carries_draft_refs(app, tmp_path):
    _commit(app)
    with session_scope() as s:
        reg = app.registry(s, "sam")
        d = reg.create_draft("support/system", version_number=1, author="sam",
                             content="wip {{ x }}")
        draft_id = d.id
    remote = _bare(tmp_path / "backup.git")
    _add_remote(remote)
    with session_scope() as s:
        app.backup.push_pending(s)
    out = subprocess.run(["git", "--git-dir", remote, "for-each-ref",
                          "--format=%(refname)", "refs/incant/drafts/"],
                         capture_output=True, text=True, check=True).stdout
    assert f"refs/incant/drafts/{draft_id}" in out


def test_disabled_remote_is_skipped(app, tmp_path):
    _commit(app)
    remote = _bare(tmp_path / "backup.git")
    _add_remote(remote, enabled=False)
    with session_scope() as s:
        statuses = app.backup.push_pending(s)
    assert statuses[0].last_pushed_sha is None       # never pushed
    assert statuses[0].pending_commits >= 1          # queue visible, not drained


def test_failed_push_keeps_queue_and_reports(app, tmp_path):
    _commit(app)
    _add_remote(str(tmp_path / "does-not-exist.git"))
    with session_scope() as s:
        statuses = app.backup.push_pending(s)
    st = statuses[0]
    assert st.error is not None
    assert st.last_pushed_sha is None
    assert st.pending_commits >= 1
    assert st.lag_seconds >= 0.0                     # exposure window is open


def test_second_pass_is_incremental(app, tmp_path):
    _commit(app, version=1)
    remote = _bare(tmp_path / "backup.git")
    _add_remote(remote)
    with session_scope() as s:
        app.backup.push_pending(s)
    first = _head(remote)
    _commit(app, version=2, content="v2 {{ x }}")
    with session_scope() as s:
        statuses = app.backup.push_pending(s)
    assert _head(remote) == app.git.head() != first
    assert statuses[0].pending_commits == 0


# ── replica follow (serve mode) ──────────────────────────────────────

def test_replica_hydrates_and_follows(app, tmp_path):
    _commit(app, version=1)
    remote = _bare(tmp_path / "backup.git")
    _add_remote(remote)
    with session_scope() as s:
        app.backup.push_pending(s)

    # A replica against an empty volume hydrates by mirror-cloning the remote…
    replica_git = GitStore(tmp_path / "replica-repo")
    replica = AppContext()
    replica.git = replica_git
    replica.backup.git = replica_git
    with session_scope() as s:
        assert replica.backup.hydrate(s) is True
    assert replica_git.head() == app.git.head()

    # …and a later commit reaches it on the next fetch pass.
    _commit(app, version=2, content="v2 {{ x }}")
    with session_scope() as s:
        app.backup.push_pending(s)
    with session_scope() as s:
        assert replica.backup.fetch_once(s) is True
    assert replica_git.head() == app.git.head()


def test_hydrate_fails_cleanly_with_no_reachable_remote(app, tmp_path):
    _add_remote(str(tmp_path / "gone.git"))
    replica_git = GitStore(tmp_path / "replica-repo")
    replica = AppContext()
    replica.git = replica_git
    replica.backup.git = replica_git
    with session_scope() as s:
        assert replica.backup.hydrate(s) is False
    assert not replica_git.exists()


# ── URL redaction ────────────────────────────────────────────────────

def test_redact_url_masks_embedded_credentials():
    assert redact_url("https://alice:tok3n@git.example.com/x.git") == \
        "https://alice:***@git.example.com/x.git"
    assert redact_url("git@github.com:org/repo.git") == "git@github.com:org/repo.git"
    assert redact_url("/var/backups/repo.git") == "/var/backups/repo.git"


# ── mgmt endpoints ───────────────────────────────────────────────────

def test_remotes_crud_push_and_rbac(tmp_path):
    with make_client(tmp_path) as client:
        remote = _bare(tmp_path / "backup.git")

        # Admin registers a remote; the kick endpoint pushes it synchronously.
        r = client.post("/mgmt/remotes", json={"url": remote}, headers=auth())
        assert r.status_code == 200, r.text
        assert "warning" not in r.json()  # plain path: nothing to warn about
        rid = r.json()["id"]
        r = client.post(f"/mgmt/remotes/{rid}/push", headers=auth())
        assert r.status_code == 200 and r.json()["error"] is None, r.text
        assert r.json()["last_pushed_sha"] == _head(tmp_path / "repo")

        # The list reports a drained queue.
        listing = client.get("/mgmt/remotes", headers=auth()).json()["remotes"]
        assert listing[0]["pending_commits"] == 0

        # Duplicate URL refused; disable via PATCH; delete.
        assert client.post("/mgmt/remotes", json={"url": remote},
                           headers=auth()).status_code == 409
        r = client.patch(f"/mgmt/remotes/{rid}", json={"enabled": False}, headers=auth())
        assert r.status_code == 200 and r.json()["enabled"] is False
        assert client.delete(f"/mgmt/remotes/{rid}", headers=auth()).status_code == 200
        assert client.get("/mgmt/remotes", headers=auth()).json()["remotes"] == []

        # Remotes are admin-only: an operator can't even list them.
        op = make_key(client, "operator", env="prod")
        assert client.get("/mgmt/remotes", headers=auth(op)).status_code == 403


def test_remote_list_redacts_credentials(tmp_path):
    with make_client(tmp_path) as client:
        r = client.post("/mgmt/remotes",
                        json={"url": "https://bob:s3cret@git.example.com/backup.git"},
                        headers=auth())
        assert r.status_code == 200 and "s3cret" not in r.text
        # Embedded credentials are allowed but discouraged: the response says so.
        assert "auth_ref" in r.json().get("warning", "")
        listing = client.get("/mgmt/remotes", headers=auth())
        assert "s3cret" not in listing.text
        assert "***" in listing.json()["remotes"][0]["url"]


def test_failed_remote_never_leaks_credentials_to_logs_response_or_audit(
    tmp_path, monkeypatch, caplog,
):
    secret = "super-secret-token"
    raw_url = f"https://backup:{secret}@git.example.com/missing.git"

    def fail_with_echo(self, url, **kwargs):
        # Simulate git echoing the exact credential-bearing URL in stderr.
        raise GitError(f"fatal: unable to access {url}: authentication failed for {secret}")

    monkeypatch.setattr(GitStore, "push_mirror", fail_with_echo)
    caplog.set_level(logging.WARNING, logger="incant.backup")

    with make_client(tmp_path) as client:
        remote_id = client.post(
            "/mgmt/remotes", json={"url": raw_url}, headers=auth()
        ).json()["id"]
        response = client.post(f"/mgmt/remotes/{remote_id}/push", headers=auth())
        assert response.status_code == 200
        assert response.json()["error"]
        assert secret not in response.text
        assert secret not in caplog.text

        with session_scope() as s:
            audit = s.execute(select(models.AuditLog).where(
                models.AuditLog.action == "remote.push"
            ).order_by(models.AuditLog.id.desc())).scalars().first()
            assert audit is not None
            assert secret not in str(audit.after)


def test_changing_remote_url_resets_push_history(tmp_path):
    with make_client(tmp_path) as client:
        remote = _bare(tmp_path / "backup.git")
        rid = client.post("/mgmt/remotes", json={"url": remote}, headers=auth()).json()["id"]
        client.post(f"/mgmt/remotes/{rid}/push", headers=auth())
        other = _bare(tmp_path / "other.git")
        r = client.patch(f"/mgmt/remotes/{rid}", json={"url": other}, headers=auth())
        assert r.status_code == 200
        listing = client.get("/mgmt/remotes", headers=auth()).json()["remotes"]
        # A different repository has its own empty push history — the queue reopens.
        assert listing[0]["last_pushed_sha"] is None
        assert listing[0]["pending_commits"] >= 1


def test_remote_auth_by_scheme(tmp_path):
    # auth_ref is a PATH, interpreted by URL scheme: ssh → deploy key via
    # GIT_SSH_COMMAND; https → git credential-store file via credential.helper —
    # the secret itself never enters the DB or a process argument.
    from incant.gitstore.store import _remote_auth

    argv, env = _remote_auth("git@github.com:org/x.git", "/secrets/key", "/etc/kh")
    assert argv == []
    assert env["GIT_SSH_COMMAND"] == \
        "ssh -i /secrets/key -o IdentitiesOnly=yes -o UserKnownHostsFile=/etc/kh"

    argv, env = _remote_auth("https://github.com/org/x.git", "/secrets/creds", None)
    assert env == {}
    assert argv == ["-c", "credential.helper=store --file=/secrets/creds"]

    argv, env = _remote_auth("https://github.com/org/x.git", None, None)
    assert (argv, env) == ([], {})
    argv, env = _remote_auth(str(tmp_path / "bare.git"), None, None)
    assert (argv, env) == ([], {})
