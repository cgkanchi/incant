"""environments.content_version — poll freshness key for non-targeting snapshot inputs

A serve replica rebuilds an environment's in-memory snapshot when its freshness key
moves. ``rules_version`` only moves on targeting changes, so a new validated SHA (the
tip and the servable index), a new version row or a variable default never reached
replicas. ``content_version`` is that missing key: bumped on every environment at once
whenever deployment-wide content inputs change.

Revision ID: b3d8f5a17c92
Revises: a9c4e17f2b60
Create Date: 2026-08-31 12:00:00.000000
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "b3d8f5a17c92"
down_revision: Union[str, None] = "a9c4e17f2b60"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # server_default backfills existing rows; every node starts at 1 and only ever
    # compares against its own cached value, so the starting point is arbitrary.
    op.add_column("environments", sa.Column("content_version", sa.Integer(),
                                            nullable=False, server_default="1"))
    op.create_check_constraint("ck_environment_content_version", "environments",
                               "content_version >= 1")


def downgrade() -> None:
    op.drop_constraint("ck_environment_content_version", "environments", type_="check")
    op.drop_column("environments", "content_version")
