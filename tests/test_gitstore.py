import concurrent.futures as cf

from incant.gitstore import ContentStore, GitStore, validate_source


def make_store(tmp_path):
    g = GitStore(tmp_path / "repo")
    g.init()
    return g


def test_concurrent_commits_all_reachable_from_main(tmp_path):
    # Compare-and-swap on refs/heads/main: concurrent publishers must not strand a
    # validated commit unreachable from the branch (prunable by git gc).
    g = make_store(tmp_path)
    N = 12

    def commit(i: int):
        g.commit_version(f"p/a{i}", 1, f"content {i}",
                         author_name="A", author_email="a@x", message=f"c{i}")

    with cf.ThreadPoolExecutor(max_workers=N) as ex:
        errs = [f.exception() for f in [ex.submit(commit, i) for i in range(N)]]
    assert not any(errs), [e for e in errs if e]

    reachable = int(g._git("rev-list", "--count", "main").strip())
    assert reachable == 1 + N            # initial commit + every version, none lost
    assert len(g.list_files()) == N


def test_init_creates_bare_repo_with_main(tmp_path):
    g = make_store(tmp_path)
    assert g.exists()
    assert len(g.head()) == 40
    assert g.list_files() == []


def test_commit_and_read_version(tmp_path):
    g = make_store(tmp_path)
    sha = g.commit_version(
        "support/system", 1, "Hello {{ name }}",
        author_name="Sam", author_email="sam@x.com", message="initial v1",
    )
    assert len(sha) == 40
    assert g.read("support/system/v1.j2") == "Hello {{ name }}"
    assert g.list_files() == ["support/system/v1.j2"]
    # trailer present in the commit message
    body = g._git("log", "-1", "--format=%B", sha)
    assert "Incant-Prompt: support/system" in body
    assert "Incant-Version: v1" in body


def test_latest_commits_memoized_on_unchanged_head(tmp_path):
    # Head-keyed memo: a second call with an unchanged head does ZERO extra `git log`
    # walks (only a cheap rev-parse to check the head), and a new commit refreshes it.
    g = make_store(tmp_path)
    g.commit_version("p/a", 1, "a1", author_name="A", author_email="a@x", message="c1")

    log_walks: list = []
    real_git = g._git

    def counting(*args, **kwargs):
        if args and args[0] == "log":            # rev-parse (head check) is allowed, log is the walk
            log_walks.append(args)
        return real_git(*args, **kwargs)

    g._git = counting
    try:
        first = g.latest_commits()
        assert len(log_walks) == 1               # cold: one full walk
        second = g.latest_commits()
        assert len(log_walks) == 1               # warm: NO additional walk
        assert second is first                   # cached map object returned as-is

        g._git = real_git                        # let commit_version use the real runner
        g.commit_version("p/b", 1, "b1", author_name="B", author_email="b@x", message="c2")
        g._git = counting
        third = g.latest_commits()
        assert len(log_walks) == 2               # new head → exactly one fresh walk
        assert set(third) == {"p/a/v1.j2", "p/b/v1.j2"}
    finally:
        g._git = real_git


def test_history_tracks_a_version_file(tmp_path):
    g = make_store(tmp_path)
    g.commit_version("p/a", 1, "one", author_name="A", author_email="a@x", message="c1")
    g.commit_version("p/a", 1, "two", author_name="B", author_email="b@x", message="c2")
    hist = g.history("p/a/v1.j2")
    assert [c.subject for c in hist] == ["c2", "c1"]
    assert g.read("p/a/v1.j2") == "two"


def test_content_store_reads_at_commit(tmp_path):
    g = make_store(tmp_path)
    c1 = g.commit_version("p/a", 1, "first", author_name="A", author_email="a@x", message="c1")
    g.commit_version("p/a", 1, "second", author_name="A", author_email="a@x", message="c2")
    cs = ContentStore(g)
    assert cs.get("p/a", 1, c1).source == "first"     # historical commit
    # second read of same key is a cache hit
    before = cs.misses
    cs.get("p/a", 1, c1)
    assert cs.misses == before


def test_draft_lifecycle(tmp_path):
    g = make_store(tmp_path)
    g.commit_version("p/a", 2, "live", author_name="A", author_email="a@x", message="v2")
    base = g.head()
    g.write_draft("d_1", "p/a", 2, "draft content", base_sha=base)
    assert g.read_draft("d_1", "p/a", 2) == "draft content"
    # main is untouched by the draft
    assert g.read("p/a/v2.j2") == "live"
    g.delete_draft("d_1")
    assert g.read_draft("d_1", "p/a", 2) is None


def test_validation_detects_syntax_and_cycles(tmp_path):
    ok = validate_source(
        "Hi {{ name }}", "p/a",
        is_known_prompt=lambda _p: True, include_source=lambda _p: None,
    )
    assert ok.ok and ok.extracted_variables["required"] == ["name"]

    bad = validate_source(
        "Hi {{ name }", "p/a",
        is_known_prompt=lambda _p: True, include_source=lambda _p: None,
    )
    assert not bad.ok and "template error" in bad.error

    sources = {"a": '{% include "b" %}', "b": '{% include "a" %}'}
    cyc = validate_source(
        sources["a"], "a",
        is_known_prompt=lambda p: p in sources,
        include_source=lambda p: sources.get(p),
    )
    assert not cyc.ok and "cycle" in cyc.error

    unknown = validate_source(
        '{% include "ghost" %}', "a",
        is_known_prompt=lambda p: False, include_source=lambda _p: None,
    )
    assert not unknown.ok and "not a registered prompt" in unknown.error
