"""Add crypto_intel_stories table for Crypto Intel storyteller cache

Revision ID: 024
Revises: 023
Create Date: 2026-05-13 09:00:00.000000

3 tab (overview/erc20/stable) × 2 tier (premium/advance) = 6 satır.
Scheduler her 6 saatte refresh; UPSERT (tab, tier) idempotent.

`story_md` = Gemini'den gelen 3-4 cümlelik narrative (Premium)
             veya 5-6 cümlelik market commentary (Advance).
`action_box` = deterministic kural tabanlı aksiyon listesi (LLM değil),
               halüsinasyon riski yok — score zone'a göre fix template.
`source_snapshot` = narrative üretildiğinde girdiler (numerik değerler) —
                    user data-yaşı + sentence-window validator için.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = '024'
down_revision = '023'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'crypto_intel_stories',
        sa.Column('id', sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column('tab', sa.Text(), nullable=False),
        sa.Column('tier', sa.Text(), nullable=False),
        sa.Column('story_md', sa.Text(), nullable=False),
        sa.Column(
            'action_box',
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            'source_snapshot',
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
        sa.CheckConstraint("tab IN ('overview','erc20','stable')", name='ck_intel_stories_tab'),
        sa.CheckConstraint("tier IN ('premium','advance')", name='ck_intel_stories_tier'),
        sa.UniqueConstraint('tab', 'tier', name='uq_intel_stories_tab_tier'),
    )
    op.create_index(
        'ix_intel_stories_generated_at',
        'crypto_intel_stories',
        ['generated_at'],
    )


def downgrade() -> None:
    op.drop_index('ix_intel_stories_generated_at', table_name='crypto_intel_stories')
    op.drop_table('crypto_intel_stories')
