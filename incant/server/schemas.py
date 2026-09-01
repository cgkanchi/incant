"""Pydantic request/response models for the serving + mgmt APIs."""

from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, field_validator

from ..core.parse import parse_condition, parse_serve


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
    # Two doors, one endpoint: humans sign in with email + password; an API key is
    # still accepted (machine access, recovery, tests). Exactly one of the two forms
    # must be presented — the handler enforces it. Both are verified under the same
    # failed-auth throttle.
    email: Optional[str] = None
    password: Optional[str] = None
    key: Optional[str] = None
    remember: bool = False


class SetupRequest(BaseModel):
    # First-boot only: create the initial admin account (refused once any user exists).
    name: str
    email: str
    password: str
    # Names the deployment's ONE project (optional — the first prompt's prefix can
    # also claim it). Slug rules match environment ids.
    project: Optional[str] = None


class AcceptInviteRequest(BaseModel):
    # Redeem an invite/reset token for a password. Signs the user in on success.
    token: str
    password: str
    name: Optional[str] = None      # invitees may correct/set their display name


class PasswordChangeRequest(BaseModel):
    current_password: str
    new_password: str


class InviteUserRequest(BaseModel):
    email: str
    name: str = ""
    # Optional initial role binding, same shape as key issuance.
    role: Optional[str] = None
    project_id: Optional[str] = None
    environment_id: Optional[str] = None


class UserStatusRequest(BaseModel):
    disabled: bool


# ── authoring ────────────────────────────────────────────────────────

class CreatePromptRequest(BaseModel):
    prompt_id: str
    description: str = ""


class VersionUpdateRequest(BaseModel):
    notes: Optional[str] = None
    status: Optional[Literal["active", "archived"]] = None
    # Pre-1.1.0 clients set version labels here. Refuse with the reason rather than
    # silently ignoring the field (pydantic's default would drop it and return 200).
    label: Optional[str] = None

    @field_validator("label")
    @classmethod
    def _label_removed(cls, value):
        if value is None:
            return None
        raise ValueError("version labels were removed in 1.1.0 — rules and pins name "
                         "versions by number")


class CreateDraftRequest(BaseModel):
    version_number: Optional[int] = None      # None => allocate a new version
    seed_from_version: Optional[int] = None
    # With seed_from_version: seed from that version's content AT THIS COMMIT rather
    # than its tip. The UI passes the live pointer's sha when creating a new version,
    # so unpublished (possibly deliberately rolled-back) tip edits don't resurrect.
    seed_from_sha: Optional[str] = Field(default=None, pattern=r"^[0-9a-f]{40}$")
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
    id: str = Field(min_length=1, max_length=255)
    prompt_id: str = Field(min_length=1)
    priority: int = Field(default=10, ge=0, le=1_000_000)
    when: Optional[dict[str, Any]] = None
    serve: dict[str, Any]
    status: Literal["active", "paused", "archived"] = "active"
    comment: str = ""
    # Pre-1.1.0 payloads carried `scope`. "prompt" is tolerated (it is the only shape
    # left); "global" must fail LOUDLY with the reason, not be silently ignored into a
    # prompt-scoped rule — the one thing an agent working from stale instructions needs.
    scope: Optional[str] = None

    @field_validator("scope")
    @classmethod
    def _scope_removed(cls, value):
        if value is None or value == "prompt":
            return None
        raise ValueError("global rules were removed in 1.1.0 — rules are scoped to one prompt "
                         "(drop `scope`; set prompt_id)")

    @field_validator("when")
    @classmethod
    def _valid_when(cls, value):
        parse_condition(value)
        return value

    @field_validator("serve")
    @classmethod
    def _valid_serve(cls, value):
        parse_serve(value)
        return value


class RuleBatchRequest(BaseModel):
    # A set of rule upserts applied as ONE atomic act (composer priority-shift plan, or a
    # two-rule reorder swap). Each element is the exact shape the single upsert takes; the
    # whole batch lands in one request/transaction so a mid-sequence failure can't leave
    # rules at colliding/half-applied priorities (DESIGN.md §7).
    rules: list[RuleRequest] = Field(max_length=200)


class RuleStatusRequest(BaseModel):
    status: Literal["active", "paused", "archived"]


class RollbackRequest(BaseModel):
    to_rules_version: int = Field(ge=1)
    confirm: Optional[str] = None  # locked env: must echo the env name


class PointerRequest(BaseModel):
    prompt_id: str
    version_number: int = Field(ge=1)
    to_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    comment: str = ""
    confirm: Optional[str] = None  # locked env: must echo the prompt id


class PublishRequest(BaseModel):
    # "Publish latest edits" / "Stop test & publish": advance the live pointer AND archive
    # the now-redundant test rules in ONE atomic act, so the pointer can't move while the
    # archives fail (DESIGN.md §7). `confirm` echoes the prompt id on a locked env, exactly
    # as the pointer endpoint requires; `archive_rule_ids` may be empty (a plain publish).
    prompt_id: str
    version_number: int = Field(ge=1)
    to_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    comment: str = ""
    confirm: Optional[str] = None  # locked env: must echo the prompt id
    archive_rule_ids: list[str] = Field(default_factory=list)
    # Also make version_number the environment default for this prompt, in the same
    # transaction. Used by "Stop test & publish" (promoting the tested version so
    # EVERYONE gets it, as the confirmation promises) and by a first publish (a
    # brand-new prompt has no default; a pointer alone would serve nobody).
    make_default: bool = False


class DefaultRequest(BaseModel):
    prompt_id: str
    version_number: int = Field(ge=1)
    confirm: Optional[str] = None  # locked env: must echo the prompt id


class KillRequest(BaseModel):
    engaged: bool = True


# ── admin ────────────────────────────────────────────────────────────

class RemoteRequest(BaseModel):
    # A backup remote (§6): any URL `git push` understands. `auth_ref` is a path to a
    # push-only ssh deploy key (mounted into the container); https URLs may embed a
    # token instead (redacted in every response/log).
    url: str
    auth_ref: Optional[str] = None
    enabled: bool = True

    @field_validator("url")
    @classmethod
    def _nonempty_url(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("remote url must not be empty")
        return v.strip()


class RemotePatchRequest(BaseModel):
    # Partial update; unset fields untouched.
    url: Optional[str] = None
    auth_ref: Optional[str] = None
    enabled: Optional[bool] = None

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
