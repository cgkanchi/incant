"""Reconciliation of git draft refs against DB draft rows, and of the git ``main`` tree
against the DB control plane.

Draft create/commit/discard mutate git and Postgres in two steps; a failure of the
outer DB transaction after a git mutation (or vice versa) can leave the two out of
sync. Full outbox/saga machinery is deliberately OUT OF SCOPE — a durable log of
intended git mutations, two-phase-committed against Postgres, is more moving parts
(and more failure modes) than this system's drift surface warrants. Instead we make two
pragmatic guarantees the pieces below implement, and treat anything they can't prevent
as *detectable, non-destructive residue* rather than lost work:

  1. **Ordering** — the mutation that DESTROYS recoverable state runs LAST, after the DB
     transaction it depends on has committed. Concretely: ``RegistryService.commit_draft``
     no longer deletes the draft ref mid-transaction; it defers the delete to an
     ``after_commit`` hook, so a failed outer commit leaves the draft ref AND its open row
     intact (fully recoverable, still editable) with only an unvalidated `main` tip as
     residue. This turns "publish strands user work" into "publish leaves a re-runnable
     draft".
  2. **Surfacing** — residue is DETECTED and made loud (logs + metrics + /healthz), never
     silently swallowed and never auto-repaired: auto-registering an orphan or fabricating
     a validation row could resurrect a deliberately rolled-back commit. A human decides.

The **draft sweep** (``reconcile_drafts``, run once at boot in full mode before serving
warms) repairs the two convergent draft states:

  * a draft ref in git (``refs/incant/drafts/*``) with no *live* DB draft row
    (open/approved) → delete the orphan ref (this is where a leftover ref from a
    now-``committed`` draft, or a discarded draft, is cleaned up);
  * a DB draft row still open/approved whose ref is missing → mark it discarded.

The **main sweep** (``reconcile_main_commits``, run at boot AND on an interval —
``INCANT_RECONCILE_INTERVAL_SECONDS`` — so post-boot drift is caught too) is pure
detection: it reports orphan commits, DB versions with no file, and unvalidated `main`
tips left by a rolled-back ``commit_draft`` outer transaction. Its result is recorded on
the AppContext, exported as ``incant_reconcile_*`` gauges, and folded into /healthz —
WITHOUT flipping readiness, because a drifted node still serves correctly from the last
validated SHAs (§3 "git owns content, the DB owns state"; §5 "Validation first").

Every direction logs, and each sweep emits a one-line summary.
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass

from sqlalchemy import delete, event, func, select
from sqlalchemy.orm import Session

from .. import models
from ..gitstore import GitStore
from ..gitstore.store import ConcurrentUpdate
from ..targeting.service import bump_content_version

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


# ── content adoption (bootstrap-from-remote / manual restore) ────────────────

@dataclass
class AdoptResult:
    project: str | None
    prompts: int
    versions: int
    valid_tips: int
    invalid_tips: int

    def summary(self) -> str:
        return (f"adopt: project {self.project!r}, {self.prompts} prompt(s), "
                f"{self.versions} version(s), {self.valid_tips} valid / "
                f"{self.invalid_tips} invalid tip(s)")


def adopt_content_tree(session: Session, git: GitStore) -> AdoptResult | None:
    """Rebuild the content registry from a populated repo into an EMPTY database —
    the §6 promise ("the version registry rebuilds from the tree + trailers") made
    executable. This is bootstrap-from-remote and manual volume restore, NOT drift
    repair: it runs only when the prompts table is empty (a live system's git↔DB
    drift stays detect-only in ``reconcile_main_commits``). Returns None when it
    did not apply.

    Rebuilds: the single project (from the tree's top directory — a repo with
    several top directories predates one-project-per-deployment and is refused),
    prompt and version rows, and a fresh ``CommitValidation`` for each version's
    TIP (the static checks against the adopted tree: compile + include resolution +
    cycle check; test contexts don't exist yet, so no render check). Targeting,
    users, RBAC, and audit live in Postgres — restoring those needs the DB backup.
    """
    from ..gitstore import validate_source

    has_prompts = session.execute(select(models.Prompt.id).limit(1)).first()
    if has_prompts is not None:
        return None
    files: dict[tuple[str, int], str] = {}
    for path in git.list_files(ref="main", suffix=".j2"):
        parsed = _parse_version_path(path)
        if parsed is not None:
            files[parsed] = path
    if not files:
        return None

    projects = sorted({pid.split("/", 1)[0] for pid, _ in files})
    if len(projects) > 1:
        raise RuntimeError(
            f"cannot adopt: the repo contains several top-level projects {projects} "
            "— this deployment hosts exactly one. Split the repo (one deployment "
            "each) or consolidate under a single top directory first."
        )
    project_id = projects[0]
    session.add(models.Project(id=project_id, name=project_id))
    session.flush()

    prompt_ids = sorted({pid for pid, _ in files})
    newest = {pid: max(v for p, v in files if p == pid) for pid in prompt_ids}
    for pid in prompt_ids:
        session.add(models.Prompt(id=pid, project_id=project_id))
    session.flush()

    known = set(prompt_ids)

    def include_source(target: str) -> str | None:
        top = newest.get(target)
        return git.read(f"{target}/v{top}.j2") if top else None

    valid = invalid = versions = 0
    for (pid, number), path in sorted(files.items()):
        session.add(models.Version(prompt_id=pid, number=number, created_by="adopted"))
        versions += 1
        source = git.read(path) or ""
        hist = git.history(path, limit=1, ref="main")
        tip_sha = hist[0].sha if hist else None
        if tip_sha is None:  # pragma: no cover - a listed file always has a commit
            continue
        result = validate_source(source, pid, is_known_prompt=lambda p: p in known,
                                 include_source=include_source, test_render=None)
        session.add(models.CommitValidation(
            sha=tip_sha, blob_sha=git.blob_sha(path, ref="main") or "", path=path,
            prompt_id=pid, version_number=number,
            status=result.status, error=result.error,
            extracted_variables=result.extracted_variables,
        ))
        valid += int(result.ok)
        invalid += int(not result.ok)
    session.flush()
    if versions:
        # New Version + CommitValidation rows are snapshot content: bump so a warm
        # replica (the periodic reconcile runs long after boot) rebuilds on its poll.
        bump_content_version(session)

    out = AdoptResult(project=project_id, prompts=len(prompt_ids), versions=versions,
                      valid_tips=valid, invalid_tips=invalid)
    log.warning("adopted content from the repo tree — %s. Targeting, users, and "
                "RBAC are NOT in git; restore the Postgres backup for those.",
                out.summary())
    return out


# ── pending-promotion recovery (staged publishes interrupted by a crash) ─────

@dataclass
class PendingRecoveryResult:
    promoted: int      # DB committed, main was at the staged parent → fast-forwarded
    rebased: int       # DB committed, main diverged → replay staged; lands at commit
    discarded: int     # DB never committed → staging residue deleted, draft intact

    def summary(self) -> str:
        return (f"pending recovery: promoted {self.promoted}, rebased {self.rebased}, "
                f"discarded {self.discarded} staged publish(es)")


def _validation_for(session: Session, sha: str | None) -> models.CommitValidation | None:
    if sha is None:
        return None
    return session.execute(
        select(models.CommitValidation).where(models.CommitValidation.sha == sha)
    ).scalars().first()


def _ordered_pending_refs(git: GitStore) -> list[tuple[str, str]]:
    """Stranded pending refs in REPLAY order: parents before children, oldest first.

    ``list_pending_refs`` yields refname order, which means nothing here: one
    transaction stages several publishes as a CHAIN (each onto the previous staged
    sha — see ``commit_draft``), and replaying a chain out of order would drive every
    later link down the diverged branch — a synthetic sha per link, the original shas
    orphaned — when the whole chain could simply fast-forward. So: roots (a parent
    that is not itself pending) by commit time, each followed depth-first by the
    pending commits built on it.
    """
    rows = git.list_pending_refs()
    by_sha = {sha: draft_id for draft_id, sha in rows}
    parent = {sha: git.commit_parent(sha) for sha in by_sha}
    order_key = {sha: (git.commit_time(sha), sha) for sha in by_sha}
    children: dict[str, list[str]] = {}
    roots: list[str] = []
    for sha in by_sha:
        if parent[sha] in by_sha:
            children.setdefault(parent[sha], []).append(sha)
        else:
            roots.append(sha)

    ordered: list[tuple[str, str]] = []

    def walk(sha: str) -> None:
        ordered.append((by_sha[sha], sha))
        for child in sorted(children.get(sha, ()), key=order_key.__getitem__):
            walk(child)

    for root in sorted(roots, key=order_key.__getitem__):
        walk(root)
    return ordered


def _revalidate_replay(session: Session, git: GitStore, cv: models.CommitValidation,
                       content: str, tree_sha: str) -> tuple[str, str | None, dict]:
    """(status, error, extracted_variables) for ``content`` replayed into the tree at
    ``tree_sha``.

    Validation is NOT a pure function of the content: the static cycle check walks
    the include graph of the tree it lands in, and the render check resolves includes
    through the default environment's live pointers. Both can differ between the
    original staging and now — a fragment edited while the publish was stranded can
    close a cycle. The static checks are cheap and need only git + the version
    registry, so they run again against the FINAL tree. The render check needs the
    default-env snapshot (and the environment name) that recovery does not have at
    boot, so its verdict is INHERITED from the original sha; the log line and the
    replay's commit message say so. Conservative merge: a failure on either side is
    a failure — the original verdict was what the client saw for this content, and a
    replay never silently upgrades it."""
    from ..gitstore import validate_source

    def include_source(target: str) -> str | None:
        top = session.execute(
            select(func.max(models.Version.number)).where(models.Version.prompt_id == target)
        ).scalar()
        return git.read(f"{target}/v{top}.j2", ref=tree_sha) if top else None

    static = validate_source(
        content, cv.prompt_id,
        is_known_prompt=lambda pid: session.get(models.Prompt, pid) is not None,
        include_source=include_source, test_render=None,
    )
    if not static.ok:
        return "invalid", static.error, static.extracted_variables
    return cv.status, cv.error, static.extracted_variables


def recover_pending_promotions(session: Session, git: GitStore) -> PendingRecoveryResult:
    """Converge staged publishes that a crash stranded between their two phases.

    ``commit_draft`` stages each publish on ``refs/incant/pending/<draft>`` and
    promotes it to main only after the control-plane transaction commits. A crash in
    between leaves the pending ref; the DB is the arbiter of which side of the line
    the publish died on — a ``CommitValidation`` row for the staged sha (or, see
    below, for its recovered anchor) means the transaction COMMITTED and the
    promotion is owed; no row means it never did, so the staging residue is deleted
    and the draft ref + still-open row remain the recoverable source of truth.

    An owed promotion fast-forwards main when main is still at the staged parent (or
    already contains the sha — a double-run). When main has DIVERGED the same content
    is replayed as a fresh commit, and that replay follows the live publish protocol
    exactly rather than a shortcut:

    * **The original sha stays reachable.** It was already returned to the client,
      may already be a pointer's ``to_sha``, and is selectable as a validated commit;
      a bare repo has no reflog and replicas only mirror-fetch ``refs/*``. So before
      anything else it is anchored at ``refs/incant/recovered/<draft>`` — never
      deleted automatically: that ref IS the durable identity for whatever already
      references the sha (see ``GitStore.anchor_recovered``).
    * **The DB row lands before git moves.** The replay is staged on the draft's
      pending ref (``commit_version_pending``, chained onto anything already staged
      in this transaction), its ``CommitValidation`` row is added to the CALLER's
      transaction, and main moves + the pending/draft refs are dropped only in an
      ``after_commit`` hook — the same mechanism as ``commit_draft``. A rollback or
      crash after staging leaves the pending ref holding a row-less replay and the
      anchor holding the committed original: that is the "no row, but anchored"
      clause of the arbiter, and the next pass replays again from the anchor.
      Nothing is ever left as an unvalidated tip on main.
    * **Stranded refs replay in chain order** (``_ordered_pending_refs``), so a
      multi-publish transaction that died fast-forwards link by link instead of
      needlessly diverging. Once a replay is staged in a pass, every later ref is
      replayed onto it too — a fast-forward would move main out from under the
      staged replay's CAS.
    * **The replayed content is re-validated statically against the final tree**;
      the render verdict is inherited (``_revalidate_replay``).

    Runs at boot and on the reconcile interval. Holds the publish lock from the
    first ref through the deferred promotion so it never races a live publish.
    Caller owns the transaction."""
    promoted = rebased = discarded = 0
    lock = git.publish_lock
    lock.acquire()
    released = False

    def _release_lock() -> None:
        nonlocal released
        if not released:
            released = True
            lock.release()

    # Publishes staged but not yet promoted in THIS transaction (shared with
    # commit_draft so a publish in the same transaction chains onto our replays).
    chain: list[str] = session.info.setdefault("incant_pending_chain", [])
    staged: list[tuple[str, str, str, str]] = []  # (draft_id, original, new_sha, parent)
    try:
        for draft_id, sha in _ordered_pending_refs(git):
            cv = _validation_for(session, sha)
            original = sha
            if cv is None:
                # No row for the staged sha: plain residue of a transaction that
                # never committed — unless an anchor says this pending ref holds a
                # replay whose own transaction failed; then the anchored original
                # is the committed identity and the replay is still owed.
                original = git.recovered_sha(draft_id)
                cv = _validation_for(session, original)
                if cv is None:
                    git.delete_pending(draft_id)
                    discarded += 1
                    log.warning(
                        "pending recovery: discarded staged publish %s for draft %s — its "
                        "control-plane transaction never committed; the draft is intact.",
                        sha, draft_id,
                    )
                    continue
            elif git.is_ancestor(sha):
                git.delete_pending(draft_id)
                git.delete_draft(draft_id)
                continue  # promotion already happened (crash after CAS, before cleanup)
            else:
                parent = git.commit_parent(sha)
                if not chain and parent is not None and git.head() == parent:
                    git.promote_pending(sha, parent)
                    git.delete_pending(draft_id)
                    git.delete_draft(draft_id)
                    promoted += 1
                    log.warning(
                        "pending recovery: promoted staged publish %s for draft %s "
                        "(crash between DB commit and ref promotion).", sha, draft_id,
                    )
                    continue

            # Diverged (or queued behind a replay staged earlier in this transaction).
            content = git.read(cv.path, ref=original)
            if content is None:  # pragma: no cover - staged object vanished
                log.error("pending recovery: staged content for %s unreadable; "
                          "leaving the ref for a human", draft_id)
                continue
            anchor_ref = git.anchor_recovered(draft_id, original)
            new_sha, parent = git.commit_version_pending(
                cv.prompt_id, cv.version_number, content,
                author_name="Incant recovery", author_email="incant@localhost",
                message=(f"recovered publish (draft {draft_id}): replays {original} atop a "
                         "diverged main; static checks re-run, render verdict inherited"),
                draft_id=draft_id, parent=chain[-1] if chain else None,
            )
            chain.append(new_sha)
            status, error, variables = _revalidate_replay(session, git, cv, content, new_sha)
            if status != cv.status:
                log.warning(
                    "pending recovery: replay of %s for draft %s is %s on the current tree "
                    "(originally %s): %s", original, draft_id, status, cv.status, error,
                )
            # A deterministic replay (same tree, parent, message, second) can reproduce
            # a sha whose row already committed on an earlier pass; never duplicate it.
            if _validation_for(session, new_sha) is None:
                session.add(models.CommitValidation(
                    sha=new_sha, blob_sha=git.blob_sha(cv.path, ref=new_sha) or "",
                    path=cv.path, prompt_id=cv.prompt_id, version_number=cv.version_number,
                    status=status, error=error, extracted_variables=variables,
                ))
            staged.append((draft_id, original, new_sha, parent))
            rebased += 1
            log.warning(
                "pending recovery: main diverged from staged publish %s for draft %s — "
                "replay staged as %s (original anchored at %s); main moves when the "
                "transaction commits.", original, draft_id, new_sha, anchor_ref,
            )
    except BaseException:
        _release_lock()
        raise

    if rebased:
        # Replayed commits are new validated SHAs — content a warm replica must learn.
        bump_content_version(session)
    if not staged:
        _release_lock()
    else:
        def _promote(_session: Session) -> None:
            try:
                for draft_id, original, new_sha, parent in staged:
                    try:
                        git.promote_pending(new_sha, parent)
                    except ConcurrentUpdate:
                        if not git.is_ancestor(new_sha):
                            log.critical(
                                "pending recovery: could not promote replay %s for draft %s "
                                "— main moved under it. The row is committed and the "
                                "pending + recovered refs are kept; the next pass replays "
                                "again.", new_sha, draft_id,
                            )
                            break  # every later link chains onto this one
                    git.delete_pending(draft_id)
                    git.delete_draft(draft_id)
                    log.warning(
                        "pending recovery: replayed publish %s for draft %s promoted as %s.",
                        original, draft_id, new_sha,
                    )
            except Exception:  # pragma: no cover - best-effort post-commit cleanup
                log.warning("pending recovery: post-commit promotion hit an error; the "
                            "next pass converges it.", exc_info=True)
            finally:
                for _, _, new_sha, _ in staged:
                    if new_sha in chain:
                        chain.remove(new_sha)
                _release_lock()

        def _abandon(_session: Session) -> None:
            # Nothing to undo in git: the pending refs hold row-less replays and the
            # anchors hold the committed originals — exactly what the "no row, but
            # anchored" clause re-derives on the next pass.
            for _, _, new_sha, _ in staged:
                if new_sha in chain:
                    chain.remove(new_sha)
            _release_lock()

        event.listen(session, "after_commit", _promote, once=True)
        event.listen(session, "after_rollback", _abandon, once=True)

    result = PendingRecoveryResult(promoted=promoted, rebased=rebased, discarded=discarded)
    if promoted or rebased or discarded:
        log.info(result.summary())
    return result


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
