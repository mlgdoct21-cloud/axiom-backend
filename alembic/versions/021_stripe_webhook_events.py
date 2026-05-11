"""Add stripe_webhook_events table for webhook idempotency.

Revision ID: 021
Revises: 020
Create Date: 2026-05-11 21:00:00.000000

Stripe retries on any non-2xx response and can also redeliver the same
event.id after a network blip. Without dedupe, a replay can corrupt
subscription state — e.g. a late `customer.subscription.updated` arriving
after `customer.subscription.deleted` can re-upgrade a canceled user, and
a re-delivered `invoice.payment_failed` after a successful retry can
re-flag a healthy subscription as past_due.

This table records every processed event.id once. The webhook handler
does INSERT … ON CONFLICT DO NOTHING and short-circuits if the row
already existed — race-safe under concurrent deliveries.
"""
from alembic import op
import sqlalchemy as sa


revision = '021'
down_revision = '020'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'stripe_webhook_events',
        sa.Column('event_id', sa.Text(), primary_key=True),
        sa.Column('event_type', sa.Text(), nullable=False),
        sa.Column(
            'processed_at',
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text('NOW()'),
        ),
    )
    # Index on processed_at for retention-window cleanup queries
    # (Stripe retains events 30 days; we can prune older rows on schedule).
    op.create_index(
        'ix_stripe_webhook_events_processed_at',
        'stripe_webhook_events',
        ['processed_at'],
    )


def downgrade() -> None:
    op.drop_index('ix_stripe_webhook_events_processed_at', table_name='stripe_webhook_events')
    op.drop_table('stripe_webhook_events')
