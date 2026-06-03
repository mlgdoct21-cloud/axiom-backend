"""Watchlist + Dossier Diffs — kişisel takip listesi ve dossier değişim feed'i

Revision ID: 030
Revises: 029
Create Date: 2026-06-03 23:00:00.000000

Lean v3 sonrası kişisel araç: Mehmet bir sembolü "watchlist"e ekler (kategori:
long_term USD ağırlıklı veya swing BIST+kripto). Supervisor kategoriye göre
periyodik dossier üretir; iki snapshot arasında structured değişiklik varsa
2. bir Gemini Flash call ile diff sentezlenir, mid/high severity Telegram'a
broadcast edilir.

Railway alembic auto-run yok; canonical kayıt burada,
core/schema_guard.py runtime garanti verir (CREATE IF NOT EXISTS).
"""
from alembic import op
import sqlalchemy as sa


revision = '030'
down_revision = '029'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'watchlist_items',
        sa.Column('id', sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('symbol', sa.String(length=16), nullable=False),
        sa.Column('category', sa.String(length=16), nullable=False),  # 'long_term' | 'swing'
        sa.Column('avg_cost', sa.Float(), nullable=True),
        sa.Column('qty', sa.Float(), nullable=True),
        sa.Column('notes', sa.Text(), nullable=True),
        sa.Column('last_dossier_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('last_trigger_check_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            'created_at',
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text('NOW()'),
        ),
        sa.UniqueConstraint('user_id', 'symbol', name='uq_watchlist_user_symbol'),
    )
    op.create_index('ix_watchlist_user', 'watchlist_items', ['user_id'])
    op.create_index('ix_watchlist_category', 'watchlist_items', ['category'])

    op.create_table(
        'dossier_diffs',
        sa.Column('id', sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('symbol', sa.String(length=16), nullable=False),
        sa.Column('prev_snapshot_id', sa.BigInteger(), nullable=True),
        sa.Column('curr_snapshot_id', sa.BigInteger(), nullable=False),
        sa.Column('diff_type', sa.String(length=32), nullable=False),  # decision_shift | target_change | trigger_near | risk_escalation | thesis_change
        sa.Column('severity', sa.String(length=8), nullable=False),  # low | mid | high
        sa.Column('summary', sa.Text(), nullable=False),
        sa.Column('details', sa.Text(), nullable=True),  # JSON string — structured changes (decision before/after, target diff, vs.)
        sa.Column('sent_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            'created_at',
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text('NOW()'),
        ),
    )
    op.create_index('ix_dossier_diffs_user', 'dossier_diffs', ['user_id'])
    op.create_index('ix_dossier_diffs_symbol', 'dossier_diffs', ['symbol'])
    op.create_index('ix_dossier_diffs_created_at', 'dossier_diffs', ['created_at'])


def downgrade() -> None:
    op.drop_index('ix_dossier_diffs_created_at', table_name='dossier_diffs')
    op.drop_index('ix_dossier_diffs_symbol', table_name='dossier_diffs')
    op.drop_index('ix_dossier_diffs_user', table_name='dossier_diffs')
    op.drop_table('dossier_diffs')
    op.drop_index('ix_watchlist_category', table_name='watchlist_items')
    op.drop_index('ix_watchlist_user', table_name='watchlist_items')
    op.drop_table('watchlist_items')
