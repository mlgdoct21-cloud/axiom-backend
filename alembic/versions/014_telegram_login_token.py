"""Add telegram_login_token table for deep-link auth

Revision ID: 014
Revises: 013
Create Date: 2026-05-04 00:00:00.000000

T7 — Production-correct dashboard login. Telegram bot generates a one-time
token via /login command, user clicks the deep-link, dashboard exchanges
token for JWT. 5-minute TTL, one-time-use enforced via used_at.
"""
from alembic import op
import sqlalchemy as sa


revision = '014'
down_revision = '013'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'telegram_login_token',
        sa.Column('token', sa.String(64), primary_key=True),
        sa.Column('telegram_id', sa.String(32), nullable=False),
        sa.Column('created_at', sa.TIMESTAMP(timezone=True), server_default=sa.text('NOW()'), nullable=False),
        sa.Column('expires_at', sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column('used_at', sa.TIMESTAMP(timezone=True), nullable=True),
    )
    op.create_index('ix_telegram_login_token_expires', 'telegram_login_token', ['expires_at'])
    op.create_index('ix_telegram_login_token_telegram_id', 'telegram_login_token', ['telegram_id'])


def downgrade() -> None:
    op.drop_index('ix_telegram_login_token_telegram_id', table_name='telegram_login_token')
    op.drop_index('ix_telegram_login_token_expires', table_name='telegram_login_token')
    op.drop_table('telegram_login_token')
