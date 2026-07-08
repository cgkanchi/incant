"""Pydantic request/response models for the serving + mgmt APIs."""

from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field


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


class DraftRenderRequest(BaseModel):
    environment: str = "prod"
    flags: dict[str, Any] = Field(default_factory=dict)
    variables: dict[str, Any] = Field(default_factory=dict)
    test_context: Optional[str] = None


class ReviewRequest(BaseModel):
    # `reviewer` is ignored — the reviewer is the authenticated principal.
    reviewer: Optional[str] = None
    state: str = "approved"


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


class RuleStatusRequest(BaseModel):
    status: str


class SegmentRequest(BaseModel):
    name: str
    when: Any = None


class RollbackRequest(BaseModel):
    to_rules_version: int


class PointerRequest(BaseModel):
    prompt_id: str
    version_number: int
    to_sha: str
    comment: str = ""
    force: bool = False  # break-glass direct release (releaser-gated at the route)


class DefaultRequest(BaseModel):
    prompt_id: str
    version_number: int


class KillRequest(BaseModel):
    engaged: bool = True


# ── admin ────────────────────────────────────────────────────────────

class ProjectRequest(BaseModel):
    id: str
    review_policy: int = 0


class EnvironmentRequest(BaseModel):
    id: str
    protected: bool = False
    track_tip: bool = False
    allow_self_approval: bool = True


class EnvSettingsRequest(BaseModel):
    # Partial update of an environment's governance settings; unset fields untouched.
    protected: Optional[bool] = None
    track_tip: Optional[bool] = None
    allow_self_approval: Optional[bool] = None


class KeyRequest(BaseModel):
    principal_name: str
    role: str = "renderer"
    project_id: Optional[str] = None
    environment_id: Optional[str] = None
