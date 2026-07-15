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

import datetime as dt
import subprocess
import tempfile

import pytest
from sqlalchemy import event, func, select

from incant import db, models
from incant.db import session_scope
from incant.gitstore.store import GitStore
from incant.server.mgmt.helpers import (
    _OVERVIEW_TIP_CAP,
    _current_live,
    _current_live_bulk,
    _tip_ahead,
    _tip_ahead_from_map,
    _validated_by_version,
)
from incant.server.mgmt.prompts import _VERSION_HISTORY_LIMIT
from incant.service import get_app
from incant.targeting import build_snapshot
from incant.targeting.snapshot import _VALIDATED_ORDER_CAP

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
    # loader keeps it a small constant. Assert well under any proportional growth. (The
    # snapshot's validated-SHA read is deliberately TWO constant queries — a complete
    # servable-pair scan plus a windowed newest-K ordering read — so the constant is a
    # touch higher than the single-query era, but still flat regardless of library size.)
    assert len(statements) < 20, f"{len(statements)} SQL statements (should be constant):\n" + "\n".join(statements)
    # One `git log` walk for the whole library (was one per prompt).
    assert len(git_spawns) <= 3, git_spawns


# ── bounded work: windowed SQL scans ─────────────────────────────────
#
# The per-CALL query COUNT was already constant; these pin the per-call WORK — the
# validation/pointer scans are windowed to a constant K rows per (prompt, version), never
# the whole history — while the tip/live facts they compute stay exactly correct.

def _seed_validations(pid, ver, n, *, base=dt.datetime(2021, 1, 1, tzinfo=dt.timezone.utc)):
    """Insert `n` valid CommitValidation rows for (pid, ver), validated_at increasing with
    i (so i=n-1 is newest). Returns the SHAs newest-first. No Prompt/Version row needed —
    CommitValidation.prompt_id is a plain column, and these helpers scan the table directly."""
    shas = [f"{i:040d}" for i in range(n)]
    with session_scope() as s:
        for i, sha in enumerate(shas):
            s.add(models.CommitValidation(
                sha=sha, blob_sha="b" + sha, path=f"{pid}/v{ver}.j2",
                prompt_id=pid, version_number=ver, status="valid",
                validated_at=base + dt.timedelta(minutes=i),
            ))
    return list(reversed(shas))  # newest-first


def test_validated_by_version_windows_to_cap(client):
    """More than K validations for one (prompt, version): the bulk map is capped at K,
    newest-first order is preserved, the tip is still the newest, and tip_ahead is the real
    index for a recent live pointer but saturates at K (the honest cap) for an ancient one."""
    K = _OVERVIEW_TIP_CAP
    pid, ver = "scale/window", 1
    newest_first = _seed_validations(pid, ver, K + 10)

    with session_scope() as s:
        by_version = _validated_by_version(s)
    got = by_version[(pid, ver)]

    assert len(got) == K                       # capped — NOT the full K+10 rows
    assert got == newest_first[:K]             # newest-first order preserved within window
    assert got[0] == newest_first[0]           # tip_sha (head) is still the newest validated

    # Live pointer INSIDE the window → real distance from tip.
    assert _tip_ahead_from_map(got, newest_first[3]) == 3
    # Live pointer OUTSIDE the window (an ancient SHA) → saturates at the cap K, not a
    # larger true index (which we deliberately never scan far enough to compute).
    assert _tip_ahead_from_map(got, newest_first[-1]) == K


def test_current_live_bulk_equivalent_to_reduce(client):
    """The windowed row_number()==1 bulk read returns the SAME newest move per
    (prompt, version) as the per-call `_current_live` reduce, and partitions cleanly across
    keys (no bleed between versions)."""
    env, pid = "prod", "scale/moves"
    base = dt.datetime(2021, 1, 1, tzinfo=dt.timezone.utc)
    with session_scope() as s:
        for i in range(6):                     # v1: six moves, newest = sha5
            s.add(models.PointerMove(environment_id=env, prompt_id=pid, version_number=1,
                                     to_sha=f"v1-sha{i}", moved_at=base + dt.timedelta(minutes=i)))
        for i in range(3):                     # v2: three moves, newest = v2-sha2
            s.add(models.PointerMove(environment_id=env, prompt_id=pid, version_number=2,
                                     to_sha=f"v2-sha{i}", moved_at=base + dt.timedelta(minutes=i)))

    with session_scope() as s:
        bulk = _current_live_bulk(s, env)
        live_v1 = _current_live(s, env, pid, 1)
        live_v2 = _current_live(s, env, pid, 2)

    # Same row (id and to_sha) as the reduce, for each partition independently.
    assert bulk[(pid, 1)].to_sha == live_v1.to_sha == "v1-sha5"
    assert bulk[(pid, 1)].id == live_v1.id
    assert bulk[(pid, 2)].to_sha == live_v2.to_sha == "v2-sha2"
    assert bulk[(pid, 2)].id == live_v2.id


def test_get_versions_history_capped_at_constant(client):
    """The version-detail `history` array is bounded to `_VERSION_HISTORY_LIMIT` even when
    the file has many more commits — the per-version `git log` walk is capped, not unbounded."""
    pid = "scale/histcap"   # fresh project → review_policy 0, commit freely (as elsewhere here)
    client.post("/mgmt/prompts", json={"prompt_id": pid}, headers=auth())
    d = client.post(f"/mgmt/prompts/{pid}/drafts",
                    json={"version_number": 1, "content": "rev 0"}, headers=auth()).json()
    assert client.post(f"/mgmt/drafts/{d['id']}/commit", json={}, headers=auth()).status_code == 200
    # Pile on more than the cap's worth of commits on the SAME version file, directly in git.
    git = get_app().git
    for i in range(_VERSION_HISTORY_LIMIT + 3):
        git.commit_version(pid, 1, f"rev {i + 1}", author_name="A", author_email="a@x",
                           message=f"c{i + 1}")

    r = client.get(f"/mgmt/prompts/{pid}/versions?environment=prod", headers=auth())
    assert r.status_code == 200, r.text
    v1 = next(v for v in r.json()["versions"] if v["version"] == 1)
    assert len(v1["history"]) == _VERSION_HISTORY_LIMIT   # bounded, not the full 54 commits


# ── bounded work: snapshot windows ordering but keeps servability complete ──

def test_snapshot_servable_complete_despite_validation_window(client):
    """The snapshot's per-(prompt,version) validation ordering is windowed to K (drives
    tip_sha), but the servable set is the DELIBERATELY complete (prompt, sha) pair read: a
    validated SHA far outside the newest-K window is still servable — an old pinned rule or
    rolled-back pointer must never lose servability just because newer edits pushed it down."""
    env, pid, ver = "prod", "scale/snap", 1
    with session_scope() as s:
        s.add(models.Version(prompt_id=pid, number=ver))
    newest_first = _seed_validations(pid, ver, _VALIDATED_ORDER_CAP + 5)
    ancient = newest_first[-1]      # oldest validated sha — beyond the ordering window

    with session_scope() as s:
        snap = build_snapshot(s, env)

    vinfo = snap.versions[pid][ver]
    assert vinfo.tip_sha == newest_first[0]              # tip from the windowed ordering head
    assert snap.servable(pid, ancient) is True           # COMPLETE set: still servable
    assert snap.servable(pid, newest_first[0]) is True
    assert snap.servable(pid, "never-validated") is False


def test_snapshot_previous_live_recent_distinct(client):
    """The windowed pointer history still yields the §10 `previous_live` fallback: recent
    distinct previous-live SHAs, newest-first, excluding the current live."""
    env, pid, ver = "prod", "scale/prev", 1
    base = dt.datetime(2021, 1, 1, tzinfo=dt.timezone.utc)
    with session_scope() as s:
        s.add(models.Version(prompt_id=pid, number=ver))
        for i, sha in enumerate(["sA", "sB", "sA", "sC"]):   # sC is newest (live)
            s.add(models.PointerMove(environment_id=env, prompt_id=pid, version_number=ver,
                                     to_sha=sha, moved_at=base + dt.timedelta(minutes=i)))

    with session_scope() as s:
        snap = build_snapshot(s, env)

    vinfo = snap.versions[pid][ver]
    assert vinfo.live_sha == "sC"
    assert vinfo.previous_live == ("sA", "sB")           # recent distinct, newest-first


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
