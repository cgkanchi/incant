"""GitStore — the canonical content repository.

A single bare repository, Incant-owned, with an opinionated layout: one file per
version (``<prompt_id>/vN.j2``) on a single ``main`` branch. All writes go through
here as commits authored as the acting user; drafts live on
``refs/incant/drafts/<id>``. Nothing here touches a working tree — every operation
uses git plumbing against a temporary index, so it works on a bare repo.
"""

from __future__ import annotations

import os
import re
import shlex
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


# ── remote URL / credential-path grammar ─────────────────────────────
#
# Both values below end up inside strings that git hands to a SHELL: GIT_SSH_COMMAND
# is run through `sh -c`, and a credential helper is too. Quoting (see _remote_auth)
# is the actual defence; these validators are the belt to those suspenders — they turn
# a hostile or merely mistyped admin input into a clear 422 / boot error instead of an
# opaque git failure, and they keep the accepted alphabet small enough that the
# quoted form is always the plain form.

AUTH_REF_MAX = 1024
# An absolute path of ordinary filename characters: no whitespace, no shell
# metacharacters, no leading `-` (an option look-alike to ssh/git).
_AUTH_REF_RE = re.compile(r"^/[A-Za-z0-9._/@-]+$")

REMOTE_URL_MAX = 2048
# scp-like `[user@]host:path` — the form deploy keys are documented with
# (`git@github.com:acme/backup.git`). The path after the colon must not start with
# another colon: `ext::` / `fd::` are git's *remote-helper* transports (a shell
# command, a file descriptor) and never a backup target.
_SCP_LIKE_RE = re.compile(r"^(?:[A-Za-z0-9._-]+@)?[A-Za-z0-9._-]+:(?!:)\S+$")
_ALLOWED_SCHEMES = ("https://", "http://", "ssh://", "file://")

REMOTE_URL_GRAMMAR = (
    "allowed forms: https://…, http://…, ssh://…, scp-like user@host:path, file://…, "
    "or an absolute local path; no whitespace; remote-helper transports (ext::, fd::) "
    "and other schemes are refused"
)


def validate_auth_ref(path: str) -> str:
    """The credential PATH grammar (``auth_ref`` on a remote, INCANT_BOOTSTRAP_REMOTE_KEY).
    Raises ``ValueError`` with the reason."""
    if not isinstance(path, str) or not path:
        raise ValueError("auth_ref must be a non-empty absolute path")
    if len(path) > AUTH_REF_MAX:
        raise ValueError(f"auth_ref longer than {AUTH_REF_MAX} characters")
    if not _AUTH_REF_RE.fullmatch(path):   # fullmatch: `$` would admit a trailing newline
        raise ValueError(
            f"invalid auth_ref {path!r}: must be an absolute path made of letters, digits, "
            "'.', '_', '-', '@' and '/' — no spaces or shell characters"
        )
    return path


def validate_remote_url(url: str) -> str:
    """The remote URL grammar. Returns the stripped URL; raises ``ValueError``."""
    if not isinstance(url, str) or not url.strip():
        raise ValueError("remote url must not be empty")
    url = url.strip()
    if len(url) > REMOTE_URL_MAX:
        raise ValueError(f"remote url longer than {REMOTE_URL_MAX} characters")
    if any(c.isspace() for c in url) or not url.isprintable():
        raise ValueError(f"invalid remote url: whitespace/control characters; {REMOTE_URL_GRAMMAR}")
    if re.match(r"^[A-Za-z0-9._-]+::", url):
        raise ValueError(
            f"invalid remote url {url!r}: remote-helper transports (ext::, fd::) are not "
            f"allowed; {REMOTE_URL_GRAMMAR}"
        )
    # Git's own split: anything with `://` is a scheme URL (so `git://h/x` must not be
    # read as scp-like host `git`); otherwise it is scp-like or a local path.
    if "://" in url:
        if url.startswith(_ALLOWED_SCHEMES):
            return url
        raise ValueError(f"invalid remote url {url!r}: scheme not allowed; {REMOTE_URL_GRAMMAR}")
    if url.startswith("/") or _SCP_LIKE_RE.fullmatch(url):
        return url
    raise ValueError(f"invalid remote url {url!r}: {REMOTE_URL_GRAMMAR}")


def _remote_auth(url: str, auth_ref: str | None,
                 known_hosts_path: str | None) -> tuple[list[str], dict[str, str]]:
    """(extra git argv, extra env) for a remote operation, by URL scheme.

    ``auth_ref`` is always a filesystem PATH — the secret itself never enters the
    database or a process argument:

    * ssh URLs — path to a private deploy key, wired via GIT_SSH_COMMAND (with the
      optional pinned known_hosts so a container with no ~/.ssh still verifies
      hosts);
    * http(s) URLs — path to a git *credential-store* file (one line:
      ``https://user:token@host``), wired via ``credential.helper=store`` so the
      remote URL in the DB stays credential-free.

    Git runs BOTH of these through ``sh -c`` (GIT_SSH_COMMAND always; a credential
    helper whenever its command line carries shell syntax). Every path is therefore
    ``shlex.quote``d so it reaches ssh / git-credential-store as exactly one argument:
    a path with a space still works, and a path carrying ``;``/``$(...)`` is an
    (inert, misspelt) filename rather than a second command. For the plain paths the
    validators admit, quoting is the identity — the strings below are unchanged.
    """
    argv: list[str] = []
    env: dict[str, str] = {}
    if url.startswith(("http://", "https://")):
        if auth_ref:
            argv += ["-c", f"credential.helper=store --file={shlex.quote(auth_ref)}"]
        return argv, env
    parts = ["ssh"]
    if auth_ref:
        parts += ["-i", shlex.quote(auth_ref), "-o", "IdentitiesOnly=yes"]
    if known_hosts_path:
        parts += ["-o", f"UserKnownHostsFile={shlex.quote(known_hosts_path)}"]
    if len(parts) > 1:
        env["GIT_SSH_COMMAND"] = " ".join(parts)
    return argv, env


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

    def has_main(self) -> bool:
        """True iff refs/heads/main resolves — a mirror-clone of a BLANK remote
        yields a repo with no refs at all."""
        proc = subprocess.run(
            ["git", "--git-dir", str(self.repo), "rev-parse", "--verify", "--quiet",
             "refs/heads/main"],
            capture_output=True,
        )
        return proc.returncode == 0

    def seed_main(self) -> None:
        """Create the initial empty commit on main (fresh repo, or one mirror-cloned
        from a blank remote)."""
        empty_tree = self._git("hash-object", "-t", "tree", "--stdin", "-w", input="").strip()
        env = self._author_env("Incant", "incant@localhost")
        commit = self._git(
            "commit-tree", empty_tree, "-m", "Initialize Incant content repository",
            env=env,
        ).strip()
        self._git("update-ref", "refs/heads/main", commit)

    def init(self) -> None:
        """Create a bare repo with an initial empty commit on main. A repo that
        already exists but lacks main (cloned from a blank remote) is seeded too."""
        if self.exists():
            if not self.has_main():
                self.seed_main()
            return
        self.repo.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["git", "init", "--bare", "--initial-branch=main", str(self.repo)],
            capture_output=True, check=True,
        )
        self.seed_main()

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
        # ``-z`` (NUL-separated) is load-bearing: without it git C-quotes any path
        # holding a quote, backslash, newline or non-ASCII byte (``"a\\"b/v1.j2"``), so
        # the entry no longer ends in ``.j2`` and a DR adoption silently drops it.
        try:
            out = self._git("ls-tree", "-r", "-z", "--name-only", ref)
        except GitError:
            return []
        return sorted(p for p in out.split("\0") if p.endswith(suffix))

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
        auth_ref: str | None = None,
        known_hosts_path: str | None = None,
        timeout: float | None = None,
    ) -> None:
        """Force-push this repo's entire ref set to ``url`` (``git push --mirror``).

        Remotes are write-only backup targets: Incant force-pushes its own lineage,
        and anything anyone else pushed there is overwritten/pruned (§6). ``--mirror``
        carries main AND the draft refs, so a clone of the remote is the complete
        content history. ``auth_ref`` is the remote's credential PATH (ssh deploy
        key, or an https credential-store file — see ``_remote_auth``);
        ``known_hosts_path`` pins host keys so a container with no ~/.ssh still
        verifies the remote host. ``--`` precedes the URL in every remote
        invocation so a value beginning with ``-`` is a (bad) URL, never an option.
        """
        try:
            auth_argv, auth_env = _remote_auth(url, auth_ref, known_hosts_path)
            proc = subprocess.run(
                ["git", *auth_argv, "--git-dir", str(self.repo),
                 "push", "--mirror", "--quiet", "--", url],
                capture_output=True, text=True, timeout=timeout,
                env={**os.environ, "GIT_TERMINAL_PROMPT": "0", **auth_env},
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
    # pushes to. Nothing pushes on publish, so a SHA is fetchable within one backup
    # push interval PLUS one fetch interval (~45 s at the defaults); the §10
    # within-version fallback serves the previous live SHA across that window.

    def mirror_fetch(
        self, url: str, *,
        auth_ref: str | None = None,
        known_hosts_path: str | None = None,
        timeout: float | None = None,
    ) -> None:
        """Fetch ``url``'s complete ref set into this repo, forced + pruned — the
        read-side mirror of :meth:`push_mirror`. The remote (fed by the full node)
        is authoritative; local refs move to match it."""
        try:
            auth_argv, auth_env = _remote_auth(url, auth_ref, known_hosts_path)
            proc = subprocess.run(
                ["git", *auth_argv, "--git-dir", str(self.repo),
                 "fetch", "--prune", "--quiet", "--", url, "+refs/*:refs/*"],
                capture_output=True, text=True, timeout=timeout,
                env={**os.environ, "GIT_TERMINAL_PROMPT": "0", **auth_env},
            )
        except subprocess.TimeoutExpired:
            raise RemoteGitError("mirror fetch", url, "operation timed out") from None
        if proc.returncode != 0:
            raise RemoteGitError("mirror fetch", url, proc.stderr)

    def clone_mirror(
        self, url: str, *,
        auth_ref: str | None = None,
        known_hosts_path: str | None = None,
        timeout: float | None = None,
    ) -> None:
        """Hydrate this (not-yet-existing) repo as a bare mirror clone of ``url`` —
        a serve replica's first boot against an empty volume."""
        if self.exists():
            raise GitError(f"refusing to clone over existing repo at {self.repo}")
        self.repo.parent.mkdir(parents=True, exist_ok=True)
        try:
            auth_argv, auth_env = _remote_auth(url, auth_ref, known_hosts_path)
            proc = subprocess.run(
                ["git", *auth_argv, "clone", "--mirror", "--quiet", "--", url, str(self.repo)],
                capture_output=True, text=True, timeout=timeout,
                env={**os.environ, "GIT_TERMINAL_PROMPT": "0", **auth_env},
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

    def commit_time(self, sha: str) -> int:
        """Committer timestamp (unix seconds) of ``sha`` — the tie-breaking order key
        pending recovery uses so refs stranded by one transaction replay oldest-first."""
        return int(self._git("show", "-s", "--format=%ct", sha).strip())

    # ── recovered publishes ──────────────────────────────────────────
    #
    # When recovery cannot fast-forward a stranded publish (main diverged) it
    # re-commits the same content as a NEW sha. The ORIGINAL sha was already handed
    # to the client, may already be a pointer's to_sha, and is selectable as a
    # validated commit — so it must stay reachable forever: a bare repo has no
    # reflog, and replicas only ever mirror-fetch ``refs/*``. The anchor lives under
    # refs/incant/recovered/ (mirror-pushed/fetched like every other ref) and is
    # never deleted automatically: it IS the durable identity for anything that
    # already references the sha.

    def recovered_ref(self, draft_id: str) -> str:
        return f"refs/incant/recovered/{draft_id}"

    def anchor_recovered(self, draft_id: str, sha: str) -> str:
        """Pin ``sha`` under refs/incant/recovered/ and return the ref written.

        The first anchor for a draft takes ``recovered/<draft>``. A LATER, different
        sha for the same draft (a replay whose row committed but whose promotion
        lost a race, then diverged again) must not overwrite that identity, so it
        goes to ``recovered/<draft>-<sha7>`` instead. Idempotent for a repeat of the
        same sha."""
        ref = self.recovered_ref(draft_id)
        current = self.recovered_sha(draft_id)
        if current is not None and current != sha:
            ref = f"{ref}-{sha[:7]}"
        self._git("update-ref", ref, sha)
        return ref

    def recovered_sha(self, draft_id: str) -> str | None:
        """The original sha anchored for ``draft_id``, or None if never recovered."""
        proc = subprocess.run(
            ["git", "--git-dir", str(self.repo), "rev-parse", "--verify", "--quiet",
             self.recovered_ref(draft_id)],
            capture_output=True, text=True,
        )
        return proc.stdout.strip() if proc.returncode == 0 else None

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
