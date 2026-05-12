"""Add published_at to macro_releases.

Revision ID: 022
Revises: 021
Create Date: 2026-05-12 15:00:00.000000

`released_at` historically holds the observation period start (Apr CPI →
2026-04-01) because FRED's series/observations endpoint exposes the period
start, not the publication date. The storyteller has been writing
"1 Nisan 2026'da açıklanan" — misleading because the data is FOR April but
was actually PUBLISHED on May 12.

FMP economic-calendar (primary release source since 2026-05-12) exposes the
real publication timestamp ("2026-05-12 12:30:00 UTC"). This migration adds
a `published_at` column to capture it. Storyteller prompts will say:
"Nisan 2026 enflasyon verileri 12 Mayıs'ta açıklandı..."

Nullable — historic rows from FRED-only era won't have this; the storyteller
falls back to released_at + "yayımlandı" wording when published_at is null.
"""
from alembic import op
import sqlalchemy as sa


revision = "022"
down_revision = "021"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "macro_releases",
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade():
    op.drop_column("macro_releases", "published_at")
