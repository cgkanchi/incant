"""rule_revisions.state — complete env targeting state per revision

Every targeting bump now records the environment's COMPLETE post-change state
(rules, segments, defaults, kills, live pointers, tips, labels) beside the
per-object snapshot. This is what makes environment rollback total (pointers and
defaults included, not just rules) and §9's ``pin.rules_version`` replay
implementable. Nullable: pre-upgrade rows keep only the per-object snapshot, and
state-based features fall back gracefully for them.

Revision ID: c4e8a17d5b23
Revises: b7d2e6f4a1c9
Create Date: 2026-08-28 12:00:00.000000
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'c4e8a17d5b23'
down_revision: Union[str, None] = 'b7d2e6f4a1c9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('rule_revisions', sa.Column('state', sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column('rule_revisions', 'state')
