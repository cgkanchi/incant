"""RegistryService — authoring: versions, drafts, reviews, commits, refinements.

Ties git (content) + DB (state) + validation together. Every commit is validated
on landing and recorded per SHA; only validated SHAs can ever be referenced by a
pointer or rule.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models
from ..core import ExtractedVars, extract
from ..gitstore import ContentStore, GitStore, validate_source


class RegistryError(Exception):
    pass


class ReviewRequired(RegistryError):
    pass


class ConcurrencyError(RegistryError):
    def __init__(self, message: str, *, base_sha: str | None = None,
                 current_sha: str | None = None) -> None:
        super().__init__(message)
        # The publisher must see the intervening diff (base -> current tip) to
        # re-confirm; carry the endpoints so the handler can compute it.
        self.base_sha = base_sha
        self.current_sha = current_sha


@dataclass
class CommitOutcome:
    sha: str
    blob_sha: str
    version_number: int
    validation: dict


class RegistryService:
    def __init__(self, session: Session, git: GitStore, content: ContentStore,
                 default_env: str = "prod") -> None:
        self.s = session
        self.git = git
        self.content = content
        self.default_env = default_env

    # ── projects & prompts ───────────────────────────────────────────

    def ensure_project(self, project_id: str, review_policy: int = 0,
                       allow_self_review: bool = True) -> models.Project:
        p = self.s.get(models.Project, project_id)
        if p is None:
            p = models.Project(id=project_id, name=project_id, review_policy=review_policy,
                               allow_self_review=allow_self_review)
            self.s.add(p)
            self.s.flush()
        return p

    def create_prompt(self, prompt_id: str, description: str = "") -> models.Prompt:
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
        return v

    # ── drafts ───────────────────────────────────────────────────────

    def create_draft(
        self,
        prompt_id: str,
        *,
        version_number: int | None = None,
        seed_from_version: int | None = None,
        author: str = "",
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

        if content is None:
            if seed_from_version is not None:
                content = self.git.read(f"{prompt_id}/v{seed_from_version}.j2") or ""
            elif not new_version:
                content = self.git.read(f"{prompt_id}/v{version_number}.j2") or ""
            else:
                content = ""

        base_sha = self.git.head()
        draft_id = "d_" + uuid.uuid4().hex[:8]
        draft_sha = self.git.write_draft(
            draft_id, prompt_id, version_number, content, base_sha=base_sha,
            author_name=author or "draft",
        )
        d = models.Draft(
            id=draft_id, prompt_id=prompt_id,
            version_number=None if new_version else version_number,
            base_sha=base_sha, git_ref=self.git.draft_ref(draft_id),
            draft_sha=draft_sha, title=title, author=author, status="open",
        )
        # Remember the target version number even for new versions (in draft_sha path).
        d.version_number = version_number
        self.s.add(d)
        self.s.flush()
        return d

    def get_draft(self, draft_id: str) -> models.Draft:
        d = self.s.get(models.Draft, draft_id)
        if d is None:
            raise RegistryError(f"unknown draft {draft_id!r}")
        return d

    def draft_content(self, draft_id: str) -> str:
        d = self.get_draft(draft_id)
        return self.git.read_draft(draft_id, d.prompt_id, d.version_number) or ""

    def put_draft_content(self, draft_id: str, content: str, author: str = "") -> ExtractedVars:
        d = self.get_draft(draft_id)
        d.draft_sha = self.git.write_draft(
            draft_id, d.prompt_id, d.version_number, content,
            base_sha=d.base_sha, author_name=author or d.author or "draft",
        )
        self.s.flush()
        return extract(content)

    def discard_draft(self, draft_id: str) -> models.Draft:
        d = self.get_draft(draft_id)
        if d.status in ("committed", "discarded"):
            raise RegistryError(f"draft {draft_id!r} is already {d.status}")
        d.status = "discarded"
        self.git.delete_draft(draft_id)
        self.s.flush()
        return d

    # ── review ───────────────────────────────────────────────────────

    def approvals(self, draft_id: str) -> list[models.Review]:
        return list(self.s.execute(
            select(models.Review).where(
                models.Review.draft_id == draft_id, models.Review.state == "approved"
            )
        ).scalars())

    def reviews(self, draft_id: str) -> list[models.Review]:
        """Every principal's *current* review state (one row per reviewer)."""
        return list(self.s.execute(
            select(models.Review).where(models.Review.draft_id == draft_id)
            .order_by(models.Review.id)
        ).scalars())

    def add_review(self, draft_id: str, reviewer: str, state: str = "approved") -> models.Review:
        d = self.get_draft(draft_id)
        # A principal holds a single, current review state: a later verdict replaces
        # the earlier one. So "changes_requested" clears a prior "approved" (and vice
        # versa) — only "approved" rows count toward the review policy (see approvals()).
        r = self.s.execute(
            select(models.Review).where(
                models.Review.draft_id == draft_id, models.Review.reviewer == reviewer
            )
        ).scalar_one_or_none()
        if r is None:
            r = models.Review(draft_id=draft_id, reviewer=reviewer, state=state)
            self.s.add(r)
        else:
            r.state = state
        self.s.flush()
        # Keep the draft's status in sync with the (possibly changed) approval count,
        # so a withdrawn approval re-locks the draft. commit re-checks _policy_met too.
        if d.status not in ("committed", "discarded", "abandoned"):
            d.status = "approved" if self._policy_met(d) else "open"
        self.s.flush()
        return r

    # ── comments ─────────────────────────────────────────────────────

    def list_comments(self, draft_id: str) -> list[models.ReviewComment]:
        return list(self.s.execute(
            select(models.ReviewComment).where(models.ReviewComment.draft_id == draft_id)
            .order_by(models.ReviewComment.created_at, models.ReviewComment.id)
        ).scalars())

    def add_comment(self, draft_id: str, author: str, body: str,
                    anchor: str = "") -> models.ReviewComment:
        c = models.ReviewComment(draft_id=draft_id, author=author, anchor=anchor, body=body)
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
        reviewers = {r.reviewer for r in self.approvals(draft.id)
                     if allow_self or r.reviewer != draft.author}
        return len(reviewers) >= need

    # ── validation & commit ──────────────────────────────────────────

    def _include_source(self, target_prompt_id: str) -> str | None:
        versions = self.get_versions(target_prompt_id)
        if not versions:
            return None
        top = versions[0].number  # newest version number
        return self.git.read(f"{target_prompt_id}/v{top}.j2")

    def validate(self, prompt_id: str, source: str):
        return validate_source(
            source, prompt_id,
            is_known_prompt=self.prompt_exists,
            include_source=self._include_source,
            test_render=self._make_test_render(prompt_id),
        )

    def _make_test_render(self, prompt_id: str):
        """A strict-render check over the prompt's test contexts (§5). Returns a
        callable(source)->error|None, or None when there are no contexts to render
        against or the default-env snapshot can't be built."""
        contexts = self.get_test_contexts(prompt_id)
        if not contexts:
            return None
        # Lazy imports avoid an import cycle (targeting/core -> registry).
        from ..core import MissingVariable, RenderError, Unservable, render_source
        from ..core.errors import UnresolvedPrompt
        from ..targeting import build_snapshot
        try:
            snap = build_snapshot(self.s, self.default_env)
        except Exception:
            return None  # no snapshot (e.g. env missing) -> skip render check

        def check(source: str) -> str | None:
            for c in contexts:
                try:
                    render_source(snap, prompt_id, source, c.flags or {}, c.variables or {},
                                  self.content)
                except (MissingVariable, RenderError, Unservable, UnresolvedPrompt) as exc:
                    return f"render failed for test context {c.name!r}: {exc}"
            return None

        return check

    def commit_draft(
        self, draft_id: str, *, author: str, email: str = "", message: str = "",
        force: bool = False,
    ) -> CommitOutcome:
        d = self.get_draft(draft_id)
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

        sha = self.git.commit_version(
            d.prompt_id, d.version_number, source,
            author_name=author, author_email=email or f"{author}@incant",
            message=message or d.title or f"update v{d.version_number}",
            draft_id=draft_id,
        )
        blob_sha = self.git.blob_sha(f"{d.prompt_id}/v{d.version_number}.j2", ref=sha) or ""

        self._ensure_version_row(d.prompt_id, d.version_number, author)

        cv = models.CommitValidation(
            sha=sha, blob_sha=blob_sha, path=f"{d.prompt_id}/v{d.version_number}.j2",
            prompt_id=d.prompt_id, version_number=d.version_number,
            status=result.status, error=result.error,
            extracted_variables=result.extracted_variables,
        )
        self.s.add(cv)

        d.status = "committed"
        self.git.delete_draft(draft_id)
        self.s.flush()

        # Warm the content cache for the freshly-validated SHA.
        if result.ok:
            self.content.warm(d.prompt_id, d.version_number, sha)

        return CommitOutcome(sha, blob_sha, d.version_number, {
            "status": result.status, "error": result.error,
            "variables": result.extracted_variables,
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
        return existing

    def get_test_contexts(self, prompt_id: str) -> list[models.TestContext]:
        return list(self.s.execute(
            select(models.TestContext).where(models.TestContext.prompt_id == prompt_id)
        ).scalars())

    def set_test_context(self, prompt_id: str, name: str, flags: dict, variables: dict):
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
