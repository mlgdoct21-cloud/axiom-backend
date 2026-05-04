"""Add feature_quota_log for dashboard feature gating

Revision ID: 015
Revises: 014
Create Date: 2026-05-04 00:00:00.000000

C2 — Sliding 24h quota for dashboard tabs (Whitepaper + On-Chain).
Free 2/24h per tab, premium/advance unlimited. Each visit logged with
used_at; check counts rows where used_at > NOW() - 24h.
"""
from alembic import op
import sqlalchemy as sa


revision = '015'
down_revision = '014'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'feature_quota_log',
        sa.Column('id', sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column('telegram_id', sa.String(32), nullable=False),
        sa.Column('command', sa.String(64), nullable=False),
        sa.Column('used_at', sa.TIMESTAMP(timezone=True), server_default=sa.text('NOW()'), nullable=False),
    )
    op.create_index('ix_feature_quota_log_user_cmd_used', 'feature_quota_log', ['telegram_id', 'command', 'used_at'])
    op.create_index('ix_feature_quota_log_used_at', 'feature_quota_log', ['used_at'])


def downgrade() -> None:
    op.drop_index('ix_feature_quota_log_used_at', table_name='feature_quota_log')
    op.drop_index('ix_feature_quota_log_user_cmd_used', table_name='feature_quota_log')
    op.drop_table('feature_quota_log')
