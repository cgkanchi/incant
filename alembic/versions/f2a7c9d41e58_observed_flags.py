"""observed flags: serving-traffic flag values for the targeting composer

Revision ID: f2a7c9d41e58
Revises: e9a1c4f27b63
Create Date: 2026-08-30 12:00:00.000000
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "f2a7c9d41e58"
down_revision: Union[str, None] = "e9a1c4f27b63"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # pg_trgm is a trusted contrib extension (PG13+): the database owner may create it
    # without superuser, and every hosted Postgres ships it.
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    op.create_table(
        "observed_flags",
        sa.Column("environment_id", sa.String(), nullable=False),
        sa.Column("flag", sa.String(), nullable=False),
        sa.Column("value", sa.String(), nullable=False),
        sa.Column("value_type", sa.String(), nullable=False),
        sa.Column("first_seen", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["environment_id"], ["environments.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("environment_id", "flag", "value"),
    )
    op.create_index("ix_observed_flags_last_seen", "observed_flags", ["last_seen"])
    op.create_index(
        "ix_observed_flags_value_trgm", "observed_flags", ["value"],
        postgresql_using="gin", postgresql_ops={"value": "gin_trgm_ops"},
    )
    op.create_table(
        "observed_flag_suppressions",
        sa.Column("environment_id", sa.String(), nullable=False),
        sa.Column("flag", sa.String(), nullable=False),
        sa.Column("values_seen", sa.Integer(), nullable=False),
        sa.Column("suppressed_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["environment_id"], ["environments.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("environment_id", "flag"),
    )


def downgrade() -> None:
    op.drop_table("observed_flag_suppressions")
    op.drop_index("ix_observed_flags_value_trgm", table_name="observed_flags")
    op.drop_index("ix_observed_flags_last_seen", table_name="observed_flags")
    op.drop_table("observed_flags")
    # The extension is left in place: other objects may have come to depend on it.
