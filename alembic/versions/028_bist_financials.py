"""BIST TL UFRS Bilanço — bist_financials (isyatirimhisse scraper)

Revision ID: 028
Revises: 027
Create Date: 2026-06-01 12:00:00.000000

Long-format tablo: her satır = (symbol, period, item_code, value).
isyatirimhisse fetch_financials() XI_29 (financial_group=1) ya da UFRS=2
döndürür; biz UFRS_K (3) tercih ediyoruz — konsolide.

Periodlar: "YYYY/Q" formatında (örn: "2024/3", "2024/6", "2024/9", "2024/12").
Item kodları XI_29 kodlarıdır (örn: 1A=Dönen Varlıklar, 1AA=Nakit, vb.).

Railway alembic auto-run yok; canonical kayıt burada,
core/schema_guard.py runtime garanti veriyor (CREATE IF NOT EXISTS).
"""
from alembic import op
import sqlalchemy as sa


revision = '028'
down_revision = '027'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'bist_financials',
        sa.Column('id', sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column('symbol', sa.String(length=16), nullable=False),
        sa.Column('period', sa.String(length=8), nullable=False),
        sa.Column('item_code', sa.String(length=16), nullable=False),
        sa.Column('item_name_tr', sa.Text(), nullable=True),
        sa.Column('item_name_en', sa.Text(), nullable=True),
        sa.Column('value', sa.Numeric(28, 2), nullable=True),
        sa.Column('currency', sa.String(length=4), nullable=False, server_default=sa.text("'TRY'")),
        sa.Column('financial_group', sa.String(length=4), nullable=False, server_default=sa.text("'1'")),
        sa.Column('source', sa.String(length=32), nullable=False, server_default=sa.text("'isyatirimhisse'")),
        sa.Column(
            'fetched_at',
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text('NOW()'),
        ),
        sa.UniqueConstraint('symbol', 'period', 'item_code', 'currency', 'financial_group', name='uq_bist_fin_spi'),
    )
    op.create_index('ix_bist_fin_symbol', 'bist_financials', ['symbol'])
    op.create_index('ix_bist_fin_symbol_period', 'bist_financials', ['symbol', 'period'])


def downgrade() -> None:
    op.drop_index('ix_bist_fin_symbol_period', table_name='bist_financials')
    op.drop_index('ix_bist_fin_symbol', table_name='bist_financials')
    op.drop_table('bist_financials')
