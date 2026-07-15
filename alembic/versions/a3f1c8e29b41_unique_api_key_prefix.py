"""unique api_key prefix

Make ``api_keys.prefix`` UNIQUE (was a plain index). New keys widen the stored prefix
from 16 to 20 chars in application code; the unique constraint is the backstop that
turns the (astronomically unlikely) prefix collision into a caught IntegrityError that
issuance retries, rather than a silent duplicate that could mis-route a lookup.

Layered on top of the browser-sessions migration.

Revision ID: a3f1c8e29b41
Revises: 67fb7465ee07
Create Date: 2026-07-12 20:30:00.000000
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = 'a3f1c8e29b41'
down_revision: Union[str, None] = '67fb7465ee07'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Existing duplicate prefixes are near-impossible: keys have always been generated
    # from a UUID, and the old 16-char prefix meant a ~50% birthday collision only near
    # ~5k keys — well beyond any current install. We therefore assume none. If a legacy
    # DB somehow held a duplicate, create_unique_constraint fails loudly here and an
    # operator dedupes by hand; we deliberately do NOT silently drop keys.
    op.drop_index(op.f('ix_api_keys_prefix'), table_name='api_keys')
    op.create_unique_constraint('uq_apikey_prefix', 'api_keys', ['prefix'])


def downgrade() -> None:
    op.drop_constraint('uq_apikey_prefix', 'api_keys', type_='unique')
    op.create_index(op.f('ix_api_keys_prefix'), 'api_keys', ['prefix'], unique=False)
