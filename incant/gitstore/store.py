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


class GitError(RuntimeError):
    pass


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
        self._main_lock = threading.Lock()

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

    def diff(self, path: str, sha_a: str, sha_b: str) -> str:
        try:
            return self._git("diff", "--unified=3", sha_a, sha_b, "--", path)
        except GitError:
            return ""

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
        """

        path = f"{prompt_id}/v{version_number}.j2"
        trailers = [
            f"Incant-Prompt: {prompt_id}",
            f"Incant-Version: v{version_number}",
        ]
        if draft_id:
            trailers.append(f"Incant-Draft: {draft_id}")
        full_message = message.rstrip() + "\n\n" + "\n".join(trailers) + "\n"
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
