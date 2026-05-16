"""Kurumsal Sentez Faz 1 — corporate_syntheses + corporate_source_state

Revision ID: 025
Revises: 024
Create Date: 2026-05-16 09:00:00.000000

`corporate_syntheses`: haftalık makro sentez raporu. (event_id, tier)
idempotent UPSERT — event_id = sha1('mahfi-week|<pazartesi iso>')[:16].
`synthesis_md` Commit 2'de Gemini'den dolacak; Commit 1'de boş kalır.

`corporate_source_state`: kaynak başına ETag / Last-Modified persist —
fed_rss'in in-memory v0'ının Postgres'e terfisi (conditional GET).

Railway alembic auto-run yok; bu migration kanonik kayıt,
core/schema_guard.py runtime garantiyi sağlar (CREATE IF NOT EXISTS).
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = '025'
down_revision = '024'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'corporate_syntheses',
        sa.Column('id', sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column('event_id', sa.Text(), nullable=False),
        sa.Column('tier', sa.Text(), nullable=False),
        sa.Column('week_start', sa.Date(), nullable=False),
        sa.Column('synthesis_md', sa.Text(), nullable=True),
        sa.Column(
            'source_count',
            sa.Integer(),
            nullable=False,
            server_default=sa.text('0'),
        ),
        sa.Column(
            'meta',
            postgresql.JSONB(),
            nullable=True,
        ),
        sa.Column(
            'generated_at',
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text('NOW()'),
        ),
        sa.Column('broadcasted_premium_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('broadcasted_advance_at', sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint('event_id', 'tier', name='uq_corp_synth_eid_tier'),
    )
    op.create_index(
        'ix_corp_synth_week',
        'corporate_syntheses',
        [sa.text('week_start DESC')],
    )
    op.create_index(
        'ix_corp_synth_eid',
        'corporate_syntheses',
        ['event_id'],
    )

    op.create_table(
        'corporate_source_state',
        sa.Column('source', sa.Text(), primary_key=True),
        sa.Column('etag', sa.Text(), nullable=True),
        sa.Column('last_modified', sa.Text(), nullable=True),
        sa.Column(
            'updated_at',
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text('NOW()'),
        ),
    )


def downgrade() -> None:
    op.drop_table('corporate_source_state')
    op.drop_index('ix_corp_synth_eid', table_name='corporate_syntheses')
    op.drop_index('ix_corp_synth_week', table_name='corporate_syntheses')
    op.drop_table('corporate_syntheses')
