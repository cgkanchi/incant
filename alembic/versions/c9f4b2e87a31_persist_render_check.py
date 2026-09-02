"""Persist whether the test-context render ran for each commit verdict.

``render_checked`` False = the verdict is static-only (the §5 render check was
skipped — ``render_skipped_reason`` says why). Existing rows predate the flag and
were validated when the render check was unconditional-or-silently-skipped; they
are marked False so nobody mistakes an unknown for a proven render.

Revision ID: c9f4b2e87a31
Revises: b3d8f5a17c92
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "c9f4b2e87a31"
down_revision: Union[str, None] = "b3d8f5a17c92"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("commit_validations", sa.Column(
        "render_checked", sa.Boolean(), nullable=False, server_default=sa.text("false")))
    op.add_column("commit_validations", sa.Column(
        "render_skipped_reason", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("commit_validations", "render_skipped_reason")
    op.drop_column("commit_validations", "render_checked")
