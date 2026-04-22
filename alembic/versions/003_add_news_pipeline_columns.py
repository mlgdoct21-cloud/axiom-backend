"""Add two-stage pipeline columns to news_items

Revision ID: 003
Revises: 002
Create Date: 2026-04-21 00:00:00.000000

Adds columns to support the fast-fetch + batch-analyze + smart-broadcast architecture:
  - symbol: FMP stock symbol (for context lookups)
  - analyzed: whether Gemini analysis has run
  - broadcast_at: when Telegram broadcast was delivered
  - is_urgent: routing flag for smart broadcast
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '003'
down_revision = '002'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # All columns use IF NOT EXISTS semantics via try/except pattern at runtime
    # (see services.pipeline_migration). Alembic version is the declarative source of truth.
    with op.batch_alter_table('news_items') as batch_op:
        batch_op.add_column(sa.Column('symbol', sa.String(length=32), nullable=True))
        batch_op.add_column(sa.Column(
            'analyzed', sa.Boolean(),
            nullable=False,
            server_default=sa.text('false')
        ))
        batch_op.add_column(sa.Column(
            'broadcast_at', sa.DateTime(timezone=True),
            nullable=True
        ))
        batch_op.add_column(sa.Column(
            'is_urgent', sa.Boolean(),
            nullable=False,
            server_default=sa.text('false')
        ))
        batch_op.create_index('ix_news_items_symbol', ['symbol'])
        batch_op.create_index('ix_news_items_analyzed', ['analyzed'])
        batch_op.create_index('ix_news_items_created_at', ['created_at'])


def downgrade() -> None:
    with op.batch_alter_table('news_items') as batch_op:
        batch_op.drop_index('ix_news_items_created_at')
        batch_op.drop_index('ix_news_items_analyzed')
        batch_op.drop_index('ix_news_items_symbol')
        batch_op.drop_column('is_urgent')
        batch_op.drop_column('broadcast_at')
        batch_op.drop_column('analyzed')
        batch_op.drop_column('symbol')
