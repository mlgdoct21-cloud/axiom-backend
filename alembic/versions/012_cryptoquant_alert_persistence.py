"""Add cryptoquant alert log + cooldown tables

Revision ID: 012
Revises: 011
Create Date: 2026-05-04 00:00:00.000000

T1 — daily budget + cooldown moved from in-memory dicts to Postgres so:
  · multi-replica deploy doesn't double-send alerts
  · Railway restart doesn't reset cooldowns mid-event
  · alert log table also feeds the future "Son 7 Gün Alarmlar" dashboard widget (T2)
"""
from alembic import op
import sqlalchemy as sa


revision = '012'
down_revision = '011'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'cryptoquant_alert_log',
        sa.Column('id', sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column('telegram_id', sa.String(32), nullable=False),
        sa.Column('alert_key', sa.String(64), nullable=False),
        sa.Column('severity', sa.String(16), nullable=True),
        sa.Column('title', sa.String(128), nullable=True),
        sa.Column('sent_at', sa.TIMESTAMP(timezone=True), server_default=sa.text('NOW()'), nullable=False),
        sa.Column('sent_date', sa.Date(), nullable=False),
    )
    op.create_index('ix_cq_alert_log_user_date', 'cryptoquant_alert_log', ['telegram_id', 'sent_date'])
    op.create_index('ix_cq_alert_log_sent_at',  'cryptoquant_alert_log', ['sent_at'])

    op.create_table(
        'cryptoquant_alert_cooldown',
        sa.Column('alert_key', sa.String(64), primary_key=True),
        sa.Column('expires_at', sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column('updated_at', sa.TIMESTAMP(timezone=True), server_default=sa.text('NOW()'), nullable=False),
    )
    op.create_index('ix_cq_alert_cooldown_expires', 'cryptoquant_alert_cooldown', ['expires_at'])


def downgrade() -> None:
    op.drop_index('ix_cq_alert_cooldown_expires', table_name='cryptoquant_alert_cooldown')
    op.drop_table('cryptoquant_alert_cooldown')
    op.drop_index('ix_cq_alert_log_sent_at',  table_name='cryptoquant_alert_log')
    op.drop_index('ix_cq_alert_log_user_date', table_name='cryptoquant_alert_log')
    op.drop_table('cryptoquant_alert_log')
