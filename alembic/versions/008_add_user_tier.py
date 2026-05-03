"""Add users.tier column

Revision ID: 008
Revises: 007
Create Date: 2026-05-04 00:00:00.000000

Tier guard for macro broadcasts:
- free      → 5-min delay queue + filigran prefix
- premium   → instant, no watermark
- advance   → instant, no watermark, future advanced features
"""
from alembic import op
import sqlalchemy as sa


revision = '008'
down_revision = '007'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        'users',
        sa.Column('tier', sa.String(length=20), nullable=False, server_default='free'),
    )


def downgrade() -> None:
    op.drop_column('users', 'tier')
