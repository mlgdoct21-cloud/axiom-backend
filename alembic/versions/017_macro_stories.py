"""Add macro_stories table for tiered storyteller output

Revision ID: 017
Revises: 016
Create Date: 2026-05-11 00:00:00.000000

Premium ve Advance tier kullanıcılarına gösterilecek 4-5 paragraflık hikaye
formatındaki makro yorumlar `macro_stories` tablosunda yaşar. Free tier'ın
gördüğü tek-paragraf `narrative_md` zaten `macro_releases.narrative_md`
kolonunda; bu tablo onun üstüne çıkar, ezmez.

Unique constraint (event_id, tier) idempotent regen sağlar — admin bir
event_id için Premium veya Advance hikayeyi yeniden üretebilir, INSERT ON
CONFLICT pattern'ı her durumda tek satır tutar.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = '017'
down_revision = '016'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'macro_stories',
        sa.Column('id', sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column('event_id', sa.Text(), nullable=False),
        sa.Column('tier', sa.Text(), nullable=False),
        sa.Column('story_md', sa.Text(), nullable=False),
        sa.Column(
            'meta',
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            'generated_at',
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text('NOW()'),
        ),
        sa.CheckConstraint("tier IN ('premium','advance')", name='ck_macro_stories_tier'),
        sa.UniqueConstraint('event_id', 'tier', name='uq_macro_stories_event_tier'),
    )
    op.create_index(
        'ix_macro_stories_event_id',
        'macro_stories',
        ['event_id'],
    )
    op.create_index(
        'ix_macro_stories_generated_at',
        'macro_stories',
        ['generated_at'],
    )


def downgrade() -> None:
    op.drop_index('ix_macro_stories_generated_at', table_name='macro_stories')
    op.drop_index('ix_macro_stories_event_id', table_name='macro_stories')
    op.drop_table('macro_stories')
