"""Add macro_releases.last_broadcast_at

Revision ID: 009
Revises: 008
Create Date: 2026-05-04 00:00:00.000000

Tracks when a release was last fanned out to Telegram so /macro/latest can
sort by "most recently broadcast" rather than "most recently inserted".
Multiple releases share the same released_at (FRED stores observation period
start), and a stale row whose narrative was re-generated for some other
reason was outranking a freshly-broadcast row.
"""
from alembic import op
import sqlalchemy as sa


revision = '009'
down_revision = '008'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        'macro_releases',
        sa.Column('last_broadcast_at', sa.DateTime(timezone=True), nullable=True),
    )
    # Backfill with created_at so existing rows are sortable from day one.
    op.execute("UPDATE macro_releases SET last_broadcast_at = created_at WHERE last_broadcast_at IS NULL")


def downgrade() -> None:
    op.drop_column('macro_releases', 'last_broadcast_at')
