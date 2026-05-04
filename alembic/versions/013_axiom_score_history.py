"""Add axiom_score_history table

Revision ID: 013
Revises: 012
Create Date: 2026-05-04 00:00:00.000000

T5 — daily axiom score snapshot for trend tracking.
Sabah brifinginde 'dün 65 → bugün 73 (+8)' karşılaştırması bu tabloya
yazılmış son iki günün satırından üretilir.
"""
from alembic import op
import sqlalchemy as sa


revision = '013'
down_revision = '012'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'axiom_score_history',
        sa.Column('id', sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column('symbol', sa.String(16), nullable=False),
        sa.Column('score', sa.Numeric(5, 2), nullable=False),
        sa.Column('score_zone', sa.String(16), nullable=False),
        sa.Column('recorded_at', sa.TIMESTAMP(timezone=True), server_default=sa.text('NOW()'), nullable=False),
        sa.Column('recorded_date', sa.Date(), nullable=False),
    )
    op.create_index('uq_axiom_score_history_sym_date', 'axiom_score_history', ['symbol', 'recorded_date'], unique=True)
    op.create_index('ix_axiom_score_history_recorded_at', 'axiom_score_history', ['recorded_at'])


def downgrade() -> None:
    op.drop_index('ix_axiom_score_history_recorded_at', table_name='axiom_score_history')
    op.drop_index('uq_axiom_score_history_sym_date', table_name='axiom_score_history')
    op.drop_table('axiom_score_history')
