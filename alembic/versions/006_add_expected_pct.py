"""Add expected_mom_pct + expected_yoy_pct to macro_releases

Revision ID: 006
Revises: 005
Create Date: 2026-05-04 00:00:00.000000

Stores admin-entered consensus values for the MoM and YoY % readings.
We keep them as separate columns from the existing `expected_value`
(raw index level) so the admin entry endpoint can take direct % input
without back-computing from the prior raw index.
"""
from alembic import op
import sqlalchemy as sa


revision = '006'
down_revision = '005'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        'macro_releases',
        sa.Column('expected_mom_pct', sa.Numeric(precision=8, scale=4), nullable=True),
    )
    op.add_column(
        'macro_releases',
        sa.Column('expected_yoy_pct', sa.Numeric(precision=8, scale=4), nullable=True),
    )


def downgrade() -> None:
    op.drop_column('macro_releases', 'expected_yoy_pct')
    op.drop_column('macro_releases', 'expected_mom_pct')
