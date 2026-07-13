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
    """Delete browser sessions whose absolute expiry has passed. Run once at boot
    (piggybacks the draft reconcile sweep). Caller owns the transaction. Returns the
    number of rows deleted and emits one log line."""
    now = dt.datetime.now(dt.timezone.utc)
    deleted = session.execute(
        delete(models.Session).where(models.Session.expires_at <= now)
    ).rowcount or 0
    log.info("session sweep: deleted %d expired session(s)", deleted)
    return deleted
