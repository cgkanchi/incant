"""RegistryService — authoring: versions, drafts, reviews, commits, refinements.

Ties git (content) + DB (state) + validation together. Every commit is validated
on landing and recorded per SHA; only validated SHAs can ever be referenced by a
pointer or rule.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass

from sqlalchemy import event, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .. import models
from ..core import ExtractedVars, extract
from ..core.ids import validate_project_id, validate_prompt_id
from ..gitstore import ContentStore, GitError, GitStore, validate_source
from ..gitstore.store import ConcurrentUpdate
from ..targeting.service import bump_content_version

log = logging.getLogger("incant.registry")


class RegistryConflict(Exception):
    """A registry change that would break something currently relied on (HTTP 409):
    archiving an environment's default version."""


class RegistryError(Exception):
    pass


class ReviewRequired(RegistryError):
    pass


class StaleDraftWrite(RegistryError):
    """A draft-content write was based on a draft_sha that is no longer current
    (optimistic-concurrency conflict — Finding 2). Carries the current draft tip and
    its content so the caller can surface a 409 and let the client recover/rebase."""

    def __init__(self, message: str = "stale draft write", *,
                 current_sha: str, current_content: str) -> None:
        super().__init__(message)
        self.current_sha = current_sha
        self.current_content = current_content


class ConcurrencyError(RegistryError):
    def __init__(self, message: str, *, base_sha: str | None = None,
                 current_sha: str | None = None) -> None:
        super().__init__(message)
        # The publisher must see the intervening diff (base -> current tip) to
        # re-confirm; carry the endpoints so the handler can compute it.
        self.base_sha = base_sha
        self.current_sha = current_sha


class DraftDiverged(ConcurrencyError):
    """The draft's git ref no longer agrees with the DB's recorded revision
    (``Draft.draft_sha``). Staged draft writes advance the git ref before the outer
    DB transaction commits, so a failed outer commit can leave git holding NEWER
    text than the revision the DB (and therefore the recorded approvals) describe.
    Committing in that state would publish bytes nobody reviewed — commit_draft
    refuses instead, and the caller re-opens/re-saves the draft to re-sync both
    sides. base_sha stays None on purpose: the HTTP handler's diff rendering is for
    version-file conflicts, not for this git/DB disagreement."""


@dataclass
class CommitOutcome:
    sha: str
    blob_sha: str
    version_number: int
    validation: dict


class RegistryService:
    def __init__(self, session: Session, git: GitStore, content: ContentStore,
                 default_env: str = "prod", actor: str = "system") -> None:
        self.s = session
        self.git = git
        self.content = content
        self.default_env = default_env
        self.actor = actor

    # ── projects & prompts ───────────────────────────────────────────

    def ensure_project(self, project_id: str, review_policy: int = 0,
                       allow_self_review: bool = True) -> models.Project:
        """The deployment's project — exactly ONE per database. The first call
        names it (setup screen, seed, or the first prompt's prefix); every later
        call must match. Multi-project is deliberately a multi-deployment (and,
        later, a schema-sharding) story, not an in-database one — it keeps RBAC,
        keys, and the library free of a cross-project mental model."""
        try:
            validate_project_id(project_id)
        except ValueError as exc:
            raise RegistryError(str(exc)) from exc
        p = self.s.get(models.Project, project_id)
        if p is None:
            existing = self.s.execute(select(models.Project)).scalars().first()
            if existing is not None:
                raise RegistryError(
                    f"this deployment's project is {existing.id!r} — prompt ids must "
                    f"start with {existing.id!r}/. One project per deployment; run "
                    "another instance (or database) for another project."
                )
            p = models.Project(id=project_id, name=project_id, review_policy=review_policy,
                               allow_self_review=allow_self_review)
            self.s.add(p)
            self.s.flush()
        return p

    def create_prompt(self, prompt_id: str, description: str = "") -> models.Prompt:
        # The one grammar (incant.core.ids), enforced here as well as in the mgmt schema
        # so seed/CLI callers can't create a row for a path git will refuse to write.
        try:
            validate_prompt_id(prompt_id)
        except ValueError as exc:
            raise RegistryError(str(exc)) from exc
        if self.s.get(models.Prompt, prompt_id):
            raise RegistryError(f"prompt {prompt_id!r} already exists")
        project_id = prompt_id.split("/", 1)[0]
        self.ensure_project(project_id)
        p = models.Prompt(id=prompt_id, project_id=project_id, description=description)
        self.s.add(p)
        self.s.flush()
        return p

    def prompt_exists(self, prompt_id: str) -> bool:
        return self.s.get(models.Prompt, prompt_id) is not None

    def list_prompts(self) -> list[models.Prompt]:
        return list(self.s.execute(select(models.Prompt).order_by(models.Prompt.id)).scalars())

    def get_versions(self, prompt_id: str) -> list[models.Version]:
        return list(self.s.execute(
            select(models.Version)
            .where(models.Version.prompt_id == prompt_id)
            .order_by(models.Version.number.desc())
        ).scalars())

    def next_version_number(self, prompt_id: str) -> int:
        nums = [v.number for v in self.get_versions(prompt_id)]
        return (max(nums) + 1) if nums else 1

    def _ensure_version_row(self, prompt_id: str, number: int, created_by: str) -> models.Version:
        v = self.s.execute(
            select(models.Version).where(
                models.Version.prompt_id == prompt_id, models.Version.number == number
            )
        ).scalar_one_or_none()
        if v is None:
            v = models.Version(prompt_id=prompt_id, number=number, created_by=created_by)
            self.s.add(v)
            self.s.flush()
        elif v.status == "archived":
            raise RegistryError(
                f"version {number} of {prompt_id!r} is archived and accepts no new commits"
            )
        return v

    def update_version(
        self, prompt_id: str, number: int, *,
        notes: str | None = None, status: str | None = None,
    ) -> models.Version:
        v = self.s.execute(select(models.Version).where(
            models.Version.prompt_id == prompt_id,
            models.Version.number == number,
        ).with_for_update()).scalar_one_or_none()
        if v is None:
            raise RegistryError(f"unknown version {number} for prompt {prompt_id!r}")
        if notes is not None:
            v.notes = notes
        if status is not None:
            if status not in ("active", "archived"):
                raise RegistryError(f"invalid version status {status!r}")
            if status == "archived" and v.status != "archived":
                # Archived versions do not serve (§5). The environment default has no
                # fallback, so archiving it would take the prompt down — refuse.
                envs = sorted(self.s.execute(select(models.EnvDefault.environment_id).where(
                    models.EnvDefault.prompt_id == prompt_id,
                    models.EnvDefault.version_number == number,
                )).scalars())
                if envs:
                    raise RegistryConflict(
                        f"v{number} of {prompt_id!r} is the environment default in "
                        f"{', '.join(envs)} — point the default at another version before "
                        "archiving")
            v.status = status
        self.s.flush()
        if status is not None:
            # Status lives in every environment's snapshot (VersionInfo), so a change
            # must propagate like any targeting change: bump each environment's
            # rules_version with a "version" revision so replicas rebuild within the
            # poll interval and pin.rules_version replay reconstructs the change.
            from ..targeting.service import TargetingService
            ts = TargetingService(self.s, self.actor)
            # id order: the global lock order shared with every other multi-environment
            # locker (bump_content_version, auto_advance_tips) — see bump_content_version.
            for env_id in self.s.execute(
                select(models.Environment.id).order_by(models.Environment.id)
            ).scalars().all():
                ts._bump(ts._env(env_id), "version",
                         {"prompt_id": prompt_id, "version": number, "status": v.status})
        return v

    @staticmethod
    def _principal_key(principal_id: str | None, display_name: str) -> str:
        # Direct library callers predate authenticated identities. Keep them
        # deterministic without confusing a display name for a real principal ID.
        return principal_id or f"legacy:{display_name}"

    # ── drafts ───────────────────────────────────────────────────────

    def create_draft(
        self,
        prompt_id: str,
        *,
        version_number: int | None = None,
        seed_from_version: int | None = None,
        seed_from_sha: str | None = None,
        author: str = "",
        author_principal_id: str | None = None,
        title: str = "",
        content: str | None = None,
    ) -> models.Draft:
        """Open a draft. If ``version_number`` is None a new version is allocated.

        Initial content = explicit ``content``, else the seed version's current text,
        else empty.
        """

        if not self.prompt_exists(prompt_id):
            raise RegistryError(f"unknown prompt {prompt_id!r}")

        new_version = version_number is None
        if new_version:
            version_number = self.next_version_number(prompt_id)
        else:
            version = self.s.execute(select(models.Version).where(
                models.Version.prompt_id == prompt_id,
                models.Version.number == version_number,
            )).scalar_one_or_none()
            if version is not None and version.status == "archived":
                raise RegistryError(
                    f"version {version_number} of {prompt_id!r} is archived and accepts no drafts"
                )

        if content is None:
            if seed_from_version is not None:
                path = f"{prompt_id}/v{seed_from_version}.j2"
                # seed_from_sha pins WHICH revision of that version seeds the draft
                # (the UI passes the live sha for new versions — published content,
                # not unpublished tip edits). Absent => the tip, as before.
                content = (self.git.read(path, ref=seed_from_sha) if seed_from_sha
                           else self.git.read(path)) or ""
            elif not new_version:
                content = self.git.read(f"{prompt_id}/v{version_number}.j2") or ""
            else:
                content = ""

        base_sha = self.git.head()
        draft_id = "d_" + uuid.uuid4().hex[:8]
        # DB-first (Git/DB reconciliation): insert the draft row before the git ref
        # exists, so a DB failure rolls the row back with no git ref stranded. The git
        # write is compensated (ref deleted) if it fails mid-flight; a failure of the
        # *outer* commit after the ref lands is repaired by the startup sweep
        # (reconcile_drafts).
        d = models.Draft(
            id=draft_id, prompt_id=prompt_id,
            version_number=version_number,
            base_sha=base_sha, git_ref=self.git.draft_ref(draft_id),
            draft_sha=None, title=title, author=author, status="open",
            author_principal_id=self._principal_key(author_principal_id, author),
        )
        self.s.add(d)
        self.s.flush()
        try:
            draft_sha = self.git.write_draft(
                draft_id, prompt_id, version_number, content, base_sha=base_sha,
                author_name=author or "draft",
            )
        except Exception:
            self.git.delete_draft(draft_id)  # compensate a partial ref
            raise
        d.draft_sha = draft_sha
        self.s.flush()
        return d

    def get_draft(self, draft_id: str) -> models.Draft:
        d = self.s.get(models.Draft, draft_id)
        if d is None:
            raise RegistryError(f"unknown draft {draft_id!r}")
        return d

    def _locked_draft(self, draft_id: str) -> models.Draft:
        d = self.s.execute(
            select(models.Draft).where(models.Draft.id == draft_id).with_for_update()
        ).scalar_one_or_none()
        if d is None:
            raise RegistryError(f"unknown draft {draft_id!r}")
        return d

    @staticmethod
    def _require_draft_status(d: models.Draft, allowed: tuple[str, ...], action: str) -> None:
        if d.status not in allowed:
            raise RegistryError(f"cannot {action} a {d.status} draft")

    def draft_content(self, draft_id: str) -> str:
        d = self.get_draft(draft_id)
        return self.git.read_draft(draft_id, d.prompt_id, d.version_number) or ""

    def put_draft_content(self, draft_id: str, content: str, author: str = "",
                          base_revision: str | None = None) -> ExtractedVars:
        d = self._locked_draft(draft_id)
        self._require_draft_status(d, ("open", "approved"), "edit")
        # Optimistic concurrency (Finding 2): when the client tells us which revision
        # its editor state was based on, the write is compare-and-swapped at the draft
        # ref — two in-flight autosaves can't finish out of order and let the older text
        # win. `base_revision is None` => legacy unconditional write (back-compat).
        try:
            new_sha = self.git.write_draft(
                draft_id, d.prompt_id, d.version_number, content,
                base_sha=d.base_sha, author_name=author or d.author or "draft",
                expected_old=base_revision,
            )
        except ConcurrentUpdate:
            # The ref moved since `base_revision`; hand back the current tip + content
            # so the client can rebase rather than clobber.
            current_sha = self.git.head(self.git.draft_ref(draft_id))
            raise StaleDraftWrite(current_sha=current_sha,
                                  current_content=self.draft_content(draft_id))
        d.draft_sha = new_sha
        self.s.flush()
        # The content changed, so any earlier verdict was cast against the old draft_sha
        # and is no longer current (Finding 1). Re-sync status: an "approved" draft that
        # no longer meets policy drops back to "open". Stale review rows are kept as
        # history (approvals()/the commit gate simply stop counting them).
        d.status = "approved" if self._policy_met(d) else "open"
        self.s.flush()
        return extract(content)

    def discard_draft(self, draft_id: str) -> models.Draft:
        d = self.get_draft(draft_id)
        if d.status in ("committed", "discarded"):
            raise RegistryError(f"draft {draft_id!r} is already {d.status}")
        d.status = "discarded"
        self.s.flush()
        # Unlike commit_draft, discard deletes the ref INLINE (not deferred to
        # after_commit) on purpose: the two operations have opposite failure intents.
        # commit_draft defers because a failed outer commit must leave the user's draft
        # RECOVERABLE (ref + open row intact). Discard's intent is "gone", so if the outer
        # commit fails after this delete, the draft row rolls back to open but its ref is
        # already gone — and the boot sweep's direction 2 (reconcile_drafts: live row, no
        # ref → discard) converges it right back to the intended end-state. A failure here
        # therefore strands NOTHING valued, so deferral would buy no safety.
        self.git.delete_draft(draft_id)
        return d

    # ── review ───────────────────────────────────────────────────────

    def approvals(self, draft_id: str) -> list[models.Review]:
        """*Current* approvals: approved verdicts still bound to the draft's current
        content (reviewed_sha == draft_sha). A verdict cast against content that has
        since changed no longer counts toward the policy — it survives as history but is
        not current (Finding 1)."""
        d = self.get_draft(draft_id)
        return [r for r in self.reviews(draft_id)
                if r.state == "approved" and r.reviewed_sha == d.draft_sha]

    def reviews(self, draft_id: str) -> list[models.Review]:
        """Every principal's *current* review state (one row per reviewer)."""
        return list(self.s.execute(
            select(models.Review).where(models.Review.draft_id == draft_id)
            .order_by(models.Review.id)
        ).scalars())

    def _find_review(self, draft_id: str, reviewer_principal_id: str) -> models.Review | None:
        # scalar_one_or_none is safe: uq_review makes (draft_id, reviewer) unique, so
        # there is never more than one row to pick (no MultipleResultsFound window).
        return self.s.execute(
            select(models.Review).where(
                models.Review.draft_id == draft_id,
                models.Review.reviewer_principal_id == reviewer_principal_id,
            )
        ).scalar_one_or_none()

    def add_review(
        self, draft_id: str, reviewer: str, state: str = "approved",
        reviewer_principal_id: str | None = None,
    ) -> models.Review:
        d = self._locked_draft(draft_id)
        self._require_draft_status(d, ("open", "approved"), "review")
        reviewer_key = self._principal_key(reviewer_principal_id, reviewer)
        # A principal holds a single, current review state: a later verdict replaces
        # the earlier one. So "changes_requested" clears a prior "approved" (and vice
        # versa) — only "approved" rows count toward the review policy (see approvals()).
        # Bind the verdict to the exact revision it reviewed (Finding 1): it counts only
        # while draft.draft_sha is unchanged. A re-review of edited content re-stamps it.
        r = self._find_review(draft_id, reviewer_key)
        if r is None:
            try:
                # Insert under a SAVEPOINT (add + flush both inside, so the rollback
                # cleanly discards the pending row and leaves the outer transaction
                # usable).
                with self.s.begin_nested():
                    r = models.Review(draft_id=draft_id, reviewer=reviewer,
                                      reviewer_principal_id=reviewer_key, state=state,
                                      reviewed_sha=d.draft_sha)
                    self.s.add(r)
                    self.s.flush()
            except IntegrityError:
                # A concurrent double-submit inserted the (draft, reviewer) row first —
                # re-read the winner and update it instead of duplicating.
                r = self._find_review(draft_id, reviewer_key)
                if r is None:  # pragma: no cover - the constraint fired, so it exists
                    raise
                r.state = state
                r.reviewed_sha = d.draft_sha
        else:
            r.state = state
            r.reviewed_sha = d.draft_sha
        self.s.flush()
        # Keep the draft's status in sync with the (possibly changed) approval count,
        # so a withdrawn approval re-locks the draft. commit re-checks _policy_met too.
        d.status = "approved" if self._policy_met(d) else "open"
        self.s.flush()
        return r

    # ── comments ─────────────────────────────────────────────────────

    def list_comments(self, draft_id: str) -> list[models.ReviewComment]:
        return list(self.s.execute(
            select(models.ReviewComment).where(models.ReviewComment.draft_id == draft_id)
            .order_by(models.ReviewComment.created_at, models.ReviewComment.id)
        ).scalars())

    def add_comment(
        self, draft_id: str, author: str, body: str, anchor: str = "",
        author_principal_id: str | None = None,
    ) -> models.ReviewComment:
        d = self._locked_draft(draft_id)
        self._require_draft_status(d, ("open", "approved"), "comment on")
        c = models.ReviewComment(
            draft_id=draft_id, author=author,
            author_principal_id=self._principal_key(author_principal_id, author),
            anchor=anchor, body=body,
        )
        self.s.add(c)
        self.s.flush()
        return c

    def _policy_met(self, draft: models.Draft) -> bool:
        prompt = self.s.get(models.Prompt, draft.prompt_id)
        project = self.s.get(models.Project, prompt.project_id) if prompt else None
        need = project.review_policy if project else 0
        if need <= 0:
            return True
        # Self-review is opt-out: when allowed, the author's own approval counts.
        allow_self = project.allow_self_review if project else True
        reviewers = {r.reviewer_principal_id for r in self.approvals(draft.id)
                     if allow_self or r.reviewer_principal_id != draft.author_principal_id}
        return len(reviewers) >= need

    # ── validation & commit ──────────────────────────────────────────

    def _include_source(self, target_prompt_id: str) -> str | None:
        versions = self.get_versions(target_prompt_id)
        if not versions:
            return None
        top = versions[0].number  # newest version number
        return self.git.read(f"{target_prompt_id}/v{top}.j2")

    def validate(self, prompt_id: str, source: str):
        """Full §5 validation of ``source`` as a commit candidate for ``prompt_id``.
        Never raises for a bad template or a bad tree — every render-time failure is
        a verdict, not an exception (the draft editor calls this on every GET, so an
        exception here would lock the author out of the very draft they need to fix).
        When the render check could not run, the result says so and why."""
        test_render, skipped = self._make_test_render(prompt_id)
        result = validate_source(
            source, prompt_id,
            is_known_prompt=self.prompt_exists,
            include_source=self._include_source,
            test_render=test_render,
        )
        if test_render is None:
            result.render_skipped_reason = skipped
        return result

    def _make_test_render(self, prompt_id: str):
        """A strict-render check over the prompt's test contexts (§5). Returns
        ``(callable(source)->error|None, None)``, or ``(None, reason)`` when the check
        cannot run — no contexts, or the default-env snapshot can't be built.

        The snapshot failure is the dangerous one: a misconfigured
        ``INCANT_DEFAULT_ENVIRONMENT`` would otherwise disable the render check
        deployment-wide while every commit is still recorded ``valid``. It stays
        non-fatal on purpose (a fresh deployment commits before its environment
        exists), but it is logged and carried on the result so the commit response and
        the draft payload show the check did not run."""
        contexts = self.get_test_contexts(prompt_id)
        if not contexts:
            return None, f"no test contexts for {prompt_id!r}"
        # Lazy imports avoid an import cycle (targeting/core -> registry).
        from ..core import render_source
        from ..core.errors import CoreError
        from ..targeting import build_snapshot
        try:
            snap = build_snapshot(self.s, self.default_env)
        except Exception as exc:  # noqa: BLE001 - any snapshot failure is a skipped check
            reason = (f"default environment {self.default_env!r} snapshot unavailable "
                      f"({type(exc).__name__}: {exc})")
            log.warning("validate: render check for %s skipped — %s", prompt_id, reason)
            return None, reason

        def check(source: str) -> str | None:
            for c in contexts:
                try:
                    render_source(snap, prompt_id, source, c.flags or {}, c.variables or {},
                                  self.content)
                except CoreError as exc:
                    # Every core failure is a verdict: missing variable, render error,
                    # unresolvable/unservable include — AND the include cycle / depth
                    # errors, which the static check cannot see when a live pointer
                    # (not the newest version) is what closes the cycle.
                    return f"render failed for test context {c.name!r}: {exc}"
                except KeyError as exc:
                    # ContentStore.get: a resolved SHA whose file is not in the repo.
                    detail = exc.args[0] if exc.args else exc
                    return (f"render failed for test context {c.name!r}: resolved "
                            f"content missing from store ({detail})")
            return None

        return check, None

    def commit_draft(
        self, draft_id: str, *, author: str, email: str = "", message: str = "",
        force: bool = False,
    ) -> CommitOutcome:
        d = self._locked_draft(draft_id)
        self._require_draft_status(d, ("open", "approved"), "commit")
        # The bytes committed below come from GIT (the draft ref's HEAD, via
        # draft_content), but the review policy is checked against the DB: approvals
        # count only while reviewed_sha == d.draft_sha (_policy_met/approvals). Draft
        # writes advance the git ref BEFORE their outer DB transaction commits, so a
        # failed outer commit can leave git holding newer text than d.draft_sha — and
        # approvals cast against the recorded (older) revision must never authorize
        # publishing text nobody reviewed. Refuse on any git/DB disagreement, before
        # any other check runs on state that may be describing the wrong bytes.
        try:
            ref_head = self.git.head(self.git.draft_ref(draft_id))
        except GitError:
            # A missing ref is the same disagreement (the boot sweep discards such
            # drafts); without this, draft_content would quietly commit "".
            ref_head = None
        if ref_head != d.draft_sha:
            raise DraftDiverged(
                f"draft {draft_id!r}'s git content ({(ref_head or 'missing')[:12]}) has "
                f"diverged from its recorded revision ({(d.draft_sha or 'none')[:12]}) — "
                "likely a previously failed save. Re-open the draft and re-save its "
                "content, then review and commit again."
            )
        version = self.s.execute(select(models.Version).where(
            models.Version.prompt_id == d.prompt_id,
            models.Version.number == d.version_number,
        )).scalar_one_or_none()
        if version is not None and version.status == "archived":
            raise RegistryError(
                f"version {d.version_number} of {d.prompt_id!r} is archived and accepts no commits"
            )
        if not self._policy_met(d):
            need = self._required_approvals(d)
            raise ReviewRequired(f"{need} approval(s) required before commit")

        source = self.draft_content(draft_id)

        # Optimistic concurrency: if the version file moved since the draft's base,
        # the publisher must reconfirm (git-level merge only when edits don't overlap,
        # never a silent merge of prompt text).
        path = f"{d.prompt_id}/v{d.version_number}.j2"
        if not force and d.base_sha:
            base_blob = self.git.blob_sha(path, ref=d.base_sha)
            current_blob = self.git.blob_sha(path)
            if current_blob is not None and current_blob != base_blob:
                raise ConcurrencyError(
                    f"{path} changed since this draft's base; review the intervening "
                    "diff and re-confirm to publish",
                    base_sha=d.base_sha, current_sha=self.git.head(),
                )

        result = self.validate(d.prompt_id, source)

        # STAGED PUBLISH: `main` must only ever hold commits the DB fully describes,
        # so the git commit is staged on refs/incant/pending/<draft> FIRST, the
        # control-plane rows land in the outer transaction, and main advances (a pure
        # CAS ref move to the already-recorded SHA) only in ``after_commit``. The
        # publish lock is held from staging through promotion/abandonment so
        # in-process publishes serialize — two staged commits can't share a parent —
        # and the promote CAS is the cross-process backstop. Consequences:
        #   * Outer commit SUCCEEDS → main moves to the staged SHA, pending + draft
        #     refs are dropped. Same caller-visible result as before.
        #   * Outer commit FAILS → ``after_rollback`` deletes the pending ref; main
        #     NEVER moved, and the draft ref + still-"open" row survive: the user's
        #     work is fully recoverable and there is NO unvalidated-tip residue (the
        #     drift `reconcile_main_commits` exists to catch can no longer be
        #     *produced* by this path — the sweep remains as an invariant check).
        #   * Crash between the two phases → the pending ref survives;
        #     ``recover_pending_promotions`` (boot + reconcile loop) promotes it when
        #     the DB shows the transaction committed, discards it otherwise.
        # We never ``event.remove`` from inside a listener (SQLAlchemy dispatches
        # while iterating the deque); ``once=True`` is the safe one-shot. Both
        # listeners share an idempotent lock release — whichever fires, fires once.
        lock = self.git.publish_lock
        if not lock.acquire(timeout=30):
            raise RegistryError("publish serialization timeout; try again")
        released = False

        def _release_lock() -> None:
            nonlocal released
            if not released:
                released = True
                lock.release()

        # One DB transaction may stage several publishes (seed, batch flows): each
        # chains onto the previous staged SHA — not main, which hasn't moved yet — so
        # the promotions (dispatched FIFO at commit) fast-forward one after another.
        chain: list[str] = self.s.info.setdefault("incant_pending_chain", [])
        sha = None
        try:
            sha, parent = self.git.commit_version_pending(
                d.prompt_id, d.version_number, source,
                author_name=author, author_email=email or f"{author}@incant",
                message=message or d.title or f"update v{d.version_number}",
                draft_id=draft_id,
                parent=chain[-1] if chain else None,
            )
            chain.append(sha)
            blob_sha = self.git.blob_sha(f"{d.prompt_id}/v{d.version_number}.j2", ref=sha) or ""

            self._ensure_version_row(d.prompt_id, d.version_number, author)

            cv = models.CommitValidation(
                sha=sha, blob_sha=blob_sha, path=f"{d.prompt_id}/v{d.version_number}.j2",
                prompt_id=d.prompt_id, version_number=d.version_number,
                status=result.status, error=result.error,
                extracted_variables=result.extracted_variables,
                render_checked=result.render_checked,
                render_skipped_reason=result.render_skipped_reason,
            )
            self.s.add(cv)
            # Every publish changes what snapshots are built from — a new version row
            # and/or a new validated SHA (tip, servable index) — so every node's poll
            # must rebuild. Same transaction as the rows it announces.
            bump_content_version(self.s)

            d.status = "committed"
            self.s.flush()
        except BaseException:
            # Staging failed before the listeners took ownership: clean up + unlock.
            try:
                if sha is not None and chain and chain[-1] == sha:
                    chain.pop()
                self.git.delete_pending(draft_id)
            finally:
                _release_lock()
            raise

        git, draft_ref_id = self.git, draft_id

        def _promote(session: Session) -> None:
            try:
                try:
                    git.promote_pending(sha, parent)
                except ConcurrentUpdate:
                    # Cross-process race (unsupported multi-full-node deployment) or a
                    # recovery double-run. If main already contains the SHA, promotion
                    # happened elsewhere; otherwise leave the pending ref for
                    # recover_pending_promotions and say so LOUDLY.
                    if not git.is_ancestor(sha):
                        log.critical(
                            "commit_draft: could not promote %s for draft %s — main "
                            "diverged from the staged parent. The publish is recorded in "
                            "the DB and the pending ref is kept; recovery will rebase it.",
                            sha, draft_ref_id,
                        )
                        return
                git.delete_pending(draft_ref_id)
                git.delete_draft(draft_ref_id)
            except Exception:  # pragma: no cover - best-effort post-commit cleanup
                log.warning(
                    "commit_draft: post-commit promotion/cleanup for %s hit an error; "
                    "recovery (boot + reconcile loop) converges it.", draft_ref_id,
                    exc_info=True,
                )
            finally:
                if sha in chain:
                    chain.remove(sha)
                _release_lock()

        def _abandon(session: Session) -> None:
            try:
                git.delete_pending(draft_ref_id)
            except Exception:  # pragma: no cover - best-effort rollback cleanup
                log.warning("commit_draft: pending-ref cleanup for %s failed on rollback",
                            draft_ref_id, exc_info=True)
            finally:
                if sha in chain:
                    chain.remove(sha)
                _release_lock()

        event.listen(self.s, "after_commit", _promote, once=True)
        event.listen(self.s, "after_rollback", _abandon, once=True)

        # Warm the content cache for the freshly-validated SHA.
        if result.ok:
            self.content.warm(d.prompt_id, d.version_number, sha)

        return CommitOutcome(sha, blob_sha, d.version_number, {
            "status": result.status, "error": result.error,
            "variables": result.extracted_variables,
            "render_checked": result.render_checked,
            "render_skipped_reason": result.render_skipped_reason,
        })

    def _required_approvals(self, draft: models.Draft) -> int:
        prompt = self.s.get(models.Prompt, draft.prompt_id)
        project = self.s.get(models.Project, prompt.project_id) if prompt else None
        return project.review_policy if project else 0

    # ── refinements & test contexts ──────────────────────────────────

    def get_refinements(self, prompt_id: str, version_number: int) -> list[models.VariableRefinement]:
        return list(self.s.execute(
            select(models.VariableRefinement).where(
                models.VariableRefinement.prompt_id == prompt_id,
                models.VariableRefinement.version_number == version_number,
            )
        ).scalars())

    def set_refinement(self, prompt_id: str, version_number: int, name: str, **fields):
        # A refinement recorded against a prompt/version that doesn't exist is
        # dormant config: a typo'd write would silently activate if that prompt or
        # version is created later. Refuse at write time, where the author can fix
        # it. (Service-level check on purpose — refinements have no FK to versions.)
        if not self.prompt_exists(prompt_id):
            raise RegistryError(f"unknown prompt {prompt_id!r}")
        version_exists = self.s.execute(
            select(models.Version.id).where(
                models.Version.prompt_id == prompt_id,
                models.Version.number == version_number,
            )
        ).first() is not None
        if not version_exists:
            raise RegistryError(
                f"unknown version {version_number} for prompt {prompt_id!r} — commit "
                "the version before refining its variables")
        existing = self.s.execute(
            select(models.VariableRefinement).where(
                models.VariableRefinement.prompt_id == prompt_id,
                models.VariableRefinement.version_number == version_number,
                models.VariableRefinement.name == name,
            )
        ).scalar_one_or_none()
        if existing is None:
            existing = models.VariableRefinement(
                prompt_id=prompt_id, version_number=version_number, name=name
            )
            self.s.add(existing)
        for k, v in fields.items():
            setattr(existing, k, v)
        self.s.flush()
        # Refinement defaults are folded into every environment's snapshot (the render
        # path resolves optional variables from memory), so replicas must rebuild.
        bump_content_version(self.s)
        return existing

    def get_test_contexts(self, prompt_id: str) -> list[models.TestContext]:
        return list(self.s.execute(
            select(models.TestContext).where(models.TestContext.prompt_id == prompt_id)
        ).scalars())

    def set_test_context(self, prompt_id: str, name: str, flags: dict, variables: dict):
        # Same dormant-config hazard as set_refinement: a test context saved under a
        # typo'd prompt id would silently start gating validation the moment a prompt
        # with that name appears. The prompt must already exist.
        if not self.prompt_exists(prompt_id):
            raise RegistryError(f"unknown prompt {prompt_id!r}")
        existing = self.s.execute(
            select(models.TestContext).where(
                models.TestContext.prompt_id == prompt_id, models.TestContext.name == name
            )
        ).scalar_one_or_none()
        if existing is None:
            existing = models.TestContext(prompt_id=prompt_id, name=name)
            self.s.add(existing)
        existing.flags = flags
        existing.variables = variables
        self.s.flush()
        return existing
