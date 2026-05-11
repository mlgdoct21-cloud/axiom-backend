"""Add macro_release_revisions audit table + macro_releases.revision_count

Revision ID: 019
Revises: 018
Create Date: 2026-05-11 13:00:00.000000

Mevcut release_detect.py `ON CONFLICT (event_id) DO NOTHING` ile yazıyor —
FRED/BLS bir önceki ay rakamını revize ettiğinde DB sessizce eski değeri
tutuyor. Advance tier'ın "trader edge" özelliği için revizyonları
yakalayıp Telegram push'lamamız gerek.

Bu migration iki şey yapıyor:
1. `macro_release_revisions` audit tablosu — her revizyonun (event_id,
   old_value, new_value, delta_pct, detected_at) satırını tutuyor.
   Trader'ın "Aralık NFP 6 ay sonra hâlâ revize oluyor mu?" sorusuna
   cevap verecek tarihsel veri.
2. `macro_releases.revision_count` — hızlı "kaç kez revize edildi" sayacı.
   Default 0; release_detect UPSERT akışı her revizyonda +1 atacak.
"""
from alembic import op
import sqlalchemy as sa


revision = '019'
down_revision = '018'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'macro_release_revisions',
        sa.Column('id', sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column('event_id', sa.Text(), nullable=False),
        sa.Column('event_type', sa.Text(), nullable=False),
        sa.Column('source', sa.Text(), nullable=False),
        sa.Column('old_actual_value', sa.Numeric(20, 6), nullable=True),
        sa.Column('new_actual_value', sa.Numeric(20, 6), nullable=False),
        sa.Column('delta_abs', sa.Numeric(20, 6), nullable=True),
        sa.Column('delta_pct', sa.Numeric(10, 4), nullable=True),
        sa.Column(
            'detected_at',
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text('NOW()'),
        ),
        sa.Column('broadcasted_at', sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        'ix_macro_revisions_event_id',
        'macro_release_revisions',
        ['event_id'],
    )
    op.create_index(
        'ix_macro_revisions_detected_at',
        'macro_release_revisions',
        ['detected_at'],
    )

    op.add_column(
        'macro_releases',
        sa.Column(
            'revision_count',
            sa.Integer(),
            nullable=False,
            server_default=sa.text('0'),
        ),
    )


def downgrade() -> None:
    op.drop_column('macro_releases', 'revision_count')
    op.drop_index('ix_macro_revisions_detected_at', table_name='macro_release_revisions')
    op.drop_index('ix_macro_revisions_event_id', table_name='macro_release_revisions')
    op.drop_table('macro_release_revisions')
