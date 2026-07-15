"""Startup reconciliation of git draft refs against DB draft rows.

Draft create/commit/discard mutate git and Postgres in two steps; a failure of the
outer DB transaction after a git mutation (or vice versa) can leave the two out of
sync. Full outbox machinery is out of scope — this is the pragmatic repair, run once
at boot (full mode) before serving is warmed:

  * a draft ref in git (``refs/incant/drafts/*``) with no *live* DB draft row
    (open/approved) → delete the orphan ref;
  * a DB draft row still open/approved whose ref is missing → mark it discarded.

Both directions log, and the sweep emits a one-line summary.
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from .. import models
from ..gitstore import GitStore

log = logging.getLogger("incant.reconcile")

# Draft statuses that still "own" a git ref (work in flight).
_LIVE_STATUSES = ("open", "approved")


@dataclass
class ReconcileResult:
    orphan_refs_deleted: int
    drafts_discarded: int
    scanned_refs: int
    scanned_drafts: int

    def summary(self) -> str:
        return (
            f"reconcile: deleted {self.orphan_refs_deleted} orphan draft ref(s), "
            f"discarded {self.drafts_discarded} refless draft(s) "
            f"(scanned {self.scanned_refs} ref(s), {self.scanned_drafts} live draft(s))"
        )


def reconcile_drafts(session: Session, git: GitStore) -> ReconcileResult:
    """Repair git↔DB draft drift. Caller owns the transaction (commit after)."""
    live_drafts = session.execute(
        select(models.Draft).where(models.Draft.status.in_(_LIVE_STATUSES))
    ).scalars().all()
    live_ids = {d.id for d in live_drafts}

    # Direction 1: git ref without a live DB row → orphan, delete it.
    orphan_deleted = 0
    ref_ids = git.list_draft_refs()
    for draft_id in ref_ids:
        if draft_id not in live_ids:
            git.delete_draft(draft_id)
            orphan_deleted += 1
            log.warning("reconcile: deleted orphan draft ref %s (no live DB row)", draft_id)

    # Direction 2: live DB row whose ref is missing → mark discarded.
    discarded = 0
    for d in live_drafts:
        if not git.draft_ref_exists(d.id):
            d.status = "discarded"
            discarded += 1
            log.warning("reconcile: discarded draft %s (%s) — git ref missing",
                        d.id, d.prompt_id)

    result = ReconcileResult(
        orphan_refs_deleted=orphan_deleted,
        drafts_discarded=discarded,
        scanned_refs=len(ref_ids),
        scanned_drafts=len(live_drafts),
    )
    log.info(result.summary())
    return result


def sweep_expired_sessions(session: Session) -> int:
    """Delete browser sessions whose absolute expiry has passed. Run at boot and then
    hourly by the background loop. Caller owns the transaction. Returns the number of
    rows deleted and emits a single log line only when something was actually deleted."""
    now = dt.datetime.now(dt.timezone.utc)
    deleted = session.execute(
        delete(models.Session).where(models.Session.expires_at <= now)
    ).rowcount or 0
    if deleted:
        log.info("session sweep: deleted %d expired session(s)", deleted)
    return deleted


# ── main-commit orphan detection (detect-and-log, never auto-repair) ─────────

@dataclass
class MainReconcileResult:
    """Drift between refs/heads/main version files and DB Version + CommitValidation rows."""

    git_orphans: int        # a version file on main with no DB Version row
    missing_files: int      # a DB Version row with no file on main
    unvalidated_tips: int   # a version file whose tip commit has no CommitValidation row
    scanned_files: int
    scanned_versions: int

    def summary(self) -> str:
        return (
            f"main reconcile: {self.git_orphans} orphan main commit(s) (git file, no DB "
            f"row), {self.missing_files} DB version(s) with no file on main, "
            f"{self.unvalidated_tips} unvalidated tip commit(s) (on main, no "
            f"CommitValidation row) (scanned {self.scanned_files} file(s), "
            f"{self.scanned_versions} version row(s))"
        )


def _parse_version_path(path: str) -> tuple[str, int] | None:
    """`<prompt_id>/v<N>.j2` → (prompt_id, N); None for anything else."""
    if not path.endswith(".j2"):
        return None
    prompt_id, _, vpart = path[:-len(".j2")].rpartition("/")
    if not prompt_id or not vpart.startswith("v"):
        return None
    try:
        return prompt_id, int(vpart[1:])
    except ValueError:
        return None


def reconcile_main_commits(session: Session, git: GitStore) -> MainReconcileResult:
    """Detect (and LOUDLY log — never auto-repair) drift between the git ``main`` tree
    and the DB control-plane rows (``Version`` + ``CommitValidation``).

    Publishing is a two-step git-then-DB write (DESIGN.md §3 "git owns content, the DB
    owns state"; §5 "Validation first" — only validated SHAs may ever serve), and two
    distinct failures leave git ahead of the DB. One boot sweep catches both:

    * **Orphan** — a version file on ``refs/heads/main`` with no ``Version`` row: a commit
      landed but its control-plane transaction never did, so the whole version is unknown
      to the DB.
    * **Unvalidated tip** — a version file whose *tip* commit SHA has no
      ``CommitValidation`` row. ``RegistryService.commit_draft`` advances ``main``
      (``commit_version``) and only *then* stages the ``CommitValidation`` row + the
      version/draft-status flip in the outer transaction. If that transaction fails after
      ``main`` already moved, the validation row rolls back with it, leaving a commit on
      ``main`` that no ``CommitValidation`` row describes. This is precisely the case the
      orphan check MISSES: when the version already existed (editing a live version — the
      common case), the ``Version`` row is still present from the earlier publish, so only
      the missing *validation* row betrays the drift. Serving keeps quietly using the last
      VALIDATED SHA while ``main`` shows newer, unvalidated content.

    We do NOT auto-repair either: auto-registering could resurrect a deliberately
    rolled-back commit — a human decides (re-validate/re-publish, or roll back the git
    commit). The reverse (a ``Version`` row whose file is missing from ``main``) is also
    surfaced. Read-only.

    No false positives on legitimately row-less commits: ``GitStore.init`` seeds an empty
    root commit that carries no version files (so it is never the tip of a ``.j2`` path),
    and every seeded/authored version lands through ``commit_draft``, which records a
    ``CommitValidation`` row (status ``valid`` OR ``invalid``) in the same transaction —
    so a version-file tip with *no* row at all is definitive drift, not a bootstrap
    artefact."""
    db_versions = {
        (v.prompt_id, v.number)
        for v in session.execute(select(models.Version)).scalars()
    }
    # Every commit that legitimately lands a version file on main is recorded per SHA by
    # commit_draft (whether validation passed or failed), so a tip SHA absent from this
    # set is definitive drift — see the docstring on why init/seed never false-positive.
    cv_shas = set(session.execute(select(models.CommitValidation.sha)).scalars())

    git_files: dict[tuple[str, int], str] = {}
    for path in git.list_files(ref="main", suffix=".j2"):
        parsed = _parse_version_path(path)
        if parsed is not None:
            git_files[parsed] = path

    git_orphans = 0
    unvalidated_tips = 0
    # One history call per file feeds BOTH checks: the tip SHA (for the orphan log) and
    # the tip-has-a-CommitValidation-row lookup.
    for (prompt_id, version), path in git_files.items():
        hist = git.history(path, limit=1, ref="main")
        tip_sha = hist[0].sha if hist else None

        if (prompt_id, version) not in db_versions:
            log.warning(
                "main reconcile: ORPHAN commit — %s v%d exists on refs/heads/main "
                "(sha %s) with NO DB Version row. A commit landed but its control-plane "
                "transaction did not. NOT auto-registering (a human must decide whether "
                "to register or roll it back).",
                prompt_id, version, tip_sha or "?",
            )
            git_orphans += 1

        if tip_sha is not None and tip_sha not in cv_shas:
            log.warning(
                "main reconcile: UNVALIDATED tip — %s v%d is at commit %s on "
                "refs/heads/main with NO CommitValidation row. commit_version advanced "
                "main but the outer control-plane transaction (validation record + "
                "version/draft-status flip) rolled back afterwards, so this commit was "
                "never validated or recorded. Serving keeps using the last VALIDATED SHA "
                "while git main shows this newer content. NOT auto-repairing — a human "
                "must re-validate/re-publish this content or roll back the git commit.",
                prompt_id, version, tip_sha,
            )
            unvalidated_tips += 1

    missing_files = 0
    for prompt_id, version in db_versions:
        if (prompt_id, version) not in git_files:
            log.warning(
                "main reconcile: DB Version %s v%d has NO file on refs/heads/main "
                "(expected %s/v%d.j2) — control-plane state references content missing "
                "from git history.",
                prompt_id, version, prompt_id, version,
            )
            missing_files += 1

    result = MainReconcileResult(
        git_orphans=git_orphans, missing_files=missing_files,
        unvalidated_tips=unvalidated_tips,
        scanned_files=len(git_files), scanned_versions=len(db_versions),
    )
    log.info(result.summary())
    return result
