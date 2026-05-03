"""Add sectors_positive / sectors_negative to macro_releases

Revision ID: 005
Revises: 004
Create Date: 2026-05-04 00:00:00.000000

Gemini already produces and validates the sector chip lists for every
narrative; this migration finally persists them so the Telegram broadcaster
and dashboard ReleasePanel can render them next to the narrative.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = '005'
down_revision = '004'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        'macro_releases',
        sa.Column(
            'sectors_positive',
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )
    op.add_column(
        'macro_releases',
        sa.Column(
            'sectors_negative',
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )


def downgrade() -> None:
    op.drop_column('macro_releases', 'sectors_negative')
    op.drop_column('macro_releases', 'sectors_positive')
