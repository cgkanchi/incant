"""Control-plane ORM models. No template content lives here — only SHAs and state.

Mirrors the schema sketch in DESIGN.md §13.
"""

from __future__ import annotations

import datetime as dt
from typing import Any, Optional

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


class Project(Base):
    __tablename__ = "projects"
    id: Mapped[str] = mapped_column(String, primary_key=True)          # == name / top dir
    name: Mapped[str] = mapped_column(String)
    review_policy: Mapped[int] = mapped_column(Integer, default=0)     # approvals to commit
    # Draft review separation of duties is opt-out: by default the author's own
    # approval counts toward the policy; disable to require a distinct reviewer.
    allow_self_review: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_now)


class Prompt(Base):
    __tablename__ = "prompts"
    id: Mapped[str] = mapped_column(String, primary_key=True)          # path, e.g. support/system
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"))
    description: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_now)

    versions: Mapped[list["Version"]] = relationship(back_populates="prompt")


class Version(Base):
    __tablename__ = "versions"
    __table_args__ = (UniqueConstraint("prompt_id", "number", name="uq_version"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    prompt_id: Mapped[str] = mapped_column(ForeignKey("prompts.id"))
    number: Mapped[int] = mapped_column(Integer)
    label: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    status: Mapped[str] = mapped_column(String, default="active")      # active | archived
    notes: Mapped[str] = mapped_column(Text, default="")
    created_by: Mapped[str] = mapped_column(String, default="")
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_now)

    prompt: Mapped[Prompt] = relationship(back_populates="versions")


class CommitValidation(Base):
    __tablename__ = "commit_validations"
    __table_args__ = (UniqueConstraint("sha", "path", name="uq_validation"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    sha: Mapped[str] = mapped_column(String, index=True)
    blob_sha: Mapped[str] = mapped_column(String, index=True)
    path: Mapped[str] = mapped_column(String)
    prompt_id: Mapped[str] = mapped_column(String, index=True)
    version_number: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String)                        # valid | invalid
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    extracted_variables: Mapped[dict] = mapped_column(JSON, default=dict)
    validated_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_now)


class VariableRefinement(Base):
    __tablename__ = "variable_refinements"
    __table_args__ = (
        UniqueConstraint("prompt_id", "version_number", "name", name="uq_refinement"),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    prompt_id: Mapped[str] = mapped_column(String, index=True)
    version_number: Mapped[int] = mapped_column(Integer)
    name: Mapped[str] = mapped_column(String)
    type: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    required: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    default: Mapped[Any] = mapped_column(JSON, nullable=True)
    description: Mapped[str] = mapped_column(Text, default="")


class TestContext(Base):
    __tablename__ = "test_contexts"
    __table_args__ = (UniqueConstraint("prompt_id", "name", name="uq_testctx"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    prompt_id: Mapped[str] = mapped_column(String, index=True)
    name: Mapped[str] = mapped_column(String)
    flags: Mapped[dict] = mapped_column(JSON, default=dict)
    variables: Mapped[dict] = mapped_column(JSON, default=dict)


class Draft(Base):
    __tablename__ = "drafts"
    id: Mapped[str] = mapped_column(String, primary_key=True)          # d_1042
    prompt_id: Mapped[str] = mapped_column(String, index=True)
    version_number: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)  # None = new version
    base_sha: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    git_ref: Mapped[str] = mapped_column(String)                       # refs/incant/drafts/<id>
    draft_sha: Mapped[Optional[str]] = mapped_column(String, nullable=True)  # current draft commit
    title: Mapped[str] = mapped_column(String, default="")
    author: Mapped[str] = mapped_column(String, default="")
    status: Mapped[str] = mapped_column(String, default="open")        # open | approved | committed | abandoned
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)


class Review(Base):
    __tablename__ = "reviews"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    draft_id: Mapped[str] = mapped_column(ForeignKey("drafts.id"), index=True)
    reviewer: Mapped[str] = mapped_column(String)
    state: Mapped[str] = mapped_column(String, default="pending")      # pending | approved | changes
    # The draft revision (draft_sha) this verdict was cast against. A verdict only
    # counts toward the review policy while reviewed_sha == the draft's current
    # draft_sha: editing the content after approval invalidates (but never deletes)
    # the verdict — it survives as history, no longer current. (create_all handles the
    # column for tests/dev; a migration lands with the later migrations agent.)
    reviewed_sha: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_now)


class ReviewComment(Base):
    __tablename__ = "review_comments"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    draft_id: Mapped[str] = mapped_column(ForeignKey("drafts.id"), index=True)
    author: Mapped[str] = mapped_column(String)
    anchor: Mapped[str] = mapped_column(String, default="")            # "source:4" | "rendered"
    body: Mapped[str] = mapped_column(Text)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_now)


class Environment(Base):
    __tablename__ = "environments"
    id: Mapped[str] = mapped_column(String, primary_key=True)          # == name
    name: Mapped[str] = mapped_column(String)
    protected: Mapped[bool] = mapped_column(Boolean, default=False)
    track_tip: Mapped[bool] = mapped_column(Boolean, default=False)
    rules_version: Mapped[int] = mapped_column(Integer, default=1)


class PointerMove(Base):
    """Append-only live-pointer history. Newest row per (env,prompt,version) is live."""

    __tablename__ = "pointer_moves"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    environment_id: Mapped[str] = mapped_column(ForeignKey("environments.id"), index=True)
    prompt_id: Mapped[str] = mapped_column(String, index=True)
    version_number: Mapped[int] = mapped_column(Integer)
    from_sha: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    to_sha: Mapped[str] = mapped_column(String)
    moved_by: Mapped[str] = mapped_column(String, default="")
    moved_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_now)
    comment: Mapped[str] = mapped_column(Text, default="")


class EnvDefault(Base):
    __tablename__ = "env_defaults"
    __table_args__ = (UniqueConstraint("environment_id", "prompt_id", name="uq_default"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    environment_id: Mapped[str] = mapped_column(ForeignKey("environments.id"), index=True)
    prompt_id: Mapped[str] = mapped_column(String, index=True)
    version_number: Mapped[int] = mapped_column(Integer)


class KillSwitch(Base):
    __tablename__ = "kill_switches"
    __table_args__ = (UniqueConstraint("environment_id", "prompt_id", name="uq_kill"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    environment_id: Mapped[str] = mapped_column(ForeignKey("environments.id"), index=True)
    prompt_id: Mapped[str] = mapped_column(String, index=True)
    engaged: Mapped[bool] = mapped_column(Boolean, default=False)
    by: Mapped[str] = mapped_column(String, default="")
    at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_now)


class Segment(Base):
    __tablename__ = "segments"
    __table_args__ = (UniqueConstraint("environment_id", "name", name="uq_segment"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    environment_id: Mapped[str] = mapped_column(ForeignKey("environments.id"), index=True)
    name: Mapped[str] = mapped_column(String)
    clauses: Mapped[Any] = mapped_column(JSON)                          # condition tree
    version: Mapped[int] = mapped_column(Integer, default=1)


class Rule(Base):
    __tablename__ = "rules"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    environment_id: Mapped[str] = mapped_column(ForeignKey("environments.id"), index=True)
    scope: Mapped[str] = mapped_column(String)                          # global | prompt
    prompt_id: Mapped[Optional[str]] = mapped_column(String, nullable=True, index=True)
    priority: Mapped[int] = mapped_column(Integer, default=10)
    clauses: Mapped[Any] = mapped_column(JSON, nullable=True)           # when-condition
    serve: Mapped[Any] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String, default="active")       # active | paused | archived
    comment: Mapped[str] = mapped_column(Text, default="")


class RuleRevision(Base):
    __tablename__ = "rule_revisions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    environment_id: Mapped[str] = mapped_column(String, index=True)
    rule_id: Mapped[Optional[str]] = mapped_column(String, nullable=True, index=True)
    kind: Mapped[str] = mapped_column(String)                           # rule | segment | pointer | default | kill
    rules_version: Mapped[int] = mapped_column(Integer, default=0, index=True)  # env rules_version after this change
    snapshot: Mapped[Any] = mapped_column(JSON)
    actor: Mapped[str] = mapped_column(String, default="")
    at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_now)
    comment: Mapped[str] = mapped_column(Text, default="")


class Remote(Base):
    __tablename__ = "remotes"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    url: Mapped[str] = mapped_column(String)
    auth_ref: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    last_pushed_sha: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    last_push_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True), nullable=True)


class Principal(Base):
    __tablename__ = "principals"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    kind: Mapped[str] = mapped_column(String)                           # user | service
    subject: Mapped[str] = mapped_column(String, index=True)            # OIDC subject / key label
    name: Mapped[str] = mapped_column(String, default="")
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_now)


class ApiKey(Base):
    __tablename__ = "api_keys"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    principal_id: Mapped[str] = mapped_column(ForeignKey("principals.id"), index=True)
    prefix: Mapped[str] = mapped_column(String, index=True)             # incant_sk_xxxx lookup prefix
    # Hash of the full key. Legacy rows are plain SHA-256(key); with INCANT_KEY_PEPPER
    # set, rows are `v2$` + HMAC-SHA256(pepper, key) and legacy rows upgrade in place on
    # next successful auth. Keys are high-entropy, so both formats resist brute force.
    hash: Mapped[str] = mapped_column(String)
    name: Mapped[str] = mapped_column(String, default="")
    expires_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    last_used_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)


class RoleBinding(Base):
    __tablename__ = "role_bindings"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    principal_id: Mapped[str] = mapped_column(ForeignKey("principals.id"), index=True)
    role: Mapped[str] = mapped_column(String)                           # renderer|viewer|editor|operator|releaser|admin
    project_id: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    environment_id: Mapped[Optional[str]] = mapped_column(String, nullable=True)


class Session(Base):
    """Server-side browser session for the UI. The raw token lives only in the user's
    HttpOnly cookie; here we keep just its hash (hashed exactly like an API key via
    ``hash_key``, pepper-aware) so a DB read never exposes a live credential. API keys
    remain the service-to-service mechanism; sessions are control-plane (UI) only.
    """

    __tablename__ = "sessions"
    id: Mapped[str] = mapped_column(String, primary_key=True)           # s_<hex>
    # Hash of the opaque session token (never the token itself). Same hashing as keys.
    token_hash: Mapped[str] = mapped_column(String, index=True, unique=True)
    principal_id: Mapped[str] = mapped_column(ForeignKey("principals.id"), index=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_now)
    # Absolute expiry — 30d for "remember me", 12h otherwise. No sliding renewal.
    expires_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True))
    last_seen_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_now)
    # Double-submit CSRF token — random hex, stored in the clear (it is not a
    # credential): the client echoes it in the X-Incant-CSRF header on mutations.
    csrf_token: Mapped[str] = mapped_column(String)
    remember: Mapped[bool] = mapped_column(Boolean, default=False)


class AuditLog(Base):
    __tablename__ = "audit_log"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    actor: Mapped[str] = mapped_column(String, index=True)
    action: Mapped[str] = mapped_column(String, index=True)
    object_type: Mapped[str] = mapped_column(String)
    object_id: Mapped[str] = mapped_column(String)
    before: Mapped[Any] = mapped_column(JSON, nullable=True)
    after: Mapped[Any] = mapped_column(JSON, nullable=True)
    at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_now, index=True)
