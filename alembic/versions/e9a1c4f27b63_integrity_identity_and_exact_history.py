"""integrity checks, principal-backed review identity, exact pointer history

Revision ID: e9a1c4f27b63
Revises: d7f3b92e6a41
Create Date: 2026-08-29 18:00:00.000000
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "e9a1c4f27b63"
down_revision: Union[str, None] = "d7f3b92e6a41"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Immutable principal identity replaces mutable/non-unique display names for
    # policy decisions. Legacy rows receive stable synthetic identities; future
    # writes store the authenticated principal id.
    op.add_column("drafts", sa.Column("author_principal_id", sa.String(), nullable=True))
    op.add_column("reviews", sa.Column("reviewer_principal_id", sa.String(), nullable=True))
    op.add_column(
        "review_comments", sa.Column("author_principal_id", sa.String(), nullable=True)
    )
    op.execute("UPDATE drafts SET author_principal_id = 'legacy:' || author")
    op.execute("UPDATE reviews SET reviewer_principal_id = 'legacy:' || reviewer")
    op.execute("UPDATE review_comments SET author_principal_id = 'legacy:' || author")
    op.alter_column("drafts", "author_principal_id", nullable=False)
    op.alter_column("reviews", "reviewer_principal_id", nullable=False)
    op.alter_column("review_comments", "author_principal_id", nullable=False)
    op.create_index("ix_drafts_author_principal_id", "drafts", ["author_principal_id"])
    op.create_index("ix_reviews_reviewer_principal_id", "reviews", ["reviewer_principal_id"])
    op.create_index(
        "ix_review_comments_author_principal_id",
        "review_comments",
        ["author_principal_id"],
    )
    op.drop_constraint("uq_review", "reviews", type_="unique")
    op.create_unique_constraint(
        "uq_review_principal", "reviews", ["draft_id", "reviewer_principal_id"]
    )

    # A nullable destination is an append-only tombstone used by exact rollback.
    op.alter_column("pointer_moves", "to_sha", existing_type=sa.String(), nullable=True)

    # Repair only corrupt legacy revision identifiers. Healthy identifiers remain
    # stable. Duplicate and non-positive rows are first moved to unique temporary
    # negative values, then allocated after the environment's highest valid version.
    op.execute(
        """
        WITH ranked AS (
          SELECT id, rules_version,
                 row_number() OVER (
                   PARTITION BY environment_id, rules_version ORDER BY at, id
                 ) AS duplicate_number
          FROM rule_revisions
        )
        UPDATE rule_revisions AS rr
           SET rules_version = -rr.id
          FROM ranked
         WHERE rr.id = ranked.id
           AND (ranked.rules_version < 1 OR ranked.duplicate_number > 1)
        """
    )
    op.execute(
        """
        WITH maxima AS (
          SELECT environment_id, COALESCE(MAX(rules_version), 0) AS max_version
          FROM rule_revisions WHERE rules_version > 0 GROUP BY environment_id
        ), repairs AS (
          SELECT rr.id, rr.environment_id,
                 COALESCE(maxima.max_version, 0) + row_number() OVER (
                   PARTITION BY rr.environment_id ORDER BY rr.at, rr.id
                 ) AS repaired_version
          FROM rule_revisions AS rr
          LEFT JOIN maxima ON maxima.environment_id = rr.environment_id
          WHERE rr.rules_version < 1
        )
        UPDATE rule_revisions AS rr
           SET rules_version = repairs.repaired_version
          FROM repairs
         WHERE rr.id = repairs.id
        """
    )
    op.execute("UPDATE environments SET rules_version = 1 WHERE rules_version < 1")
    op.execute(
        """
        UPDATE environments AS env
           SET rules_version = latest.max_version
          FROM (
            SELECT environment_id, MAX(rules_version) AS max_version
            FROM rule_revisions GROUP BY environment_id
          ) AS latest
         WHERE env.id = latest.environment_id
           AND env.rules_version < latest.max_version
        """
    )
    op.create_unique_constraint(
        "uq_rule_revision_version",
        "rule_revisions",
        ["environment_id", "rules_version"],
    )

    # Normalize the one historical spelling before enforcing the current vocabulary.
    op.execute("UPDATE reviews SET state = 'changes_requested' WHERE state = 'changes'")
    # Global rules never legitimately carry a prompt_id, but the pre-constraint
    # merge path could leave one behind on a rescope — scrub before ck_rule_prompt_scope
    # lands, or the constraint creation itself fails on such a row.
    op.execute("UPDATE rules SET prompt_id = NULL WHERE scope = 'global' AND prompt_id IS NOT NULL")

    checks = [
        ("projects", "ck_project_review_policy", "review_policy >= 0"),
        ("versions", "ck_version_number", "number > 0"),
        ("versions", "ck_version_status", "status IN ('active', 'archived')"),
        ("commit_validations", "ck_validation_version", "version_number > 0"),
        ("commit_validations", "ck_validation_status", "status IN ('valid', 'invalid')"),
        ("drafts", "ck_draft_version", "version_number IS NULL OR version_number > 0"),
        ("drafts", "ck_draft_status",
         "status IN ('open', 'approved', 'committed', 'discarded', 'abandoned')"),
        ("reviews", "ck_review_state",
         "state IN ('pending', 'approved', 'changes_requested')"),
        ("environments", "ck_environment_rules_version", "rules_version >= 1"),
        ("pointer_moves", "ck_pointer_version", "version_number > 0"),
        ("segments", "ck_segment_version", "version > 0"),
        ("rules", "ck_rule_scope", "scope IN ('global', 'prompt')"),
        ("rules", "ck_rule_status", "status IN ('active', 'paused', 'archived')"),
        ("rules", "ck_rule_priority", "priority BETWEEN 0 AND 1000000"),
        ("rules", "ck_rule_prompt_scope",
         "(scope = 'prompt' AND prompt_id IS NOT NULL) OR "
         "(scope = 'global' AND prompt_id IS NULL)"),
        ("rule_revisions", "ck_rule_revision_version", "rules_version >= 1"),
        ("principals", "ck_principal_kind", "kind IN ('user', 'service')"),
        ("users", "ck_user_status", "status IN ('invited', 'active', 'disabled')"),
        ("role_bindings", "ck_role_binding_role",
         "role IN ('renderer', 'viewer', 'editor', 'operator', 'releaser', 'admin')"),
    ]
    for table, name, condition in checks:
        op.create_check_constraint(name, table, condition)


def downgrade() -> None:
    checks = [
        ("role_bindings", "ck_role_binding_role"),
        ("users", "ck_user_status"),
        ("principals", "ck_principal_kind"),
        ("rule_revisions", "ck_rule_revision_version"),
        ("rules", "ck_rule_prompt_scope"),
        ("rules", "ck_rule_priority"),
        ("rules", "ck_rule_status"),
        ("rules", "ck_rule_scope"),
        ("segments", "ck_segment_version"),
        ("pointer_moves", "ck_pointer_version"),
        ("environments", "ck_environment_rules_version"),
        ("reviews", "ck_review_state"),
        ("drafts", "ck_draft_status"),
        ("drafts", "ck_draft_version"),
        ("commit_validations", "ck_validation_status"),
        ("commit_validations", "ck_validation_version"),
        ("versions", "ck_version_status"),
        ("versions", "ck_version_number"),
        ("projects", "ck_project_review_policy"),
    ]
    for table, name in checks:
        op.drop_constraint(name, table, type_="check")

    op.drop_constraint("uq_rule_revision_version", "rule_revisions", type_="unique")
    op.execute("DELETE FROM pointer_moves WHERE to_sha IS NULL")
    op.alter_column("pointer_moves", "to_sha", existing_type=sa.String(), nullable=False)

    op.drop_constraint("uq_review_principal", "reviews", type_="unique")
    op.create_unique_constraint("uq_review", "reviews", ["draft_id", "reviewer"])
    op.drop_index("ix_review_comments_author_principal_id", table_name="review_comments")
    op.drop_index("ix_reviews_reviewer_principal_id", table_name="reviews")
    op.drop_index("ix_drafts_author_principal_id", table_name="drafts")
    op.drop_column("review_comments", "author_principal_id")
    op.drop_column("reviews", "reviewer_principal_id")
    op.drop_column("drafts", "author_principal_id")
