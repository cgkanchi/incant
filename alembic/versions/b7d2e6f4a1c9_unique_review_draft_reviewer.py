"""unique review (draft_id, reviewer)

Enforce one current verdict per (draft, reviewer). ``add_review`` upserts, but with no
unique constraint a concurrent double-submit could insert two rows, after which every
``scalar_one_or_none`` read raised MultipleResultsFound. The constraint closes the race
(add_review now retries the loser as an update).

Revision ID: b7d2e6f4a1c9
Revises: a3f1c8e29b41
Create Date: 2026-07-12 20:31:00.000000
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = 'b7d2e6f4a1c9'
down_revision: Union[str, None] = 'a3f1c8e29b41'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Defensive dedupe (keep the latest verdict): collapse any pre-existing duplicate
    # (draft_id, reviewer) rows before adding the constraint, keeping the newest by id.
    # In practice there should be none — add_review has always upserted a single row —
    # but a pre-constraint DB could have raced a duplicate in, and we must not error out
    # of the migration on it. Postgres-only DELETE..USING (migrations run only on PG).
    op.execute(
        """
        DELETE FROM reviews a
        USING reviews b
        WHERE a.draft_id = b.draft_id
          AND a.reviewer = b.reviewer
          AND a.id < b.id
        """
    )
    op.create_unique_constraint('uq_review', 'reviews', ['draft_id', 'reviewer'])


def downgrade() -> None:
    op.drop_constraint('uq_review', 'reviews', type_='unique')
