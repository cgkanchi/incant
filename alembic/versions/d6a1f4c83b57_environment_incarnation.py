"""environments.incarnation — immutable row identity against env-id reuse (ABA)

``rules_version`` and ``content_version`` restart at 1 when an environment is deleted
and recreated under the SAME id, so a node whose poll straddled the delete+recreate
could see identical freshness keys and keep serving the dead environment's snapshot
(and its §9 replay memos) forever. ``incarnation`` is an opaque, equality-only token
minted per ROW: the poll compares it alongside the counters and treats a change as
evict-then-rebuild. It is never updated in place — a rename mints a new one, because
a rename is a new identity to the snapshot caches.

Revision ID: d6a1f4c83b57
Revises: c9f4b2e87a31
Create Date: 2026-09-01 12:00:00.000000
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "d6a1f4c83b57"
down_revision: Union[str, None] = "c9f4b2e87a31"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # gen_random_uuid() is core Postgres since 13 (no pgcrypto extension needed). A
    # VOLATILE column default makes ADD COLUMN rewrite the table evaluating the
    # default once PER ROW, so every EXISTING environment is backfilled with its own
    # DISTINCT incarnation — the same semantics new rows get from the ORM default.
    # (A shared sentinel would also be safe — the ABA risk only starts at the next
    # delete/recreate — but distinct values cost nothing here and never surprise.)
    op.add_column("environments", sa.Column(
        "incarnation", sa.String(), nullable=False,
        server_default=sa.text("gen_random_uuid()::text")))


def downgrade() -> None:
    op.drop_column("environments", "incarnation")
