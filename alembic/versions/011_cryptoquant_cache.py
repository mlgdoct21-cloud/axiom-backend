"""Add cryptoquant_cache table

Revision ID: 011
Revises: 010
Create Date: 2026-05-04 00:00:00.000000

On-chain metric cache for CryptoQuant API responses.
metric_key + symbol + window is the natural unique key.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = '011'
down_revision = '010'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'cryptoquant_cache',
        sa.Column('id', sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column('metric_key', sa.String(64), nullable=False),
        sa.Column('symbol', sa.String(16), nullable=False),
        sa.Column('window', sa.String(16), nullable=False),
        sa.Column('data', postgresql.JSONB(), nullable=False),
        sa.Column('fetched_at', sa.TIMESTAMP(timezone=True), server_default=sa.text('NOW()'), nullable=False),
        sa.Column('expires_at', sa.TIMESTAMP(timezone=True), nullable=False),
    )
    op.create_index(
        'uq_cryptoquant_cache_key_sym_win',
        'cryptoquant_cache',
        ['metric_key', 'symbol', 'window'],
        unique=True,
    )
    op.create_index(
        'ix_cryptoquant_cache_expires',
        'cryptoquant_cache',
        ['expires_at'],
    )


def downgrade() -> None:
    op.drop_index('ix_cryptoquant_cache_expires', table_name='cryptoquant_cache')
    op.drop_index('uq_cryptoquant_cache_key_sym_win', table_name='cryptoquant_cache')
    op.drop_table('cryptoquant_cache')
