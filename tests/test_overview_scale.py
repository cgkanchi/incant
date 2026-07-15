"""Scale + equivalence tests for GET /mgmt/overview.

The landing screen used to fan out — per prompt in the library — into a validation
SELECT, a pointer SELECT, a draft-count SELECT, and a `git log` subprocess, so it
degraded linearly (queries) and painfully (subprocesses) with library size. The fix
bulk-loads each of those facts once. These tests pin two things:

  * correctness — the bulk-loaded payload is byte-for-byte what the old per-prompt
    logic produced (asserted against concrete seed values *and* recomputed live with the
    surviving per-call helpers);
  * scale — the whole overview is a small constant number of queries and a single git
    subprocess regardless of how many prompts exist.

Plus a unit test for the new GitStore.latest_commits bulk walk.
"""

from __future__ import annotations

import subprocess
import tempfile

import pytest
from sqlalchemy import event, func, select

from incant import db, models
from incant.db import session_scope
from incant.gitstore.store import GitStore
from incant.server.mgmt.helpers import _current_live, _tip_ahead
from incant.service import get_app

from .test_server import auth, make_client


@pytest.fixture()
def client(tmp_path):
    with make_client(tmp_path) as c:
        yield c


# ── correctness: concrete seed values ────────────────────────────────

def test_overview_payload_matches_seed(client):
    """Pin the payload fields against the known seed dataset (which is exactly the
    described fixture: prompts across two projects, one with multiple versions,
    publishes and an open draft)."""
    ov = client.get("/mgmt/overview?environment=prod", headers=auth()).json()
    rows = {p["prompt_id"]: p for proj in ov["projects"] for p in proj["prompts"]}

    sysrow = rows["support/system"]
    assert sysrow["live_version"] == 2
    assert sysrow["versions"] == 3
    assert sysrow["live"] is True
    assert sysrow["live_by"] == "Dana"            # v2 baseline pointer moved by Dana
    assert sysrow["live_at"]                       # ISO timestamp present
    assert sysrow["tip_ahead"] == 2                # two tweak commits past the live baseline
    assert sysrow["newest_version"] == 3
    assert sysrow["newest_version_live"] is True   # v3 has a prod live pointer
    assert sysrow["open_drafts"] == 1              # one seeded open draft
    # `updated` is derived from git (the newest commit on v2.j2 = Sam's warm-tone tweak),
    # NOT from the pointer mover — so it must report Sam even though Dana released it.
    assert sysrow["updated"]["who"] == "Sam"
    assert sysrow["updated"]["when"]

    greet = rows["support/greeting"]
    assert greet["live_version"] == 1
    assert greet["versions"] == 2
    assert greet["live_by"] == "Maya"
    assert greet["newest_version"] == 2
    assert greet["newest_version_live"] is False   # v2 committed but never published
    assert greet["updated"]["who"] == "Maya"

    lang = rows["shared/style/language-rules"]
    assert lang["live_version"] == 1
    assert lang["live_by"] == "Rae"
    assert lang["newest_version"] == 2
    assert lang["newest_version_live"] is False

    triage = rows["support/escalation/triage"]
    assert triage["live_version"] == 1
    assert triage["newest_version_live"] is True
    assert triage["updated"]["who"] == "Dana"


def test_overview_equivalent_to_per_call_helpers(client):
    """The strongest equivalence pin: recompute every field the overview reports with the
    ORIGINAL per-prompt helpers (`_current_live`, `_tip_ahead`, `app.git.history`, a
    per-prompt draft COUNT) and assert the bulk-loaded payload is identical for every
    prompt in the library. Robust to seed changes — it compares bulk vs. per-call, not
    against hard-coded values."""
    ov = client.get("/mgmt/overview?environment=prod", headers=auth()).json()
    app = get_app()
    with session_scope() as s:
        for proj in ov["projects"]:
            for row in proj["prompts"]:
                pid = row["prompt_id"]
                dv = row["live_version"]           # == snap.defaults.get(pid)

                open_n = s.execute(
                    select(func.count()).select_from(models.Draft).where(
                        models.Draft.prompt_id == pid,
                        models.Draft.status.in_(["open", "approved"]),
                    )
                ).scalar_one()
                assert row["open_drafts"] == open_n, pid

                if dv is None:
                    assert row["live_by"] is None
                    assert row["live_at"] is None
                    assert row["tip_ahead"] == 0
                    assert row["updated"] is None
                    continue

                live = _current_live(s, "prod", pid, dv)
                assert row["live_by"] == ((live.moved_by or None) if live else None), pid
                assert row["live_at"] == (live.moved_at.isoformat() if live else None), pid

                expected_tip = _tip_ahead(s, "prod", pid, dv, live.to_sha) if live else 0
                assert row["tip_ahead"] == expected_tip, pid

                hist = app.git.history(f"{pid}/v{dv}.j2")
                top = hist[0] if hist else None
                expected_updated = {"when": top.date, "who": top.author} if top else None
                assert row["updated"] == expected_updated, pid


# ── scale: constant queries + one subprocess regardless of size ──────

def test_overview_query_and_subprocess_counts_are_constant(client):
    """~40 prompts with committed content and live pointers, yet the overview is a small
    constant number of SQL statements (not ~4*N) and a single git subprocess (not one
    `git log` per prompt)."""
    for i in range(40):
        pid = f"scale/p{i:02d}"
        client.post("/mgmt/prompts", json={"prompt_id": pid}, headers=auth())
        d = client.post(f"/mgmt/prompts/{pid}/drafts",
                        json={"version_number": 1, "content": f"Hi {{{{ name }}}} #{i}"},
                        headers=auth()).json()
        sha = client.post(f"/mgmt/drafts/{d['id']}/commit", json={}, headers=auth()).json()["full_sha"]
        # staging is unprotected -> no type-to-confirm needed to default/publish.
        client.post("/mgmt/envs/staging/defaults",
                    json={"prompt_id": pid, "version_number": 1}, headers=auth())
        client.post("/mgmt/envs/staging/pointers",
                    json={"prompt_id": pid, "version_number": 1, "to_sha": sha}, headers=auth())

    # Warm the in-memory auth cache first, so the *measured* request doesn't fold in a
    # one-off principals/keys refresh (which is unrelated to library size).
    assert client.get("/mgmt/overview?environment=staging", headers=auth()).status_code == 200

    eng = db.engine()
    statements: list[str] = []

    def _count(conn, cur, statement, params, context, executemany):
        statements.append(statement)

    event.listen(eng, "before_cursor_execute", _count)

    git = get_app().git
    orig_git = git._git
    git_spawns: list = []

    def _wrapped(*args, **kwargs):
        git_spawns.append(args)
        return orig_git(*args, **kwargs)

    git._git = _wrapped
    try:
        r = client.get("/mgmt/overview?environment=staging", headers=auth())
    finally:
        event.remove(eng, "before_cursor_execute", _count)
        git._git = orig_git

    assert r.status_code == 200, r.text
    # 45 prompts (5 seeded + 40) — the payload really did cover them all.
    total = sum(len(p["prompts"]) for p in r.json()["projects"])
    assert total >= 44, total
    # A per-prompt implementation would run ~4*45 + baseline > 180 statements; the bulk
    # loader keeps it a small constant. Assert well under any proportional growth.
    assert len(statements) < 15, f"{len(statements)} SQL statements (should be constant):\n" + "\n".join(statements)
    # One `git log` walk for the whole library (was one per prompt).
    assert len(git_spawns) <= 3, git_spawns


# ── unit: GitStore.latest_commits ────────────────────────────────────

def test_latest_commits_newest_wins(tmp_path):
    """Several files across several commits, one file updated twice: latest_commits maps
    each path to the NEWEST commit that touched it, matching history()[0]."""
    g = GitStore(tmp_path / "repo")
    g.init()
    g.commit_version("p/a", 1, "a-first", author_name="Alice", author_email="a@x", message="add a")
    g.commit_version("p/b", 1, "b-only", author_name="Bob", author_email="b@x", message="add b")
    g.commit_version("p/a", 1, "a-second", author_name="Carol", author_email="c@x", message="update a once")
    g.commit_version("p/a", 1, "a-third", author_name="Dave", author_email="d@x", message="update a twice")

    latest = g.latest_commits()
    assert set(latest) == {"p/a/v1.j2", "p/b/v1.j2"}
    # p/a was updated twice -> the newest commit (Dave's) wins over the two older ones.
    assert latest["p/a/v1.j2"].author == "Dave"
    assert latest["p/a/v1.j2"].subject == "update a twice"
    assert latest["p/b/v1.j2"].author == "Bob"
    # Parsing is correct end-to-end: the bulk walk agrees with the per-path history query.
    assert latest["p/a/v1.j2"].sha == g.history("p/a/v1.j2")[0].sha
    assert latest["p/a/v1.j2"].date == g.history("p/a/v1.j2")[0].date
    assert latest["p/b/v1.j2"].sha == g.history("p/b/v1.j2")[0].sha
    # The suffix filter excludes non-matching paths.
    assert g.latest_commits(suffix=".txt") == {}


def _rewrite_tree(g: GitStore, parent: str, *, add=None, remove=None, author="X", msg="x") -> str:
    """Build one commit onto `parent` that adds/removes paths, via a temp index (bare-repo
    safe). `add` = {path: blob_sha}, `remove` = [path]. Returns the new commit sha and
    advances main."""
    idx = tempfile.mktemp()
    env = {"GIT_INDEX_FILE": idx}
    import os
    env = {**os.environ, **env}
    subprocess.run(["git", "--git-dir", str(g.repo), "read-tree", parent],
                   env=env, check=True, capture_output=True)
    for path, blob in (add or {}).items():
        subprocess.run(["git", "--git-dir", str(g.repo), "update-index", "--add",
                        "--cacheinfo", f"100644,{blob},{path}"], env=env, check=True, capture_output=True)
    for path in (remove or []):
        subprocess.run(["git", "--git-dir", str(g.repo), "update-index", "--index-info"],
                       env=env, input=f"0 {'0'*40}\t{path}\n", text=True, check=True, capture_output=True)
    tree = subprocess.run(["git", "--git-dir", str(g.repo), "write-tree"],
                          env=env, check=True, capture_output=True, text=True).stdout.strip()
    commit = g._git("commit-tree", tree, "-p", parent, "-m", msg,
                    env=g._author_env(author, f"{author}@x")).strip()
    g._git("update-ref", "refs/heads/main", commit)
    return commit


def test_latest_commits_handles_rename_and_delete(tmp_path):
    """A rename must attribute the NEW path (the content lives there now); a delete must
    drop the path entirely (no current content)."""
    g = GitStore(tmp_path / "repo")
    g.init()
    g.commit_version("p/a", 1, "content of a long enough for rename detection", author_name="Alice", author_email="a@x", message="add a")
    g.commit_version("p/c", 1, "content c", author_name="Cara", author_email="c@x", message="add c")

    blob = g._git("rev-parse", "main:p/a/v1.j2").strip()
    _rewrite_tree(g, g.head(), add={"p/renamed/v1.j2": blob}, remove=["p/a/v1.j2"],
                  author="Dana", msg="rename a")
    _rewrite_tree(g, g.head(), remove=["p/c/v1.j2"], author="Frank", msg="delete c")

    latest = g.latest_commits()
    assert set(latest) == {"p/renamed/v1.j2"}          # only the surviving, renamed file
    assert latest["p/renamed/v1.j2"].author == "Dana"  # the rename commit, new path attributed
    assert "p/a/v1.j2" not in latest                    # old name is gone
    assert "p/c/v1.j2" not in latest                    # deleted -> not reported
    assert g.list_files() == ["p/renamed/v1.j2"]
