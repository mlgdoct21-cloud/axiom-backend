"""Add macro_release_market_snapshots table

Revision ID: 007
Revises: 006
Create Date: 2026-05-04 00:00:00.000000

Append-only DXY / SPY / US10Y snapshots taken at T+0 and T+5 minutes after
each macro release. Read by macro_public to compute the deltas the
"📉 Piyasa tepkisi" line shows in Telegram + dashboard.
"""
from alembic import op
import sqlalchemy as sa


revision = '007'
down_revision = '006'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'macro_release_market_snapshots',
        sa.Column('id', sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column('event_id', sa.String(length=64), nullable=False),
        sa.Column('t_offset_seconds', sa.Integer(), nullable=False),
        sa.Column('dxy', sa.Numeric(precision=10, scale=4), nullable=True),
        sa.Column('spy', sa.Numeric(precision=12, scale=4), nullable=True),
        sa.Column('us10y', sa.Numeric(precision=8, scale=4), nullable=True),
        sa.Column('taken_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index(
        'ix_market_snapshots_event_offset',
        'macro_release_market_snapshots',
        ['event_id', 't_offset_seconds'],
    )


def downgrade() -> None:
    op.drop_index('ix_market_snapshots_event_offset', table_name='macro_release_market_snapshots')
    op.drop_table('macro_release_market_snapshots')
