"""Backup pusher + serve-replica content follow (§6, §13, §15).

The full node force-pushes its complete ref set to every enabled remote — the
canonical repo's off-site durability. The "queue" is the natural one: commits pile
up between a remote's ``last_pushed_sha`` and main's head, and a push drains it.
Remote down → the queue grows, ``incant_backup_lag_seconds`` rises, nothing else
happens (§6: "nothing user-visible").

The same remotes double as the content-distribution channel: serve replicas
hydrate (mirror-clone) and follow (periodic mirror-fetch) an enabled remote, so a
make-live that references a fresh commit becomes fetchable on every replica within
one fetch interval. Rules propagate via the DB poll; content propagates via this.
"""

from __future__ import annotations

import datetime as dt
import logging
import time
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models
from .store import GitError, GitStore, redact_remote_url, sanitize_remote_error

log = logging.getLogger("incant.backup")


def redact_url(url: str) -> str:
    """Backward-compatible public name for the shared URL sanitizer."""
    return redact_remote_url(url)


@dataclass
class RemoteStatus:
    id: int
    url: str                    # redacted
    enabled: bool
    last_pushed_sha: str | None
    last_push_at: str | None    # isoformat
    pending_commits: int
    lag_seconds: float          # 0.0 when caught up
    error: str | None = None    # last push failure this pass, if any

    def as_dict(self) -> dict:
        return {
            "id": self.id, "url": self.url, "enabled": self.enabled,
            "last_pushed_sha": self.last_pushed_sha, "last_push_at": self.last_push_at,
            "pending_commits": self.pending_commits,
            "lag_seconds": round(self.lag_seconds, 1), "error": self.error,
        }


def _publish_metrics(statuses: list[RemoteStatus]) -> None:
    """Feed the §14 backup gauges. Lazy + defensive import so gitstore never
    hard-depends on the server package (the content.py idiom)."""
    try:
        from ..server.metrics import backup_lag_seconds, backup_queue_depth
        depth = max((s.pending_commits for s in statuses if s.enabled), default=0)
        backup_queue_depth.set(depth)
        for s in statuses:
            if s.enabled:
                backup_lag_seconds.labels(str(s.id)).set(s.lag_seconds)
    except Exception:  # pragma: no cover - metrics are best-effort telemetry
        pass


class BackupPusher:
    """Push-side (full node) and fetch-side (serve replica) remote operations.

    All methods are synchronous and blocking (subprocess git over the network) —
    the server calls them via ``asyncio.to_thread`` from its background loops.
    """

    def __init__(self, git: GitStore, *, known_hosts_path: str | None = None,
                 timeout: float = 60.0) -> None:
        self.git = git
        self.known_hosts_path = known_hosts_path or None
        self.timeout = timeout

    # ── shared ───────────────────────────────────────────────────────

    def _remotes(self, session: Session, *, enabled_only: bool = False) -> list[models.Remote]:
        q = select(models.Remote).order_by(models.Remote.id)
        if enabled_only:
            q = q.where(models.Remote.enabled.is_(True))
        return list(session.execute(q).scalars())

    def _status_of(self, r: models.Remote, *, error: str | None = None) -> RemoteStatus:
        try:
            pending = self.git.commits_ahead(r.last_pushed_sha)
        except GitError:
            pending = 0
        lag = 0.0
        if pending:
            try:
                oldest = self.git.oldest_unpushed_at(r.last_pushed_sha)
                if oldest is not None:
                    lag = max(0.0, time.time() - oldest)
            except GitError:
                pass
        return RemoteStatus(
            id=r.id, url=redact_url(r.url), enabled=r.enabled,
            last_pushed_sha=r.last_pushed_sha,
            last_push_at=r.last_push_at.isoformat() if r.last_push_at else None,
            pending_commits=pending, lag_seconds=lag, error=error,
        )

    # ── push side (full node) ────────────────────────────────────────

    def status(self, session: Session) -> list[RemoteStatus]:
        """Per-remote queue state (no pushes). Also refreshes the backup gauges so
        a status read keeps telemetry honest even between push passes."""
        statuses = [self._status_of(r) for r in self._remotes(session)]
        _publish_metrics(statuses)
        return statuses

    def push_remote(self, session: Session, remote: models.Remote) -> RemoteStatus:
        """Force-push the full ref set to one remote; record the drained queue on
        success. Failures are returned in the status (and logged), never raised —
        a dead remote must not take the loop down (§6)."""
        head = self.git.head()
        try:
            self.git.push_mirror(
                remote.url, ssh_key_path=remote.auth_ref,
                known_hosts_path=self.known_hosts_path, timeout=self.timeout,
            )
        except Exception as exc:  # GitError, subprocess.TimeoutExpired
            safe_error = sanitize_remote_error(str(exc), remote.url)
            log.warning("backup push to remote %d (%s) failed: %s",
                        remote.id, redact_url(remote.url), safe_error)
            return self._status_of(remote, error=safe_error)
        remote.last_pushed_sha = head
        remote.last_push_at = dt.datetime.now(dt.timezone.utc)
        session.flush()
        return self._status_of(remote)

    def push_pending(self, session: Session) -> list[RemoteStatus]:
        """One pass: push every enabled remote that is behind main. Returns the
        post-pass status for ALL remotes and updates the backup gauges."""
        statuses: list[RemoteStatus] = []
        for r in self._remotes(session):
            if not r.enabled:
                statuses.append(self._status_of(r))
                continue
            if r.last_pushed_sha == self.git.head():
                statuses.append(self._status_of(r))
                continue
            statuses.append(self.push_remote(session, r))
        _publish_metrics(statuses)
        return statuses

    # ── fetch side (serve replicas) ──────────────────────────────────

    def fetch_once(self, session: Session) -> bool:
        """Mirror-fetch from the first enabled remote that answers. True on
        success. Replicas call this on a loop; the full node never does."""
        for r in self._remotes(session, enabled_only=True):
            try:
                self.git.mirror_fetch(
                    r.url, ssh_key_path=r.auth_ref,
                    known_hosts_path=self.known_hosts_path, timeout=self.timeout,
                )
                return True
            except Exception as exc:
                safe_error = sanitize_remote_error(str(exc), r.url)
                log.warning("content fetch from remote %d (%s) failed: %s",
                            r.id, redact_url(r.url), safe_error)
        return False

    def hydrate(self, session: Session) -> bool:
        """First boot of a serve replica against an empty volume: mirror-clone from
        the first enabled remote that answers. True iff the repo now exists."""
        if self.git.exists():
            return True
        for r in self._remotes(session, enabled_only=True):
            try:
                self.git.clone_mirror(
                    r.url, ssh_key_path=r.auth_ref,
                    known_hosts_path=self.known_hosts_path, timeout=self.timeout,
                )
                log.info("hydrated content repo from remote %d (%s)",
                         r.id, redact_url(r.url))
                return True
            except Exception as exc:
                safe_error = sanitize_remote_error(str(exc), r.url)
                log.warning("hydration clone from remote %d (%s) failed: %s",
                            r.id, redact_url(r.url), safe_error)
        return False
