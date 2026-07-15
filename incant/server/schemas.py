"""Pydantic request/response models for the serving + mgmt APIs."""

from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field, field_validator


# ── serving ──────────────────────────────────────────────────────────

class RenderRequest(BaseModel):
    flags: dict[str, Any] = Field(default_factory=dict)
    variables: dict[str, Any] = Field(default_factory=dict)
    environment: Optional[str] = None
    # §9 reproducibility: feed a prior response's `versions` (+ rules_version) back
    # to replay it exactly. Shape: {"versions": {prompt_id: {"version", "commit"}}}.
    pin: Optional[dict[str, Any]] = None


class EvaluateRequest(BaseModel):
    flags: dict[str, Any] = Field(default_factory=dict)
    environment: Optional[str] = None


# ── browser sessions ─────────────────────────────────────────────────

class SessionLoginRequest(BaseModel):
    # An API key presented once to exchange for an HttpOnly session cookie. Verified
    # through the same machinery (and failed-auth throttle) as bearer auth.
    key: str
    remember: bool = False


# ── authoring ────────────────────────────────────────────────────────

class CreatePromptRequest(BaseModel):
    prompt_id: str
    description: str = ""


class CreateDraftRequest(BaseModel):
    version_number: Optional[int] = None      # None => allocate a new version
    seed_from_version: Optional[int] = None
    author: str = ""
    title: str = ""
    content: Optional[str] = None


class DraftContentRequest(BaseModel):
    content: str
    author: str = ""
    # Optimistic concurrency (Finding 2): the `draft_sha` the client's editor state was
    # based on. When set and != the draft's current draft_sha, the write is refused with
    # a 409 stale_write (carrying current_sha + current_content). Omit for a legacy
    # unconditional write (back-compat for tests/integrations).
    base_revision: Optional[str] = None


class DraftRenderRequest(BaseModel):
    environment: str = "prod"
    flags: dict[str, Any] = Field(default_factory=dict)
    variables: dict[str, Any] = Field(default_factory=dict)
    test_context: Optional[str] = None


class ReviewRequest(BaseModel):
    # `reviewer` is ignored — the reviewer is the authenticated principal.
    reviewer: Optional[str] = None
    state: str = "approved"                     # "approved" | "changes_requested"


class CommentRequest(BaseModel):
    # `author` is never body-supplied — it is the authenticated principal.
    anchor: str = ""                            # "source:4" | "rendered" | ""
    body: str

    @field_validator("body")
    @classmethod
    def _nonempty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("comment body must not be empty")
        return v


class CommitRequest(BaseModel):
    # `author` is ignored — the author is the authenticated principal.
    author: Optional[str] = None
    email: str = ""
    message: str = ""
    force: bool = False


class RefinementRequest(BaseModel):
    name: str
    type: Optional[str] = None
    required: Optional[bool] = None
    default: Any = None
    description: str = ""


class TestContextRequest(BaseModel):
    name: str
    flags: dict[str, Any] = Field(default_factory=dict)
    variables: dict[str, Any] = Field(default_factory=dict)


# ── targeting ────────────────────────────────────────────────────────

class RuleRequest(BaseModel):
    id: str
    scope: str = "prompt"
    prompt_id: Optional[str] = None
    priority: int = 10
    when: Any = None
    serve: dict[str, Any]
    status: str = "active"
    comment: str = ""


class RuleBatchRequest(BaseModel):
    # A set of rule upserts applied as ONE atomic act (composer priority-shift plan, or a
    # two-rule reorder swap). Each element is the exact shape the single upsert takes; the
    # whole batch lands in one request/transaction so a mid-sequence failure can't leave
    # rules at colliding/half-applied priorities (DESIGN.md §7).
    rules: list[RuleRequest]


class RuleStatusRequest(BaseModel):
    status: str


class SegmentRequest(BaseModel):
    name: str
    when: Any = None


class RollbackRequest(BaseModel):
    to_rules_version: int
    confirm: Optional[str] = None  # locked env: must echo the env name


class PointerRequest(BaseModel):
    prompt_id: str
    version_number: int
    to_sha: str
    comment: str = ""
    confirm: Optional[str] = None  # locked env: must echo the prompt id


class PublishRequest(BaseModel):
    # "Publish latest edits" / "Stop test & publish": advance the live pointer AND archive
    # the now-redundant test rules in ONE atomic act, so the pointer can't move while the
    # archives fail (DESIGN.md §7). `confirm` echoes the prompt id on a locked env, exactly
    # as the pointer endpoint requires; `archive_rule_ids` may be empty (a plain publish).
    prompt_id: str
    version_number: int
    to_sha: str
    comment: str = ""
    confirm: Optional[str] = None  # locked env: must echo the prompt id
    archive_rule_ids: list[str] = Field(default_factory=list)


class DefaultRequest(BaseModel):
    prompt_id: str
    version_number: int
    confirm: Optional[str] = None  # locked env: must echo the prompt id


class KillRequest(BaseModel):
    engaged: bool = True


# ── admin ────────────────────────────────────────────────────────────

class ProjectRequest(BaseModel):
    id: str
    review_policy: int = 0
    allow_self_review: bool = True


class ProjectSettingsRequest(BaseModel):
    # Partial update of a project's review settings; unset fields untouched.
    review_policy: Optional[int] = None
    allow_self_review: Optional[bool] = None


class EnvironmentRequest(BaseModel):
    id: str
    protected: bool = False
    track_tip: bool = False


class EnvSettingsRequest(BaseModel):
    # Partial update of an environment's settings; unset fields untouched.
    protected: Optional[bool] = None
    track_tip: Optional[bool] = None


class RenameEnvRequest(BaseModel):
    # Rename an environment: move ALL of its targeting rows to `new_id` in one
    # transaction. A locked (protected) env requires `confirm` to echo the CURRENT id.
    new_id: str
    confirm: Optional[str] = None  # locked env: must echo the current env id


class KeyRequest(BaseModel):
    principal_name: str
    role: str = "renderer"
    project_id: Optional[str] = None
    environment_id: Optional[str] = None
    # Optional key lifetime. None ⇒ never expires; N ⇒ expires_at = now + N days.
    expires_in_days: Optional[int] = None


class IssueKeyRequest(BaseModel):
    # Body for issuing/rotating a key on an existing principal (both optional-bodied).
    expires_in_days: Optional[int] = None


class BindingRequest(BaseModel):
    role: str
    project_id: Optional[str] = None
    environment_id: Optional[str] = None
