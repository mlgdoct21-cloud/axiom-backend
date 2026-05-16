"""Kurumsal Sentez S1c — accumulation store (corporate_posts + holdings_snapshots)

Revision ID: 026
Revises: 025
Create Date: 2026-05-16 12:00:00.000000

İki bağımsız smoke kanıtladı: fetch-at-synthesis yalnız Mahfi (düşük
hacim) için yeterli. İş Yatırım feed derinliği ~1 gün, ARK günlük
snapshot → accumulation store şart. Tüm prose/structured kaynakların
ortak borusu; scheduler (Commit 3) buraya idempotent UPSERT eder,
sentez (Commit 2) `read_window` ile haftalık pencereyi buradan okur.

`corporate_posts`: prose kaynaklar (Mahfi/İş Yatırım/podcast başlık/...).
  external_id = sha1(source|link)[:24] (yoksa source|title|published_iso).
  ON CONFLICT(source,external_id) DO UPDATE → revizyon yakalanır,
  first_seen_at KORUNUR (re-broadcast guard).
`corporate_holdings_snapshots`: ARK structured günlük snapshot. (fund,
  as_of) idempotent; gün-gün delta için ardışık as_of diff'lenir.

Railway alembic auto-run yok; core/schema_guard.py runtime garanti.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = '026'
down_revision = '025'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'corporate_posts',
        sa.Column('id', sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column('source', sa.Text(), nullable=False),
        sa.Column('external_id', sa.Text(), nullable=False),
        sa.Column('kind', sa.Text(), nullable=True),
        sa.Column('title', sa.Text(), nullable=False),
        sa.Column('link', sa.Text(), nullable=True),
        sa.Column('published', sa.DateTime(timezone=True), nullable=False),
        sa.Column('body_text', sa.Text(), nullable=True),
        sa.Column(
            'truncated',
            sa.Boolean(),
            nullable=False,
            server_default=sa.text('FALSE'),
        ),
        sa.Column('author', sa.Text(), nullable=True),
        sa.Column('meta', postgresql.JSONB(), nullable=True),
        sa.Column(
            'first_seen_at',
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text('NOW()'),
        ),
        sa.Column(
            'fetched_at',
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text('NOW()'),
        ),
        sa.UniqueConstraint('source', 'external_id', name='uq_corp_posts_src_eid'),
    )
    op.create_index(
        'ix_corp_posts_src_pub',
        'corporate_posts',
        ['source', sa.text('published DESC')],
    )
    op.create_index(
        'ix_corp_posts_pub',
        'corporate_posts',
        [sa.text('published DESC')],
    )

    op.create_table(
        'corporate_holdings_snapshots',
        sa.Column('id', sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            'source',
            sa.Text(),
            nullable=False,
            server_default=sa.text("'ark'"),
        ),
        sa.Column('fund', sa.Text(), nullable=False),
        sa.Column('as_of', sa.Date(), nullable=False),
        sa.Column('payload', postgresql.JSONB(), nullable=False),
        sa.Column(
            'holding_count',
            sa.Integer(),
            nullable=False,
            server_default=sa.text('0'),
        ),
        sa.Column(
            'fetched_at',
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text('NOW()'),
        ),
        sa.UniqueConstraint('fund', 'as_of', name='uq_corp_hold_fund_asof'),
    )
    op.create_index(
        'ix_corp_hold_fund_asof',
        'corporate_holdings_snapshots',
        ['fund', sa.text('as_of DESC')],
    )


def downgrade() -> None:
    op.drop_index('ix_corp_hold_fund_asof', table_name='corporate_holdings_snapshots')
    op.drop_table('corporate_holdings_snapshots')
    op.drop_index('ix_corp_posts_pub', table_name='corporate_posts')
    op.drop_index('ix_corp_posts_src_pub', table_name='corporate_posts')
    op.drop_table('corporate_posts')
