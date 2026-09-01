"""flags-only targeting: drop segments, version labels, rollouts and global rules

Revision ID: a9c4e17f2b60
Revises: f2a7c9d41e58
Create Date: 2026-08-31 12:00:00.000000
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "a9c4e17f2b60"
down_revision: Union[str, None] = "f2a7c9d41e58"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    conn = op.get_bind()
    # Refuse — loudly, with the offenders — rather than silently rewrite targeting: a
    # rule that still relies on a removed construct must be retired or restated as
    # flag clauses by a human before the schema moves on.
    # The LIKE patterns match the removed constructs as JSON OBJECT KEYS (`"label":`),
    # never as values — a flag legitimately NAMED "segment" serialises as
    # `"flag": "segment"` and must not block the upgrade.
    offenders = conn.execute(sa.text(
        "SELECT id FROM rules WHERE scope = 'global' "
        "   OR serve::text LIKE '%\"label\":%' OR serve::text LIKE '%\"rollout\":%' "
        "   OR clauses::text LIKE '%\"segment\":%' ORDER BY id"
    )).scalars().all()
    if offenders:
        raise RuntimeError(
            "cannot upgrade to flags-only targeting: these rules use removed features "
            f"(global scope, label/rollout serve targets, or segment conditions): "
            f"{offenders}. Restate each as a prompt-scoped rule with flag clauses, or "
            "DELETE it (archiving is not enough — the check reads every row), then rerun "
            "the migration.")
    op.drop_index(op.f("ix_segments_environment_id"), table_name="segments")
    op.drop_table("segments")
    op.drop_column("versions", "label")
    op.drop_constraint("ck_rule_prompt_scope", "rules", type_="check")
    op.drop_constraint("ck_rule_scope", "rules", type_="check")
    op.drop_column("rules", "scope")
    op.alter_column("rules", "prompt_id", nullable=False)


def downgrade() -> None:
    op.alter_column("rules", "prompt_id", nullable=True)
    op.add_column("rules", sa.Column("scope", sa.String(), nullable=False,
                                     server_default="prompt"))
    op.create_check_constraint("ck_rule_scope", "rules", "scope IN ('global', 'prompt')")
    op.create_check_constraint(
        "ck_rule_prompt_scope", "rules",
        "(scope = 'prompt' AND prompt_id IS NOT NULL) OR (scope = 'global' AND prompt_id IS NULL)")
    op.add_column("versions", sa.Column("label", sa.String(), nullable=True))
    op.create_table(
        "segments",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("environment_id", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("clauses", sa.JSON(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["environment_id"], ["environments.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("environment_id", "name", name="uq_segment"),
        sa.CheckConstraint("version > 0", name="ck_segment_version"),
    )
    op.create_index(op.f("ix_segments_environment_id"), "segments", ["environment_id"])
