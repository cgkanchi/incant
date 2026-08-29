"""Control-plane ORM models. No template content lives here — only SHAs and state.

Mirrors the schema sketch in DESIGN.md §13.
"""

from __future__ import annotations

import datetime as dt
from typing import Any, Optional

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
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
    __table_args__ = (CheckConstraint("review_policy >= 0", name="ck_project_review_policy"),)
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
    __table_args__ = (
        UniqueConstraint("prompt_id", "number", name="uq_version"),
        CheckConstraint("number > 0", name="ck_version_number"),
        CheckConstraint("status IN ('active', 'archived')", name="ck_version_status"),
    )
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
    __table_args__ = (
        UniqueConstraint("sha", "path", name="uq_validation"),
        CheckConstraint("version_number > 0", name="ck_validation_version"),
        CheckConstraint("status IN ('valid', 'invalid')", name="ck_validation_status"),
    )
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
    __table_args__ = (
        CheckConstraint("version_number IS NULL OR version_number > 0", name="ck_draft_version"),
        CheckConstraint(
            "status IN ('open', 'approved', 'committed', 'discarded', 'abandoned')",
            name="ck_draft_status",
        ),
    )
    id: Mapped[str] = mapped_column(String, primary_key=True)          # d_1042
    prompt_id: Mapped[str] = mapped_column(String, index=True)
    version_number: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)  # None = new version
    base_sha: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    git_ref: Mapped[str] = mapped_column(String)                       # refs/incant/drafts/<id>
    draft_sha: Mapped[Optional[str]] = mapped_column(String, nullable=True)  # current draft commit
    title: Mapped[str] = mapped_column(String, default="")
    author: Mapped[str] = mapped_column(String, default="")
    # Immutable identity used for authorization/policy. ``author`` is only the
    # historical display-name snapshot.
    author_principal_id: Mapped[str] = mapped_column(String, default="", index=True)
    status: Mapped[str] = mapped_column(String, default="open")        # open | approved | committed | abandoned
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)


class Review(Base):
    __tablename__ = "reviews"
    # One current verdict per (draft, reviewer): add_review upserts, and the unique
    # constraint stops a concurrent double-submit from creating duplicate rows (which
    # would make every later scalar_one_or_none read raise MultipleResultsFound).
    __table_args__ = (
        UniqueConstraint("draft_id", "reviewer_principal_id", name="uq_review_principal"),
        CheckConstraint(
            "state IN ('pending', 'approved', 'changes_requested')",
            name="ck_review_state",
        ),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    draft_id: Mapped[str] = mapped_column(ForeignKey("drafts.id"), index=True)
    reviewer: Mapped[str] = mapped_column(String)
    reviewer_principal_id: Mapped[str] = mapped_column(String, default="", index=True)
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
    author_principal_id: Mapped[str] = mapped_column(String, default="", index=True)
    anchor: Mapped[str] = mapped_column(String, default="")            # "source:4" | "rendered"
    body: Mapped[str] = mapped_column(Text)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_now)


class Environment(Base):
    __tablename__ = "environments"
    __table_args__ = (CheckConstraint("rules_version >= 1", name="ck_environment_rules_version"),)
    id: Mapped[str] = mapped_column(String, primary_key=True)          # == name
    name: Mapped[str] = mapped_column(String)
    protected: Mapped[bool] = mapped_column(Boolean, default=False)
    track_tip: Mapped[bool] = mapped_column(Boolean, default=False)
    rules_version: Mapped[int] = mapped_column(Integer, default=1)


class PointerMove(Base):
    """Append-only live-pointer history. Newest row per (env,prompt,version) is live."""

    __tablename__ = "pointer_moves"
    __table_args__ = (CheckConstraint("version_number > 0", name="ck_pointer_version"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    environment_id: Mapped[str] = mapped_column(ForeignKey("environments.id"), index=True)
    prompt_id: Mapped[str] = mapped_column(String, index=True)
    version_number: Mapped[int] = mapped_column(Integer)
    from_sha: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    # ``None`` is an append-only tombstone: rollback can restore the exact state
    # from before this pointer existed without deleting its audit history.
    to_sha: Mapped[Optional[str]] = mapped_column(String, nullable=True)
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
    __table_args__ = (
        UniqueConstraint("environment_id", "name", name="uq_segment"),
        CheckConstraint("version > 0", name="ck_segment_version"),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    environment_id: Mapped[str] = mapped_column(ForeignKey("environments.id"), index=True)
    name: Mapped[str] = mapped_column(String)
    clauses: Mapped[Any] = mapped_column(JSON)                          # condition tree
    version: Mapped[int] = mapped_column(Integer, default=1)


class Rule(Base):
    __tablename__ = "rules"
    __table_args__ = (
        CheckConstraint("scope IN ('global', 'prompt')", name="ck_rule_scope"),
        CheckConstraint("status IN ('active', 'paused', 'archived')", name="ck_rule_status"),
        CheckConstraint("priority BETWEEN 0 AND 1000000", name="ck_rule_priority"),
        CheckConstraint(
            "(scope = 'prompt' AND prompt_id IS NOT NULL) OR "
            "(scope = 'global' AND prompt_id IS NULL)",
            name="ck_rule_prompt_scope",
        ),
    )
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
    __table_args__ = (
        UniqueConstraint("environment_id", "rules_version", name="uq_rule_revision_version"),
        CheckConstraint("rules_version >= 1", name="ck_rule_revision_version"),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    environment_id: Mapped[str] = mapped_column(String, index=True)
    rule_id: Mapped[Optional[str]] = mapped_column(String, nullable=True, index=True)
    kind: Mapped[str] = mapped_column(String)                           # rule | segment | pointer | default | kill
    rules_version: Mapped[int] = mapped_column(Integer, default=1, index=True)  # env rules_version after this change
    snapshot: Mapped[Any] = mapped_column(JSON)                         # the changed object (revision-list display)
    # COMPLETE environment targeting state after this change — materialized only on
    # CHECKPOINT revisions (baseline/rollback/every Kth); other revisions carry only
    # their per-object snapshot and reconstruct via state_at's forward replay.
    # none_as_null matters: a Python None must land as SQL NULL, or the
    # `state IS NOT NULL` checkpoint queries would happily return JSON-null rows.
    state: Mapped[Optional[Any]] = mapped_column(JSON(none_as_null=True), nullable=True)
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
    __table_args__ = (CheckConstraint("kind IN ('user', 'service')", name="ck_principal_kind"),)
    id: Mapped[str] = mapped_column(String, primary_key=True)
    kind: Mapped[str] = mapped_column(String)                           # user | service
    subject: Mapped[str] = mapped_column(String, index=True)            # OIDC subject / key label
    name: Mapped[str] = mapped_column(String, default="")
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_now)


class User(Base):
    """A human account: email + password sign-in for the UI (the §11 'Users' door).

    One-to-one with a ``Principal`` (kind ``user``) — roles/bindings/keys stay on the
    principal, so RBAC is identical for humans and services. The password hash is
    scrypt with per-user salt and self-describing parameters (see server/passwords.py);
    invites/resets store only the token's hash, never the token."""

    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint("status IN ('invited', 'active', 'disabled')", name="ck_user_status"),
    )
    id: Mapped[str] = mapped_column(String, primary_key=True)            # u_<hex>
    principal_id: Mapped[str] = mapped_column(ForeignKey("principals.id"), unique=True)
    email: Mapped[str] = mapped_column(String, unique=True, index=True)  # stored lowercase
    name: Mapped[str] = mapped_column(String, default="")
    # None until the invite is accepted (or after an admin reset that forces a new one).
    password_hash: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    status: Mapped[str] = mapped_column(String, default="invited")       # invited | active | disabled
    invite_token_hash: Mapped[Optional[str]] = mapped_column(String, nullable=True, index=True)
    invite_expires_at: Mapped[Optional[dt.datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_now)
    last_login_at: Mapped[Optional[dt.datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True)


class ApiKey(Base):
    __tablename__ = "api_keys"
    # The lookup prefix is UNIQUE: 24 random bits (raw[:16]) collided near ~5k keys, so
    # new keys widen it (raw[:20] = 40 bits) and issuance regenerates on any collision.
    # The unique constraint is the backstop that makes that regeneration observable.
    __table_args__ = (UniqueConstraint("prefix", name="uq_apikey_prefix"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    principal_id: Mapped[str] = mapped_column(ForeignKey("principals.id"), index=True)
    prefix: Mapped[str] = mapped_column(String)                         # incant_sk_xxxx lookup prefix (unique)
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
    __table_args__ = (
        CheckConstraint(
            "role IN ('renderer', 'viewer', 'editor', 'operator', 'releaser', 'admin')",
            name="ck_role_binding_role",
        ),
    )
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
