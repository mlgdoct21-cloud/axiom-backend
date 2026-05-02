"""Add macro_releases table for Macro Intelligence Layer

Revision ID: 004
Revises: 003
Create Date: 2026-05-03 00:00:00.000000

Schema for FED/CPI/NFP/PCE/FOMC release events. Numbers are stored as
Numeric (never floats) so the number-validator can compare exact strings
against Gemini narrative output. narrative_md is populated post-release
by macro_narrative.py once the LLM step lands (Hafta 5).
"""
from alembic import op
import sqlalchemy as sa


revision = '004'
down_revision = '003'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'macro_releases',
        sa.Column('event_id', sa.String(length=64), primary_key=True),
        sa.Column('event_type', sa.String(length=32), nullable=False),
        sa.Column('country', sa.String(length=8), nullable=False, server_default='US'),
        sa.Column('scheduled_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('released_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('prior_value', sa.Numeric(precision=18, scale=6), nullable=True),
        sa.Column('expected_value', sa.Numeric(precision=18, scale=6), nullable=True),
        sa.Column('actual_value', sa.Numeric(precision=18, scale=6), nullable=True),
        sa.Column('surprise_pct', sa.Numeric(precision=10, scale=4), nullable=True),
        sa.Column('source', sa.String(length=32), nullable=True),
        sa.Column('source_url', sa.Text(), nullable=True),
        sa.Column('narrative_md', sa.Text(), nullable=True),
        sa.Column('sentiment_score', sa.Numeric(precision=4, scale=3), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
    )
    op.create_index('ix_macro_releases_scheduled_at', 'macro_releases', ['scheduled_at'])
    op.create_index('ix_macro_releases_event_type_released_at', 'macro_releases',
                    ['event_type', 'released_at'])
    op.create_index('ix_macro_releases_country', 'macro_releases', ['country'])


def downgrade() -> None:
    op.drop_index('ix_macro_releases_country', table_name='macro_releases')
    op.drop_index('ix_macro_releases_event_type_released_at', table_name='macro_releases')
    op.drop_index('ix_macro_releases_scheduled_at', table_name='macro_releases')
    op.drop_table('macro_releases')
