"""Add current_period_end to users for Settings page subscription display

Revision ID: 016
Revises: 015
Create Date: 2026-05-06 00:00:00.000000

Stripe webhook events carry `current_period_end` (Unix ts) on subscription
objects but we previously discarded it. The Settings page needs it to show
"Premium until 15 Jun" so paying users can see what they're paying for.

Existing premium rows are NULL after upgrade — a one-shot backfill script
(scripts/backfill_period_end.py) reads the Stripe subscription for each
non-null stripe_subscription_id and writes the field. New webhook events
populate it going forward.
"""
from alembic import op
import sqlalchemy as sa


revision = '016'
down_revision = '015'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        'users',
        sa.Column('current_period_end', sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column('users', 'current_period_end')
