"""GitStore — the canonical content repository.

A single bare repository, Incant-owned, with an opinionated layout: one file per
version (``<prompt_id>/vN.j2``) on a single ``main`` branch. All writes go through
here as commits authored as the acting user; drafts live on
``refs/incant/drafts/<id>``. Nothing here touches a working tree — every operation
uses git plumbing against a temporary index, so it works on a bare repo.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit


class GitError(RuntimeError):
    pass


def redact_remote_url(url: str) -> str:
    """Return a display-safe remote URL with embedded credentials masked."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return "<invalid remote URL>"
    if not (parts.username or parts.password):
        return url
    host = parts.hostname or ""
    if parts.port:
        host += f":{parts.port}"
    user = parts.username or ""
    netloc = f"{user}:***@{host}" if parts.password else f"{user}@{host}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


def sanitize_remote_error(text: str, url: str) -> str:
    """Remove both a credential-bearing URL and its password from git output."""
    safe = redact_remote_url(url)
    cleaned = (text or "remote operation failed").replace(url, safe)
    try:
        password = urlsplit(url).password
    except ValueError:
        password = None
    if password:
        cleaned = cleaned.replace(password, "***")
    return cleaned.strip()


class RemoteGitError(GitError):
    """A remote-operation failure whose string form is always credential-safe."""

    def __init__(self, operation: str, url: str, detail: str = "") -> None:
        self.operation = operation
        self.display_url = redact_remote_url(url)
        self.detail = sanitize_remote_error(detail, url)
        super().__init__(f"{operation} for {self.display_url} failed: {self.detail}")


def _ssh_env(ssh_key_path: str | None, known_hosts_path: str | None) -> dict[str, str]:
    """GIT_SSH_COMMAND for a remote operation: a push-only deploy key (a remote's
    ``auth_ref``) and/or a pinned known_hosts file, so a container with no ~/.ssh
    still authenticates and verifies the host. Empty when neither is set (https
    remotes, file:// test remotes)."""
    parts = ["ssh"]
    if ssh_key_path:
        parts += ["-i", ssh_key_path, "-o", "IdentitiesOnly=yes"]
    if known_hosts_path:
        parts += ["-o", f"UserKnownHostsFile={known_hosts_path}"]
    return {"GIT_SSH_COMMAND": " ".join(parts)} if len(parts) > 1 else {}


class ConcurrentUpdate(GitError):
    """A ref moved out from under a compare-and-swap update-ref."""


@dataclass
class CommitInfo:
    sha: str
    author: str
    email: str
    date: str
    subject: str


class GitStore:
    def __init__(self, repo_path: str | os.PathLike) -> None:
        self.repo = Path(repo_path).resolve()
        # Serialize commits to main within this process so the CAS retry loop only
        # ever has to defend against *other* processes (uvicorn workers/replicas).
        # Reentrant: a staged publish (commit_draft) holds it from staging through
        # promotion, and one DB transaction may stage several publishes (seed, batch
        # flows) — same thread, nested acquires.
        self._main_lock = threading.RLock()
        # Head-keyed memo for latest_commits (see there): {(ref, suffix) -> (head_sha, map)}.
        self._latest_cache: dict[tuple[str, str], tuple[str, dict[str, "CommitInfo"]]] = {}

    # ── low-level git ────────────────────────────────────────────────

    def _git(self, *args: str, input: str | None = None, env: dict | None = None) -> str:
        full_env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
        if env:
            full_env.update(env)
        proc = subprocess.run(
            ["git", "--git-dir", str(self.repo), *args],
            input=input,
            capture_output=True,
            text=True,
            env=full_env,
        )
        if proc.returncode != 0:
            raise GitError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
        return proc.stdout

    def _git_bytes(self, *args: str) -> bytes:
        proc = subprocess.run(
            ["git", "--git-dir", str(self.repo), *args],
            capture_output=True,
        )
        if proc.returncode != 0:
            raise GitError(f"git {' '.join(args)} failed: {proc.stderr.decode().strip()}")
        return proc.stdout

    # ── lifecycle ────────────────────────────────────────────────────

    def exists(self) -> bool:
        return (self.repo / "HEAD").exists()

    def init(self) -> None:
        """Create a bare repo with an initial empty commit on main."""
        if self.exists():
            return
        self.repo.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["git", "init", "--bare", "--initial-branch=main", str(self.repo)],
            capture_output=True, check=True,
        )
        # Seed an empty root commit so `main` exists.
        empty_tree = self._git("hash-object", "-t", "tree", "--stdin", "-w", input="").strip()
        env = self._author_env("Incant", "incant@localhost")
        commit = self._git(
            "commit-tree", empty_tree, "-m", "Initialize Incant content repository",
            env=env,
        ).strip()
        self._git("update-ref", "refs/heads/main", commit)

    def _author_env(self, name: str, email: str) -> dict:
        # The acting user is the author; Incant is the committer. Dates are real
        # wall-clock. A test hook (INCANT_FIXED_GIT_DATE) can pin them so seeded
        # repos are byte-identical across runs — never set in normal operation.
        env = {
            "GIT_AUTHOR_NAME": name, "GIT_AUTHOR_EMAIL": email,
            "GIT_COMMITTER_NAME": "Incant", "GIT_COMMITTER_EMAIL": "incant@localhost",
        }
        stamp = os.environ.get("INCANT_FIXED_GIT_DATE")
        if stamp:
            env["GIT_AUTHOR_DATE"] = stamp
            env["GIT_COMMITTER_DATE"] = stamp
        return env

    # ── reads ────────────────────────────────────────────────────────

    def head(self, ref: str = "refs/heads/main") -> str:
        return self._git("rev-parse", ref).strip()

    def read(self, path: str, ref: str = "main") -> str | None:
        try:
            return self._git("cat-file", "-p", f"{ref}:{path}")
        except GitError:
            return None

    def blob_sha(self, path: str, ref: str = "main") -> str | None:
        try:
            return self._git("rev-parse", f"{ref}:{path}").strip()
        except GitError:
            return None

    def read_blob(self, blob_sha: str) -> str:
        return self._git("cat-file", "-p", blob_sha)

    def exists_at(self, path: str, ref: str = "main") -> bool:
        return self.blob_sha(path, ref) is not None

    def list_files(self, ref: str = "main", suffix: str = ".j2") -> list[str]:
        try:
            out = self._git("ls-tree", "-r", "--name-only", ref)
        except GitError:
            return []
        return sorted(p for p in out.splitlines() if p.endswith(suffix))

    def history(self, path: str, limit: int = 50, ref: str = "main") -> list[CommitInfo]:
        try:
            out = self._git(
                "log", f"-{limit}", "--format=%H%x1f%an%x1f%ae%x1f%aI%x1f%s",
                ref, "--", path,
            )
        except GitError:
            return []
        rows: list[CommitInfo] = []
        for line in out.splitlines():
            if not line.strip():
                continue
            sha, an, ae, ai, subj = line.split("\x1f")
            rows.append(CommitInfo(sha, an, ae, ai, subj))
        return rows

    def latest_commits(self, ref: str = "main", suffix: str = ".j2") -> dict[str, CommitInfo]:
        """Map every content path under ``ref`` to the newest commit that produced its
        current content — one ``git log`` walk in place of a ``history()`` subprocess
        per file.

        This exists for the library overview, whose per-prompt "updated {when, who}" is
        exactly ``history(path)[0]``. Fanning that out into one ``git log -- path`` per
        prompt is what made the landing screen degrade with library size; a single
        newest-first pass over the whole branch answers all of them at once.

        We walk newest-first and keep the *first* commit that touches each path — its most
        recent change. ``--name-status -M`` gives us rename detection (``R### old new``) so
        a rename is attributed to its NEW path, not the vanished old one. Record framing
        mirrors ``history``: the same ``%an/%ae/%aI/%s`` fields, prefixed with a
        record-separator byte (``%x1e``) so each commit's header is unambiguously separable
        from the name-status block that follows it — and unit (``%x1f``) separators between
        fields, as ``history`` already uses.

        A ``decided`` set is the crux of getting deletes/renames right on a newest-first
        walk: once a path's fate is settled — recorded as present, or tombstoned by a
        delete / the old side of a rename — an *older* commit touching that same path must
        NOT resurrect it. Without this, walking past a ``D old`` would find the original
        ``A old`` and wrongly report a deleted (or renamed-away) file as live.

        Head-keyed memo (scalability): the returned map is a pure function of the ref's
        current commit — it only changes when the branch tip moves. So we resolve that tip
        first (one ``rev-parse``-equivalent) and, while it matches the last computed key,
        return the cached map WITHOUT walking the log at all. A new commit moves the head,
        the key misses, and we do the one full walk that new commit warrants. Net cost:
        one ``rev-parse`` per call + one full ``git log`` walk per new commit, in place of
        a full walk on every /mgmt/overview call.
        """
        try:
            head = self.head(ref)
        except GitError:
            return {}
        cache_key = (ref, suffix)
        cached = self._latest_cache.get(cache_key)
        if cached is not None and cached[0] == head:
            return cached[1]
        try:
            out = self._git(
                "log", ref, "-M", "--name-status",
                "--format=%x1e%H%x1f%an%x1f%ae%x1f%aI%x1f%s",
            )
        except GitError:
            return {}
        latest: dict[str, CommitInfo] = {}
        decided: set[str] = set()  # paths whose fate is settled (present or gone)

        def present(path: str, info: CommitInfo) -> None:
            if path.endswith(suffix) and path not in decided:
                latest[path] = info
                decided.add(path)

        def gone(path: str) -> None:
            if path.endswith(suffix):
                decided.add(path)

        for record in out.split("\x1e"):
            record = record.strip("\n")
            if not record:
                continue
            header, *changes = record.split("\n")
            sha, an, ae, ai, subj = header.split("\x1f")
            info = CommitInfo(sha, an, ae, ai, subj)
            for line in changes:
                if not line.strip():
                    continue
                fields = line.split("\t")
                code = fields[0][:1]
                if code == "R":                 # rename: [R###, old, new]
                    present(fields[2], info)    # content lives at the new path now
                    gone(fields[1])             # old path renamed away
                elif code == "C":               # copy: [C###, old, new]; old survives
                    present(fields[2], info)
                elif code == "D":               # delete: [D, path]
                    gone(fields[1])
                else:                           # add/modify/type-change: [A|M|T, path]
                    present(fields[1], info)
        # Atomic swap: publish (head, map) only after the walk fully succeeds, so a reader
        # never sees a half-built map. Two threads racing on a cold/stale key may both walk
        # (benign — the walk is a pure function of ``head``, so their maps are identical);
        # the last dict assignment wins and every caller gets a correct, head-matched map.
        self._latest_cache[cache_key] = (head, latest)
        return latest

    def diff(self, path: str, sha_a: str, sha_b: str) -> str:
        try:
            return self._git("diff", "--unified=3", sha_a, sha_b, "--", path)
        except GitError:
            return ""

    # ── backup pushes (§6) ───────────────────────────────────────────

    def push_mirror(
        self, url: str, *,
        ssh_key_path: str | None = None,
        known_hosts_path: str | None = None,
        timeout: float | None = None,
    ) -> None:
        """Force-push this repo's entire ref set to ``url`` (``git push --mirror``).

        Remotes are write-only backup targets: Incant force-pushes its own lineage,
        and anything anyone else pushed there is overwritten/pruned (§6). ``--mirror``
        carries main AND the draft refs, so a clone of the remote is the complete
        content history. ``ssh_key_path`` (the remote's ``auth_ref``) selects a
        push-only deploy key; ``known_hosts_path`` pins host keys so a container
        with no ~/.ssh still verifies the remote host.
        """
        try:
            proc = subprocess.run(
                ["git", "--git-dir", str(self.repo), "push", "--mirror", "--quiet", url],
                capture_output=True, text=True, timeout=timeout,
                env={**os.environ, "GIT_TERMINAL_PROMPT": "0",
                     **_ssh_env(ssh_key_path, known_hosts_path)},
            )
        except subprocess.TimeoutExpired:
            raise RemoteGitError("push --mirror", url, "operation timed out") from None
        if proc.returncode != 0:
            raise RemoteGitError("push --mirror", url, proc.stderr)

    def commits_ahead(self, last_pushed: str | None) -> int:
        """How many commits on main are not yet at ``last_pushed`` (the backup queue
        depth for one remote). ``None`` — or a SHA this repo no longer knows (a remote
        recorded against a rewritten lineage) — counts the whole branch."""
        spec = "refs/heads/main"
        if last_pushed:
            try:
                return int(self._git("rev-list", "--count",
                                     f"{last_pushed}..refs/heads/main").strip())
            except GitError:
                pass  # unknown/gc'd SHA — fall through to the full count
        return int(self._git("rev-list", "--count", spec).strip())

    def oldest_unpushed_at(self, last_pushed: str | None) -> int | None:
        """Committer timestamp (unix) of the OLDEST main commit not yet at
        ``last_pushed`` — the far edge of the §6 durability exposure window — or
        ``None`` when the remote is fully caught up."""
        args = ["log", "--format=%ct"]
        if last_pushed:
            try:
                out = self._git(*args, f"{last_pushed}..refs/heads/main")
            except GitError:
                out = self._git(*args, "refs/heads/main")
        else:
            out = self._git(*args, "refs/heads/main")
        lines = [ln for ln in out.splitlines() if ln.strip()]
        return int(lines[-1]) if lines else None

    # ── serve-replica content follow (§13/§15) ───────────────────────
    #
    # Targeting propagates to replicas through the DB poll, but content does not
    # propagate by itself: a make-live can point at a commit a replica's repo copy
    # has never seen. Replicas therefore hydrate (mirror-clone) and then follow
    # (periodic mirror-fetch) a backup remote — the same remotes the full node
    # pushes to — so every SHA targeting references becomes fetchable within one
    # fetch interval.

    def mirror_fetch(
        self, url: str, *,
        ssh_key_path: str | None = None,
        known_hosts_path: str | None = None,
        timeout: float | None = None,
    ) -> None:
        """Fetch ``url``'s complete ref set into this repo, forced + pruned — the
        read-side mirror of :meth:`push_mirror`. The remote (fed by the full node)
        is authoritative; local refs move to match it."""
        try:
            proc = subprocess.run(
                ["git", "--git-dir", str(self.repo), "fetch", "--prune", "--quiet",
                 url, "+refs/*:refs/*"],
                capture_output=True, text=True, timeout=timeout,
                env={**os.environ, "GIT_TERMINAL_PROMPT": "0",
                     **_ssh_env(ssh_key_path, known_hosts_path)},
            )
        except subprocess.TimeoutExpired:
            raise RemoteGitError("mirror fetch", url, "operation timed out") from None
        if proc.returncode != 0:
            raise RemoteGitError("mirror fetch", url, proc.stderr)

    def clone_mirror(
        self, url: str, *,
        ssh_key_path: str | None = None,
        known_hosts_path: str | None = None,
        timeout: float | None = None,
    ) -> None:
        """Hydrate this (not-yet-existing) repo as a bare mirror clone of ``url`` —
        a serve replica's first boot against an empty volume."""
        if self.exists():
            raise GitError(f"refusing to clone over existing repo at {self.repo}")
        self.repo.parent.mkdir(parents=True, exist_ok=True)
        try:
            proc = subprocess.run(
                ["git", "clone", "--mirror", "--quiet", url, str(self.repo)],
                capture_output=True, text=True, timeout=timeout,
                env={**os.environ, "GIT_TERMINAL_PROMPT": "0",
                     **_ssh_env(ssh_key_path, known_hosts_path)},
            )
        except subprocess.TimeoutExpired:
            raise RemoteGitError("mirror clone", url, "operation timed out") from None
        if proc.returncode != 0:
            raise RemoteGitError("mirror clone", url, proc.stderr)

    # ── writes ───────────────────────────────────────────────────────

    def _hash_object(self, content: str) -> str:
        return self._git("hash-object", "-w", "--stdin", input=content).strip()

    def _update_ref_cas(self, ref: str, new: str, expected_old: str | None) -> None:
        """update-ref with an optional expected-old (compare-and-swap)."""
        args = ["update-ref", ref, new]
        if expected_old is not None:
            args.append(expected_old)
        proc = subprocess.run(
            ["git", "--git-dir", str(self.repo), *args],
            capture_output=True, text=True,
        )
        if proc.returncode != 0:
            # With expected_old set, a non-zero exit is (almost always) the ref
            # having moved concurrently — surface it as retryable.
            if expected_old is not None:
                raise ConcurrentUpdate(f"update-ref {ref} CAS failed: {proc.stderr.strip()}")
            raise GitError(f"update-ref {ref} failed: {proc.stderr.strip()}")

    def _commit_file(
        self,
        path: str,
        content: str,
        parent: str,
        message: str,
        author_name: str,
        author_email: str,
        update_ref: str,
        expected_old: str | None = None,
    ) -> str:
        """Commit a single file onto ``parent`` via a temporary index. Returns commit sha."""

        blob = self._hash_object(content)
        with tempfile.NamedTemporaryFile(prefix="incant-index-", delete=False) as tf:
            index_path = tf.name
        try:
            env = {**os.environ, "GIT_INDEX_FILE": index_path}
            # Load the parent tree, stage the one file, write the new tree.
            subprocess.run(
                ["git", "--git-dir", str(self.repo), "read-tree", parent],
                env=env, capture_output=True, check=True,
            )
            subprocess.run(
                ["git", "--git-dir", str(self.repo), "update-index", "--add",
                 "--cacheinfo", f"100644,{blob},{path}"],
                env=env, capture_output=True, check=True,
            )
            tree = subprocess.run(
                ["git", "--git-dir", str(self.repo), "write-tree"],
                env=env, capture_output=True, check=True, text=True,
            ).stdout.strip()
        finally:
            os.unlink(index_path)

        commit = self._git(
            "commit-tree", tree, "-p", parent, "-m", message,
            env=self._author_env(author_name, author_email),
        ).strip()
        self._update_ref_cas(update_ref, commit, expected_old)
        return commit

    @staticmethod
    def _version_message(prompt_id: str, version_number: int, message: str,
                         draft_id: str | None) -> str:
        trailers = [
            f"Incant-Prompt: {prompt_id}",
            f"Incant-Version: v{version_number}",
        ]
        if draft_id:
            trailers.append(f"Incant-Draft: {draft_id}")
        return message.rstrip() + "\n\n" + "\n".join(trailers) + "\n"

    def commit_version(
        self,
        prompt_id: str,
        version_number: int,
        content: str,
        *,
        author_name: str,
        author_email: str,
        message: str,
        draft_id: str | None = None,
    ) -> str:
        """Commit a version file onto main. Returns the new commit sha.

        Uses compare-and-swap on ``refs/heads/main``: if a concurrent publisher
        advances main between our read and write, retry onto the new tip rather
        than stranding a validated commit unreachable from the branch.

        This is the direct-to-main path (seed tooling, pending-promotion recovery,
        tests). The publish pipeline itself stages through a pending ref instead —
        see :meth:`commit_version_pending` / :meth:`promote_pending`.
        """

        path = f"{prompt_id}/v{version_number}.j2"
        full_message = self._version_message(prompt_id, version_number, message, draft_id)
        last: ConcurrentUpdate | None = None
        with self._main_lock:
            for _ in range(16):
                parent = self.head()
                try:
                    return self._commit_file(
                        path, content, parent, full_message, author_name, author_email,
                        "refs/heads/main", expected_old=parent,
                    )
                except ConcurrentUpdate as exc:
                    last = exc
        raise last or GitError("commit_version: exhausted CAS retries")

    # ── staged publishes (pending refs) ──────────────────────────────
    #
    # `main` must only ever hold commits the DB fully describes. A publish therefore
    # stages its commit on refs/incant/pending/<draft> BEFORE the control-plane
    # transaction, and promotes it to main (a pure CAS ref move — the SHA is already
    # recorded in the staged rows) only AFTER that transaction commits. The caller
    # (RegistryService.commit_draft) holds `publish_lock` from staging through
    # promotion so in-process publishes serialize; the CAS in promote_pending is the
    # cross-process backstop. A crash between the two phases leaves a pending ref
    # that boot recovery promotes or discards by consulting the DB.

    @property
    def publish_lock(self) -> threading.RLock:
        return self._main_lock

    def pending_ref(self, draft_id: str) -> str:
        return f"refs/incant/pending/{draft_id}"

    def commit_version_pending(
        self,
        prompt_id: str,
        version_number: int,
        content: str,
        *,
        author_name: str,
        author_email: str,
        message: str,
        draft_id: str,
        parent: str | None = None,
    ) -> tuple[str, str]:
        """Stage a version commit on the draft's pending ref. Returns ``(sha,
        parent)`` — promotion is a later CAS of main from ``parent`` to ``sha``.
        Caller must hold :attr:`publish_lock`. ``parent`` defaults to the current
        main tip; a transaction staging SEVERAL publishes chains each onto the
        previous staged SHA so their promotions fast-forward in order."""
        path = f"{prompt_id}/v{version_number}.j2"
        parent = parent or self.head()
        sha = self._commit_file(
            path, content, parent,
            self._version_message(prompt_id, version_number, message, draft_id),
            author_name, author_email,
            self.pending_ref(draft_id),  # unconditional: replaces a crashed attempt
        )
        return sha, parent

    def promote_pending(self, sha: str, expected_parent: str) -> None:
        """Advance main from ``expected_parent`` to the staged ``sha`` (fast-forward
        CAS). Raises :class:`ConcurrentUpdate` if main is no longer at the parent —
        only possible cross-process, since in-process publishes hold the lock."""
        self._update_ref_cas("refs/heads/main", sha, expected_parent)

    def delete_pending(self, draft_id: str) -> None:
        try:
            self._git("update-ref", "-d", self.pending_ref(draft_id))
        except GitError:
            pass

    def list_pending_refs(self) -> list[tuple[str, str]]:
        """[(draft_id, sha)] for every staged-but-unpromoted publish."""
        try:
            out = self._git("for-each-ref", "--format=%(refname) %(objectname)",
                            "refs/incant/pending/")
        except GitError:
            return []
        prefix = "refs/incant/pending/"
        rows: list[tuple[str, str]] = []
        for line in out.splitlines():
            ref, _, sha = line.partition(" ")
            if ref.startswith(prefix) and ref[len(prefix):] and sha:
                rows.append((ref[len(prefix):], sha))
        return rows

    def is_ancestor(self, sha: str, ref: str = "refs/heads/main") -> bool:
        proc = subprocess.run(
            ["git", "--git-dir", str(self.repo), "merge-base", "--is-ancestor", sha, ref],
            capture_output=True,
        )
        return proc.returncode == 0

    def commit_parent(self, sha: str) -> str | None:
        try:
            return self._git("rev-parse", f"{sha}^").strip()
        except GitError:
            return None

    # ── drafts ───────────────────────────────────────────────────────

    def draft_ref(self, draft_id: str) -> str:
        return f"refs/incant/drafts/{draft_id}"

    def write_draft(
        self, draft_id: str, prompt_id: str, version_number: int, content: str,
        *, base_sha: str | None = None, author_name: str = "draft", author_email: str = "draft@localhost",
        expected_old: str | None = None,
    ) -> str:
        """Create/update a draft commit on refs/incant/drafts/<id>. Returns draft sha.

        When ``expected_old`` is given, the ref update is compare-and-swapped against it:
        if the draft ref has moved since (a concurrent autosave), ``ConcurrentUpdate`` is
        raised instead of clobbering the newer draft. ``None`` => unconditional write.
        """

        path = f"{prompt_id}/v{version_number}.j2"
        parent = base_sha or self.head()
        return self._commit_file(
            path, content, parent, f"draft {draft_id}", author_name, author_email,
            self.draft_ref(draft_id), expected_old=expected_old,
        )

    def read_draft(self, draft_id: str, prompt_id: str, version_number: int) -> str | None:
        path = f"{prompt_id}/v{version_number}.j2"
        return self.read(path, self.draft_ref(draft_id))

    def delete_draft(self, draft_id: str) -> None:
        try:
            self._git("update-ref", "-d", self.draft_ref(draft_id))
        except GitError:
            pass

    def draft_ref_exists(self, draft_id: str) -> bool:
        """True iff refs/incant/drafts/<id> resolves to a commit."""
        proc = subprocess.run(
            ["git", "--git-dir", str(self.repo), "rev-parse", "--verify", "--quiet",
             self.draft_ref(draft_id)],
            capture_output=True, text=True,
        )
        return proc.returncode == 0

    def list_draft_refs(self) -> list[str]:
        """Return the draft ids that currently have a ref under refs/incant/drafts/."""
        try:
            out = self._git("for-each-ref", "--format=%(refname)", "refs/incant/drafts/")
        except GitError:
            return []
        prefix = "refs/incant/drafts/"
        return [line[len(prefix):] for line in out.splitlines()
                if line.startswith(prefix) and line[len(prefix):]]
