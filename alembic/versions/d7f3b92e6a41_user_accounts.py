"""users — human accounts with email + password sign-in

One-to-one with principals (kind `user`): roles/bindings stay on the principal, so
RBAC is identical for humans and services. Password hashes are scrypt with encoded
parameters; invite/reset tokens are stored hashed with an expiry.

Revision ID: d7f3b92e6a41
Revises: c4e8a17d5b23
Create Date: 2026-08-29 12:00:00.000000
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'd7f3b92e6a41'
down_revision: Union[str, None] = 'c4e8a17d5b23'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'users',
        sa.Column('id', sa.String(), nullable=False),
        sa.Column('principal_id', sa.String(), nullable=False),
        sa.Column('email', sa.String(), nullable=False),
        sa.Column('name', sa.String(), nullable=False),
        sa.Column('password_hash', sa.String(), nullable=True),
        sa.Column('status', sa.String(), nullable=False),
        sa.Column('invite_token_hash', sa.String(), nullable=True),
        sa.Column('invite_expires_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('last_login_at', sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(['principal_id'], ['principals.id'], ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('principal_id'),
        sa.UniqueConstraint('email'),
    )
    op.create_index(op.f('ix_users_email'), 'users', ['email'], unique=True)
    op.create_index(op.f('ix_users_invite_token_hash'), 'users', ['invite_token_hash'],
                    unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_users_invite_token_hash'), table_name='users')
    op.drop_index(op.f('ix_users_email'), table_name='users')
    op.drop_table('users')
