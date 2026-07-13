"""add browser sessions

Server-side sessions for the UI: the raw token lives only in the user's HttpOnly
cookie, and only its hash is stored here (same hashing as API keys). Layered on top of
the baseline schema; SQLite test databases keep using create_all.

Revision ID: 67fb7465ee07
Revises: da3e34b2b8fe
Create Date: 2026-07-12 19:00:00.000000
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '67fb7465ee07'
down_revision: Union[str, None] = 'da3e34b2b8fe'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'sessions',
        sa.Column('id', sa.String(), nullable=False),
        sa.Column('token_hash', sa.String(), nullable=False),
        sa.Column('principal_id', sa.String(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('last_seen_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('csrf_token', sa.String(), nullable=False),
        sa.Column('remember', sa.Boolean(), nullable=False),
        sa.ForeignKeyConstraint(['principal_id'], ['principals.id'], ),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_sessions_principal_id'), 'sessions', ['principal_id'], unique=False)
    op.create_index(op.f('ix_sessions_token_hash'), 'sessions', ['token_hash'], unique=True)


def downgrade() -> None:
    op.drop_index(op.f('ix_sessions_token_hash'), table_name='sessions')
    op.drop_index(op.f('ix_sessions_principal_id'), table_name='sessions')
    op.drop_table('sessions')
