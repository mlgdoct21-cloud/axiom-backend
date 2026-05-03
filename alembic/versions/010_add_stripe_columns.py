"""Add Stripe billing columns to users

Revision ID: 010
Revises: 009
Create Date: 2026-05-04 00:00:00.000000

Stripe checkout writes back stripe_customer_id and stripe_subscription_id;
the webhook flips subscription_status (and tier) on subscription lifecycle
events. All nullable because free users never touch Stripe.
"""
from alembic import op
import sqlalchemy as sa


revision = '010'
down_revision = '009'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('users', sa.Column('stripe_customer_id', sa.String(length=64), nullable=True))
    op.add_column('users', sa.Column('stripe_subscription_id', sa.String(length=64), nullable=True))
    op.add_column('users', sa.Column('subscription_status', sa.String(length=32), nullable=True))


def downgrade() -> None:
    op.drop_column('users', 'subscription_status')
    op.drop_column('users', 'stripe_subscription_id')
    op.drop_column('users', 'stripe_customer_id')
